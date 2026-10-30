"""Doing things to a game: the game menu's actions, running them as
jobs, confirmations, and what happens when a job finishes."""

import shutil
import time
from pathlib import Path, PureWindowsPath

from PyQt6.QtCore import QProcess, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QFileDialog

from nexus import platform
from nexus.features import has

from .common import alive, CLI, cli_args, HOME, MAX_TITLE
from .fmt import fmt_bytes
from .theme import collection_look, G

# Windows Security on its Virus & threat protection page (Protection history
# is there). Opening it changes nothing.
DEFENDER_URL = 'windowsdefender://threat'
FITGIRL_FAQ = 'https://fitgirl-repacks.site/faq/'


def _breakable(path: str) -> str:
    """A path that may wrap after any separator (a zero-width space)."""
    return path.replace('\\', '\\​').replace('/', '/​')


def antivirus_text(ev: dict) -> tuple[str, str]:
    """(title, text) for an install the antivirus broke, from the CLI's
    `antivirus` event: what Windows Defender quarantined (or, when it can't
    tell, the setup log's unpacking errors), what that means and what the
    user can do about it. Plain text; nothing in it changes a setting."""
    found = ev.get('detections') or []
    if found:
        title = ("Windows Defender removed a game file" if len(found) == 1 else
                 f"Windows Defender removed {len(found)} game files")
        what = "\n".join(f"{PureWindowsPath(d['file']).name} — {d.get('threat') or 'a threat'}\n"
                         f"in {_breakable(str(PureWindowsPath(d['file']).parent))}"
                         for d in found[:5])
        means = ("Repack files (unarc.dll, ISDone.dll, a game's cracked DLLs) are often flagged "
                 "by mistake. Without it the game can't run, so it isn't in your library; its "
                 "folder is kept for another try.")
    else:
        title = "An antivirus stopped the install"
        what = "Setup couldn't unpack the repack:\n" + "\n".join(
            ln[:160] for ln in ev.get('log_lines') or [])
        means = ("That's usually an antivirus taking unarc.dll or ISDone.dll away "
                 "mid-install, not a broken download.")
    todo = ("If you trust this repack, restore the file or allow it in Windows Security → "
            "Protection history, then install again. FitGirl's FAQ explains these false "
            "positives. Nexus doesn't change antivirus settings.")
    return title, f"{what}\n\n{means}\n\n{todo}"


