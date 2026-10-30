"""Controllers, Theatre (TV) mode and the button hints; requests from a
second `nexus` launch."""

import os

from PyQt6.QtCore import QProcess, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QLineEdit

from nexus import winfocus

from .common import alive
from .theme import build_stylesheet, G, GLYPHS


class ControllerMixin:
    """Part of MainWindow (nexus/gui/window.py), which mixes it in; `self` is
    the window."""

    def _maybe_attract(self):
        """Start the screensaver, unless a game runs or something's open."""
        if not self._theatre or self._playing or self._launching \
                or not self.isActiveWindow() or self.trailer_view.isVisible() \
                or self.theatre.trailer.isVisible() or self.theatre_sheet.isVisible() or self.attract.running:
            self._on_activity()                         # look again later
            return
        self.attract.start(self._games)

    def _apply_controls(self, controls: dict | None = None):
        """Settings → Controls, applied at once (a library preference)."""
        if controls is not None:
            self.prefs.controls = {**self.prefs.controls, **controls}
            self.prefs.save()
            if 'dedupe' in controls:
                mark_duplicates(self._games, self.prefs.controls['dedupe'])
                self._home_sig = None
                self.apply_filter()
        self.theatre.a_plays = self.prefs.controls['pad_a'] == 'play'
        if not self.prefs.controls['attract']:
            self._idle_timer.stop()
        else:
            self._on_activity()
        self.theatre.double_plays = self.prefs.controls['double_click'] == 'play'
        self.sync_tray()
        if hasattr(self, 'hints'):
            self._update_hints()

    def _open_on_play(self, game):
        self._tile_clicked(game, Qt.KeyboardModifier.NoModifier)
        QTimer.singleShot(0, lambda: alive(self.details.btn_play)
                          and self.details.btn_play.isVisible()
                          and self.details.btn_play.isEnabled()
                          and focus_and_reveal(self.details.btn_play))

    def _poll_battery(self):
        """Controller battery for the top bar and the hint bar (sysfs, cheap):
        every minute, and whenever a pad comes or goes."""
        from nexus import gamepad
        self._battery_timer.start(60_000)
        pads = gamepad.battery()
        self.topbar.set_battery(pads)
        b = pads[0] if pads else None
        self.hints.set_battery(f"Controller {b['percent']}%" + (" ⚡" if b['charging'] else '')
                               if b else '')
        low = b and b['percent'] < 15 and not b['charging']
        if low and not getattr(self, '_battery_warned', False):
            self.toast(f"Controller battery low ({b['percent']}%).", 'warning', None, 6000)
        self._battery_warned = bool(low)

    def _pad_connected(self, name, kind):
        self._battery_timer.start(1500)                 # sysfs appears just after
        self.hints.kind = kind
        self.input.set('pad')
        self._sync_hints()
        self.toast(f"{name} connected — D-pad to move, {GLYPHS.get(kind, GLYPHS['xbox'])['a']} "
                   f"to select.", 'info', None, 4000)
        if QApplication.focusWidget() is None:
            t = self.default_focus()
            t and focus_and_reveal(t)

    def _pad_disconnected(self, name):
        self._battery_timer.start(500)
        self._sync_hints()
        self.toast(f"{name} disconnected.", 'info', None, 3000)

    def _pad_used(self):
        if QApplication.focusWidget() is None or QApplication.focusWidget().window() is not self:
            t = self.default_focus()
            t and focus_and_reveal(t)

    def _nav_order(self):
        """LB / RB cycle the content tabs (the system pages are one press
        of B away, back on Home)."""
        return [p for p in self.TABS if not self.nav[p].isHidden()]

    def _pad_action(self, act):
        if act in ('start', 'quick'):
            # Start and Guide: Theatre's quick menu, from any screen; on the
            # desktop, into Theatre (F11 is the way straight out).
            if self._theatre:
                self.toggle_quick()
            else:
                self.set_theatre(True)
            return
        if self._theatre and self.quick.isVisible():
            return                                  # LB / RB / View / Y: not under the menu
        if self._theatre and act in ('page-prev', 'page-next') \
                and self.theatre.details.isVisible():
            # The game page: LB / RB switch the drawer's sections.
            if not self.theatre.trailer.isVisible() and self._open_sheet() is None:
                self.theatre.details.cycle_section(-1 if act == 'page-prev' else 1)
                self._update_hints()
            return
        if self._theatre and act in ('page-prev', 'page-next', 'view'):
            self.theatre.cycle_tab(-1 if act == 'page-prev' else 1)
            return
        if self._theatre and act == 'search':
            if self.theatre.search.isVisible():
                self.theatre.search.type(' ')        # Y on the keyboard: a space
            elif not self.theatre.details.isVisible():
                self.theatre.open_search()
            self._update_hints()
            return
        if act in ('page-prev', 'page-next'):
            order = self._nav_order()
            cur = self.current()
            i = order.index(cur) if cur in order else 0
            self.go(order[(i + (1 if act == 'page-next' else -1)) % len(order)])
            QTimer.singleShot(50, lambda: alive(self) and focus_and_reveal(self.default_focus()))
        elif act == 'view' and self.current() == self.LIBRARY:
            views = ['home', 'grid', 'list']
            self.set_view(views[(views.index(self._view) + 1) % 3])
            QTimer.singleShot(50, self._focus_view)
        elif act == 'search':
            if self.desk_search.isVisible():
                self.desk_search.type(' ')           # Y on the keyboard: a space
            elif self._steam_up():
                self._focus_search()
                self._pad_action('keyboard')
            else:
                self.sheet.close_sheet(restore=False)
                self.desk_search.open([g for g in self._games if not g.get('hidden')])
            self._update_hints()
        elif act == 'keyboard':
            # Steam's on-screen keyboard, when Steam is running (Big Picture's).
            if self._steam_up():
                QProcess.startDetached('steam', ['steam://open/keyboard'])

    def _desk_search_picked(self, g):
        self.desk_search.setVisible(False)
        self._reveal_game(g['slug'])
        self._open_on_play(g)
        self._update_hints()

    def _desk_search_closed(self):
        self._update_hints()
        t = self.default_focus()
        if t is not None and self.input.mode != 'mouse':
            focus_and_reveal(t)

    def _toggle_tv(self):
        self.set_theatre(not self._theatre)

    # ---- the quick menu: Theatre's system panel ----
    def toggle_quick(self):
        """Guide, Start, F10 or a remote's Home key in Theatre: open or close
        the quick menu. It replaces an open Options sheet."""
        if not self._theatre:
            return
        if self.attract.running:                        # the press ends the screensaver
            self.attract.stop()
            self._on_activity()
            return
        if self.quick.isVisible():
            self.quick.close_menu()
            return
        self.theatre_sheet.close_sheet()
        self.quick.open(self._quick_rows(), self._quick_pads(), focus='exit-theatre')
        self._update_hints()

    def _quick_rows(self) -> list:
        """The menu's rows, top to bottom. Rows with a section (POWER) sit
        at the bottom of the panel."""
        return [QuickRow('exit-theatre', "Exit Theatre", G['log_out']),
                QuickRow('quit', "Quit Nexus", G['close'])]

    def _quick_pads(self) -> list[dict]:
        """The connected pads for the menu's strip (Steam's virtual copy of
        a real one left out). sysfs can't tell which pad a battery is, so a
        battery shows only when there's one pad."""
        ctrl = getattr(self, 'ctrl', None)
        if ctrl is None:
            return []
        from nexus import gamepad
        g = ctrl.pads
        names = [n for iid, (_h, n, _k) in g.pads.items() if not g.shadowed(iid)]
        batt = gamepad.battery() if len(names) == 1 else []
        return [{'name': n, 'percent': batt[0]['percent'] if batt else None,
                 'charging': bool(batt and batt[0]['charging'])} for n in names]

    def _quick_chosen(self, key: str):
        if key == 'exit-theatre':
            self.quick.close_menu(restore=False)
            self.set_theatre(False)
        elif key == 'quit':
            reasons = self._quit_reasons()
            if reasons:                                 # asks only with work to lose
                self.quick.ask("Quit Nexus?", "\n\n".join(reasons), "Quit anyway",
                               G['close'], self.quit_after_jobs)
                self._update_hints()
                return
            self.quick.close_menu(restore=False)
            self._tray_quit = True                      # quit for real, not to the tray
            self.close()

    def handle_remote(self, argv: list):
        """Another `nexus` launch handed us its arguments (see InstanceServer),
        or the background agent something it found (no raising the window)."""
        if argv[:1] in (['--agent-ready'], ['--agent-install']):
            self._agent_message(argv)
            return
        if '--leave-theatre' in argv:
            self.set_theatre(False)
            return
        if '--hidden' in argv:              # the Startup shortcut, with Nexus already up
            return
        if '--theatre' in argv:
            self.set_theatre(True)
        self.bring_to_front()

    def bring_to_front(self):
        if self.isMinimized() or not self.isVisible():
            self._unminimize()
        self.raise_()
        self.activateWindow()
        # Wayland ignores activateWindow() from a background app; niri can do
        # it — through `niri msg`, on the thread pool (up to 2 s each).
        if winfocus.available():
            pid = os.getpid()

            def focus_own():
                wid = winfocus.own_window(winfocus.windows(), pid=pid)
                return wid is not None and winfocus.focus(wid)
            in_thread(focus_own, lambda *_: None)

    def set_theatre(self, on: bool):
        """Theatre mode: full screen, controller first, for the TV."""
        if on == self._theatre:
            return
        self._theatre = on
        if on:
            self.desk_search.close_search()
            self._was_state = self.windowState() & ~Qt.WindowState.WindowMinimized
            self.theatre.set_games([g for g in self._games if not g.get('hidden')])

            if self.config.get('news', True) and not self.newspage._loaded:
                self.newspage.refresh()           # the game page's News card (CLI-cached)
            self.shell.setCurrentWidget(self.theatre)
            self.showFullScreen()
            self.hints.setStyleSheet(build_stylesheet(THEATRE_SCALE))
            self.sheet.close_sheet(restore=False)
            self.focus_ring.scale = 1.5
            self.focus_ring.nudge()
            self.cursor_hider.start()
            self._on_activity()                   # the screensaver clock starts
        else:
            self._idle_timer.stop()
            self.attract.stop()
            self.cursor_hider.stop()
            self.theatre_sheet.close_sheet(restore=False)
            self.quick.close_menu(restore=False)
            self.theatre.trailer.stop()
            self.theatre.search.setVisible(False)
            self.theatre.details.setVisible(False)
            self.theatre.pages.setEnabled(True)
            self.theatre.tabs.setEnabled(True)
            self.shell.setCurrentWidget(self.desk)
            self.hints.setStyleSheet("")
            self.focus_ring.scale = 1.0
            was = self._was_state
            if not was & Qt.WindowState.WindowFullScreen:
                self.setWindowState(was)         # maximised stays maximised
        self._sync_hints()
        QTimer.singleShot(80, lambda: alive(self) and focus_and_reveal(self.default_focus()))

    # Actions that open a page of the desktop app: Theatre steps aside first,
    # or they'd happen invisibly behind it.
    DESK_ACTIONS = ('artwork', 'settings', 'view-log', 'uninstall')

    def _theatre_action(self, act, game):
        if act in self.DESK_ACTIONS:
            self.set_theatre(False)
        self.on_game_action(act, game)

    def _a_label(self, f) -> str:
        game = f.game if isinstance(f, GameTile) else {}
        return "Play" if self.prefs.controls['pad_a'] == 'play' and game \
            and can_quick_play(game) else "Open"

    def _sync_hints(self):
        """Button hints are for a controller: always in Theatre, and on the
        desktop only while a pad is what's being used."""
        ctrl = getattr(self, 'ctrl', None)
        show = self._theatre or (self.input.mode == 'pad' and bool(ctrl and ctrl.pads.pads))
        self.hints.setVisible(show)
        self._update_hints()

    def _update_hints(self):
        if not self.hints.isVisible():
            return
        f = QApplication.focusWidget()
        if self._open_sheet() is not None:
            self.hints.show_hints([('a', 'Select'), ('b', 'Close')])
            return
        if not self._theatre and self.desk_search.isVisible():
            self.hints.show_hints(([('a', 'Open')] if isinstance(f, GameTile) else
                                   [('a', 'Type')]) + [('x', 'Delete'), ('y', 'Space'),
                                                       ('b', 'Close')])
            return
        if self._theatre:
            th = self.theatre
            if th.trailer.isVisible():
                self.hints.show_hints([('b', 'Close')])
                return
            if th.search.isVisible():
                hints = ([('a', 'Open')] if isinstance(f, GameTile) else [('a', 'Type')]) \
                    + [('x', 'Delete'), ('y', 'Space'), ('b', 'Close')]
                self.hints.show_hints(hints)
                return
            if th.details.isVisible():                  # stage, drawer or reader
                self.hints.show_hints(th.details.hints(f))
                return
            if f is not None and th.hero.isAncestorOf(f):
                # The spotlight on Home: the game that has focus in "Jump back in".
                hints = [('a', 'Play' if f is th.hero.btn else 'Open'), ('x', 'Options'),
                         ('↓', 'Your games')]
            elif isinstance(f, GameTile):
                hints = [('a', self._a_label(f)), ('x', 'Options'), ('b', 'Home')]
            else:
                hints = [('a', 'Select'), ('b', 'Home')]
            hints += [('y', 'Search'), ('LB RB', 'Home / Library'), ('Menu', 'Exit Theatre')]
            self.hints.show_hints(hints)
            return
        if isinstance(f, GameTile) or isinstance(f, ListView):
            hints = [('a', self._a_label(f)), ('x', 'Menu'), ('y', 'Search'), ('b', 'Back')]
        elif isinstance(f, DealCard):
            hints = [('a', f.primary_label), ('x', 'Links'), ('b', 'Back')]
        elif isinstance(f, DealSpotlight):
            hints = [('a', "Open deal"), ('x', 'Options'), ('b', 'Back')]
        elif f is not None and self.details.isAncestorOf(f):
            hints = [('a', 'Select'), ('b', 'Back to game'), ('x', 'Menu')]
        elif isinstance(f, QLineEdit):
            hints = ([('a', 'Keyboard')] if not f.isReadOnly() and self._steam_up() else []) \
                + [('b', 'Back')]
        else:
            hints = [('a', 'Select'), ('b', 'Back')]
        hints += [('LB RB', 'Pages')]
        if self.current() == self.LIBRARY:
            hints.append(('View', 'Switch view'))
        hints.append(('Menu', 'Theatre mode'))
        self.hints.show_hints(hints)


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .dashboard import mark_duplicates  # noqa: E402
from .deals import DealCard, DealSpotlight  # noqa: E402
from .gameinfo import can_quick_play  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import in_thread  # noqa: E402
from .quickmenu import QuickRow  # noqa: E402
from .library import ListView  # noqa: E402
from .theatre import THEATRE_SCALE  # noqa: E402
from .tiles import GameTile  # noqa: E402
