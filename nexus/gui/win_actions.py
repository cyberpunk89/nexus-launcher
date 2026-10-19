"""Doing things to a game: the game menu's actions, running them as
jobs, confirmations, and what happens when a job finishes."""

import shutil
import time
from pathlib import Path

from PyQt6.QtCore import QProcess, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication

from .common import CLI_BIN, HOME, MAX_TITLE


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
        if kind != 'launch' and (self.jobs.find([CLI_BIN, *args]) or self.gate.waiting(title)):
            self.toast(f"“{title}” is already queued.", 'info', None, 3000)
            return

        def submit():
            if not self.jobs.find([CLI_BIN, *args]):
                self.jobs.submit(Job([CLI_BIN, *args], title, slug, kind, name))
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
        if action == 'restore-saves' and not getattr(self, '_confirmed', False):
            self.confirm(f"Restore {name}'s saves?",
                         "Copies the newest save backup over the saves in its prefix. The "
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
            self._start_install([CLI_BIN, game['repack_update'], '--yes', '--log',
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
                       "by hand). Results appear in the game's Log section.", 'info', None, 8000)
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
        elif job.state == 'failed':
            self.toast(f"{job.title} — failed", 'error', ("Show log", lambda: self._open_job(job)),
                       10000)
            if not self.isActiveWindow():
                notify_desktop(f"{job.title} failed", job.detail or "Open Nexus for "
                               "the log.")
        if job.kind == 'install-proton' or (job.argv[1:2] and job.argv[1].startswith('--proton')):
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


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .actions import NOT_WHILE_BUSY, NOT_WHILE_PLAYING  # noqa: E402
from .gameinfo import is_installed, play_enabled  # noqa: E402
from .jobs import cli_cache_clear, Job  # noqa: E402
from .widgets import notify_desktop  # noqa: E402
