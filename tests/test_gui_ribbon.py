"""Theatre's Ribbon layout in the window (w-108), offscreen: switching
layouts keeps the focused game, the D-pad and shoulder buttons move, A opens
the game page and B comes back to the same item, B never leaves Theatre,
Start opens the quick menu, the background's ribbon has no timer when it
must be still, and no widget flashes up as a window of its own. Plus a
wording guard for the layout's names."""

import re
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import set_gui  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication, QMenu, QPushButton, QWidget
    from nexus.gui import art
    from nexus.gui.tiles import GameTile

REPO = Path(__file__).resolve().parent.parent


def fake_games():
    return [
        {'slug': 'alpha', 'display_name': 'Alpha', 'source': 'nexus', 'launcher': '/a',
         'last_played': '2026-09-30T10:00:00', 'playtime_minutes': 120},
        {'slug': 'steam-1', 'display_name': 'Beta', 'source': 'steam', 'appid': 1,
         'launcher': 'steam://rungameid/1', 'last_played': '2026-09-29T10:00:00'},
        {'slug': 'gamma', 'display_name': 'Gamma', 'source': 'nexus', 'launcher': '/g'},
    ]


@unittest.skipIf(nx is None, "PyQt6 not available")
class Ribbon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        nx.LibraryPrefs.save = lambda self: None
        set_gui('CliQuery', lambda *a, **k: None)

    @classmethod
    def tearDownClass(cls):
        set_gui('cli_json', cls._saved[0])
        nx.LibraryPrefs.save = cls._saved[1]
        set_gui('CliQuery', cls._saved[2])

    def setUp(self):
        for name, ret in (('power_caps', set()), ('power', True), ('bluetooth_pads', []),
                          ('bluetooth_disconnect', True)):
            p = mock.patch(f'nexus.platform.{name}', return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nx.MainWindow, '_poll_running', lambda self: None)
        p.start()
        self.addCleanup(p.stop)
        self.win = nx.MainWindow()
        self.win.resize(1300, 850)
        self.win.show()
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    # ---- helpers ----
    def _pump(self, n=3):
        for _ in range(n):
            QApplication.processEvents()

    def _ribbon(self):
        """Theatre with the Ribbon layout, focus on it."""
        w = self.win
        w.activateWindow()
        w.set_theatre(True)
        w._apply_controls({'theatre_layout': 'ribbon'})
        self._pump()
        focus = QApplication.focusWidget()
        if focus is not w.theatre.ribbon:
            w.theatre.ribbon.setFocus()
        self._pump()
        return w.theatre.ribbon

    def _press(self, key, w=None):
        QApplication.sendEvent(w or QApplication.focusWidget(),
                               QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))
        QApplication.processEvents()

    def _where(self, rb):
        it = rb.current()
        return rb.state.cat, it.key if it else None

    # ---- tests ----
    # ---- Classic colours (w-110: a setting; the secret code is parked) ----
    def test_classic_colours_is_a_ribbon_setting(self):
        from nexus.gui import theatre_settings as ts
        w = self.win
        row = next(r for r in ts.ROWS if r[1] == 'ribbon_classic')
        self.assertEqual(row[2], "Classic colours")
        self.assertIs(row[3], ts.SWITCH)
        self.assertIn('ribbon_classic', ts.HELP)
        self._ribbon()
        with mock.patch.object(nx.LibraryPrefs, 'save', lambda self: None), \
                mock.patch.object(w.background, 'ribbon_marks') as marks:
            w._theatre_setting('ribbon_classic', True)
            self.assertIs(w.prefs.controls['ribbon_classic'], True)
            self.assertTrue(w.background.classic)
            marks.assert_called_once()                            # the marks drift as it turns on
            w._theatre_setting('ribbon_classic', False)
            self.assertIs(w.prefs.controls['ribbon_classic'], False)
            self.assertFalse(w.background.classic)
            marks.assert_called_once()                            # not again when it turns off
        s = w.settings
        self.assertTrue(any(k.endswith('.ribbon_classic') for k in s.rows))
        s.set_controls({**w.prefs.controls, 'theatre_layout': 'tabs'})
        self.assertFalse(s.ctl_classic.isEnabled())
        s.set_controls({**w.prefs.controls, 'theatre_layout': 'ribbon', 'ribbon_classic': True})
        self.assertTrue(s.ctl_classic.isEnabled())
        self.assertTrue(s.ctl_classic.isChecked())
        w.set_theatre(False)

    def test_no_secret_code_on_the_ribbon(self):
        """The code is parked for later: the arrows, B and A do only what they say."""
        w = self.win
        rb = self._ribbon()
        K = Qt.Key
        for k in (K.Key_Up, K.Key_Up, K.Key_Down, K.Key_Down, K.Key_Left, K.Key_Right,
                  K.Key_Left, K.Key_Right, K.Key_B, K.Key_A):
            self._press(k, rb)
        self.assertFalse(w.prefs.controls['ribbon_classic'])
        w.set_theatre(False)

    def test_classic_colours_and_marks_paint_in_the_band(self):
        from datetime import datetime
        from nexus.gui import ribbon as rbm
        from nexus.gui.theme import LOOK
        w = self.win
        self._ribbon()
        bg = w.background
        self.assertEqual(bg.ribbon, 'input')
        light = LOOK['light']
        self.assertEqual(bg._ribbon_color().rgb(), rbm.ribbon_color(bg._tint, light).rgb())
        w._apply_controls({'ribbon_classic': True})
        self.assertTrue(bg.classic)
        when = datetime(2026, 3, 10, 14, 0)
        with mock.patch('nexus.gui.widgets.datetime') as dt:
            dt.now.return_value = when
            self.assertEqual(bg._ribbon_color().rgb(), rbm.classic_color(when, light).rgb())
        w._apply_controls({'ribbon_classic': False})
        self.assertFalse(bg.classic)
        w._apply_controls({'ribbon_classic': True})
        rects = []
        with mock.patch.object(bg, 'update', lambda *a: rects.append(a)):
            with mock.patch.object(art, 'MOTION', True):
                bg.ribbon_marks()
                self.assertTrue(bg._marks_timer.isActive())
                bg._marks_tick()
                self.assertTrue(rects)
                band = bg._ribbon_band().toAlignedRect()
                self.assertTrue(all(len(a) == 1 and a[0] == band for a in rects))
                bg._marks_t0 -= bg.MARKS_MS / 1000 + 1
                bg._marks_tick()
                self.assertFalse(bg._marks_timer.isActive())
                self.assertIsNone(bg._marks)
            for why in ('motion', 'contrast', 'glow'):
                with mock.patch.object(art, 'MOTION', why != 'motion'), \
                        mock.patch.dict(LOOK, {'contrast': why == 'contrast',
                                               'glow': 0 if why == 'glow' else 1.0}):
                    bg.ribbon_marks()
                    self.assertFalse(bg._marks_timer.isActive(), why)
        bg.update()
        bg.grab()                                                 # paints with a colour, no qFatal
        with mock.patch.object(art, 'MOTION', True):
            bg.ribbon_marks()
            bg.grab()                                             # and with marks
        bg._marks_timer.stop()
        w.set_theatre(False)

    def test_live_timer_runs_only_when_awake(self):
        from nexus.gui.theme import LOOK
        w = self.win
        K = Qt.ApplicationState
        bg = w.background
        rb = self._ribbon()
        running = lambda: bg._live_timer.isActive()  # noqa: E731
        with mock.patch.object(art, 'MOTION', True):
            self.assertFalse(running())                           # 'input': no timer
            w._apply_controls({'ribbon_motion': 'live'})
            self.assertEqual(bg.ribbon, 'live')
            w._ribbon_app_state(K.ApplicationActive)
            self.assertTrue(running())
            w._ribbon_app_state(K.ApplicationInactive)            # the window lost focus
            self.assertFalse(running())
            w._ribbon_app_state(K.ApplicationActive)
            self.assertTrue(running())
            w._playing = {'alpha'}                                # a game runs
            w._sync_ribbon()
            self.assertFalse(running())
            w._playing = set()
            w._sync_ribbon()
            self.assertTrue(running())
            w.attract.show()                                      # the screensaver
            self.assertFalse(running())
            w.attract.hide()
            self.assertTrue(running())
            rb.select('library', 'game:alpha')                    # a page over the Ribbon
            self._press(Qt.Key.Key_Return, rb)
            self._pump()
            self.assertTrue(w.theatre.details.isVisible())
            self.assertFalse(running())
            w._escape()
            self._pump()
            self.assertFalse(w.theatre.details.isVisible())
            self.assertTrue(running())
            w._launching['alpha'] = 0.0                           # a game is starting
            w._sync_ribbon()
            self.assertFalse(running())
            w._launching.clear()
            w._sync_ribbon()
            self.assertTrue(running())
            for name in ('search', 'reader', 'quick', 'theatre_sheet', 'theatre_settings'):
                ov = getattr(w.theatre, name, None) or getattr(w, name)
                with mock.patch.object(type(ov), 'isVisible', lambda self, ov=ov: self is ov):
                    w._sync_ribbon()
                    self.assertFalse(running(), name)
                w._sync_ribbon()
                self.assertTrue(running(), name)
            with mock.patch.object(type(w), 'isMinimized', lambda self: True):
                w._sync_ribbon()
                self.assertFalse(running())
                w.changeEvent(QEvent(QEvent.Type.WindowStateChange))   # the hook itself
            w._sync_ribbon()
            self.assertTrue(running())
            with mock.patch.object(w, '_sync_ribbon') as sync:
                w.changeEvent(QEvent(QEvent.Type.WindowStateChange))
                self.assertTrue(sync.called)
                sync.reset_mock()
                w._begin_launch({'slug': 'alpha', 'display_name': 'Alpha'})
                self.assertTrue(sync.called)
                sync.reset_mock()
                w._launch_expired(['alpha'])
                self.assertTrue(sync.called)
            w._launching.clear()
            w._sync_ribbon()
            for look in ({'contrast': True}, {'glow': 0}):        # High contrast, Glow off
                with mock.patch.dict(LOOK, look):
                    w._sync_ribbon()
                    self.assertFalse(running(), look)
                w._sync_ribbon()
                self.assertTrue(running())
            rects = []
            with mock.patch.object(bg, 'update', lambda *a: rects.append(a)):
                bg._live_tick()
            self.assertEqual(rects, [(bg._ribbon_band().toAlignedRect(),)])
            w._apply_controls({'ribbon_motion': 'still'})
            self.assertFalse(running())
            w._apply_controls({'ribbon_motion': 'live'})
            self.assertTrue(running())
            w.set_theatre(False)
            self.assertFalse(running())
            w.set_theatre(True)
            w._sync_ribbon()
            self.assertTrue(running())
        with mock.patch.object(art, 'MOTION', False):             # NEXUS_NO_MOTION
            w._sync_ribbon()
            self.assertFalse(running())
        with mock.patch.object(art, 'MOTION', True):
            w._sync_ribbon()
            self.assertTrue(running())
        with mock.patch.object(w, '_sync_ribbon') as sync:
            w._apply_running(set())
            self.assertTrue(sync.called)
        with mock.patch.object(w, '_sync_ribbon') as sync:
            w.set_appearance(dict(w.prefs.appearance), save=False)
            self.assertTrue(sync.called)
        bg._live_timer.stop()
        w.set_theatre(False)

    def test_moving_is_a_ribbon_background_value(self):
        """w-110: 'Moving' is the third Ribbon background value, in the
        Theatre sheet and the desktop combo, with the power warning."""
        from nexus.gui import theatre_settings as ts
        row = next(r for r in ts.ROWS if r[1] == 'ribbon_motion')
        self.assertEqual([v for v, _ in row[3]], ['input', 'still', 'live'])
        self.assertEqual(dict(row[3])['live'], "Moving")
        self.assertIn("more power", ts.HELP['ribbon_motion'])
        s = self.win.settings
        self.assertEqual([s.ctl_ribbon.itemData(i) for i in range(s.ctl_ribbon.count())],
                         ['input', 'still', 'live'])
        self.assertEqual(s.ctl_ribbon.itemText(2), "Moving")

    def test_layout_switch_keeps_the_focused_game(self):
        w = self.win
        th = w.theatre
        w.activateWindow()
        w.set_theatre(True)
        self._pump()
        th.tabs.set_current('library', emit=True)
        self._pump()
        tile = next(t for t in th.lib_page.findChildren(GameTile) if t.game['slug'] == 'gamma')
        tile.setFocus()
        self._pump()
        w._apply_controls({'theatre_layout': 'ribbon'})
        self._pump()
        self.assertEqual(th.layout_mode, 'ribbon')
        self.assertIs(QApplication.focusWidget(), th.ribbon)
        self.assertEqual(self._where(th.ribbon), ('library', 'game:gamma'))
        self.assertIs(th.pages.currentWidget(), th.ribbon)
        for b in (th.tabs, th.btn_search, th.btn_surprise):
            self.assertTrue(b.isHidden())
        self.assertFalse(th.btn_settings.isHidden())              # the gear stays
        self.assertEqual(w.background.ribbon, 'input')
        th.ribbon.move('up')                                      # Beta, then back to Tabs
        w._apply_controls({'theatre_layout': 'tabs'})
        self._pump()
        f = QApplication.focusWidget()
        self.assertIsInstance(f, GameTile)
        self.assertEqual(f.game['slug'], 'steam-1')
        self.assertEqual(th.tabs.current(), 'library')
        self.assertFalse(th.tabs.isHidden())
        self.assertEqual(w.background.ribbon, 'off')
        w.set_theatre(False)

    def test_d_pad_and_shoulders_move(self):
        w = self.win
        rb = self._ribbon()
        self.assertEqual(self._where(rb), ('home', 'game:alpha'))
        self._press(Qt.Key.Key_Left)                              # the first: stays
        self.assertEqual(rb.state.cat, 'home')
        self._press(Qt.Key.Key_Down)
        self.assertEqual(self._where(rb), ('home', 'game:steam-1'))
        self.assertIs(QApplication.focusWidget(), rb)             # focus never leaves it
        self._press(Qt.Key.Key_Down)                              # the end of Home
        self.assertEqual(self._where(rb)[1], 'game:steam-1')
        self._press(Qt.Key.Key_Right)
        self.assertEqual(self._where(rb), ('library', 'game:alpha'))
        self.assertEqual(w.theatre.tabs.current(), 'library')    # the tabs follow, unseen
        self._press(Qt.Key.Key_Down)
        self._press(Qt.Key.Key_Up)
        self._press(Qt.Key.Key_Down)
        self.assertEqual(self._where(rb), ('library', 'game:steam-1'))
        self._press(Qt.Key.Key_Left)
        self.assertEqual(self._where(rb), ('home', 'game:steam-1'))   # Home remembered
        self._press(Qt.Key.Key_Right)
        self.assertEqual(self._where(rb), ('library', 'game:steam-1'))   # and Library
        keys = [c.key for c in rb.cats]
        rb.select('discover')                                     # (Right in Collections opens a folder)
        for _ in range(len(keys)):
            self._press(Qt.Key.Key_Right)
        self.assertEqual(rb.state.cat, keys[-1])                  # Right stops at the end
        w._pad_action('page-next')                                # RB wraps round
        self.assertEqual(rb.state.cat, 'home')
        w._pad_action('page-prev')                                # LB back
        self.assertEqual(rb.state.cat, keys[-1])
        self.assertEqual(w.hints._shown[1][-2], ('LB RB', 'Category'))
        w.set_theatre(False)

    def test_a_opens_the_game_page_and_b_comes_back_to_the_same_item(self):
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        rb.select('library', 'game:gamma')
        self._press(Qt.Key.Key_Return, rb)
        self._pump()
        self.assertTrue(th.details.isVisible())
        self.assertEqual(th.details.game['slug'], 'gamma')
        self.assertFalse(th.pages.isEnabled())                    # the Ribbon is out of reach
        w._escape()                                               # B, maybe the drawer first
        if th.details.isVisible():
            w._escape()
        self._pump()
        self.assertFalse(th.details.isVisible())
        self.assertIs(QApplication.focusWidget(), rb)
        self.assertEqual(self._where(rb), ('library', 'game:gamma'))
        w.set_theatre(False)

    def test_pin_opens_page_b_back_to_item(self):
        """w-100: a pinned category heads the Library column; A opens its
        page (the crumb names it), B comes back to the same item."""
        w = self.win
        th = w.theatre
        w.prefs.category_pins = ['src:nexus']
        rb = self._ribbon()
        th.pins_changed()
        th._ribbon_flush()
        lib = next(c for c in rb.cats if c.key == 'library')
        self.assertEqual(lib.items[0].key, 'cat:src:nexus')
        rb.select('library', 'cat:src:nexus')
        self._press(Qt.Key.Key_Return, rb)
        self._pump()
        self.assertIs(th.pages.currentWidget(), th.catpage)
        self.assertEqual(th.catpage.cat.key, 'src:nexus')
        self.assertEqual(th.crumb.text(), "Repacks")
        self.assertFalse(th.crumb.isHidden())
        self.assertTrue(th.catpage.isAncestorOf(QApplication.focusWidget()))
        self.assertEqual(th.tabs.current(), 'library')            # mirrored, unseen
        w._escape()                                               # B
        self._pump()
        self.assertIs(th.pages.currentWidget(), rb)
        self.assertIs(QApplication.focusWidget(), rb)
        self.assertEqual(self._where(rb), ('library', 'cat:src:nexus'))
        self.assertTrue(th.crumb.isHidden())
        w.set_theatre(False)

    def test_b_goes_home_and_never_leaves_theatre(self):
        w = self.win
        rb = self._ribbon()
        rb.select('settings')
        w._escape()
        self.assertEqual(rb.state.cat, 'home')
        self.assertTrue(w._theatre)
        for _ in range(3):
            w._escape()
        self._pump()
        self.assertTrue(w._theatre)
        self.assertEqual(rb.state.cat, 'home')
        self.assertIs(w.theatre.pages.currentWidget(), rb)
        w.set_theatre(False)

    def test_y_searches_and_b_comes_back_to_the_item(self):
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        rb.select('library', 'game:gamma')
        w._pad_action('search')
        self._pump()
        self.assertTrue(th.search.isVisible())
        w._escape()
        self._pump()
        self.assertFalse(th.search.isVisible())
        self.assertIs(QApplication.focusWidget(), rb)
        self.assertEqual(self._where(rb), ('library', 'game:gamma'))
        w.set_theatre(False)

    def test_start_opens_the_quick_menu(self):
        w = self.win
        self._ribbon()
        w._pad_action('start')
        self._pump()
        self.assertTrue(w.quick.isVisible())
        w._escape()
        self._pump()
        self.assertFalse(w.quick.isVisible())
        self.assertIs(QApplication.focusWidget(), w.theatre.ribbon)
        w.set_theatre(False)

    def test_a_page_item_opens_its_tab_and_b_returns(self):
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        rb.select('downloads')
        self._press(Qt.Key.Key_Return, rb)
        self._pump()
        self.assertIs(th.pages.currentWidget(), th.downloads_page)
        self.assertEqual(th.tabs.current(), 'downloads')
        self.assertFalse(th.crumb.isHidden())
        self.assertEqual(th.crumb.text(), "Downloads")
        self.assertTrue(th.tabs.isHidden())                       # still no tabs
        self.assertTrue(w.theatre.back())                         # B: back to the Ribbon
        self._pump()
        self.assertIs(th.pages.currentWidget(), rb)
        self.assertTrue(th.crumb.isHidden())
        self.assertIs(QApplication.focusWidget(), rb)
        self.assertEqual(self._where(rb), ('downloads', 'page:downloads'))
        w.set_theatre(False)

    def test_settings_items_step_and_x_opens_the_sheet_on_that_row(self):
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        rb.select('settings', 'set:pad_a')
        self.assertEqual(rb.current().value, 'details')
        self._press(Qt.Key.Key_Return, rb)                        # A steps it
        self._pump()
        self.assertEqual(w.prefs.controls['pad_a'], 'play')
        self.assertTrue(th.a_plays)
        self.assertEqual(self._where(rb), ('settings', 'set:pad_a'))
        self.assertEqual(rb.current().value, 'play')              # the item shows it
        self._press(Qt.Key.Key_Menu, rb)                          # X: the sheet, on that row
        self._pump()
        st = w.theatre_settings
        self.assertTrue(st.isVisible())
        self.assertIs(QApplication.focusWidget(), st.rows_by_key['pad_a'])
        self.assertTrue(st.rows_by_key['ribbon_motion'].isEnabled())
        w._escape()
        self._pump()
        self.assertIs(QApplication.focusWidget(), rb)
        # Layout from the Ribbon's own Settings: back to Tabs, focus on a page.
        rb.select('settings', 'set:theatre_layout')
        self._press(Qt.Key.Key_Return, rb)
        self._pump()
        self.assertEqual(w.prefs.controls['theatre_layout'], 'tabs')
        self.assertEqual(th.layout_mode, 'tabs')
        f = QApplication.focusWidget()
        self.assertTrue(f is not None and th.pages.isAncestorOf(f), f)
        w.set_theatre(False)

    def test_no_ribbon_timer_when_still(self):
        """The background's ribbon moves only when you move, and never in
        still mode, under NEXUS_NO_MOTION, Glow off or High contrast."""
        w = self.win
        bg = w.background
        rb = self._ribbon()
        timer = bg._ribbon_timer
        self.assertFalse(art.MOTION)                              # the tests' NEXUS_NO_MOTION
        self._press(Qt.Key.Key_Right, rb)
        self.assertFalse(timer.isActive())
        self.assertFalse(rb.sliding())
        with mock.patch.object(art, 'MOTION', True):
            self.assertTrue(bg.ribbon_moves())
            bg.ribbon_step(1)
            self.assertTrue(timer.isActive())                     # one glide, then it stops
            bg._glide = (bg._phase, bg._glide[1], 0.0)            # long over
            bg._ribbon_tick()
            self.assertFalse(timer.isActive())
            rb.move('left')
            self.assertTrue(rb.sliding())
            rb._anim.stop()
            w._apply_controls({'ribbon_motion': 'still'})
            self.assertEqual(bg.ribbon, 'still')
            bg.ribbon_step(1)
            self.assertFalse(timer.isActive())
            w._apply_controls({'ribbon_motion': 'input'})
            from nexus.gui.theme import LOOK
            with mock.patch.dict(LOOK, {'contrast': True}):
                bg.ribbon_step(1)
                self.assertFalse(timer.isActive())
            with mock.patch.dict(LOOK, {'glow': 0.0}):
                bg.ribbon_step(1)
                self.assertFalse(timer.isActive())
        w.set_theatre(False)
        self.assertEqual(bg.ribbon, 'off')

    def test_the_ribbon_draws_its_own_focus(self):
        w = self.win
        rb = self._ribbon()
        w.input.set('pad')
        rb.setFocus()
        self._pump()
        self.assertFalse(w.focus_ring.isVisible())
        self.assertEqual(rb.accessibleName(), "Home: Alpha")
        w.set_theatre(False)

    def test_no_transient_windows(self):
        """Switching layouts, opening a page from the Ribbon and back show no
        widget as a window of its own (a real window on Wayland)."""

        class Spy(QObject):
            def __init__(self):
                super().__init__()
                self.seen, self.main = [], None

            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Type.Show and isinstance(obj, QWidget) \
                        and obj.isWindow() and obj is not self.main \
                        and not isinstance(obj, QMenu) and obj.windowType() not in (
                            Qt.WindowType.ToolTip, Qt.WindowType.Popup):
                    self.seen.append(f"{type(obj).__name__}:{obj.objectName()}")
                return False
        spy = Spy()
        app = QApplication.instance()
        app.installEventFilter(spy)
        try:
            spy.main = self.win
            rb = self._ribbon()
            rb.select('discover')
            self._press(Qt.Key.Key_Return, rb)
            self._pump()
            self.win.theatre.back()
            self.win._apply_controls({'theatre_layout': 'tabs'})
            self._pump()
            self.win._apply_controls({'theatre_layout': 'ribbon'})
            self._pump()
            self.win.set_theatre(False)
            self._pump()
        finally:
            app.removeEventFilter(spy)
        self.assertEqual(spy.seen, [])


    def test_x_on_a_game_opens_its_options_and_view_opens_sort_and_filter(self):
        w = self.win
        rb = self._ribbon()
        rb.select('library', 'game:gamma')
        self._press(Qt.Key.Key_Menu, rb)                          # X
        self._pump()
        self.assertTrue(w.theatre_sheet.isVisible())
        self.assertEqual(w.theatre_sheet.title.text(), "Gamma")    # the game's options
        w._escape()
        self._pump()
        self.assertFalse(w.theatre_sheet.isVisible())
        self.assertIs(QApplication.focusWidget(), rb)
        w._pad_action('view')                                     # View on Library
        self._pump()
        self.assertTrue(w.theatre_sheet.isVisible())
        self.assertEqual(w.theatre_sheet.title.text(), "Sort & filter")
        w._pad_action('view')                                     # View again closes it
        self._pump()
        self.assertFalse(w.theatre_sheet.isVisible())
        rb.select('home')
        w._pad_action('view')                                     # not on Home
        self.assertFalse(w.theatre_sheet.isVisible())
        w.set_theatre(False)

    def test_two_quick_steps_of_the_theme_advance_twice(self):
        """A look change waits a moment before it restyles (one restyle for
        a held D-pad); the Ribbon steps on from the waiting value."""
        w = self.win
        rb = self._ribbon()
        themes = list(nx.THEMES)
        start = themes.index(w.prefs.appearance['theme'])
        rb.select('settings', 'set:theme')
        with mock.patch.object(w, '_flush_appearance'):            # the restyle never comes
            self._press(Qt.Key.Key_Return, rb)
            self._press(Qt.Key.Key_Return, rb)
            self.assertEqual(w._ap_pending['theme'], themes[(start + 2) % len(themes)])
            self.assertEqual(rb.current().value, themes[(start + 2) % len(themes)])
            w._ap_pending = None
        w.set_theatre(False)

    def test_switching_with_a_page_open_never_leaves_focus_on_a_hidden_tab(self):
        w = self.win
        th = w.theatre
        w.activateWindow()
        w.set_theatre(True)
        self._pump()
        th.tabs.set_current('downloads', emit=True)
        self._pump()
        th.tabs.item('downloads').setFocus()                      # focus on the tab itself
        self._pump()
        w._apply_controls({'theatre_layout': 'ribbon'})
        self._pump()
        f = QApplication.focusWidget()
        self.assertIsNotNone(f)
        self.assertTrue(f.isVisible(), f)
        self.assertFalse(th.tabs.isAncestorOf(f))
        self.assertIs(th.pages.currentWidget(), th.downloads_page)  # still open, over it
        self.assertTrue(th.downloads_page.isAncestorOf(f) or f is th.downloads_page, f)
        w.set_theatre(False)

    def test_switching_from_desktop_settings_starts_on_the_ribbon(self):
        """Theatre's tabs last on Discover, the layout changed on the
        desktop: Theatre opens on the Ribbon, not on that page."""
        w = self.win
        th = w.theatre
        w.set_theatre(True)
        th.tabs.set_current('discover', emit=True)
        self._pump()
        w.set_theatre(False)
        self._pump()
        w._apply_controls({'theatre_layout': 'ribbon'})
        w.activateWindow()
        w.set_theatre(True)
        self._pump()
        self.assertIs(th.pages.currentWidget(), th.ribbon)
        self.assertIsNone(th._ribbon_sub)
        self.assertTrue(th.crumb.isHidden())
        self.assertEqual(th.ribbon.state.cat, 'home')
        w.set_theatre(False)

    def test_categories_follow_the_tab_gates(self):
        """Discover and Downloads are where the tabs are (sync_tabs: News and
        Game-db, repack features). Runs under the Windows feature set too:
        the model itself has no feature-dependent output."""
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        tabs = [k for k in th.tabs.keys() if not th.tabs.item(k).isHidden()]
        self.assertEqual([c.key for c in rb.cats],
                         tabs[:2] + ['collections'] + tabs[2:] + ['settings'])
        th.sync_tabs(False, False)
        self._pump()
        self.assertEqual([c.key for c in rb.cats], ['home', 'library', 'collections',
                                                    'settings'])
        th.sync_tabs(True, True)
        self._pump()
        self.assertEqual([c.key for c in rb.cats], ['home', 'library', 'collections',
                                                    'discover', 'downloads', 'settings'])
        w.set_theatre(False)

    def test_seven_categories_follow_the_gates(self):
        """Home, Library, Collections, Discover, Downloads, Friends, Settings;
        Friends only while someone plays. The Windows feature set has no
        smart:linux or src:heroic folder."""
        from nexus import features
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        th.sync_tabs(True, True)
        th._friend_entries = [{'appid': 1, 'title': "Beta", 'slug': 'steam-1', 'minutes': 80,
                               'people': [{'name': "Ana"}]}]
        th.refresh_ribbon()
        th._ribbon_flush()
        self.assertEqual([c.key for c in rb.cats], ['home', 'library', 'collections', 'discover',
                                                    'downloads', 'friends', 'settings'])
        self.assertEqual(rb.cats[5].items[0].key, 'friend:1')
        th._friend_entries = []
        th._ribbon_flush()
        th.refresh_ribbon()
        th._ribbon_flush()
        self.assertNotIn('friends', [c.key for c in rb.cats])
        folders = set()

        def walk(items):
            for it in items:
                if it.kind == 'folder':
                    folders.add(it.key)
                    walk(it.children)
        walk(next(c for c in rb.cats if c.key == 'collections').items)
        if not features.has('proton'):
            self.assertNotIn('col:smart:linux', folders)
        if not features.has('heroic'):
            self.assertNotIn('col:src:heroic', folders)
        self.assertIn('group:stores', folders)
        w.set_theatre(False)

    def _into(self, rb, cat, *keys):
        """Select `cat` and open the folders `keys` (A on each)."""
        rb.select(cat, keys[0] if keys else None)
        for i, k in enumerate(keys):
            rb.select(cat, k) if i == 0 else self._focus_key(rb, k)
            self._press(Qt.Key.Key_Return, rb)
            self._pump()

    def _focus_key(self, rb, key):
        from nexus.gui import ribbon_model as rm
        cid, items = rm.column(rb.state, rb.cats)
        rb.state = rm.RibbonState(rb.state.cat, {**rb.state.focus, cid: [
            i for i, it in enumerate(items) if it.key == key][0]}, rb.state.path)

    def test_nested_column_b_goes_up_then_home(self):
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        self._into(rb, 'collections', 'group:stores')
        self.assertEqual(rb.state.path, ('group:stores',))
        self.assertEqual(rb.depth(), 1)
        first = rb.current()
        self.assertEqual(first.kind, 'folder')
        self._press(Qt.Key.Key_Return, rb)                        # A: into the store
        self._pump()
        self.assertEqual(rb.depth(), 2)
        self.assertEqual(rb.current().kind, 'game')
        self.assertIn("›", rb.accessibleName())
        self.assertIs(QApplication.focusWidget(), rb)
        self.assertEqual(w.hints._shown[1][-4], ('b', 'Back'))
        w._escape()                                               # B: up to the stores
        self.assertEqual(rb.depth(), 1)
        self.assertEqual(rb.current().key, first.key)             # on the folder it came from
        self._press(Qt.Key.Key_Left, rb)                          # Left goes up too
        self.assertEqual((rb.state.cat, rb.depth()), ('collections', 0))
        self.assertEqual(rb.current().key, 'group:stores')
        w._escape()                                               # B at the top: Home
        self.assertEqual(rb.state.cat, 'home')
        self._into(rb, 'collections', 'group:stores')
        w._pad_action('page-next')                                # RB closes folders
        self.assertEqual((rb.state.cat, rb.depth()), ('discover', 0))
        w.set_theatre(False)

    def test_download_item_a_runs_its_action_x_options(self):
        w = self.win
        th = w.theatre
        raw = [{'hash': 'h1', 'name': 'Repack', 'title': 'Repack', 'slug': 'repack',
                'tracked': True, 'percent': 40.0, 'size': 4 << 30, 'repack_bytes': 4 << 30,
                'eta': 600, 'rate': 5e6}]
        import time
        from nexus import pipeline
        w.downloads.data = {'torrents': raw, 'pipeline': pipeline.cards(raw, {}, time.time(),
                                                                       set())}
        w._mine_torrents = raw
        rb = self._ribbon()
        w._feed_theatre_downloads()
        self._pump()
        th._ribbon_flush()
        dl = next(c for c in rb.cats if c.key == 'downloads')
        self.assertEqual([it.key for it in dl.items], ['dl:torrent:h1', 'page:downloads'])
        item = dl.items[0]
        self.assertEqual((item.kind, item.kicker, item.primary), ('download', "IN PROGRESS",
                                                                  'dl:pause'))
        self.assertEqual(len(item.track), 4)
        rb.select('downloads', 'dl:torrent:h1')
        rb.grab()                                                 # paints the track, no qFatal
        sent = []
        names = lambda: [b.text() for b in w.theatre_sheet.findChildren(  # noqa: E731
            QPushButton) if b.isVisible()]
        with mock.patch.object(w, '_torrent_send', lambda *a: sent.append(a)):
            self._press(Qt.Key.Key_Return, rb)                    # A: its action
            self.assertEqual(sent, [('pause', ['h1'], False)])
            self._press(Qt.Key.Key_Menu, rb)                      # X: its options
            self._pump()
            self.assertEqual(names(), ["Pause", "Stop…"])
            w._escape()
            self._pump()
        self.assertIs(QApplication.focusWidget(), rb)
        w.downloads.data = {}
        w._mine_torrents = []
        w.set_theatre(False)

    def test_news_item_opens_reader_and_b_returns(self):
        w = self.win
        th = w.theatre
        w.config = {**w.config, 'news': True}
        w.newspage.data = {'items': [{'kind': 'feed', 'title': "Story one", 'source': "Site",
                                      'link': "https://n/1", 'summary': "One."},
                                     {'kind': 'feed', 'title': "Story two", 'source': "Site",
                                      'link': "https://n/2", 'summary': "Two."}]}
        rb = self._ribbon()
        th.sync_tabs(True, False)
        w._feed_theatre_discover()
        self._pump()
        th._ribbon_flush()
        self._into(rb, 'discover', 'shelf:Latest news')
        self.assertEqual(rb.depth(), 1)
        self._focus_key(rb, rb.current().key)
        self._press(Qt.Key.Key_Down, rb)                          # the second story
        self.assertEqual(rb.current().title, "Story two")
        self._press(Qt.Key.Key_Return, rb)
        self._pump()
        self.assertTrue(th.reader.isVisible())
        self.assertEqual(th.reader.item['title'], "Story two")
        w._escape()                                               # B closes the reader
        self._pump()
        self.assertFalse(th.reader.isVisible())
        self.assertIs(QApplication.focusWidget(), rb)
        self.assertEqual((rb.depth(), rb.current().title), (1, "Story two"))
        w.set_theatre(False)

    def test_keyboard_reaches_every_category_without_a_pad(self):
        """Right opens a folder, so the keyboard has PageDown/PageUp (and
        [ ]) for the categories: arrows and PageDown alone reach Settings."""
        w = self.win
        rb = self._ribbon()
        for _ in range(len(rb.cats)):
            self._press(Qt.Key.Key_PageDown, rb)
        self.assertEqual(rb.state.cat, rb.cats[0].key)           # wrapped round
        self._press(Qt.Key.Key_PageUp, rb)
        self.assertEqual(rb.state.cat, 'settings')
        self._press(Qt.Key.Key_PageUp, rb)
        self._press(Qt.Key.Key_BracketRight, rb)
        self.assertEqual(rb.state.cat, 'settings')
        rb.select('home')
        seen = []
        while rb.state.cat != 'settings' and len(seen) < 12:
            self._press(Qt.Key.Key_PageDown, rb)
            seen.append(rb.state.cat)
        self.assertEqual(rb.state.cat, 'settings')
        self._press(Qt.Key.Key_Down, rb)                         # arrows still move items
        w.set_theatre(False)

    def test_ribbon_landing_on_discover_asks_for_the_fetch(self):
        w = self.win
        th = w.theatre
        rb = self._ribbon()
        th.sync_tabs(True, False)
        th._ribbon_flush()
        asked = []
        th.discover_shown.connect(lambda: asked.append(1))
        self._press(Qt.Key.Key_PageDown, rb)                     # Library
        self.assertEqual(asked, [])
        rb.select('collections')
        self._press(Qt.Key.Key_PageDown, rb)                     # Discover
        self.assertEqual(rb.state.cat, 'discover')
        self.assertEqual(asked, [1])
        w.set_theatre(False)

    def test_category_move_plays_category_sound(self):
        w = self.win
        rb = self._ribbon()
        played = []
        with mock.patch.object(w.sounds, 'play', lambda name: played.append(name) or True):
            self._press(Qt.Key.Key_Right, rb)                     # Home > Library
            self._press(Qt.Key.Key_Down, rb)                      # an item
            w._pad_action('page-next')                            # RB
            self._press(Qt.Key.Key_Right, rb)                     # into a folder (Collections)
        self.assertEqual(played, ['category', 'move', 'category', 'move'])
        self.assertEqual(rb.last_move, 'folder')
        w.set_theatre(False)

    def test_a_click_reports_its_real_direction(self):
        w = self.win
        rb = self._ribbon()
        rb.select('library')
        rb.grab()                                                  # a paint: the hit rects
        moved = []
        rb.moved.connect(moved.append)
        from PyQt6.QtTest import QTest
        for want, i, start in ((-1, 0, 'library'), (1, len(rb.cats) - 1, 'downloads')):
            rb.select(start)
            rb.grab()
            r = next(r for r, kind, j in rb._hits if kind == 'cat' and j == i)
            QTest.mouseClick(rb, Qt.MouseButton.LeftButton, pos=r.center().toPoint())
            self.assertEqual(moved[-1], want)
            self.assertEqual(rb.state.index(rb.cats), i)
            rb.grab()
        w.set_theatre(False)

    def test_art_that_does_not_decode_is_asked_for_once(self):
        """A file that is not an image: the Ribbon and its art panel ask
        once, then show the glyph tile and no panel, however often they
        paint."""
        import tempfile
        w = self.win
        bad = Path(tempfile.mkdtemp(prefix='nexus-w108-')) / 'hero.png'
        bad.write_text("not an image")
        for g in w._games:
            if g['slug'] == 'alpha':
                g['wide'] = g['hero'] = str(bad)
        asked = []

        def fail(key, cb=None):
            asked.append(key)
            cb(None)                                               # the decode failed
        with mock.patch.object(art.ART, 'request_key', side_effect=fail):
            rb = self._ribbon()
            w.theatre.set_games(list(w._games))
            self._pump()
            rb.select('home', 'game:alpha')
            for _ in range(2):
                w.theatre.repaint()
                rb.repaint()
                self._pump()
            first = [k for k in asked if k[0] == str(bad)]
            for _ in range(4):                                     # painting on: no new asks
                w.theatre.repaint()
                rb.repaint()
                self._pump()
            again = [k for k in asked if k[0] == str(bad)]
        self.assertTrue(first)
        self.assertEqual(again, first)
        w.set_theatre(False)

    def test_a_slide_repaints_only_what_moves(self):
        w = self.win
        rb = self._ribbon()
        rb.grab()
        bar, col = rb._bar_rect(), rb._col_rect()
        self.assertLess(bar.height(), rb.height() // 2)
        self.assertLess(col.width(), rb.width())
        self.assertGreater(col.width(), 0)
        w.set_theatre(False)


@unittest.skipIf(nx is None, "PyQt6 not available")
class Classic(unittest.TestCase):
    """w-110: the pure pieces of Classic colours."""

    def test_month_hues_are_twelve_accents(self):
        from datetime import datetime
        from nexus.gui import ribbon, theme
        self.assertEqual(len(ribbon.MONTH_ACCENTS), 12)
        self.assertEqual(sorted(ribbon.MONTH_ACCENTS), sorted(theme.ACCENTS))
        noon = [ribbon.classic_color(datetime(2026, m, 15, 15), False).name() for m in range(1, 13)]
        self.assertEqual(len(set(noon)), 12)
        from PyQt6.QtGui import QColor
        for m in range(1, 13):
            want = QColor(theme.ACCENTS[ribbon.MONTH_ACCENTS[m - 1]])
            got = ribbon.classic_color(datetime(2026, m, 15, 15), False)
            if want.saturationF() > 0.15:
                self.assertAlmostEqual(got.hueF(), want.hueF(), delta=0.01, msg=m)

    def test_brightness_follows_the_clock(self):
        from datetime import datetime
        from nexus.gui import ribbon
        vals = [ribbon.classic_color(datetime(2026, 6, 10, h), False) for h in range(24)]
        v = [c.valueF() for c in vals]
        self.assertEqual(v.index(max(v)), 15)
        self.assertGreater(v[15], v[3] + 0.2)
        self.assertGreaterEqual(min(v), 0.61)
        self.assertLessEqual(max(v), 1.0)
        self.assertTrue(all(c.saturationF() <= 0.551 for c in vals))
        light = [ribbon.classic_color(datetime(2026, 6, 10, h), True) for h in range(24)]
        self.assertLess(max(c.valueF() for c in light) - min(c.valueF() for c in light), 0.25)

    def test_marks_three_to_seven_never_all_four(self):
        from nexus.gui import ribbon
        self.assertEqual(ribbon.MARKS, ('ring', 'triangle', 'box', 'plus'))
        for seed in range(300):
            specs = ribbon.mark_specs(seed)
            self.assertTrue(3 <= len(specs) <= 7, seed)
            kinds = {sp['kind'] for sp in specs}
            self.assertTrue(kinds <= set(ribbon.MARKS) and len(kinds) <= 3, seed)
        self.assertEqual(ribbon.mark_specs(5), ribbon.mark_specs(5))

    def test_marks_stay_faint(self):
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QImage, QPainter
        from nexus.gui import ribbon
        for light in (False, True):
            top = 0
            for seed in range(25):
                specs = ribbon.mark_specs(seed)
                for t in (0.0, 0.3, 0.5, 0.8, 1.0):
                    img = QImage(400, 120, QImage.Format.Format_ARGB32)
                    img.fill(Qt.GlobalColor.transparent)
                    p = QPainter(img)
                    ribbon.paint_marks(p, QRectF(0, 0, 400, 120), t, QColor('#ffffff'), specs)
                    p.end()
                    a = max(img.pixelColor(x, y).alpha() for x in range(0, 400, 2)
                            for y in range(0, 120, 2)) if t in (0.0, 0.5) else 0
                    if t == 0.0:
                        self.assertEqual(a, 0, seed)
                    top = max(top, a)
            self.assertGreater(top, 20)                           # something is painted
            self.assertLessEqual(top, int(0.45 * 255) + 2)        # outlines only, under the cap


class Wording(unittest.TestCase):
    """The layout is the Ribbon: no other console's name for it anywhere.
    The pad code may name the hardware it drives (hid-playstation, SDL's
    pad families), so nexus/gamepad.py and nexus/platform/ are left out of
    the brand check."""
    BANNED = re.compile(r'XMB|Xross\s*Media\s*Bar', re.IGNORECASE)
    BRAND = re.compile(r'playstation', re.IGNORECASE)
    OWNER = re.compile(r'sony', re.IGNORECASE)
    HARDWARE = ('gamepad.py', 'platform')

    def test_no_banned_names(self):
        bad = []
        for path in sorted((REPO / 'nexus').rglob('*.py')):
            text = path.read_text(encoding='utf-8')
            rel = path.relative_to(REPO / 'nexus')
            if self.BANNED.search(text):
                bad.append(str(rel))
            if rel.parts[0] not in self.HARDWARE and self.BRAND.search(text):
                bad.append(f"{rel} (brand)")
        self.assertEqual(bad, [])

    def test_the_ribbon_files_name_it_ribbon(self):
        for name in ('ribbon.py', 'ribbon_model.py'):
            text = (REPO / 'nexus' / 'gui' / name).read_text(encoding='utf-8')
            self.assertIn("Ribbon", text)
            self.assertIsNone(self.BRAND.search(text))
            self.assertIsNone(self.OWNER.search(text))

    def test_marks_are_original(self):
        """w-110: the marks are thin original outlines: a plus, not a cross,
        and a box; never the four-shape set in one go."""
        from nexus.gui import ribbon
        self.assertEqual(ribbon.MARKS, ('ring', 'triangle', 'box', 'plus'))
        text = (REPO / 'nexus' / 'gui' / 'ribbon.py').read_text(encoding='utf-8')
        self.assertIsNone(re.search(r'cross|square', text, re.IGNORECASE))


if __name__ == '__main__':
    unittest.main()
