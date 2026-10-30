"""Playing: which games are running, launches in progress, and handing
keyboard/controller focus to a game's window and back."""

import os
import time
from datetime import datetime

from PyQt6.QtCore import QEvent, QTimer

from nexus import history, steamlib, winfocus

from .common import alive


class PlayMixin:
    """Part of MainWindow (nexus/gui/window.py), which mixes it in; `self` is
    the window."""

    LAUNCH_WAIT = 120                # s a launch may take to show up as running

    def _poll_running(self):
        # Keep polling while minimised if a game is starting or running: that's
        # how Nexus notices it has finished (and comes back).
        if (not self.isVisible() or self.isMinimized()) \
                and not (self._launching or self._playing):
            return
        now = time.monotonic()
        expired = [s for s, t0 in self._launching.items() if now - t0 > self.LAUNCH_WAIT]
        if expired:                                  # never showed up as running
            self._launch_expired(expired)

        if self._running_query:
            return                                   # the last one is still out
        # A /proc read on the thread pool: no subprocess, nothing re-scanned.
        keys = ('slug', 'source', 'exe', 'steam_appid', 'appid', 'install_dir')
        games = [{k: g.get(k) for k in keys} for g in self._games if is_installed(g)]
        self._running_query = True
        in_thread(lambda: match_running(games, steamlib.running()), self._on_running)

    def _on_running(self, playing, error):
        self._running_query = False
        if error is None and playing is not None:
            self._apply_running(set(playing))

    def _apply_running(self, playing: set):
        """A --running result: flip Starting… to Playing, notice games that
        closed (and come back to Nexus), refresh what changed."""
        started = playing - self._playing
        stopped = self._playing - playing
        now = datetime.now()
        for slug in started:
            self._launching.pop(slug, None)
            self._session_start[slug] = now
            g = self.game(slug)
            # Play time before this session: the library's figure isn't
            # refreshed until the next load, so Now playing adds to this.
            self._session_base[slug] = int((g or {}).get('playtime_minutes') or 0)
        for slug in stopped:
            t0 = self._session_start.pop(slug, None)
            base = self._session_base.pop(slug, None)
            g = self.game(slug)
            # Repacks are recorded by their launch tracker; the rest here.
            if t0 and g and (g.get('source', 'nexus') != 'nexus'
                             or g.get('launch_via') == 'steam'):
                history.record(slug, g['source'], t0, now, g.get('display_name'))
            if t0 and g:
                self._after_session(g, (now - t0).total_seconds())
                self._session_ended(g, (now - t0).total_seconds(), t0, now, base)
        if not self._launching:
            self._fast_timer.stop()
        changed = playing != self._playing or started
        self._playing = playing
        self._mark_states()
        for slug in stopped:
            self._game_stopped(slug)
        if started and not winfocus.available() and self._theatre:
            self.showMinimized()                 # no compositor help: step aside
        if changed or stopped:
            try:
                self.apply_filter()
                if self.selected:
                    self._sync_selection()
            except RuntimeError:
                pass

    _session_pending = None       # w-60: the band waiting for its moment

    def _session_ended(self, g: dict, secs: float, t0, ended, base):
        """A game closed after a real session (a minute or more) while Theatre
        is on: remember it for the session band, shown once Nexus is back in
        front (see _back_from_game / _show_session)."""
        if not self._theatre or not history.is_session(secs):
            return
        minutes = max(1, round(secs / 60))
        total = max(int(g.get('playtime_minutes') or 0), int(base or 0) + minutes)
        g['playtime_minutes'] = total               # the library only catches up at next load
        self._session_pending = {'slug': g['slug'], 't0': t0, 'ended': ended.strftime('%H:%M'),
                                 'session': fmt_duration(minutes), 'total': fmt_duration(total)}

    def _back_from_game(self):
        """400 ms after a game closed: the session band if there is one, else
        focus back where it belongs."""
        if self._theatre:
            self._feed_theatre_week(ttl=0)          # the week just grew
        if not self._show_session():
            focus_and_reveal(self.default_focus())

    def _show_session(self) -> bool:
        """Open the pending session's page. It waits (stays pending) while
        Nexus is minimised or hidden or the quick menu or an options sheet is
        open, and is dropped when Theatre has been left."""
        pend = self._session_pending
        if pend is None:
            return False
        if not self._theatre or self.game(pend['slug']) is None:
            self._session_pending = None
            return False
        if self.isMinimized() or not self.isVisible() or self.quick.isVisible() \
                or self.theatre_sheet.isVisible() or self.theatre_settings.isVisible():
            return False
        self._session_pending = None
        g = self.game(pend['slug'])
        self.theatre.show_session(g, pend)
        if g.get('source', 'nexus') in ('steam', 'nexus') and (g.get('appid') or
                                                              g.get('source', 'nexus') == 'nexus'):
            ident = str(g['appid']) if g.get('source') == 'steam' else g['slug']
            cli_json(['--achievements', ident, '--json'],
                     lambda data, _e, pend=pend: alive(self)
                     and self._session_achievements(pend, data or {}), 30000)
        return True

    def _session_achievements(self, pend: dict, data: dict):
        """The fresh list: the ones unlocked this session, if the band is
        still showing this session."""
        td = self.theatre.details
        if td.session_shown() and td.game.get('slug') == pend['slug']:
            td.set_session_achievements(session_unlocks(data.get('items'), pend['t0']))

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if self._session_pending is not None and ev.type() in (
                QEvent.Type.WindowStateChange, QEvent.Type.ActivationChange):
            QTimer.singleShot(0, self._show_session)       # Nexus came back to the front

    CRASH_SECONDS = 20          # a repack that closes this soon most likely crashed
    KEEP_SECONDS = 60           # a tried Proton that runs this long is worth keeping

    def _after_session(self, g: dict, secs: float):
        """The crash helper: a repack that closed within seconds gets a toast
        with its log and "Try another Proton…" (one run, nothing saved); a
        tried Proton that held up is offered for good."""
        slug, name = g['slug'], g.get('display_name', g['slug'])
        if g.get('source', 'nexus') != 'nexus' or g.get('launch_via') == 'steam':
            return
        tried = self._trying.pop(slug, None)
        if secs >= self.CRASH_SECONDS:
            if tried and secs >= self.KEEP_SECONDS:
                self.toast(f"{name} ran with {tried}. Play it with {tried} from now on?",
                           'success', [("Use it", lambda: self.run(
                               ['--proton', slug, tried], f"Switching {name} to {tried}", slug,
                               'task', bool(g.get('steam'))))], 15000)
            return
        rc = last_exit_code(slug)
        how = f"closed after {int(secs)} s" + (f" (exit code {rc})" if rc not in (None, 0)
                                                else "")
        self.toast(f"{name} {how}" + (f" with {tried}." if tried else "."), 'warn', [
            ("Show log", lambda: self.on_game_action('view-log', g)),
            ("Try another Proton…", lambda: self.try_other_proton(g))], 0)

    def try_other_proton(self, g: dict):
        """Pick an installed Proton and launch the game with it this once."""
        slug, name = g['slug'], g.get('display_name', g['slug'])
        current = g.get('proton')

        def got(data, _err):
            if not alive(self):
                return
            names = [p['name'] for p in (data or {}).get('protons') or []
                     if p['name'] != current]
            if not names:
                self.toast("No other Proton is installed — get one on the Proton page.",
                           'info', [("Proton", lambda: self.go(self.PROTON))], 6000)
                return

            def launch(p):
                self._trying[slug] = p
                self._begin_launch(g)
                self.run(['--launch', slug, '--try-proton', p], f"Launching {name} with {p}",
                         slug, 'launch')
            sheet = self.theatre_sheet if self._theatre else self.sheet
            sheet.choose(f"Try {name} with…", [(p, G['proton'], lambda p=p: launch(p))
                                              for p in names],
                         "This run only — the game keeps its Proton unless it works better.")
            self._update_hints()
        cli_json(['--list-protons', '--json'], got, 20000)

    def _launch_expired(self, slugs):
        for slug in slugs:
            self._launching.pop(slug, None)
            g = next((x for x in self._games if x['slug'] == slug), None)
            if g is None:
                continue
            name = g.get('display_name', slug)
            repack = g.get('source', 'nexus') == 'nexus' and g.get('launch_via') != 'steam'
            tried = self._trying.pop(slug, None) if repack else None
            action = [("Show log", lambda g=g: self.on_game_action('view-log', g)),
                      ("Try another Proton…", lambda g=g: self.try_other_proton(g))] \
                if repack else None
            self.toast(f"{name} doesn't seem to have started"
                       + (f" with {tried}." if tried else "."), 'warn', action, 10000)
        if not self._launching:
            self._fast_timer.stop()
        self._mark_states()
        self._home_sig = None
        self.apply_filter()
        if self.selected:
            self._sync_selection()

    def _mark_states(self):
        for g in self._games:
            g['playing'] = g['slug'] in self._playing
            g['launching'] = g['slug'] in self._launching and not g['playing']
            g['busy'] = g['slug'] in self._busy
            # When this session started and the play time before it (Now
            # playing: "42 min", "Started at 21:02 · 141.7 h played").
            g['session_start'] = self._session_start.get(g['slug']) if g['playing'] else None
            g['session_base'] = self._session_base.get(g['slug'])

    def resume_game(self, g: dict):
        """Resume: back to a running game. Its window (the one the hand-off
        found, else the best match now) is focused on the thread pool, like
        every compositor call. Best effort: without the compositor's help,
        or with no window to be found, Nexus steps aside (minimises) so the
        game is what's underneath, as it does at launch."""
        slug = g['slug']
        if not winfocus.available():
            self.showMinimized()
            return
        wid = self._game_window.get(slug)
        game = {k: g.get(k) for k in ('slug', 'source', 'appid', 'exe', 'display_name',
                                      'install_dir')}
        pid = os.getpid()

        def find():
            wins = winfocus.windows()
            me = winfocus.own_window(wins, pid=pid)
            target = wid if any(w.get('id') == wid for w in wins) else \
                winfocus.pick_game_window(wins, {me}, game, strict=True)
            return (target if target is not None and winfocus.focus(target) else None), me

        def done(res, _err):
            if not alive(self):
                return
            target, me = res or (None, None)
            if target is None:
                self.showMinimized()             # nothing to raise: step aside
                return
            self._game_window[slug] = target
            if me is not None:                   # and come back here when it ends
                self._return_to.setdefault(slug, me)
        in_thread(find, done)

    def stop_game(self, g: dict):
        """Quit game, once confirmed: `--stop` as a quick job, so the process
        scan and the kills (TERM, KILL after 10 s) stay off the GUI thread.
        The running poll then sees it go, as if it had closed by itself."""
        slug, name = g['slug'], g.get('display_name', g['slug'])
        self.run(['--stop', slug], f"Quitting {name}", slug, 'tool')

    def _game_changed(self, slug):
        """One game's state moved (download progress): repaint just it."""
        for t in self.findChildren(GameTile):
            if t.game.get('slug') == slug:
                t.update()
        g = next((x for x in self._games if x['slug'] == slug), None)
        if g is None:
            return
        if self.details.game.get('slug') == slug:
            self.details.btn_play.setText(play_label(g))
            self.details.btn_play.setEnabled(play_enabled(g))
        td = self.theatre.details
        if td.isVisible() and td.game.get('slug') == slug:
            td.set_play(g)

    def _begin_launch(self, game):
        """Play pressed: show "Starting…" right away, poll until the game is
        really running, and get ready to give its window the focus."""
        slug = game['slug']
        self._session_pending = None
        self._launching[slug] = time.monotonic()
        self._mark_states()
        game['launching'] = True
        # The windows open before the game starts, so its own can be told
        # apart later. `niri msg` runs on the thread pool: the GUI thread used
        # to wait on it (up to 2 s) on every Play.
        h = self._handoff = {'game': game, 'before': None, 'since': time.monotonic(),
                             'nexus': None}
        if winfocus.available():
            pid = os.getpid()

            def snapshot():
                wins = winfocus.windows()
                return {w.get('id') for w in wins}, winfocus.own_window(wins, pid=pid)

            def got(res, _err, h=h):
                h['before'], h['nexus'] = res if res else (set(), None)
            in_thread(snapshot, got)
            self._handoff_timer.start()
        self._fast_timer.start()
        QTimer.singleShot(1200, self._poll_running)
        self._home_sig = None
        self.apply_filter()
        if self.selected:
            self._sync_selection()

    def _handoff_tick(self):
        h = self._handoff
        if not h or time.monotonic() - h['since'] > 120:
            self._handoff_timer.stop()
            self._handoff = None
            return
        if self._handoff_query or h['before'] is None:   # still taking the snapshot
            return
        # `niri msg` on the thread pool: a busy compositor (it's starting a
        # game, after all) must not stall the window every 800 ms.
        self._handoff_query = True
        game, before = dict(h['game']), set(h['before'])
        in_thread(lambda: winfocus.pick_game_window(winfocus.windows(), before, game),
                  lambda wid, _err, h=h: self._handoff_found(h, wid))

    def _handoff_found(self, h, wid):
        self._handoff_query = False
        if wid is None or h is not self._handoff:
            return
        in_thread(lambda: winfocus.focus(wid), lambda *_: None)
        self._game_window[h['game']['slug']] = wid      # for Resume
        if h.get('nexus') is not None:
            self._return_to[h['game']['slug']] = h['nexus']
        self._handoff_timer.stop()
        self._handoff = None

    def _game_stopped(self, slug):
        """The game closed: come back to Nexus (where focus was handed off)."""
        self._game_window.pop(slug, None)
        nexus = self._return_to.pop(slug, None)
        if nexus is not None:
            in_thread(lambda: winfocus.focus(nexus), lambda *_: None)
        elif self.isMinimized():
            self._unminimize()
        QTimer.singleShot(400, self._back_from_game)


def last_exit_code(slug: str) -> int | None:
    """The game's exit code, from the `# exited <rc> after …` line its launch
    tracker writes at the end of <slug>-last-launch.log (None if not there yet)."""
    import re
    from nexus.paths import LOG_DIR
    try:
        with open(LOG_DIR / f"{slug}-last-launch.log", 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode('utf-8', 'replace')
    except OSError:
        return None
    m = re.findall(r'^# exited (-?\d+) after', tail, re.M)
    return int(m[-1]) if m else None


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .fmt import fmt_duration  # noqa: E402
from .gameinfo import is_installed, play_enabled, play_label, session_unlocks  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json, in_thread, match_running  # noqa: E402
from .theme import G  # noqa: E402
from .tiles import GameTile  # noqa: E402

