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

sys.path.insert(0, str(REPO))
# Before the QApplication: Qt reads fonts and settings from HOME when it
# starts, and CI always has a clean one. With the real HOME first, a run
# here could pass on timing CI never sees (0.5.2's focus flakes).
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)

try:
    from PyQt6.QtCore import QPoint, QRect, Qt
    from PyQt6.QtWidgets import QApplication, QWidget, QWidgetItem
except ImportError:            # PyQt6 not installed: skip the GUI tests
    QApplication = None

from tests.patching import linux_features, patch_gui, set_gui  # noqa: E402,F401  (the GUI tests import them from here too)

nx = None
if QApplication is not None:
    _app = QApplication.instance() or QApplication([])
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

    def test_widgets_are_made_when_first_needed(self):
        p = nx.DetailsPanel()
        self.addCleanup(p.deleteLater)
        self.assertFalse(p._built)
        self.assertEqual(p.findChildren(QWidget), [])
        self.assertEqual(p.game, {})                       # state the window reads early
        self.assertFalse(hasattr(p, 'no_such_widget'))
        self.assertTrue(p._built)                          # asking for any name builds it
        self.assertIsNotNone(p.btn_play)
        q = nx.DetailsPanel()
        self.addCleanup(q.deleteLater)
        q.open_section('files')                            # before any game, as a menu may
        self.assertTrue(q._built)

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

    def test_hero_art_is_sized_in_screen_pixels(self):
        """The game page's hero and the Theatre art are decoded and scaled
        to the pixels they cover: sharp at 125 %, and no bigger (w-46)."""
        import tempfile
        from PyQt6.QtGui import QImage, QColor, QPixmap
        dpr = _app.devicePixelRatio()
        hb = nx.HeroBanner()
        self.addCleanup(hb.deleteLater)
        hb.resize(400, 200)
        frame = hb._scaled('art', QPixmap(1600, 800), 400, 200,
                           Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        self.assertEqual(frame.width(), round(400 * dpr))
        self.assertAlmostEqual(frame.deviceIndependentSize().width(), 400, delta=1)
        self.assertEqual(hb._box('art'), (round(nx.HeroBanner.DECODE_W * dpr),
                                          round(200 * dpr)))
        with tempfile.TemporaryDirectory() as d:
            src = f"{d}/hero.png"
            img = QImage(8000, 2000, QImage.Format.Format_RGB32)
            img.fill(QColor(10, 20, 30))
            img.save(src)
            host = QWidget()
            self.addCleanup(host.deleteLater)
            pm = nx.screen_pixmap(src, host)
            screen = _app.primaryScreen().size()
            self.assertLess(pm.width(), 8000)                  # not decoded at full size…
            self.assertGreaterEqual(pm.width(), round(screen.width() * dpr))   # …but covers
            self.assertGreaterEqual(pm.height(), round(screen.height() * dpr))
            self.assertIsNone(nx.screen_pixmap(None, host))


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


@unittest.skipIf(nx is None, "PyQt6 not available")
class GameContextBlock(unittest.TestCase):
    """The game page's context block, now at the top of Overview (w-40,
    w-54): built from the facts index the CLI writes, each row only when its
    fact exists, a plain line with no facts, tags that ask for a Library
    filter, Play at the same height for every game."""

    INDEX = {'games': {
        'full': {'developers': ['FromSoftware'], 'publishers': ['Bandai Namco', 'FromSoftware'],
                 'year': 2022, 'genres': ['RPG', 'Action', 'Adventure', 'Indie'],
                 'modes': ['single', 'coop'], 'description': 'Rise, Tarnished. ' * 40,
                 'length': {'main': 50, 'complete': 133, 'source': 'hltb'},
                 'status': 'released'},
        'part': {'developers': ['Larian Studios'], 'publishers': ['Larian Studios'],
                 'year': 2023},
        'ea': {'developers': ['Mega Crit'], 'genres': ['Strategy'], 'status': 'early_access'},
    }}

    def setUp(self):
        import json
        from nexus import facts
        from nexus.gui import details
        self.details = details
        self._cli_json = nx.cli_json
        set_gui('cli_json', lambda *a, **k: None)
        self.path = facts.index_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({'v': facts.INDEX_V, **self.INDEX}))
        details._FACTS.update(mtime=None, games={})
        self.panel = nx.DetailsPanel()
        self.panel.resize(340, 900)
        self.panel.show()

    def tearDown(self):
        set_gui('cli_json', self._cli_json)
        self.path.unlink(missing_ok=True)
        self.details._FACTS.update(mtime=None, games={})
        self.panel.deleteLater()

    def _show(self, slug, **extra):
        self.panel.show_games([{'slug': slug, 'display_name': slug, 'source': 'steam',
                                'installed': True, **extra}])
        # The panel is built on its first game (w-33), after the window is
        # shown: on Windows its layout settles one event-loop pass later.
        QApplication.processEvents()
        QApplication.processEvents()
        return self.panel.context

    def _chips(self):
        return [self.panel.chips.itemAt(i).widget().text()
                for i in range(self.panel.chips.count())]

    def test_full_facts(self):
        ctx = self._show('full', playtime_minutes=12 * 60)
        self.assertTrue(ctx.isVisible())
        self.assertEqual(ctx.credits.text(), "FromSoftware · Bandai Namco · 2022")
        self.assertEqual([b.text() for b in ctx.tag_btns], ['RPG', 'Action', 'Adventure', 'Co-op'])
        self.assertTrue(ctx.desc.isVisible() and ctx.more.isVisible())
        self.assertLess(len(ctx.desc.text()), len(ctx.desc.full))     # clamped to 5 lines
        self.assertLessEqual(ctx.desc.height(), ctx.desc.fontMetrics().lineSpacing() * 5 + 6)
        ctx.more.click()                                                # Read more: all of it
        self.assertEqual(ctx.desc.text(), ctx.desc.full)
        self.assertEqual(ctx.more.text(), "Show less")
        ctx.more.click()
        self.assertLess(len(ctx.desc.text()), len(ctx.desc.full))
        # The play length moved to the progress strip and At a glance (w-54).
        p = self.panel
        self.assertTrue(p.meter_story.isVisible())
        self.assertEqual(p.meter_story.value.text(), "24 % of 50 h")
        self.assertEqual(p.meter_story.bar.value(), 240)
        self.assertIn('Length', p.glance_keys)
        self.assertLessEqual(p.stack.currentWidget().widget().minimumSizeHint().width(),
                             p.maximumWidth())

    def test_tags_ask_for_a_library_filter(self):
        got = []
        self.panel.filter_library.connect(got.append)
        ctx = self._show('full')
        ctx.tag_btns[0].click()
        ctx.tag_btns[-1].click()
        self.assertEqual(got, ['genre:RPG', 'smart:coop'])

    def test_partial_and_played_past_the_end(self):
        ctx = self._show('part')
        self.assertEqual(ctx.credits.text(), "Larian Studios · 2023")   # publisher = developer
        for w in (ctx.tags_host, ctx.desc, ctx.more):
            self.assertFalse(w.isVisible())
        p = self.panel
        self.assertFalse(p.progress.isVisible())                # no length, no achievements
        length = {'main': 10, 'source': 'igdb', 'count': 3}
        _t, note, frac, tip = self.details.GameContext.length_line(length, 0)
        self.assertIsNone(frac)                                 # no play time, no bar
        self.assertIn("IGDB, 3 submissions", tip)
        _t, note, frac, _tip = self.details.GameContext.length_line(length, 12 * 60)
        self.assertEqual((note, frac), ("past the main story", 1.0))

    def test_no_facts_says_so_and_play_stays_put(self):
        ctx = self._show('nothing-known')
        self.assertFalse(ctx.isVisible())
        self.assertTrue(self.panel.bare_note.isVisible())
        y_play = self.panel.btn_play.mapTo(self.panel.page, QPoint(0, 0)).y()
        self._show('full', playtime_minutes=60)
        self.assertFalse(self.panel.bare_note.isVisible())
        self.assertEqual(self.panel.btn_play.mapTo(self.panel.page, QPoint(0, 0)).y(), y_play)
        self.assertEqual(y_play, self.panel.HERO_H + self.details.SP['1'])  # right under the hero

    def test_release_status_chip(self):
        self._show('ea')
        self.assertIn("Early access", self._chips())
        self._show('full')
        self.assertFalse(any('access' in c or 'Unreleased' in c for c in self._chips()))
        self.assertEqual(self.details.status_pill({'status': 'upcoming', 'releaseText': 'Q2 2027'}),
                         ("Unreleased · Q2 2027", 'muted'))

    def test_store_info_fills_in_for_a_game_the_index_lacks(self):
        ctx = self._show('steam-only', appid='1')
        self.assertFalse(ctx.isVisible())
        self.panel._fill_about({'store': {'blurb': 'A game.', 'developers': ['Dev'],
                                          'genres': ['RPG'], 'release': '3 Mar, 2021'},
                                'reviews': {'desc': 'Very Positive', 'percent': 91,
                                            'total': 12345}})
        QApplication.processEvents()
        self.assertTrue(ctx.isVisible())
        self.assertEqual(ctx.credits.text(), "Dev · 2021")
        self.assertEqual(ctx.desc.full, "A game.")
        self.assertEqual(self.panel.glance_keys[0], "Reviews")
        self.assertFalse(self.panel.bare_note.isVisible())


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

    def _big_grid(self, n=3000, size=(900, 700)):
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QVBoxLayout
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(*size)
        view = nx.LibraryView()
        QVBoxLayout(host).addWidget(view)
        host.show()
        games = [{'slug': f'g{i}', 'display_name': f'G{i}', 'source': 'steam',
                  'installed': True} for i in range(n)]
        view.set_games(games)
        QTest.qWait(20)
        return host, view, games

    @staticmethod
    def _shown(view):
        return [t for t in view.findChildren(nx.GameTile) if not t.isHidden()]

    def test_library_grid_builds_only_the_rows_near_the_view(self):
        """w-50: 3,000 games make the tiles of about two screens, not 3,000,
        in the same places FlowLayout gave them."""
        host, view, games = self._big_grid()
        cols = view.flow_host.columns(view.flow_host.width())
        self.assertGreater(cols, 1)
        built = self._shown(view)
        self.assertLess(len(built), 8 * cols)
        self.assertLess(len(view.findChildren(nx.GameTile)), 12 * cols)   # spares included
        self.assertEqual(len(view.games), 3000)
        # The canvas is as tall as every row; positions match FlowLayout's.
        ref_host = QWidget()
        self.addCleanup(ref_host.deleteLater)
        flow = nx.FlowLayout(ref_host, margin=nx.SP['5'], spacing=nx.SP['4'], justify=True)
        cell = view.flow_host.cell
        for _ in range(3 * cols + 1):
            w = QWidget(ref_host)
            w.setFixedSize(cell)
            flow.addWidget(w)
        width = view.flow_host.width()
        ref_host.resize(width, flow.heightForWidth(width))
        flow.setGeometry(QRect(0, 0, width, flow.heightForWidth(width)))
        for i in range(3 * cols):
            self.assertEqual(view.tiles[f'g{i}'][0].pos(), flow.itemAt(i).geometry().topLeft())
        rows = -(-3000 // cols)
        self.assertEqual(view.flow_host.height(),
                         2 * nx.SP['5'] + rows * cell.height() + (rows - 1) * nx.SP['4'])
        # Scrolled to the end: the last games have tiles, the first don't.
        bar = view.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.assertIn('g2999', view.tiles)
        self.assertNotIn('g0', view.tiles)
        self.assertIs(view.tiles['g2999'][0].game, games[2999])
        self.assertLess(len(self._shown(view)), 8 * cols)
        self.assertLess(len(view.findChildren(nx.GameTile)), 12 * cols)

    def test_library_grid_keeps_focus_and_selection_while_scrolling(self):
        host, view, _games = self._big_grid()
        host.activateWindow()
        tile = view.tiles['g1'][0]
        tile.setFocus()
        view.mark_selected({'g1', 'g2500'})
        bar = view.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        bar.setValue(bar.maximum())
        # The focused tile stays put (with its neighbours, for the arrow keys).
        self.assertIs(view.tiles['g1'][0], tile)
        self.assertTrue(tile.hasFocus())
        self.assertEqual(tile.game['slug'], 'g1')
        self.assertFalse(tile.isHidden())
        self.assertIn('g0', view.tiles)
        # A game scrolled into view gets its selection from mark_selected.
        view.reveal('g2500')
        self.assertTrue(view.tiles['g2500'][0]._selected)
        self.assertFalse([t for s, ts in view.tiles.items() for t in ts
                          if t._selected != (s in ('g1', 'g2500'))])
        bar.setValue(0)
        self.assertIs(view.tiles['g1'][0], tile)
        # A reload keeps the focused tile on its game, wherever that moved.
        fresh = [{'slug': f'g{i}', 'display_name': f'G{i}', 'source': 'steam',
                  'installed': True} for i in reversed(range(3000))]
        view.set_games(fresh)
        self.assertIs(view.tiles['g1'][0], tile)
        self.assertIs(tile.game, fresh[2998])
        self.assertTrue(tile.hasFocus())

    def test_tile_for_builds_a_tile_far_down(self):
        host, view, _games = self._big_grid()
        self.assertNotIn('g2000', view.tiles)
        t = view.tile_for('g2000')
        self.assertIs(view.tiles['g2000'][0], t)
        self.assertIs(view.tile_for(), view.tiles['g0'][0])   # the first game's
        self.assertIsNone(view.tile_for('nope'))
        view.reveal('g2000')
        vp = view.viewport()
        top = t.mapTo(vp, QPoint(0, 0)).y()
        self.assertTrue(0 <= top and top + t.height() <= vp.height())

    def test_arrow_keys_cross_rows_that_had_no_tiles(self):
        """Down from the last built row: the next row is always built (one
        row over the edge), the focus moves there, the view follows and
        builds the row after. Up comes back the same way."""
        from PyQt6.QtTest import QTest
        from nexus.gui.input import SpatialNav
        host, view, _games = self._big_grid(n=600)
        nav = SpatialNav(host)
        _app.installEventFilter(nav)
        self.addCleanup(_app.removeEventFilter, nav)
        host.activateWindow()
        cols = view.flow_host.columns(view.flow_host.width())
        view.tiles['g1'][0].setFocus()
        for _ in range(40):
            QTest.keyClick(_app.focusWidget(), Qt.Key.Key_Down)
        f = _app.focusWidget()
        self.assertIsInstance(f, nx.GameTile)
        self.assertEqual(f.game['slug'], f'g{1 + 40 * cols}')
        vp = view.viewport()
        top = f.mapTo(vp, QPoint(0, 0)).y()
        self.assertTrue(0 <= top and top + f.height() <= vp.height())
        self.assertLess(len(self._shown(view)), 10 * cols)
        QTest.keyClick(f, Qt.Key.Key_Right)
        self.assertEqual(_app.focusWidget().game['slug'], f'g{2 + 40 * cols}')
        for _ in range(40):
            QTest.keyClick(_app.focusWidget(), Qt.Key.Key_Up)
        self.assertEqual(_app.focusWidget().game['slug'], 'g2')

    def test_big_library_stays_within_the_art_budget(self):
        """3,000 games used to cost about 1 GB: every tile kept its own
        decoded pixmap, so the cache's cap capped nothing (w-46). Tiles now
        keep a cache key; the art lives in ART under its byte budget, is
        decoded ahead only while that fits, and a tile on screen asks for
        its own when it paints."""
        import shutil
        import tempfile
        from PyQt6.QtGui import QColor, QImage, QPixmap
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QVBoxLayout
        dpr = _app.devicePixelRatio()
        cache = nx.ArtCache()
        one = round(100 * dpr) * round(150 * dpr) * 4      # a 100 px tile's art
        cache.budget = 20 * one

        def settle():
            for _ in range(500):
                if not cache.queue:
                    break
                QTest.qWait(10)
            QApplication.processEvents()

        def centre(tile):
            img = tile.grab().toImage()
            return img.pixelColor(img.width() // 2, img.height() // 2).name()

        with tempfile.TemporaryDirectory() as d, patch_gui('ART', cache):
            colours = ['#c82828', '#28c828', '#2828c8']
            srcs = []
            for i, c in enumerate(colours):
                img = QImage(120, 180, QImage.Format.Format_RGB32)
                img.fill(QColor(c))
                srcs.append(f"{d}/src{i}.png")
                img.save(srcs[-1])
            games = []
            for i in range(600):
                p = f"{d}/g{i}.png"
                shutil.copyfile(srcs[i % 3], p)
                games.append({'slug': f'g{i}', 'display_name': f'Game {i}', 'source': 'steam',
                              'installed': True, 'cover': p})
            host = QWidget()
            self.addCleanup(host.deleteLater)
            host.resize(700, 500)
            view = nx.LibraryView()
            view.tile_width = 100
            QVBoxLayout(host).addWidget(view)
            host.show()
            view.set_games(games)
            settle()
            self.assertLessEqual(cache.used, cache.budget)
            self.assertLess(len(cache.pix), 30)                # not 600
            tiles = [t for ts in view.tiles.values() for t in ts]
            self.assertLess(len(tiles), 60)                    # the rows near the view (w-50)
            for t in tiles:                                    # nothing outside the budget
                self.assertFalse([k for k, v in vars(t).items() if isinstance(v, QPixmap)])
            self.assertEqual(centre(view.tiles['g0'][0]), colours[0])
            # The bottom of the grid was never decoded ahead: it decodes
            # when it comes on screen, pushing out the oldest art.
            bar = view.verticalScrollBar()
            bar.setValue(bar.maximum())
            last = view.tiles['g599'][0]
            centre(last)                                       # painted: asks for it
            settle()
            self.assertEqual(centre(last), colours[599 % 3])
            self.assertLessEqual(cache.used, cache.budget)
            view.deleteLater()

    def test_placeholders_are_shared_and_capped(self):
        a = nx.placeholder_pixmap('Alpha Beta', 100, 150)
        self.assertIs(nx.placeholder_pixmap('Another Book', 100, 150), a)   # same initials
        self.assertIsNot(nx.placeholder_pixmap('Gamma', 100, 150), a)
        with patch_gui('PLACEHOLDER_BYTES', 5 * nx.pixmap_bytes(a)):
            for i in range(40):
                nx.placeholder_pixmap(f"Z{i}", 100, 150)
            used = sum(nx.pixmap_bytes(p) for p in nx._PLACEHOLDERS.values())
            self.assertLessEqual(used, 5 * nx.pixmap_bytes(a))
            self.assertIn(nx.placeholder_pixmap('Z39', 100, 150), nx._PLACEHOLDERS.values())

    def test_art_cache_budget_evicts_oldest_first(self):
        import tempfile
        from PyQt6.QtGui import QImage, QColor
        cache = nx.ArtCache()
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(6):
                img = QImage(60, 90, QImage.Format.Format_RGB32)
                img.fill(QColor(40 * i, 0, 0))
                p = f"{d}/{i}.png"
                img.save(p)
                paths.append(p)
            one = nx.pixmap_bytes(cache.pixmap(paths[0], 30, 45))
            self.assertGreater(one, 0)
            cache.pix.clear()
            cache.used = 0
            cache.budget = 3 * one                       # room for three
            for p in paths[:5]:
                self.assertIsNotNone(cache.pixmap(p, 30, 45))
            self.assertEqual(len(cache.pix), 3)
            self.assertEqual(cache.used, 3 * one)
            self.assertIsNone(cache.peek(paths[0], 30, 45))    # oldest evicted
            self.assertIsNone(cache.peek(paths[1], 30, 45))
            self.assertIsNotNone(cache.peek(paths[2], 30, 45))  # used: now the newest
            cache.pixmap(paths[5], 30, 45)
            self.assertIsNone(cache.peek(paths[3], 30, 45))    # so 3 went instead
            self.assertIsNotNone(cache.peek(paths[2], 30, 45))
            # Bytes, not count: one big image pushes out several small ones,
            # and an image bigger than the whole budget still stays (alone).
            big = cache.pixmap(paths[0], 60, 90)
            self.assertGreater(nx.pixmap_bytes(big), cache.budget)
            self.assertEqual(list(cache.pix.values()), [big])
            self.assertEqual(cache.used, nx.pixmap_bytes(big))
            cache.pixmap(paths[1], 30, 45)
            self.assertEqual(len(cache.pix), 1)
            self.assertEqual(cache.used, one)
            self.assertTrue(cache.has_room())
            cache.used = cache.budget
            self.assertFalse(cache.has_room())

    def test_shelves_keep_rows_and_tiles_across_refreshes(self):
        view = nx.HomeView('shelves')
        games = [{'slug': f'g{i}', 'display_name': f'G{i}', 'source': 'nexus' if i < 4
                  else 'steam'} for i in range(6)]
        smart = lambda gs: [{'key': 'smart:never', 'title': "Never played", 'tip': 't',
                             'games': gs[:3]}]
        view.set_games(games)
        view.set_smart(smart(games))
        rows = {r.title: r for r in view.findChildren(nx.ShelfRow)}
        first = {id(t): t for r in rows.values() for t in r.tiles}
        g1 = rows['Repacks'].tiles[1]
        fresh = [dict(g) for g in games]                 # a reload: new dicts
        view.set_games(fresh)
        view.set_smart(smart(fresh))
        now = {r.title: r for r in view.findChildren(nx.ShelfRow) if not r.isHidden()}
        self.assertEqual({k: id(v) for k, v in now.items()},
                         {k: id(v) for k, v in rows.items()})        # the same rows…
        self.assertEqual({id(t) for r in now.values() for t in r.tiles}, set(first))
        self.assertIs(now['Repacks'].tiles[0].game, fresh[0])         # …on the new data
        self.assertEqual(sorted(len(ts) for ts in view.tiles.values()), [1, 1, 1, 2, 2, 2])
        # One repack gone, one new: the rest keep their tiles, the count follows.
        fresh2 = [dict(g) for g in fresh[1:]] + [{'slug': 'g9', 'display_name': 'G9',
                                                  'source': 'nexus'}]
        view.set_games(fresh2)
        view.set_smart(smart(fresh2))
        repacks = now['Repacks']
        self.assertEqual([t.game['slug'] for t in repacks.tiles], ['g1', 'g2', 'g3', 'g9'])
        self.assertIs(repacks.tiles[0], g1)
        self.assertEqual(repacks.count_label.text(), '4')
        self.assertNotIn('g0', [s for s, ts in view.tiles.items() if ts])
        self.assertEqual(len(view.tiles['g1']), 2)                    # its shelf and Never played
        # No games: no rows, no smart shelves, the empty state.
        view.set_games([])
        QApplication.processEvents()
        self.assertEqual([r for r in view.findChildren(nx.ShelfRow) if not r.isHidden()], [])
        self.assertEqual(view._smart, [])
        view.deleteLater()

    def test_art_cache_is_bounded_and_async(self):
        import tempfile
        from PyQt6.QtGui import QImage, QColor
        cache = nx.ArtCache()
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(5):
                img = QImage(60, 90, QImage.Format.Format_RGB32)
                img.fill(QColor(40 * i, 0, 0))
                p = f"{d}/{i}.png"
                img.save(p)
                paths.append(p)
            cache.budget = 3 * nx.pixmap_bytes(cache.pixmap(paths[0], 30, 45))
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
