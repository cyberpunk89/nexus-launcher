"""Controllers, Theatre (TV) mode and the button hints; requests from a
second `nexus` launch."""

import os

from PyQt6.QtCore import QProcess, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QLineEdit

from nexus import platform, winfocus

from .common import alive
from .theme import build_stylesheet, G, GLYPHS

# Settings > Controls "Start Nexus in Theatre: On a TV": a screen this big or
# bigger. EDID sizes outside 10-120" are made up (projectors report 0, some
# TVs an aspect ratio like 16x9 cm), so they count as "not a TV".
TV_INCHES = 32
PAD_LEAVE_MS = 10_000          # last pad gone after a pad brought Theatre: back after this


def screen_inches(w_mm, h_mm) -> float | None:
    """The diagonal in inches, or None when the size is missing or not
    believable (under 10" or over 120")."""
    if not w_mm or not h_mm or w_mm <= 0 or h_mm <= 0:
        return None
    inches = (w_mm ** 2 + h_mm ** 2) ** 0.5 / 25.4
    return inches if 10 <= inches <= 120 else None


class ControllerMixin:
    """Part of MainWindow (nexus/gui/window.py), which mixes it in; `self` is
    the window."""

    def _maybe_attract(self):
        """Start the screensaver, unless a game runs or something's open."""
        if not self._theatre or self._playing or self._launching \
                or not self.isActiveWindow() or self.trailer_view.isVisible() \
                or self.theatre.trailer.isVisible() or self.theatre_sheet.isVisible() \
                or self.theatre_settings.isVisible() or self.quick.isVisible() \
                or self.attract.running:
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

    def _pad_connected(self, name, kind, guid='', hotplug=False):
        self._pad_leave_timer.stop()                    # a pad is back: Theatre stays
        self._battery_timer.start(1500)                 # sysfs appears just after
        self.hints.kind = kind
        self.input.set('pad')
        if hotplug and self._pad_may_enter():
            self.set_theatre(True)
            self._theatre_auto = True                   # so it leaves the way it came
            self.toast("Theatre mode. Start opens the menu, F11 leaves.", 'info', None, 4000)
            return
        self._sync_hints()
        self.toast(f"{name} connected — D-pad to move, {GLYPHS.get(kind, GLYPHS['xbox'])['a']} "
                   f"to select.", 'info', None, 4000)
        if QApplication.focusWidget() is None:
            t = self.default_focus()
            t and focus_and_reveal(t)

    def _pad_may_enter(self) -> bool:
        """Settings > Controls "Switch to Theatre when a controller
        connects", and nothing in the way: Nexus in front, no game running
        or starting, no sheet, palette, search or setup open, and no pad
        still connected from when Theatre was left by hand."""
        return bool(self.prefs.controls.get('theatre_on_pad')) and not self._theatre \
            and not self._pad_suppressed and self.isActiveWindow() \
            and not (self._playing or self._launching) and self._open_sheet() is None \
            and not self.palette.isVisible() and not self.desk_search.isVisible() \
            and self.current() != self.SETUP and QApplication.activeModalWidget() is None

    def _pad_disconnected(self, name):
        self._battery_timer.start(500)
        self._sync_hints()
        if self.ctrl.pads.physical:
            self.toast(f"{name} disconnected.", 'info', None, 3000)
            return
        self._pad_suppressed = False                    # the pad Theatre was left with is gone
        if self._theatre and self._theatre_auto:
            self._pad_leave_timer.start(PAD_LEAVE_MS)   # a battery swap or a Bluetooth blip
            self.toast(f"{name} disconnected.", 'info', None, 3000)
        elif self._theatre:
            self.toast("Controller disconnected. Reconnect it, or press F11 to leave Theatre.",
                       'info', None, 6000)
        else:
            self.toast(f"{name} disconnected.", 'info', None, 3000)

    def _pad_leave_timeout(self):
        """The last pad didn't come back: Theatre leaves the way it came."""
        if not (self._theatre and self._theatre_auto) or self.ctrl.pads.physical \
                or self._playing or self._launching:
            return
        self.set_theatre(False)
        self.toast("Controller disconnected, back to the desktop.", 'info', None, 5000)

    def _input_changed(self, mode: str):
        """The mouse or keyboard took over: Theatre is now the user's, never
        left on its own."""
        if mode in ('mouse', 'keyboard'):
            self._theatre_auto = False
            self._pad_leave_timer.stop()
        self._sync_hints()

    def _screen_mm(self) -> tuple | None:
        """The window's screen's physical size in mm: the platform's answer
        (Windows reads the EDID), else Qt's."""
        screen = self.screen()
        if screen is None:
            return None
        g = screen.geometry()
        got = platform.screen_size_mm(g.x() + 1, g.y() + 1)
        if got:
            return got
        size = screen.physicalSize()
        return size.width(), size.height()

    def theatre_on_start(self):
        """Settings > Controls "Start Nexus in Theatre" on a normal start:
        Always, or On a TV (a screen of TV_INCHES or more). Never during
        first-run setup; it never leaves Theatre by itself later."""
        rule = self.prefs.controls.get('theatre_start', 'never')
        if self._first_run or rule not in ('always', 'tv'):
            return
        if rule == 'tv':
            inches = screen_inches(*(self._screen_mm() or (None, None)))
            if inches is None or inches < TV_INCHES:
                return
        self.set_theatre(True)

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
        if self._theatre and self.theatre_settings.isVisible():
            if act == 'view':
                self.theatre_settings.close_sheet()
            return                                  # nor under Theatre's settings
        if self._theatre and act in ('page-prev', 'page-next') \
                and self.theatre.details.isVisible():
            # The game page: LB / RB switch the drawer's sections.
            if not self.theatre.trailer.isVisible() and self._open_sheet() is None:
                self.theatre.details.cycle_section(-1 if act == 'page-prev' else 1)
                self._update_hints()
            return
        if self._theatre and act == 'view':             # a context button, not a tab switch
            if self._open_sheet() is not None:
                self._open_sheet().close_sheet()            # View closes Sort & filter
            elif not self.theatre.trailer.isVisible():
                self.theatre.view_pressed()
            self._update_hints()
            return
        if self._theatre and act in ('page-prev', 'page-next'):
            if self.theatre.reader.isVisible():
                if self._open_sheet() is None:
                    self.theatre.reader.step(-1 if act == 'page-prev' else 1)
                return
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
        self.theatre_settings.close_sheet()
        game = now_playing(self._games)
        # Resume while a game runs or starts, else Exit Theatre; never a
        # row with a section (POWER), whatever joins the menu.
        self.quick.open(self._quick_rows(game), self._quick_pads(),
                        focus='resume' if game else 'exit-theatre', now_playing=game)
        self._update_hints()

    def _quick_rows(self, game: dict | None = None) -> list:
        """The menu's rows, top to bottom: Resume (and Quit game) while `game`
        runs or starts (Now playing), Settings (Theatre's sheet), then Exit
        Theatre and Quit Nexus. Rows
        with a section (POWER) sit at the bottom of the panel."""
        rows = []
        if game is not None:                    # Quit game once it's really running
            rows.append(QuickRow('resume', "Resume", G['play']))
            if game.get('playing'):
                rows.append(QuickRow('quit-game', "Quit game", G['stop'], danger=True))
        rows += [QuickRow('settings', "Settings", G['settings'], gap=bool(rows)),
                 QuickRow('exit-theatre', "Exit Theatre", G['log_out'], gap=True),
                 QuickRow('quit', "Quit Nexus", G['close'])]
        caps = platform.power_caps()
        for action, label, glyph in (('sleep', "Sleep", 'moon'), ('restart', "Restart PC", 'restart'),
                                     ('shutdown', "Shut down", 'power')):
            if action in caps:
                rows.append(QuickRow(f'power-{action}', label, G[glyph], section='POWER',
                                     danger=action != 'sleep'))
        return rows

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
        game = self.quick.now_game
        if key == 'resume' and game is not None:
            self.quick.close_menu(restore=False)
            self.resume_game(game)
        elif key == 'quit-game' and game is not None:
            self._ask_quit_game(game, self.quick)
        elif key == 'settings':
            self.quick.close_menu()                     # focus back first: the sheet returns there
            self.open_theatre_settings()
        elif key == 'exit-theatre':
            self.quick.close_menu(restore=False)
            self.set_theatre(False)
        elif key == 'power-sleep':
            self._power_now('sleep')
        elif key in ('power-restart', 'power-shutdown'):
            self._ask_power(key[6:])
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

    def open_theatre_settings(self):
        """Theatre's settings sheet (the gear, the quick menu's Settings),
        with the values as they are now."""
        if not self._theatre:
            return
        self.theatre_sheet.close_sheet(restore=False)
        self.theatre_settings.open(self.prefs.controls, self.prefs.appearance)
        self._update_hints()

    def _power_now(self, action: str):
        """Save what Nexus keeps, then ask the OS. close() would only go to
        the tray, so the window stays; a refusal toasts and the row stays."""
        self.quick.close_menu(restore=False)
        self.prefs.save()
        if not platform.power(action):
            self.toast("Couldn't " + {'sleep': "sleep", 'restart': "restart",
                                      'shutdown': "shut down"}[action] + " — the system refused.",
                       'error')

    def _ask_power(self, action: str):
        game = self.quick.now_game
        title = "Restart this PC?" if action == 'restart' else "Shut down this PC?"
        text = (f"{game.get('display_name', game['slug'])} is running; anything you haven't "
                f"saved will be lost." if game is not None and game.get('playing')
                else "Anything you haven't saved will be lost.")
        self.quick.ask(title, text, "Restart" if action == 'restart' else "Shut down",
                       G['restart' if action == 'restart' else 'power'],
                       lambda: self._power_now(action))
        self._update_hints()

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
        """Theatre mode: full screen, controller first, for the TV. Any call
        makes it the user's Theatre (only _pad_connected marks one a pad
        brought, after this); leaving it with a pad still connected keeps
        that pad from bringing it straight back."""
        self._theatre_auto = False
        self._pad_leave_timer.stop()
        if on == self._theatre:
            return
        self._theatre = on
        if not on:
            ctrl = getattr(self, 'ctrl', None)
            self._pad_suppressed = bool(ctrl and ctrl.pads.physical)
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
            self._feed_theatre_week()
        else:
            self._idle_timer.stop()
            self.attract.stop()
            self.cursor_hider.stop()
            self.theatre_sheet.close_sheet(restore=False)
            self.theatre_settings.close_sheet(restore=False)
            self.quick.close_menu(restore=False)
            self.theatre.trailer.stop()
            self.theatre.reader.hide()
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
        if act == 'resume':
            self.resume_game(game)
            return
        if act == 'quit-game':                       # the spotlight, or Options on its page
            self._ask_quit_game(game, self.theatre_sheet)
            return
        if act in self.DESK_ACTIONS:
            self.set_theatre(False)
        self.on_game_action(act, game)

    def _ask_quit_game(self, g: dict, where):
        """Quit game asks first, in the panel it came from (the quick menu or
        Theatre's sheet), with Cancel focused and the game named."""
        name = g.get('display_name', g['slug'])
        title, text = f"Quit {name}?", "Anything you haven't saved will be lost."
        if where is self.quick:
            where.ask(title, text, "Quit game", G['stop'], lambda: self.stop_game(g))
        else:
            where.ask(title, text, "Quit game", lambda: self.stop_game(g))
        self._update_hints()

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
        if self._theatre and self.quick.isVisible():
            self.hints.show_hints([('a', 'Select'), ('b', 'Cancel')]
                                  if self.quick.state == 'confirm' else
                                  [('a', 'Select'), ('b', 'Close'), ('Menu', 'Close')])
            return
        if self._theatre and self.theatre_settings.isVisible():
            self.hints.show_hints([('a', 'Change'), ('← →', 'Adjust'), ('b', 'Done')]
                                  if isinstance(f, CouchRow) else
                                  [('a', 'Open'), ('b', 'Done')])
            return
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
            if th.reader.isVisible():
                r = th.reader
                hints = ([('a', 'Show game')] if r.btn_game.isVisible() else []) \
                    + [('x', 'Options'), ('LB RB', 'Previous / next'), ('b', 'Close')]
                self.hints.show_hints(hints)
                return
            if th.search.isVisible():
                hints = ([('a', 'Open')] if isinstance(f, GameTile) else [('a', 'Type')]) \
                    + [('x', 'Delete'), ('y', 'Space'), ('b', 'Close')]
                self.hints.show_hints(hints)
                return
            if th.details.isVisible():                  # stage, drawer or reader
                hints = th.details.hints(f)
                if not th.details.expanded():
                    hints.append(('Menu', 'Quick menu'))
                self.hints.show_hints(hints)
                return
            if f is th.week_card:
                hints = ([('a', f"Open {f.game_name}")] if f.game_name else []) + [('b', 'Home')]
            elif f is not None and th.hero.isAncestorOf(f):
                # The spotlight on Home: the game that has focus in "Jump back in".
                pinned = th.hero.is_pinned()
                hints = [('a', ('Resume' if pinned else 'Play') if f is th.hero.btn else
                          'Quit game' if pinned and f is th.hero.btn_details else 'Open'),
                         ('x', 'Options'), ('↓', 'Your games')]
            elif isinstance(f, GameTile):
                hints = [('a', self._a_label(f)), ('x', 'Options'), ('b', 'Home')]
            elif th.tabs.current() == 'discover' and isinstance(f, (TheatreCard, TheatreSetupCard)):
                hints = [('a', 'Read' if isinstance(f, TheatreCard) and f.kind == 'news'
                          else 'Options' if isinstance(f, TheatreCard) else 'Open Settings'),
                         ('x', 'Options' if isinstance(f, TheatreCard) else 'Hide'), ('b', 'Home')]
            elif th.tabs.current() in ('discover', 'downloads'):
                hints = [('b', 'Home')]                 # nothing here to act on
            else:
                hints = [('a', 'Select'), ('b', 'Home')]
            if th.tabs.current() == 'downloads':
                from .downloads import PRIMARY_LABELS
                if isinstance(f, TheatreDownloadRow):
                    hints = [('a', PRIMARY_LABELS.get(f.r['primary'], 'Options')),
                             ('x', 'Options'), ('b', 'Home')]
                else:
                    hints = [('b', 'Home')]
            if th.moment_banner.isVisible():
                hints.insert(-1, ('View', f"Show {th.moment_banner.name}" if th.moment_banner.name
                                  else "Show it"))
            elif th.tabs.current() == 'library':
                hints.insert(-1, ('View', 'Sort & filter'))
            hints += [('y', 'Search'), ('LB RB', 'Tabs'), ('Menu', 'Quick menu')]
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
from .gameinfo import can_quick_play, now_playing  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import in_thread  # noqa: E402
from .quickmenu import QuickRow  # noqa: E402
from .library import ListView  # noqa: E402
from .theatre import THEATRE_SCALE, TheatreDownloadRow  # noqa: E402
from .theatre_discover import TheatreCard, TheatreSetupCard  # noqa: E402
from .theatre_settings import CouchRow  # noqa: E402
from .tiles import GameTile  # noqa: E402
