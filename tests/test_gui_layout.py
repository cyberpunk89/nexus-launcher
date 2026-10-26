"""Offscreen layout checks for the Nexus GUI (the nexus/gui package).

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
from unittest import mock

# Always offscreen: a desktop session exports QT_QPA_PLATFORM=wayland, and
# setdefault() would then pop real windows up (and let the compositor tile
# them to whatever size it likes).
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['NEXUS_NO_GAMEPAD'] = '1'

REPO = Path(__file__).resolve().parent.parent

try:
    from PyQt6.QtCore import QPoint, QRect
    from PyQt6.QtWidgets import QApplication, QWidget, QWidgetItem
except ImportError:            # PyQt6 not installed: skip the GUI tests
    QApplication = None

sys.path.insert(0, str(REPO))
from tests.patching import linux_features, patch_gui, set_gui  # noqa: E402,F401  (the GUI tests import them from here too)

nx = None
if QApplication is not None:
    _app = QApplication.instance() or QApplication([])
    import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
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
    _CliQuery = nx.CliQuery         # the real one: tests swap the name


def stop_leftovers(jobs) -> None:
    """Kill what a GUI test left running in JobManager `jobs` and in any
    background CLI query (cli_json), then forget it. Left alone, its process
    outlives the test's window and calls back into deleted widgets a test or
    two later: a native crash that takes the whole Windows CI run down."""
    from PyQt6.QtCore import QProcess
    for j in jobs.jobs:
        if j.proc is not None and j.proc.state() != QProcess.ProcessState.NotRunning:
            j.proc.kill()
            j.proc.waitForFinished(2000)
    jobs.jobs.clear()
    for q in list(_CliQuery._live):
        q._done = True
        _CliQuery._live.discard(q)
        if q.proc.state() != QProcess.ProcessState.NotRunning:
            q.proc.kill()
            q.proc.waitForFinished(2000)

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
        set_gui('cli_json', lambda *a, **k: None)
        self.panel = nx.DetailsPanel()
        self.panel.resize(380, 900)
        self.panel.show()

    def tearDown(self):
        set_gui('cli_json', self._cli_json)
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
        # The raw value is still what gets copied (in the OS's own separators).
        self.assertEqual(self.panel._folder, str(Path(DEEP)))

    def test_steam_game_long_install_dir_fits(self):
        game = {'slug': 'y', 'display_name': 'Y', 'source': 'steam', 'launcher': 'steam',
                'install_dir': DEEP}
        self.panel.show_games([game])
        QApplication.processEvents()
        self.assertLessEqual(self._page_min_width(), self.panel.maximumWidth())


@unittest.skipIf(nx is None, "PyQt6 not available")
class HeroBlend(unittest.TestCase):
    """The game page's hero art runs edge to edge, fades into the panel and
    tints the page with its colour (w-21)."""

    def setUp(self):
        self._cli_json = nx.cli_json
        set_gui('cli_json', lambda *a, **k: None)
        self.panel = nx.DetailsPanel()
        self.panel.resize(380, 900)
        self.panel.show()
        self.panel.show_games([{'slug': 'x', 'display_name': 'X', 'source': 'nexus',
                                'launcher': '/x'}])
        QApplication.processEvents()

    def tearDown(self):
        set_gui('cli_json', self._cli_json)
        self.panel.deleteLater()

    def test_hero_spans_the_page_and_fades_out(self):
        from PyQt6.QtGui import QColor, QPixmap
        hero, page = self.panel.hero, self.panel.page
        self.assertEqual(hero.mapTo(page, QPoint(0, 0)), QPoint(0, 0))
        self.assertEqual(hero.width(), page.width())
        art = QPixmap(64, 32)
        art.fill(QColor('#3060c0'))
        hero._set('art', art)
        img = hero._faded().toImage()
        self.assertEqual(img.pixelColor(img.width() // 2, 2).alpha(), 255)
        self.assertLess(img.pixelColor(img.width() // 2, img.height() - 1).alpha(), 10)

    def test_page_takes_the_arts_colour_and_follows_the_glow_settings(self):
        from PyQt6.QtGui import QColor, QPixmap
        from nexus.gui import details, theme
        art = QPixmap(8, 8)
        art.fill(QColor('#c03020'))
        self.panel.hero._set('art', art)
        tint = self.panel.page._tint
        self.assertEqual(tint.name(), '#c03020')
        self.assertLessEqual(details.wash_color(tint).valueF(), 0.56)

        def painted():
            img = self.panel.page.grab().toImage()
            return img.pixelColor(img.width() // 2, 230)
        base = painted()
        with mock.patch.dict(theme.LOOK, {'art_tint': False}):
            self.assertNotEqual(painted(), base)        # the wash is gone
        self.panel.hero._set('art', None)
        self.assertIsNone(self.panel.page._tint)


@unittest.skipIf(nx is None, "PyQt6 not available")
class HiDpiArt(unittest.TestCase):
    """Art is decoded at the display's scale and drawn at its logical size."""

    def test_cover_is_sharp_and_logical_sized(self):
        import tempfile
        from PyQt6.QtGui import QImage, QColor
        dpr = _app.devicePixelRatio()
        with tempfile.TemporaryDirectory() as d:
            src = f"{d}/c.png"
            img = QImage(600, 900, QImage.Format.Format_RGB32)
            img.fill(QColor(10, 20, 30))
            img.save(src)
            for pm in (nx.ArtCache().pixmap(src, 100, 150, radius=8),
                       nx.cover_pixmap(src, 100, 150)):
                self.assertEqual(pm.devicePixelRatio(), dpr)
                self.assertEqual(pm.width(), round(100 * dpr))
                size = pm.deviceIndependentSize()
                self.assertAlmostEqual(size.width(), round(100 * dpr) / dpr, delta=0.01)
                self.assertAlmostEqual(size.height(), round(150 * dpr) / dpr, delta=0.01)
        ph = nx.placeholder_pixmap('Some Game', 100, 150)
        self.assertEqual(ph.devicePixelRatio(), dpr)


