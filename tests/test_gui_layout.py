"""Offscreen layout checks for the Nexus GUI (nexus/gui.py).

A wrapped QLabel is never narrower than its longest unbreakable token, so one
long launch-options string or path used to make the details panel's page
~1200 px wide inside a ~370 px column: Play rendered blank and the Steam/Heroic
buttons were off-screen. These tests pin that down without a display, a
network or the CLI.
"""

import importlib
import os
import sys
import unittest
from pathlib import Path

# Always offscreen: a desktop session exports QT_QPA_PLATFORM=wayland, and
# setdefault() would then pop real windows up (and let the compositor tile
# them to whatever size it likes).
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['NEXUS_NO_GAMEPAD'] = '1'

REPO = Path(__file__).resolve().parent.parent

try:
    from PyQt6.QtCore import QRect
    from PyQt6.QtWidgets import QApplication, QWidget, QWidgetItem
except ImportError:            # PyQt6 not installed: skip the GUI tests
    QApplication = None

nx = None
if QApplication is not None:
    _app = QApplication.instance() or QApplication([])
    sys.path.insert(0, str(REPO))
    nx = importlib.import_module('nexus.gui')
    _app.setStyleSheet(nx.build_stylesheet())
    # Window tests must never read or rewrite the real Steam to-do list.
    import tempfile
    _tmp = tempfile.mkdtemp(prefix='nexus-test-')
    nx.MainWindow.STEAM_PENDING = Path(_tmp) / 'steam-pending.json'
    # Nor the library cache — and none at all between tests: its folder is
    # a file, so it's never written (a test that wants one patches it).
    (Path(_tmp) / 'no-cache').write_text('')
    nx.MainWindow.LIB_CACHE = Path(_tmp) / 'no-cache' / 'library.json'
    # Nor the user's library prefs: a saved filter chip or sort would change
    # what the window tests see.
    nx.LibraryPrefs.PATH = Path(_tmp) / 'library.json'

LONG_OPTS = ("PROTON_FSR4_UPGRADE=1 MANGOHUD_CONFIG=" + ",".join(
    f"option_{i}=value_{i}" for i in range(60)) + " %command%")
DEEP = "/home/someone/Games/FitGirl/" + "/".join(f"very-long-directory-{i}" for i in range(12))


@unittest.skipIf(nx is None, "PyQt6 not available")
class SoftWrap(unittest.TestCase):
    def test_breaks_after_separators(self):
        self.assertEqual(nx.soft_wrap("a/b,c=d"), "a/​b,​c=​d")
        self.assertEqual(nx.soft_wrap(None), "")

    def test_short_path(self):
        self.assertEqual(nx.short_path("/a/b/c/d/e"), "…/c/d/e")
        self.assertEqual(nx.short_path("/a/b"), "/a/b")
        self.assertEqual(nx.short_path(""), "")


@unittest.skipIf(nx is None, "PyQt6 not available")
class DetailsPanelWidth(unittest.TestCase):
    def setUp(self):
        # No CLI subprocesses from a unit test.
        self._cli_json = nx.cli_json
        nx.cli_json = lambda *a, **k: None
        self.panel = nx.DetailsPanel()
        self.panel.resize(380, 900)
        self.panel.show()

    def tearDown(self):
        nx.cli_json = self._cli_json
        self.panel.deleteLater()

    def _page_min_width(self):
        scroll = self.panel.stack.currentWidget()
        return scroll.widget().minimumSizeHint().width()

    def test_repack_with_long_options_fits(self):
        game = {'slug': 'x', 'display_name': 'X', 'source': 'nexus', 'launcher': '/x',
                'launch_options': LONG_OPTS, 'exe': DEEP + '/game.exe',
                'steam': True, 'heroic': True}
        self.panel.show_games([game])
        QApplication.processEvents()
        self.assertLessEqual(self._page_min_width(), self.panel.maximumWidth())
        # The raw value is still what gets copied.
        self.assertEqual(self.panel._folder, DEEP)

    def test_steam_game_long_install_dir_fits(self):
        game = {'slug': 'y', 'display_name': 'Y', 'source': 'steam', 'launcher': 'steam',
                'install_dir': DEEP}
        self.panel.show_games([game])
        QApplication.processEvents()
        self.assertLessEqual(self._page_min_width(), self.panel.maximumWidth())


