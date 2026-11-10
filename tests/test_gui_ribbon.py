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
    from PyQt6.QtWidgets import QApplication, QMenu, QWidget
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
        self.assertEqual([c.key for c in rb.cats], tabs + ['settings'])
        th.sync_tabs(False, False)
        self._pump()
        self.assertEqual([c.key for c in rb.cats], ['home', 'library', 'settings'])
        th.sync_tabs(True, True)
        self._pump()
        self.assertEqual([c.key for c in rb.cats], ['home', 'library', 'discover', 'downloads',
                                                    'settings'])
        w.set_theatre(False)

    def test_a_click_reports_its_real_direction(self):
        w = self.win
        rb = self._ribbon()
        rb.select('discover')
        rb.grab()                                                  # a paint: the hit rects
        moved = []
        rb.moved.connect(moved.append)
        from PyQt6.QtTest import QTest
        for want, i in ((-1, 0), (1, len(rb.cats) - 1)):
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


class Wording(unittest.TestCase):
    """The layout is the Ribbon: no other console's name for it anywhere.
    The pad code may name the hardware it drives (hid-playstation, SDL's
    pad families), so nexus/gamepad.py and nexus/platform/ are left out of
    the brand check."""
    BANNED = re.compile(r'XMB|Xross\s*Media\s*Bar', re.IGNORECASE)
    BRAND = re.compile(r'playstation', re.IGNORECASE)
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


if __name__ == '__main__':
    unittest.main()