class ActionsMixin:
    """Part of MainWindow (nexus/gui/window.py), which mixes it in; `self` is
    the window."""

    def _open_job(self, job):
        self.go(self.ACTIVITY)
        self.activity.focus_job(job)

    # ---- actions ----
    def run(self, args, title, slug=None, kind='task', steam=False, name=None, on_skip=None):
        """Queue a CLI job. With steam=True it waits (via the banner) until
        Steam is closed, since Steam overwrites its files when it exits."""
        if kind != 'launch' and (self.jobs.find([*CLI, *args]) or self.gate.waiting(title)):
            self.toast(f"“{title}” is already queued.", 'info', None, 3000)
            return

        def submit():
            if not self.jobs.find([*CLI, *args]):
                self.jobs.submit(Job([*CLI, *args], title, slug, kind, name))
        if steam:
            self.gate.run(title, submit, on_skip)
            if self._theatre and self.gate.pending:
                self.toast(f"Steam is running — “{title}” continues once Steam is closed.",
                           'warn', None, 6000)
        else:
            submit()

    def _on_request(self, args, title, steam, kind):
        if args and args[0] == '__action__':
            self.on_game_action(args[1], self.details.game)
            return
        if args and args[0] == '__batch__':
            self._batch_action(args[1], list(self.details.games))
            return
        slugs = {g['slug'] for g in self._games}
        slug = next((a for a in args[1:] if a in slugs), None)
        self.run(args, title, slug, kind, steam)

    def game(self, slug) -> dict | None:
        return next((g for g in self._games if g['slug'] == slug), None)

    def on_game_action(self, action, game):
        slug, name = game['slug'], game.get('display_name', game['slug'])
        src = game.get('source', 'nexus')
        live = self.game(slug) or game              # the panel's copy can be stale
        if action in NOT_WHILE_PLAYING and (live.get('playing') or live.get('launching')):
            self.toast(f"{name} is running — close it first.", 'warn', None, 4000)
            return
        if action in NOT_WHILE_BUSY and self.jobs.busy(slug):
            self.toast(f"{name} is busy — wait for its current job to finish.", 'warn',
                       None, 4000)
            return
        if action.startswith('rollback:') and (live.get('playing') or live.get('launching')):
            self.toast(f"{name} is running — close it first.", 'warn', None, 4000)
            return
        if action == 'restore-saves' and not getattr(self, '_confirmed', False):
            self.confirm(f"Restore {name}'s saves?",
                         "Copies the newest save backup over the saves in "
                         f"{'its prefix' if has('proton') else 'its save folders'}. The "
                         "current saves are backed up first, so this can be undone.",
                         "Restore saves", lambda: self._confirmed_action(action, game))
            return
        appid = game.get('appid')
        steam_urls = {'steam-page': 'steam://nav/games/details/{}',
                      'steam-props': 'steam://gameproperties/{}',
                      'steam-verify': 'steam://validate/{}',
                      'steam-uninstall': 'steam://uninstall/{}'}
        if action.startswith('col:'):
            cname = action[4:]
            on = self.prefs.toggle_member(cname, slug)
            self._collections_changed()
            self.toast(f"{name} {'added to' if on else 'removed from'} “{cname}”.",
                       'success', None, 2500)
        elif action == 'col-new':
            self.ask_collection_name("New collection", "",
                                     lambda n: self._create_collection(n, slug))
        elif action in ('favorite', 'hide'):
            on = self.prefs.toggle(action, slug)
            for g in self._games:
                if g['slug'] == slug:
                    g['favorite' if action == 'favorite' else 'hidden'] = on
            self.apply_filter()
            if slug in self.selected:
                self._sync_selection()            # the star in the details panel
            self.toast(f"{name} {'added to' if on else 'removed from'} "
                       f"{'favourites' if action == 'favorite' else 'hidden games'}.",
                       'success', None, 2500)
        elif action == 'launch' and game.get('incomplete'):
            self._resume_install(game)
        elif action == 'discard-incomplete':
            self.run(['--discard-incomplete', slug], f"Discarding the unfinished install "
                     f"of {name}", slug)
        elif action == 'install-update' and game.get('repack_update'):
            self._start_install([*CLI, game['repack_update'], '--yes', '--log',
                                 '--defer-steam'], name, slug)
        elif action == 'launch' and not play_enabled(game):
            return                                  # already starting / running
        elif action == 'launch' and not is_installed(game):
            self._request_install(game)
        elif action == 'launch' and src != 'nexus':
            self._begin_launch(game)
            self.run(['--open-url', game['launcher']], f"Launching {name}", slug, 'launch')
        elif action in steam_urls and appid:
            self.run(['--open-url', steam_urls[action].format(appid)],
                     f"Opening {name} in Steam", slug, 'launch')
        elif action == 'rename-local':
            def done(new, slug=slug):
                new = ' '.join(new.split())[:MAX_TITLE]
                if new:
                    self.prefs.overrides[slug] = {'name': new}
                    self.prefs.save()
                    self.reload_library()
            if self._theatre:
                self.theatre.search.open_entry("Name in your library", name, done)
            elif self.input.mode == 'pad':
                self.desk_search.open_entry("Name in your library", name, done)
            else:
                self.sheet.prompt("Name in your library", name, "Save", done,
                                  "Only Nexus shows this name.", max_len=MAX_TITLE)
            self._update_hints()
        elif action == 'rename-reset':
            if self.prefs.overrides.pop(slug, None) is not None:
                self.prefs.save()
                self.reload_library()
        elif action == 'repack-page':
            url = (game.get('repack_online') or {}).get('url') or ''
            if url.startswith('https://fitgirl-repacks.site/'):   # the pinned site only
                QDesktopServices.openUrl(QUrl(url))
        elif action in ('add-sunshine', 'remove-sunshine'):
            add = action == 'add-sunshine'
            self.run(['--sunshine' if add else '--remove-sunshine', slug],
                     f"{'Adding' if add else 'Removing'} {name} "
                     f"{'to' if add else 'from'} Sunshine", slug, 'task')
        elif action == 'protongen':
            self._open_protongen(game)
        elif action == 'artwork':
            self.open_artwork(game)
        elif action == 'open-folder' and src != 'nexus':
            if game.get('install_dir') and Path(game['install_dir']).is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(game['install_dir']))
        elif src != 'nexus':
            return                                  # Nexus-only action
        elif action == 'launch':
            self._begin_launch(game)
            self.run(['--launch', slug], f"Launching {name}", slug, 'launch')
        elif action == 'open-folder':
            if game.get('exe') and Path(game['exe']).parent.is_dir():
                target = Path(game['exe']).parent
            elif game.get('wine_prefix'):
                target = Path(game['wine_prefix'])
            elif game.get('launcher'):
                target = Path(game['launcher']).parent
            else:
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        elif action == 'pause-toggle':
            self.run(['--pause-toggle', slug], f"Pausing / resuming {name}", slug, 'tool')
        elif action == 'benchmark':
            self._begin_launch(game)
            self.run(['--benchmark', slug], f"Benchmarking {name}", slug, 'launch')
            self.toast("Logging starts 30 s after launch and runs 60 s (Shift+F2 starts it "
                       "by hand). Results appear under the game's Troubleshoot section.", 'info', None, 8000)
        elif action == 'settings':
            self._reveal_game(slug)
            self.details.open_section('launch')
        elif action == 'view-log':
            self._reveal_game(slug)
            self.details.open_section('log')
        elif action.startswith('tool:'):
            tool = action.split(':', 1)[1]
            self.run(['--prefix-tool', slug, tool],
                     "Stopping" if tool == 'kill' else tool, slug, 'tool')
        elif action == 'uninstall':
            self._reveal_game(slug)
            self.details.ask_uninstall()
        elif action == 'snapshots':
            self._reveal_game(slug)
            self.details.open_section('snapshots')
        elif action == 'snapshot-now':
            self.run(['--snapshot', slug], f"Taking a snapshot of {name}", slug)
        elif action == 'snapshots-enable':
            self.run(['--snapshots-enable', slug], f"Enabling snapshots for {name}", slug)
        elif action.startswith('rollback:'):
            snap = action.split(':', 1)[1]
            self.confirm(f"Roll {name} back?",
                         "Puts the game's files and its prefix — settings and saves included "
                         "— back the way they were then. What's there now is kept as a "
                         "snapshot, so this can be undone.", "Roll back",
                         lambda: self.run(['--rollback', slug, snap], f"Rolling {name} back",
                                          slug))
        elif action.startswith('snapshot-delete:'):
            snap = action.split(':', 1)[1]
            self.confirm("Delete this snapshot?", "It can't be rolled back to afterwards.",
                         "Delete", lambda: self.run(['--delete-snapshot', slug, snap],
                                                    f"Deleting a snapshot of {name}", slug))
        elif action == 'move':
            self._choose_move_target(game)
        else:
            spec = {
                'add-steam': (['--steam', slug], f"Adding {name} to Steam", True),
                'remove-steam': (['--remove-steam', slug], f"Removing {name} from Steam", True),
                'add-heroic': (['--heroic', slug], f"Adding {name} to Heroic", False),
                'remove-heroic': (['--remove-heroic', slug], f"Removing {name} from Heroic", False),
                'refetch-art': (['--refetch-art', slug], f"Fetching artwork for {name}", False),
                'restore-saves': (['--restore-saves', slug], f"Restoring saves for {name}", False),
                'check-integrity': (['--check-integrity', slug], f"Checking {name}'s files", False),
                'pg-default': (['--protongen-default', slug],
                               f"Applying protongen defaults to {name}", bool(game.get('steam'))),
            }.get(action)
            if spec:
                self.run(spec[0], spec[1], slug, 'task', spec[2])

    # Batch actions (several games selected): key -> the single-game action
    # whose busy/playing gate applies, and whether it's for repacks only.
    BATCH = {'apply': ('settings', True), 'steam': ('add-steam', True),
             'heroic': ('add-heroic', True), 'art': ('refetch-art', True),
             'proton': ('pg-default', True), 'pg-default': ('pg-default', True),
             'collection': ('', False), 'move': ('move', True),
             'snapshot': ('snapshot-now', True), 'uninstall': ('uninstall', True)}

    def batch_applies(self, key: str, games: list[dict]) -> bool:
        """Whether a batch action is offered for this selection: every game
        must be one it can act on."""
        _gate, repacks = self.BATCH[key]
        if not games:
            return False
        if repacks and any(g.get('source', 'nexus') != 'nexus' for g in games):
            return False
        if key in ('move', 'snapshot'):
            return all(g.get('owned_dir') for g in games)
        return True

    def _batch_action(self, key: str, games: list[dict]):
        """One action for several games: the same gates as one at a time
        (busy, running), and one toast saying what was done or skipped."""
        if key not in self.BATCH or not self.batch_applies(key, games):
            return
        gate = self.BATCH[key][0]
        ok, skipped = [], []
        for g in games:
            live = self.game(g['slug']) or g
            if gate in NOT_WHILE_PLAYING and (live.get('playing') or live.get('launching')):
                skipped.append((live, "running"))
            elif gate in NOT_WHILE_BUSY and self.jobs.busy(live['slug']):
                skipped.append((live, "busy"))
            else:
                ok.append(live)

        def summary(verb):
            parts = [f"{verb} {len(ok)} game{'s' if len(ok) != 1 else ''}"] if ok else []
            for why in ("running", "busy"):
                n = sum(1 for _g, w in skipped if w == why)
                if n:
                    parts.append(f"{n} skipped ({why})")
            self.toast(" · ".join(parts) + ".", 'success' if ok else 'warn', None, 5000)

        def each(args_of, title_of, steam_of=lambda g: False, verb="Queued"):
            for g in ok:
                self.run(args_of(g), title_of(g), g['slug'], 'task', steam_of(g))
            summary(verb)
        nm = lambda g: g.get('display_name', g['slug'])  # noqa: E731
        if not ok:
            summary("")
            return
        if key == 'apply':
            each(lambda g: ['--apply-launch', g['slug']], lambda g: f"Re-applying {nm(g)}",
                 lambda g: bool(g.get('steam')))
        elif key == 'steam':
            each(lambda g: ['--steam', g['slug']], lambda g: f"Adding {nm(g)} to Steam",
                 lambda g: True)
        elif key == 'heroic':
            each(lambda g: ['--heroic', g['slug']], lambda g: f"Adding {nm(g)} to Heroic")
        elif key == 'art':
            each(lambda g: ['--refetch-art', g['slug']], lambda g: f"Fetching art for {nm(g)}")
        elif key == 'pg-default':
            each(lambda g: ['--protongen-default', g['slug']],
                 lambda g: f"Applying protongen defaults to {nm(g)}",
                 lambda g: bool(g.get('steam')))
        elif key == 'snapshot':
            each(lambda g: ['--snapshot', g['slug']], lambda g: f"Taking a snapshot of {nm(g)}")
        elif key == 'proton':
            def got(data, _err):
                names = [p['name'] for p in (data or {}).get('protons') or []]
                if not names or not alive(self):
                    return
                self.sheet.choose(f"Play {len(ok)} games with…", [
                    (p, G['proton'], lambda p=p: each(
                        lambda g: ['--proton', g['slug'], p],
                        lambda g: f"Switching {nm(g)} to {p}", lambda g: bool(g.get('steam'))))
                    for p in names])
            cli_json(['--list-protons', '--json'], got, 20000)
        elif key == 'collection':
            def add_to(cname):
                for g in ok:
                    if g['slug'] not in self.prefs.collections.get(cname, []):
                        self.prefs.toggle_member(cname, g['slug'])
                self._collections_changed()
                self.toast(f"Added {len(ok)} games to “{cname}”.", 'success', None, 3000)

            def new():
                self.ask_collection_name("New collection", "", lambda n: (
                    (made := self.prefs.add_collection(n)) and add_to(made)))
            looks = {n: collection_look(n) or (G['collection'], 'subtext')
                     for n in self.prefs.collections}
            self.sheet.choose(f"Add {len(ok)} games to…", [
                (n, looks[n][0], lambda n=n: add_to(n), looks[n][1])
                for n in self.prefs.collections] + [("New collection…", G['plus'], new)])
        elif key == 'move':
            self._choose_move_target(ok[0], also=ok[1:])
        elif key == 'uninstall':
            names = ", ".join(nm(g) for g in ok[:4]) + ("…" if len(ok) > 4 else "")
            self.confirm(f"Uninstall {len(ok)} games?",
                         f"{names}. Their folders and prefixes are deleted; save games found "
                         "in them are backed up first. Their snapshots are kept.",
                         f"Uninstall {len(ok)} games",
                         lambda: each(lambda g: ['--uninstall', g['slug'], '--yes',
                                                 '--save-backup'],
                                      lambda g: f"Uninstalling {nm(g)}",
                                      lambda g: bool(g.get('steam')), verb="Uninstalling"))
        self._update_hints()

    def uninstall_games(self, slugs: list, save_backup: bool, remove_snapshots: bool):
        """Uninstall repacks the Storage page asked about (its inline question
        already offered the save backup and the snapshots): the same gates as
        the game menu (running, busy), one --uninstall job each, one toast."""
        ok, skipped = [], []
        for slug in slugs:
            g = self.game(slug)
            if g is None or g.get('source', 'nexus') != 'nexus':
                continue
            if g.get('playing') or g.get('launching'):
                skipped.append(f"{g.get('display_name', slug)} is running")
            elif self.jobs.busy(slug):
                skipped.append(f"{g.get('display_name', slug)} is busy")
            else:
                ok.append(g)
        for g in ok:
            args = ['--uninstall', g['slug'], '--yes']
            if save_backup:
                args.append('--save-backup')
            if remove_snapshots:
                args.append('--remove-snapshots')
            self.run(args, f"Uninstalling {g.get('display_name', g['slug'])}", g['slug'],
                     'task', bool(g.get('steam')))
        parts = [f"Uninstalling {len(ok)} game{'s' if len(ok) != 1 else ''}"] if ok else []
        if skipped:
            parts.append("skipped: " + ", ".join(skipped))
        if parts:
            self.toast(" · ".join(parts) + ".", 'success' if ok else 'warn', None, 5000)

    def _choose_move_target(self, game, also: list | None = None):
        """Move to…: the folders games can live in (free space shown against
        the game's size), or any folder."""
        games = [game, *(also or [])]
        slug, name = game['slug'], game.get('display_name', game['slug'])
        if also:
            name = f"{len(games)} games"
        size = sum(g.get('disk_bytes') or 0 for g in games)
        here = str(Path(game.get('compatdata') or '').parent) if not also else None

        def go(base):
            self.prefs.move_targets = ([base] + [t for t in self.prefs.move_targets
                                                 if t != base])[:5]
            self.prefs.save()
            for g in games:
                if str(Path(g.get('compatdata') or '').parent) == base:
                    continue                            # already there
                self.run(['--move', g['slug'], base],
                         f"Moving {g.get('display_name', g['slug'])} to {base}", g['slug'],
                         'task', bool(g.get('steam')))

        def pick():
            d = QFileDialog.getExistingDirectory(self, f"Move {name} to…", str(HOME))
            if d:
                go(d)

        def got(data, _err):
            if not alive(self):
                return
            rows = list((data or {}).get('targets') or [])
            known = {r['path'] for r in rows}
            rows += [{'path': t, 'free': None} for t in self.prefs.move_targets
                     if t not in known]
            items = []
            for r in rows:
                if r['path'] == here:
                    continue
                free = r.get('free')
                short = free is not None and size and free < size + (2 << 30)
                text = r['path'] + (f"   ·   {fmt_bytes(free)} free" if free is not None else '')
                if short:
                    text += "  ·  not enough space"
                items.append((text, G['folder'], (lambda p=r['path']: go(p)) if not short
                              else (lambda: self.toast("Not enough space there.", 'warn',
                                                       None, 3000))))
            items.append(("Choose a folder…", G['folder'], pick))
            sheet = self.theatre_sheet if self._theatre else self.sheet
            sheet.choose(f"Move {name}" + (f" ({fmt_bytes(size)})" if size else ""), items,
                         ("Each game goes into <folder>/<its name>." if also else
                          "The game goes into <folder>/" + slug + ".")
                         + " The old copy is kept until you remove it in Storage.")
            self._update_hints()
        cli_json(['--move-targets', '--json'], got, 20000)

    def _confirmed_action(self, action, game):
        self._confirmed = True
        try:
            self.on_game_action(action, game)
        finally:
            self._confirmed = False

    def confirm(self, title, text, yes, on_yes, danger=True):
        """Ask before something that can't simply be clicked back: the
        action sheet as a two-row question, for mouse, keyboard and pad
        alike (and in Theatre)."""
        sheet = self.theatre_sheet if self._theatre else self.sheet
        sheet.ask(title, text, yes, on_yes, danger)
        self._update_hints()

    def show_antivirus(self, job):
        """The sheet for an install the antivirus broke (the CLI's `antivirus`
        event): what was quarantined, what it means, and the way to Windows
        Security. No button here changes anything."""
        ev = job.events.get('antivirus') or {}
        title, text = antivirus_text(ev)
        faq = ev['faq'] if str(ev.get('faq') or '').startswith('https://') else FITGIRL_FAQ
        sheet = self.theatre_sheet if self._theatre else self.sheet
        sheet.choose(title, [
            ("Open Windows Security", G['shield'], lambda: platform.open_url(DEFENDER_URL)),
            ("FitGirl's FAQ on false positives", G['external'],
             lambda: QDesktopServices.openUrl(QUrl(faq))),
            ("Show log", G['log'], lambda: self._open_job(job)),
            ("Close", G['close'], lambda: None)], job.name or '', text)
        self._update_hints()

    def review_integrity(self, job, game):
        """After a check found changed or missing files: look at the list,
        accept the current files as the new baseline (an update or a mod),
        or keep the old one. Inline, in the sheet."""
        slug, name = game['slug'], game.get('display_name', game['slug'])
        sheet = self.theatre_sheet if self._theatre else self.sheet
        sheet.choose(f"{name}: files changed", [
            ("Show the changed files", G['log'], lambda: self._open_job(job)),
            ("Accept them as the new baseline", G['shield'],
             lambda: self.run(['--check-integrity', slug, '--rebaseline'],
                              f"Accepting {name}'s current files", slug)),
            ("Keep the old baseline", G['close'], lambda: None)], '',
            "If you updated or modded the game, accept the current files. If you didn't, "
            "a snapshot or a reinstall puts the originals back.")
        self._update_hints()

    def open_artwork(self, game, kind='hero'):
        cur = self.current()
        self._art_from = cur if cur in (self.HOME, self.LIBRARY) else self.HOME
        self._art_slug = game.get('slug')
        self.artpage.open(game, kind)
        self.go(self.ARTWORK)

    def _art_back(self):
        self.go(self._art_from)
        slug = getattr(self, '_art_slug', None)
        if slug:
            self.selected = [slug]
            self._sync_selection()

    def _open_protongen(self, game):
        """Open protongen on this game (`protongen --game <id>`: the Steam
        appid, or protongen's hash of the Heroic app name)."""
        pg = shutil.which('protongen') or str(HOME / '.local/bin/protongen')
        gid = game.get('protongen_id') or game.get('appid')
        args = ['--game', str(gid)] if gid else []
        QApplication.clipboard().setText(game.get('display_name') or game['slug'])
        if QProcess.startDetached(pg, args):
            self.toast(f"Opened protongen on {game.get('display_name')}.", 'info', None, 3000)
        else:
            self.toast(f"Couldn't start {pg}.", 'error')

    # ---- job feedback ----
    def _jobs_changed(self):
        self._sync_system()
        busy_now = self.jobs.busy_slugs()
        busy = frozenset(g['slug'] for g in self._games if g['slug'] in busy_now)
        if busy != self._busy:
            # A game's files are being changed (or stopped being): what Play
            # and the menu offer for it changes with that.
            self._busy = busy
            self._mark_states()
            if self.selected:
                self._sync_selection()
        pending = tuple(id(j) for j in self.jobs.jobs if j.kind == 'install' and j.active)
        if pending != self._pending_ids:
            self.apply_filter()
        else:
            self.views.currentWidget().set_jobs(self.jobs)
            self.homeview.set_jobs(self.jobs)

    def _job_done(self, job):
        name = job.name
        if job.kind == 'open':                    # a steam:// / heroic:// hand-off
            if job.state == 'failed':
                self.toast(f"{job.title} — failed: {job.detail or 'see the log'}", 'error',
                           ("Details", lambda: self._open_job(job)))
            return
        if job.kind == 'launch' and job.state == 'done' and job.slug in self._launching:
            # --launch has handed the game off; if nothing shows up within a
            # minute it isn't coming (a crash on start, a dialog in Steam).
            self._launching[job.slug] = min(self._launching[job.slug],
                                            time.monotonic() - (self.LAUNCH_WAIT - 60))
        if job.kind == 'launch':
            if job.state == 'failed' and job.slug:
                self._launching.pop(job.slug, None)
                if self._handoff and self._handoff['game'].get('slug') == job.slug:
                    self._handoff = None
                self._mark_states()
                self._home_sig = None
                self.apply_filter()
            if job.state == 'failed':
                self.toast(f"{job.title.replace('Launching ', '')} didn't start — "
                           f"{job.detail or 'see the log'}", 'error',
                           ("Details", lambda: self._open_job(job)))
            else:
                self.toast(f"{job.title.replace('Launching ', '')} is starting…", 'success', None,
                           3500)
            QTimer.singleShot(4000, self.reload_library)       # last-played updates
            return
        if job.kind == 'install':
            self._install_followups(job)
        elif job.state == 'done':
            self.toast(f"{job.title} — done", 'success')
        elif job.state == 'failed' and integrity_changed(job):
            game = self.game(job.slug) or {'slug': job.slug}
            self.toast(f"{game.get('display_name', job.slug)}: files changed since the "
                       f"baseline", 'warn', ("Review", lambda: self.review_integrity(job, game)),
                       15000)
            if not self.isActiveWindow():
                notify_desktop(f"{job.title}: files changed", "Open Nexus to review them.")
        elif job.state == 'failed':
            self.toast(f"{job.title} — failed", 'error', ("Show log", lambda: self._open_job(job)),
                       10000)
            if not self.isActiveWindow():
                notify_desktop(f"{job.title} failed", job.detail or "Open Nexus for "
                               "the log.")
        first = cli_args(job.argv)[:1]
        if job.kind == 'install-proton' or (first and first[0].startswith('--proton')):
            self.protonpage.refresh()
            if job.state == 'done' and job.kind == 'install-proton':
                self.toast("New Proton installed. Restart Steam to see it there; pick it per "
                           "game or as the default in Settings.", 'success', None, 8000)
        if job.slug:                            # Theatre's game page reads it afresh
            self.theatre._launch.pop(job.slug, None)
            self.theatre._tips.pop(job.slug, None)
        if job.state == 'done' and '--set-sgdb' in job.argv and self._showing(self.ARTWORK):
            self.artpage.reload()
        if job.kind != 'tool':
            cli_cache_clear()
            self.reload_library()
            self._health_stale = True
            if self._showing(self.HEALTH):
                self._health_stale = False
                self.health.refresh()
            elif self._showing(self.STORAGE):
                self.storage.load_orphans()

    def toast(self, text, kind='info', action=None, timeout=6000):
        self.toasts.show_toast(text, kind, action, timeout)


def integrity_changed(job) -> bool:
    """A --check-integrity that failed because files differ from the
    baseline (the CLI then suggests --rebaseline), not for another reason."""
    return ('--check-integrity' in job.argv and '--rebaseline' not in job.argv
            and any('--rebaseline' in line for line in job.log))


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .actions import NOT_WHILE_BUSY, NOT_WHILE_PLAYING  # noqa: E402
from .gameinfo import is_installed, play_enabled  # noqa: E402
from .jobs import cli_cache_clear, cli_json, Job  # noqa: E402
from .widgets import notify_desktop  # noqa: E402