_HOST = []


def _box(w, h):
    """A fixed-size layout item. Parented, so it isn't a hidden top-level
    (QWidgetItem reports hidden widgets as empty)."""
    if not _HOST:
        _HOST.append(QWidget())
    widget = QWidget(_HOST[0])
    widget.setFixedSize(w, h)
    return QWidgetItem(widget)


@unittest.skipIf(nx is None, "PyQt6 not available")
class FlowJustify(unittest.TestCase):
    def _layout(self, n, width, justify):
        flow = nx.FlowLayout(margin=0, spacing=10, justify=justify)
        items = [_box(100, 150) for _ in range(n)]
        for it in items:
            flow.addItem(it)
        flow.setGeometry(QRect(0, 0, width, 1000))
        return [it.geometry() for it in items]

    def test_full_rows_reach_both_edges(self):
        g = self._layout(7, 450, True)          # 4 per row, 30 px left over
        self.assertEqual(g[0].left(), 0)
        self.assertEqual(g[3].right(), 449)
        self.assertEqual(g[4].top(), 160)

    def test_last_row_keeps_columns(self):
        g = self._layout(7, 450, True)
        for above, below in zip(g[0:3], g[4:7]):
            self.assertEqual(above.left(), below.left())

    def test_plain_flow_unchanged(self):
        g = self._layout(7, 450, False)
        self.assertEqual([r.left() for r in g[:4]], [0, 110, 220, 330])

    def test_height_for_width(self):
        flow = nx.FlowLayout(margin=5, spacing=10, justify=True)
        for _ in range(3):
            flow.addItem(_box(100, 150))
        self.assertEqual(flow.heightForWidth(1000), 160)       # one row + margins
        self.assertEqual(flow.heightForWidth(230), 5 + 150 + 10 + 150 + 5)
        self.assertEqual(nx.FlowLayout().heightForWidth(100), 0)


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Caching(unittest.TestCase):
    """The speed-ups must not change what's shown."""

    def test_flow_height_follows_new_items(self):
        host = QWidget()
        flow = nx.FlowLayout(host, margin=0, spacing=10)
        for _ in range(3):
            w = QWidget(host)
            w.setFixedSize(100, 50)
            flow.addWidget(w)
        h1 = flow.heightForWidth(220)               # two per row: 2 rows
        self.assertEqual(flow.heightForWidth(220), h1)
        w = QWidget(host)
        w.setFixedSize(100, 50)
        flow.addWidget(w)
        self.assertEqual(flow.heightForWidth(220), h1)   # still 2 rows
        for _ in range(2):
            w = QWidget(host)
            w.setFixedSize(100, 50)
            flow.addWidget(w)
        self.assertGreater(flow.heightForWidth(220), h1)  # a 3rd row: cache dropped
        flow.takeAt(5)
        flow.takeAt(4)
        self.assertEqual(flow.heightForWidth(220), h1)   # back to 4 items, 2 rows
        host.deleteLater()

    def test_library_view_keeps_tiles_across_refreshes(self):
        view = nx.LibraryView()
        games = [{'slug': f'g{i}', 'display_name': f'G{i}', 'source': 'nexus'} for i in range(5)]
        view.set_games(games)
        first = {s: ts[0] for s, ts in view.tiles.items()}
        fresh = [dict(g) for g in reversed(games[1:])]   # a reload: new dicts, one gone
        view.set_games(fresh)
        self.assertNotIn('g0', view.tiles)
        for g in fresh:
            t = view.tiles[g['slug']][0]
            self.assertIs(t, first[g['slug']])           # the same widget…
            self.assertIs(t.game, g)                     # …showing the new data
        view.tile_width = nx.TILE_W + 20                 # a new size: new tiles
        view.set_games(fresh)
        self.assertIsNot(view.tiles['g1'][0], first['g1'])
        view.deleteLater()

    def test_art_cache_is_bounded_and_async(self):
        import tempfile
        from PyQt6.QtGui import QImage, QColor
        cache = nx.ArtCache()
        cache.MAX_PIX = 3
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(5):
                img = QImage(60, 90, QImage.Format.Format_RGB32)
                img.fill(QColor(40 * i, 0, 0))
                p = f"{d}/{i}.png"
                img.save(p)
                paths.append(p)
            for p in paths:
                self.assertIsNotNone(cache.pixmap(p, 30, 45))
            self.assertEqual(len(cache.pix), 3)
            self.assertIsNone(cache.peek(paths[0], 30, 45))    # oldest evicted
            from PyQt6.QtTest import QTest
            got = []
            cache.request(paths[0], 30, 45, got.append, grey=True)
            cache.request(paths[0], 30, 45, got.append, grey=True)   # one decode, two tiles
            self.assertEqual(got, [])                         # not inline…
            for _ in range(200):
                if len(got) == 2:
                    break
                QTest.qWait(5)
            self.assertEqual(len(got), 2)                     # …but soon
            self.assertIs(got[0], got[1])
            self.assertTrue(got[0].toImage().isGrayscale())
            self.assertIs(cache.peek(paths[0], 30, 45, grey=True), got[0])

    def test_thumbnails_for_big_sources(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from PyQt6.QtGui import QImage, QColor
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(nx, 'THUMB_MIN_BYTES', 1):
            src = f"{d}/big.png"
            img = QImage(300, 450, QImage.Format.Format_RGB32)
            img.fill(QColor(10, 200, 30))
            img.save(src)
            thumbs = Path(d) / 'thumbs'
            mtime = Path(src).stat().st_mtime
            first = nx.thumb_image(src, mtime, 60, 90, thumbs=thumbs)
            self.assertEqual((first.width(), first.height()), (60, 90))
            self.assertEqual(len(list(thumbs.glob('*.png'))), 1)
            with mock.patch.object(nx, 'cover_image', side_effect=AssertionError("decoded")):
                again = nx.thumb_image(src, mtime, 60, 90, thumbs=thumbs)
            self.assertEqual(again.pixelColor(30, 45), first.pixelColor(30, 45))
            self.assertIsNone(nx.thumb_image(f"{d}/missing.png", 0, 60, 90, thumbs=thumbs))


@unittest.skipIf(nx is None, "PyQt6 not available")
class ProtongenDefault(unittest.TestCase):
    def setUp(self):
        self._cli_json = nx.cli_json
        nx.cli_json = lambda *a, **k: None
        self.panel = nx.DetailsPanel()
        self.panel.show_games([{'slug': 'x', 'display_name': 'X', 'source': 'nexus',
                                'launcher': '/x'}])

    def tearDown(self):
        nx.cli_json = self._cli_json
        self.panel.deleteLater()

    def test_button_follows_the_default_profile(self):
        base = {'protons': [], 'protongen_installed': True}
        self.panel._apply_info({**base, 'protongen_default': None}, None)
        self.assertFalse(self.panel.btn_pg_default.isEnabled())
        pd = {'launch_options': 'mangohud %command%', 'proton': 'p', 'applied': False}
        self.panel._apply_info({**base, 'protongen_default': pd}, None)
        self.assertTrue(self.panel.btn_pg_default.isEnabled())
        self.panel._apply_info({**base, 'protongen_default': {**pd, 'applied': True}}, None)
        self.assertFalse(self.panel.btn_pg_default.isEnabled())

    def test_menu_offers_it_for_repacks_on_the_couch_too(self):
        repack = {'slug': 'x', 'source': 'nexus', 'launcher': '/x', 'exe': '/x.exe'}
        for theatre in (False, True):
            keys = [a.key for a in nx.game_actions(repack, theatre=theatre)]
            self.assertIn('pg-default', keys)
        steam = {'slug': 's', 'source': 'steam', 'appid': 1, 'launcher': 'steam://'}
        self.assertNotIn('pg-default', [a.key for a in nx.game_actions(steam)])
