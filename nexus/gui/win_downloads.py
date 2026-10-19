"""Getting games: torrents, the FitGirl feed and index, finished
downloads, the install queue, and Steam shortcuts that wait for Steam
to close."""

import json
import time
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices

from nexus import net, paths as NX, steamlib

from .common import alive, CLI_BIN, DATA_DIR
from .fmt import fmt_bytes
from .theme import G


class DownloadsMixin:
    """Part of MainWindow (nexus/gui/window.py), which mixes it in; `self` is
    the window."""

    def _default_install_cmd(self, path: str) -> list[str]:
        """An install with the Settings defaults (what the Install page would
        start with), for one-click installs from a toast."""
        cmd = [CLI_BIN, path, '--yes', '--log', '--defer-steam']
        if not self.config.get('unattended', True):
            cmd.append('--interactive')
        if protongen_has_default():
            cmd.append('--use-protongen-default')
        return cmd

    def _repack_ready(self, c: dict):
        name, path = c['name'], c['path']
        if c.get('update_for'):
            text, verb = f"A new release of {name} has finished downloading.", "Update"
        else:
            text, verb = f"{name} has finished downloading and is ready to install.", "Install"
        self.toast(text, 'info', [
            (verb, lambda: self._start_install(self._default_install_cmd(path), name,
                                               c.get('slug') or '')),
            ("Options…", lambda: (self.open_install(), self.install._pick(path, name)))],
            30000)
        if not self.isActiveWindow():
            notify_desktop(f"{name} is ready to install", "Open Nexus to install it.")

    def _add_torrent(self, src: str, auto: bool):
        def done(data, err):
            if data is None or (data or {}).get('error'):
                msg = ((data or {}).get('error') or {}).get('message') or err or "it failed"
                self.toast(f"Couldn't hand it to the torrent client — {msg}", 'error', None, 0)
                return
            self.toast(f"Downloading {data.get('name') or 'the repack'}"
                       + (" — it installs when it's done." if auto else "."), 'success', None, 5000)
            self._poll_torrents()
        cli_json(['--torrent-add', src, '--json'] + (['--auto-install'] if auto else []),
                 done, 30000)

    def _poll_torrents(self):
        """Follow the torrent client: every torrent for the Downloads page,
        Nexus's own on the Install page and Deals cards, and install a
        finished one that asked for it. Every 2 s while Downloads is on
        screen, 10 s while something downloads, a minute otherwise; stops
        when no client is set up."""
        if self._torrent_polling:
            return
        self._torrent_polling = True

        def done(data, err):
            self._torrent_polling = False
            if not alive(self):
                return
            if data is None and err:
                data = {'error': {'kind': 'torrent', 'message': err}, 'torrents': []}
            data = data or {}
            rows = data.get('torrents') or []
            mine = [r for r in rows if r.get('tracked')]
            try:
                self.downloads.set_data(data)
                self.install.set_downloads(mine)
                self.dealspage.set_downloads(mine)
            except RuntimeError:
                return
            if data.get('configured') is False and self.downloads._detected is None \
                    and not self._torrent_detecting:
                self._torrent_detecting = True
                cli_json(['--torrent-detect', '--json'], self._torrent_detected, 10000)
            for r in mine:
                # Once: the install marks the download installed only when it
                # succeeds, and a failed one is retried by hand (Install it now).
                if r.get('done') and r.get('auto_install') and not r.get('installed') \
                        and not r.get('install_started') \
                        and r['hash'] not in self._torrents_started \
                        and r.get('path') and Path(r['path']).is_dir():
                    self._install_download(r)
            active = self.downloads.active_count()
            self.icons[self.DOWNLOADS].set_badge(active or None)
            if data.get('configured') is False:
                self._torrent_timer.stop()
            elif self._showing(self.DOWNLOADS):
                self._torrent_timer.start(2000)
            elif active or any(not r.get('done') and r.get('auto_install') for r in mine):
                self._torrent_timer.start(10_000)
            else:
                self._torrent_timer.start(60_000)
        cli_json(['--torrents', '--all', '--json'], done, 20000)

    def _install_download(self, r: dict):
        """Install a finished download; the CLI records on the download that
        it started, and that it's installed once it succeeds."""
        self._torrents_started.add(r['hash'])
        self.repacks.mark_seen(r['path'])
        self._start_install(self._default_install_cmd(r['path']) + ['--from-torrent', r['hash']],
                            r.get('title') or r.get('name') or Path(r['path']).name, '')

    def _torrent_detected(self, data, _err):
        self._torrent_detecting = False
        if alive(self):
            self.downloads.set_detected((data or {}).get('found') or False)

    def _use_torrent_client(self, found: dict):
        """"Use Transmission": write [torrent] client/url, then look again."""
        try:
            save_config({('torrent', 'client'): found['client'],
                         ('torrent', 'url'): found['url']})
        except (ConfigError, OSError) as e:
            self.toast(f"Couldn't save the setting — {e}", 'error', None, 0)
            return
        self.config = load_config()
        self.toast(f"Nexus now manages your {found['client'].capitalize()} downloads.",
                   'success', None, 5000)
        self._poll_torrents()

    def _torrent_control(self, action: str, hashes: list, delete: bool):
        if action == 'remove' and delete:
            n = len(hashes)
            what = (next((r.get('name') for r in self.downloads.data.get('torrents') or []
                          if r['hash'] == hashes[0]), None) if n == 1 else f"{n} torrents")
            self.confirm(f"Delete {what or 'this torrent'}?",
                         "The torrent is removed from the client and its downloaded files "
                         "are deleted. This can't be undone.", "Remove and delete files",
                         lambda: self._torrent_send(action, hashes, True))
            return
        self._torrent_send(action, hashes, False)

    def _torrent_send(self, action: str, hashes: list, delete: bool):
        def done(data, err):
            if data is None or (data or {}).get('error'):
                msg = ((data or {}).get('error') or {}).get('message') or err or "it failed"
                self.toast(f"The torrent client said no — {msg}", 'error', None, 0)
            self._poll_torrents()
        cli_json([f'--torrent-{action}', *hashes, '--json']
                 + (['--delete-data'] if delete else []), done, 20000)

    def _download_action(self, action: str, r: dict):
        path = r.get('path')
        if action == 'folder' and path:
            target = Path(path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(
                str(target if target.is_dir() else target.parent)))
        elif action == 'install' and path and Path(path).is_dir():
            self._install_download(r)
        elif action == 'forget':
            cli_json(['--torrent-forget', r['hash']], lambda *_: self._poll_torrents(), 10000)

    # ---- FitGirl repacks of wishlist games (Deals) ----
    def _deal_repack(self, action: str, item: dict, match: dict):
        url = match.get('url') or ''
        if action == 'page':
            if url.startswith('https://fitgirl-repacks.site/'):   # never a clone domain
                QDesktopServices.openUrl(QUrl(url))
        elif action == 'reject':
            appid = str(item.get('appid'))
            cli_json(['--repack-reject', appid, match['slug']], lambda *_: None, 10000)
            self.dealspage.forget_repack(appid)
            self.toast(f"Won't suggest “{match.get('title')}” for {item.get('title')} again.",
                       'info', None, 5000)
        elif action == 'grab':
            self.toast(f"Reading the repack page for {item.get('title')}…", 'info', None, 3000)
            cli_json(['--repack-page', match['slug'], '--json'],
                     lambda data, err: self._grab_sheet(item, match, data, err), 40000)

    def _grab_sheet(self, item: dict, match: dict, page, err):
        if not alive(self):
            return
        if page is None or page.get('error'):
            self.toast("Couldn't read the repack page — "
                       f"{(page or {}).get('error') or err}", 'error', None, 0)
            return
        title = item.get('title') or match.get('title')
        bits = [f"{page.get('repack_size') or '?'} download",
                f"{page.get('original_size') or '?'} installed"]
        need, free = page.get('size_bytes'), page.get('free_bytes')
        if page.get('dest'):
            bits.append(f"{fmt_bytes(free)} free in {page['dest']}" if free is not None
                        else f"to {page['dest']}")
        sub = " · ".join(bits)
        if need and free is not None and free < need:
            sub += " — not enough space"
        sheet = self.theatre_sheet if self._theatre else self.sheet
        items = []
        if not page.get('client'):
            items.append(("Set up a torrent client first", G['settings'],
                          lambda: self.go(self.DOWNLOADS)))
        elif not page.get('magnets'):
            sub += " — the page has no magnet link"
        else:
            items += [("Download — install when it's done", G['download'],
                       lambda: self._grab(item, match, True), True),
                      ("Download only", G['download'], lambda: self._grab(item, match, False))]
        items.append(("Open the repack page", G['external'],
                      lambda: self._deal_repack('page', item, match)))
        sheet.choose(f"{title}: {page.get('title') or match.get('title')}", items, sub)
        self._update_hints()

    def _grab(self, item: dict, match: dict, auto: bool):
        def done(data, err):
            if data is None or (data or {}).get('error'):
                msg = ((data or {}).get('error') or {}).get('message') or err or "it failed"
                self.toast(f"Couldn't start the download — {msg}", 'error', None, 0)
                return
            self.toast(f"Downloading {item.get('title')} ({data.get('repack_size') or '?'})"
                       + (" — it installs when it's done." if auto else "."), 'success',
                       [("Downloads", lambda: self.go(self.DOWNLOADS))], 6000)
            self._poll_torrents()
        cli_json(['--repack-grab', match['slug'], '--appid', str(item.get('appid')), '--json']
                 + (['--auto-install'] if auto else []), done, 40000)

    def _check_repack_feed(self):
        """Ask the CLI whether FitGirl posted newer repacks of installed
        games (it fetches at most every 6 h); reload the library when the
        answer differs from what the tiles show."""
        self._feed_timer.start(6 * 3600 * 1000)
        if self.config.get('gamedb_enabled'):
            self._sync_repack_index()
        if not any(g.get('source', 'nexus') == 'nexus' for g in self._games):
            return

        def done(data, _err):
            if data is None:
                return
            found = data.get('updates') or {}
            shown = {g['slug']: g.get('repack_online') for g in self._games
                     if g.get('source', 'nexus') == 'nexus' and g.get('repack_online')}
            if found != shown:
                fresh = [p['title'] for k, p in found.items() if k not in shown]
                self.reload_library()
                if fresh:
                    self.toast(f"FitGirl posted an update for {', '.join(fresh[:2])}"
                               + (f" and {len(fresh) - 2} more" if len(fresh) > 2 else '')
                               + ".", 'info', None, 8000)
        cli_json(['--repack-feed', '--json'], done, 120000)

    def _sync_repack_index(self):
        """FitGirl's catalogue against the wishlist (the CLI syncs at most
        every 6 h, and pushes new matches to the phone): toast what's new,
        and show the matches on Deals."""
        def done(data, _err):
            if not data or not alive(self):
                return
            news = data.get('new_matches') or []
            if news:
                names = [n.get('title') or n['slug'] for n in news]
                text = (f"{len(names)} wishlist games are on FitGirl." if data.get('first')
                        else f"On FitGirl: {', '.join(names[:2])}"
                        + (f" and {len(names) - 2} more." if len(names) > 2 else "."))
                self.toast(text, 'info', [("Show", self._show_fitgirl_deals)], 10000)
            if news or (data.get('matches') and not self.dealspage.repacks()):
                self.dealspage._loaded = False         # pick the matches up from the CLI
                if self._showing(self.DEALS):
                    self.dealspage.refresh()
        cli_json(['--repack-index', '--json'], done, 300000)

    def _show_fitgirl_deals(self):
        self.dealspage._filter = 'fitgirl'
        self.dealspage.show_section('deals')
        self.go(self.DEALS)
        self.dealspage._fill_tabs()
        self.dealspage._fill()

    def _repack_scanned(self, cands: list[dict]):
        updates = {c['update_for']: c['path'] for c in cands
                   if c.get('update_for') and c.get('complete', True)}
        if updates == self._repack_updates:
            return
        self._repack_updates = updates
        for g in self._games:
            if g.get('source', 'nexus') != 'nexus':
                continue
            g['repack_update'] = updates.get(g['slug'])
            if g['repack_update']:
                g['update_pending'] = True
        self._home_sig = None
        self.apply_filter()
        self.dash_update('updates')

    def open_install(self, path: str | None = None):
        self.install.open(self.config)
        self.go(self.INSTALL)
        if path:
            self.install._pick(path)

    @staticmethod
    def _dropped_folder(ev) -> str | None:
        md = ev.mimeData()
        urls = md.urls() if md is not None and md.hasUrls() else []
        p = Path(urls[0].toLocalFile()) if len(urls) == 1 and urls[0].isLocalFile() else None
        return str(p) if p is not None and p.is_dir() else None

    def dragEnterEvent(self, ev):
        # A repack folder dropped on the window opens it on the Install page.
        if not self._theatre and self._dropped_folder(ev):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dropEvent(self, ev):
        folder = self._dropped_folder(ev)
        if folder and not self._theatre:
            ev.acceptProposedAction()
            self.open_install(folder)
        else:
            super().dropEvent(ev)

    # ---- installs through Steam / Heroic ----
    def _resume_install(self, game):
        """Run an unfinished install again with the arguments it started with
        (the CLI recorded them); it reuses what's already in the prefix."""
        slug, name = game['slug'], game.get('display_name', game['slug'])
        if any(j.active and j.kind == 'install' and j.slug == slug for j in self.jobs.jobs):
            self.toast(f"{name} is already installing.", 'info', None, 3000)
            return
        argv = list(game['incomplete'].get('argv') or [])
        if not argv:
            self.toast(f"Nexus doesn't know how {name} was started — install it again from "
                       f"the Install page.", 'warn')
            return
        for flag in ('--yes', '--log', '--defer-steam'):
            if flag not in argv:
                argv.append(flag)
        game['resuming'] = True
        self._start_install([CLI_BIN, *argv], name, slug)

    def _request_install(self, game):
        """Install pressed on a game you own: Steam's install dialog (or
        Heroic's prompt) opens; Nexus then follows the download."""
        slug, name = game['slug'], game.get('display_name', game['slug'])
        store = "Steam" if game.get('source') == 'steam' else "Heroic"
        self._install_req[slug] = time.monotonic()
        game['install_requested'] = True
        self.run(['--open-url', game['install_url']], f"Installing {name} with {store}",
                 slug, 'launch')
        self.toast(f"{store} will ask where to install {name} — Nexus follows the download.",
                   'info', None, 5000)
        self._home_sig = None
        self.apply_filter()
        self._game_changed(slug)
        self._watch_installs()

    def _watch_installs(self):
        """Poll while something is downloading or was just asked for."""
        now = time.monotonic()
        for slug, t0 in list(self._install_req.items()):
            if now - t0 > 600:                         # the dialog was closed, most likely
                del self._install_req[slug]
        busy = self._install_req or any(g.get('installing') for g in self._games)
        if busy and not self._install_timer.isActive():
            self._install_timer.start()
        elif not busy:
            self._install_timer.stop()

    def _install_tick(self):
        """While Steam/Heroic download something: read just their manifests
        and queue (in-process, on the thread pool). Progress repaints the
        tile; a download starting, finishing or vanishing reloads the
        library once."""
        if self._install_query:
            return
        self._install_query = True
        cfg = steamlib.find_heroic_config()
        in_thread(lambda: steamlib.downloads(NX.STEAM_ROOT, cfg), self._on_downloads)

    def _on_downloads(self, d, error):
        self._install_query = False
        if error is not None or d is None:
            return
        steam, done = d.get('steam') or {}, set(d.get('steam_installed') or [])
        queued = set(d.get('heroic') or [])
        changed = False
        for g in self._games:
            if is_installed(g):
                continue
            if g.get('source') == 'steam':
                new = steam.get(g.get('appid'))
                if g.get('appid') in done or bool(new) != bool(g.get('installing')):
                    changed = True
                elif new and new != g.get('installing'):
                    g['installing'] = new                # just progress: repaint in place
                    self._game_changed(g['slug'])
            elif g.get('source') == 'heroic':
                if (g.get('app_name') in queued) != bool(g.get('installing')):
                    changed = True
        if changed:
            # Whatever was downloading (or asked for) and is installed after
            # the reload gets a "ready to play" toast (see _on_library).
            self._watch_finish = {g['slug'] for g in self._games if not is_installed(g)
                                  and (g.get('installing') or g['slug'] in self._install_req)}
            self._home_sig = None
            self.reload_library()
            return
        self._watch_installs()

    def _start_install(self, cmd, name, slug=''):
        """Installs start at once: with --defer-steam the CLI leaves the Steam
        shortcut for later if Steam is running (see _install_followups)."""
        src = cmd[1] if len(cmd) > 1 else None
        if any(j.active and j.kind == 'install' and ((slug and j.slug == slug)
                                                      or (src and j.argv[1:2] == [src]))
               for j in self.jobs.jobs):
            self.toast(f"{name} is already being installed.", 'info', None, 3000)
            return
        self.jobs.submit(Job(cmd, f"Installing {name}", slug or None, 'install', name))
        # From the Install page, on to the library to watch it; started from a
        # toast or a menu, stay where you are (the bottom strip shows it).
        if self.current() == self.INSTALL:
            self.go(self.LIBRARY)

    def _install_followups(self, job):
        """What an install job's events ask for once it ends: the success
        toast (with a way to change the picked .exe and to bin the repack),
        a deferred Steam shortcut, or the right kind of retry."""
        ev, name = job.events, job.name
        if job.state == 'done':
            inst = ev.get('installed') or {}
            slug = inst.get('slug') or job.slug
            picked = ev.get('exe-candidates') or {}
            cands = picked.get('candidates') or []
            actions = []
            if len(cands) > 1:
                actions.append(("Change game file", lambda: self._open_job(job)))
            src = inst.get('source')
            if src and not inst.get('repack_trashed'):
                actions.append(("Trash repack", lambda: self.run(
                    ['--trash-repack-dir', src], f"Moving the {name} repack to the Trash")))
            if slug:
                actions.insert(0, ("Play", lambda: (g := self.game(slug)) is not None
                                   and self.on_game_action('launch', g)))
            text = f"{name} is installed"
            if len(cands) > 1 and picked.get('chosen'):
                text += f" — it starts {Path(picked['chosen']).name}"
            self.toast(text, 'success', actions or None, 12000)
            if not self.isActiveWindow():
                notify_desktop(f"{name} is installed", "Ready to play.")
            if inst.get('steam_pending') and slug:
                self._add_to_steam_later(slug, name)
        elif job.state == 'failed':
            argv = [a for a in job.argv if a not in ('--interactive', '--unattended')]
            log = ("Show log", lambda: self._open_job(job))
            if 'retry-interactive' in ev:
                actions = [("Retry with setup window", lambda: self._resubmit(
                    job, [*argv, '--interactive'])), log]
            else:
                actions = [("Retry", lambda: self._resubmit(job, job.argv))]
                if '--verify' not in job.argv:
                    actions.append(("Verify & retry", lambda: self._resubmit(
                        job, [*job.argv, '--verify'])))
                actions.append(log)
            self.toast(f"{job.title} — failed", 'error', actions, 15000)
            if not self.isActiveWindow():
                notify_desktop(f"{job.title} failed", job.detail or "Open Nexus for the log.")

    def _resubmit(self, job, argv):
        self.jobs.submit(Job(list(argv), job.title, job.slug, job.kind, job.name))

    STEAM_PENDING = DATA_DIR / 'steam-pending.json'

    def _steam_pending(self, slug=None, name=None, add=True) -> list[dict]:
        """Shortcuts still owed to Steam, on disk: quitting Nexus before
        Steam doesn't lose them (they're picked up at the next start)."""
        try:
            items = json.loads(self.STEAM_PENDING.read_text())
            items = [i for i in items if isinstance(i, dict) and i.get('slug')]
        except (OSError, ValueError):
            items = []
        if slug is not None:
            items = [i for i in items if i['slug'] != slug]
            if add:
                items.append({'slug': slug, 'name': name or slug})
            net.save_json(self.STEAM_PENDING, items)
        return items

    def _resume_steam_pending(self):
        known = {g['slug']: g for g in self._games if g.get('source', 'nexus') == 'nexus'}
        for i in self._steam_pending():
            g = known.get(i['slug'])
            if g is None or g.get('steam'):          # gone, or added some other way
                self._steam_pending(i['slug'], add=False)
            else:
                self._add_to_steam_later(i['slug'], i.get('name') or i['slug'])

    def _add_to_steam_later(self, slug, name):
        """The Steam shortcut an install skipped because Steam was running:
        written as soon as Steam is closed (the banner waits for it)."""
        self._steam_pending(slug, name)

        def go():
            self._steam_pending(slug, add=False)
            j = Job([CLI_BIN, '--steam', slug], f"Adding {name} to Steam", slug, 'task', name)
            j.done.connect(lambda code: code == 0 and self.toast(
                f"{name} is in Steam now.", 'success', ("Start Steam", start_steam), 10000))
            self.jobs.submit(j)
        self.gate.run(f"add {name} to Steam", go)


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .config import ConfigError, load_config, save_config  # noqa: E402
from .gameinfo import is_installed  # noqa: E402
from .install import protongen_has_default  # noqa: E402
from .jobs import cli_json, in_thread, Job  # noqa: E402
from .widgets import notify_desktop, start_steam  # noqa: E402
