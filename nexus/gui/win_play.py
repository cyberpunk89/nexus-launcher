"""Playing: which games are running, launches in progress, and handing
keyboard/controller focus to a game's window and back."""

import os
import time
from datetime import datetime

from PyQt6.QtCore import QTimer

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
        for slug in stopped:
            t0 = self._session_start.pop(slug, None)
            g = self.game(slug)
            # Repacks are recorded by their launch tracker; the rest here.
            if t0 and g and (g.get('source', 'nexus') != 'nexus'
                             or g.get('launch_via') == 'steam'):
                history.record(slug, g['source'], t0, now, g.get('display_name'))
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

    def _launch_expired(self, slugs):
        for slug in slugs:
            self._launching.pop(slug, None)
            g = next((x for x in self._games if x['slug'] == slug), None)
            if g is None:
                continue
            name = g.get('display_name', slug)
            action = ("Show log", lambda g=g: self.on_game_action('view-log', g)) \
                if g.get('source', 'nexus') == 'nexus' else None
            self.toast(f"{name} doesn't seem to have started.", 'warn', action, 10000)
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
            td.btn_play.setText(play_label(g))
            td.btn_play.setEnabled(play_enabled(g))

    def _begin_launch(self, game):
        """Play pressed: show "Starting…" right away, poll until the game is
        really running, and get ready to give its window the focus."""
        slug = game['slug']
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
        if h.get('nexus') is not None:
            self._return_to[h['game']['slug']] = h['nexus']
        self._handoff_timer.stop()
        self._handoff = None

    def _game_stopped(self, slug):
        """The game closed: come back to Nexus (where focus was handed off)."""
        nexus = self._return_to.pop(slug, None)
        if nexus is not None:
            in_thread(lambda: winfocus.focus(nexus), lambda *_: None)
        elif self.isMinimized():
            self._unminimize()
        QTimer.singleShot(400, lambda: alive(self) and focus_and_reveal(self.default_focus()))


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .gameinfo import is_installed, play_enabled, play_label  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import in_thread, match_running  # noqa: E402
from .tiles import GameTile  # noqa: E402