@unittest.skipIf(nx is None, "PyQt6 not available")
@unittest.skipIf(os.environ.get('QT_SCALE_FACTOR'), "already the scaled run")
class DisplayScaling(unittest.TestCase):
    """The layout and art tests again at 125% and 150% display scaling. Qt
    reads QT_SCALE_FACTOR once, at QApplication(), so each scale is its own
    process."""

    TESTS = ['tests.test_gui_layout.SoftWrap', 'tests.test_gui_layout.DetailsPanelWidth',
             'tests.test_gui_layout.HiDpiArt']

    def _run(self, scale):
        import subprocess
        env = {**os.environ, 'QT_SCALE_FACTOR': scale}
        out = subprocess.run([sys.executable, '-m', 'unittest', *self.TESTS], cwd=REPO,
                             env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-3000:])
        self.assertNotIn('skipped', out.stderr.splitlines()[-1])

    def test_at_125_percent(self):
        self._run('1.25')

    def test_at_150_percent(self):
        self._run('1.5')


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
                patch_gui('THUMB_MIN_BYTES', 1):
            src = f"{d}/big.png"
            img = QImage(300, 450, QImage.Format.Format_RGB32)
            img.fill(QColor(10, 200, 30))
            img.save(src)
            thumbs = Path(d) / 'thumbs'
            mtime = Path(src).stat().st_mtime
            first = nx.thumb_image(src, mtime, 60, 90, thumbs=thumbs)
            self.assertEqual((first.width(), first.height()), (60, 90))
            self.assertEqual(len(list(thumbs.glob('*.png'))), 1)
            with patch_gui('cover_image', side_effect=AssertionError("decoded")):
                again = nx.thumb_image(src, mtime, 60, 90, thumbs=thumbs)
            self.assertEqual(again.pixelColor(30, 45), first.pixelColor(30, 45))
            self.assertIsNone(nx.thumb_image(f"{d}/missing.png", 0, 60, 90, thumbs=thumbs))


@unittest.skipIf(nx is None, "PyQt6 not available")
class ProtongenDefault(unittest.TestCase):
    def setUp(self):
        self._cli_json = nx.cli_json
        set_gui('cli_json', lambda *a, **k: None)
        self.panel = nx.DetailsPanel()
        self.panel.show_games([{'slug': 'x', 'display_name': 'X', 'source': 'nexus',
                                'launcher': '/x'}])

    def tearDown(self):
        set_gui('cli_json', self._cli_json)
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

    @linux_features
    def test_menu_offers_it_for_repacks_on_the_couch_too(self):
        repack = {'slug': 'x', 'source': 'nexus', 'launcher': '/x', 'exe': '/x.exe'}
        for theatre in (False, True):
            keys = [a.key for a in nx.game_actions(repack, theatre=theatre)]
            self.assertIn('pg-default', keys)
        steam = {'slug': 's', 'source': 'steam', 'appid': 1, 'launcher': 'steam://'}
        self.assertNotIn('pg-default', [a.key for a in nx.game_actions(steam)])
