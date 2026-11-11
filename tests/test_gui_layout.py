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
def browse_groups(games, **pv):
    """Browse's groups (w-99) over plain dicts, as the window makes them."""
    from nexus import categories
    return categories.browse(categories.categories(games, {}, categories.PrefsView(**pv)))


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
class StallWatchTimer(unittest.TestCase):
    """w-101: the UI heartbeat is a child QTimer (bound method) that re-arms
    the faulthandler watchdog every BEAT_SECONDS, and only with a native file."""

    def setUp(self):
        import tempfile
        from nexus.report import crash
        self.crash = crash
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(crash, '_dir', lambda: Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_beats_re_arm_and_stop_with_the_file(self):
        from PyQt6.QtCore import QObject
        from nexus.gui.app import StallWatch
        owner = QObject()
        self.addCleanup(owner.deleteLater)
        watch = StallWatch(owner)
        self.assertIs(watch.timer.parent(), watch)
        self.assertEqual(watch.timer.interval(), self.crash.BEAT_SECONDS * 1000)
        self.assertLess(self.crash.BEAT_SECONDS * 2, self.crash.STALL_SECONDS)
        self.assertFalse(watch.start())                   # no native file: off
        self.assertFalse(watch.timer.isActive())
        self.crash.enable_native()
        try:
            with mock.patch.object(self.crash, 'stall_beat') as beat:
                self.assertTrue(watch.start())          # arms at once
                self.assertTrue(watch.timer.isActive())
                n = beat.call_count
                watch.timer.timeout.emit()
                self.assertEqual(beat.call_count, n + 1)
            # The event loop ended: stop() stops beating and cancels the
            # pending dump, so a slow teardown then a kill is no freeze.
            import faulthandler
            with mock.patch.object(faulthandler, 'cancel_dump_traceback_later') as cancel:
                watch.stop()
            self.assertFalse(watch.timer.isActive())
            cancel.assert_called_once_with()
            self.assertFalse(self.crash._native['watch'])
        finally:
            watch.stop()
            self.crash.disable_native()


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
             'tests.test_gui_layout.HiDpiArt', 'tests.test_gui_layout.PipelineCardFit']

    def _run(self, scale, tests=None):
        import subprocess
        env = {**os.environ, 'QT_SCALE_FACTOR': scale}
        out = subprocess.run([sys.executable, '-m', 'unittest', *(tests or self.TESTS)],
                             cwd=REPO,
                             env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-3000:])
        self.assertNotIn('skipped', out.stderr.splitlines()[-1])

    def test_at_125_percent(self):
        self._run('1.25')

    def test_at_150_percent(self):
        self._run('1.5')

    def test_pipeline_at_140_percent(self):
        self._run('1.4', ['tests.test_gui_layout.PipelineCardFit'])      # incl. the sparkline


@unittest.skipIf(nx is None, "PyQt6 not available")
class PipelineCardFit(unittest.TestCase):
    """The Downloads page's pipeline cards (w-94): the track's shares, the
    buttons per state, and a card that fits without clipping."""
    NOW = 1_800_000_000.0

    def card(self, **kw):
        from nexus import pipeline
        r = {'hash': 'a' * 40, 'name': 'Hollow Quarry', 'title': 'Hollow Quarry',
             'slug': 'hollow-quarry', 'tracked': True, 'installed': False, 'percent': 62.0,
             'done': False, 'state': 'downloading', 'rate': 42 << 20, 'eta': 900,
             'size': 14 << 30, 'repack_bytes': 14 << 30, 'original_bytes': 38 << 30}
        r.update(kw)
        return pipeline.cards([r], {}, self.NOW, set())[0], r

    def test_track_weights_and_skipped_verify_label(self):
        from nexus import pipeline
        from nexus.gui.pipeline_view import SKIPPED_VERIFY, StageTrack
        t = StageTrack()
        self.addCleanup(t.deleteLater)
        t.resize(604, t.sizeHint().height())
        c, _r = self.card()
        t.set_track(c['track'])
        segs = {s['stage']: s for s in t.segments()}
        room = 604 - 3 * nx.scaled(4)
        for name, weight in pipeline.STAGES:           # the skipped Verify keeps its share
            self.assertAlmostEqual(segs[name]['w'], room * weight, places=3)
        self.assertEqual(segs['verify']['label'], "VERIFY –")    # short; the why in the tip
        self.assertEqual(segs['verify']['status'], 'skipped')
        self.assertEqual(segs['download']['status'], 'active')
        self.assertAlmostEqual(segs['download']['frac'], 0.62, places=3)
        self.assertIn(SKIPPED_VERIFY, t.toolTip())
        c, _r = self.card(done=True, percent=100.0, installed=True,
                          installed_at='2027-01-15T08:00:00')
        t.set_track(c['track'])
        segs = {s['stage']: s for s in t.segments()}
        self.assertEqual({s['status'] for s in segs.values()}, {'done', 'skipped'})
        self.assertTrue(segs['download']['label'].startswith("✓ "))
        t.grab()                                         # paints without a qFatal
        t.set_track([{'stage': 'download'}, {}])         # a short or odd track still paints
        self.assertEqual(len(t.segments()), 4)
        t.grab()

    def test_stage_track_scales_for_theatre(self):
        from nexus.gui.pipeline_view import StageTrack, paint_track
        a, b = StageTrack(), StageTrack(scale=1.4)
        self.addCleanup(a.deleteLater)
        self.addCleanup(b.deleteLater)
        self.assertAlmostEqual(b._bar_h(), a._bar_h() * 1.4, places=3)
        self.assertAlmostEqual(b._gap(), a._gap() * 1.4, places=3)
        self.assertGreater(b._font().pixelSize(), a._font().pixelSize())
        self.assertGreater(b.sizeHint().height(), a.sizeHint().height())
        c, _r = self.card()
        for t in (a, b):
            t.resize(604, t.sizeHint().height())
            t.set_track(c['track'])
        self.assertAlmostEqual(sum(s['w'] for s in b.segments()) + 3 * b._gap(), 604, places=3)
        b.grab()
        # the shared painter draws onto any paint device (the Ribbon's items)
        from PyQt6.QtGui import QImage, QPainter
        img = QImage(300, 60, QImage.Format.Format_ARGB32)
        pt = QPainter(img)
        paint_track(pt, b.segments(), b.labels(), 8.0, b._font(), 14.0, False, 0.0, 5.0)
        pt.end()

    def test_eta_line_across_stages(self):
        from nexus.gui.pipeline_view import eta_line
        c, _r = self.card()
        self.assertEqual(eta_line(c), "Download done in about 15 min")
        c = {**c, 'eta': 3600 + 900, 'eta_all': True, 'eta_est': True}
        self.assertEqual(eta_line(c), "Done in about 75 min, est.")
        # Between stages (nothing moving) the one ETA still shows.
        c2 = {**c, 'state': 'downloaded', 'stage': 'install'}
        self.assertEqual(eta_line(c2), "Done in about 75 min, est.")
        self.assertEqual(eta_line({**c2, 'eta_all': False}), '')
        self.assertEqual(eta_line({**c, 'eta': None}), '')

    def test_checking_verify_is_indeterminate(self):
        from nexus.gui.pipeline_view import PipelineCard, state_line
        host = QWidget()
        self.addCleanup(host.deleteLater)
        # The client re-checks a finished download: Verify, with its figure...
        c, r = self.card(done=True, percent=100.0, state='checking', completed=True, check=0.43)
        self.assertEqual(c['state'], 'verifying')
        self.assertEqual(state_line(c)[:2], ("Checking files", "43%"))
        w = PipelineCard(c, host)
        w.set(c, r)
        w.resize(900, 200)
        segs = {s['stage']: s for s in w.track.segments()}
        self.assertFalse(segs['verify']['indeterminate'])
        # ... or without one (qBittorrent): no figure, an indeterminate bar.
        c, r = self.card(done=True, percent=100.0, state='checking', completed=True, check=None)
        w.set(c, r)
        segs = {s['stage']: s for s in w.track.segments()}
        self.assertTrue(segs['verify']['indeterminate'])
        self.assertEqual(segs['verify']['status'], 'active')
        self.assertEqual(state_line(c)[:2], ("Checking files", ''))
        self.assertTrue(w.track.shimmer)
        self.assertEqual(w.figure.text(), '')
        for ph in (0.0, 0.5, 0.99):
            w.track._phase = ph
            w.track.grab()                                   # paints without a qFatal
        # An unfinished download being checked stays Download, with the figure.
        c, r = self.card(state='checking', check=0.43)
        self.assertEqual(c['stage'], 'download')
        self.assertEqual(state_line(c)[:2], ("Checking files", "43%"))
        w.set(c, r)
        w.track.grab()

    def test_sparkline_fits(self):
        from PyQt6.QtWidgets import QVBoxLayout
        from nexus.gui.pipeline_view import PipelineCard, Sparkline
        host = QWidget()
        self.addCleanup(host.deleteLater)
        lay = QVBoxLayout(host)
        c, r = self.card(rate=40 << 20)
        w = PipelineCard(c, host)
        lay.addWidget(w)
        w.set(c, r, client='Transmission')
        for width, narrow in ((900, False), (620, True)):
            host.resize(width, 400)
            host.show()
            w.set_narrow(narrow)
            for i in range(30):                             # a few poll ticks
                w.sample(c, 1000.0 + 2 * i)
            for _ in range(3):
                QApplication.processEvents()
            sp = w.spark
            with self.subTest(width=width):
                self.assertIsInstance(sp, Sparkline)
                self.assertFalse(sp.isHidden())
                self.assertEqual(sp.parent(), w.side)
                self.assertTrue(w.side.rect().contains(sp.geometry()))
                self.assertGreaterEqual(sp.width(), nx.scaled(64))
                self.assertGreaterEqual(sp.height(), nx.scaled(16))
                if not w.narrow:    # beside the card: under the ETA, above the buttons (a wide font can force the narrow layout)
                    self.assertGreaterEqual(sp.geometry().top(), w.eta.geometry().bottom())
                    self.assertLessEqual(sp.geometry().bottom(), w.btnhost.geometry().top())
                self.assertFalse(w.grab().isNull())         # the card paints
                for kind in ('download', 'disk'):           # flat, empty, spiky: no qFatal
                    for pts in ([(0.0, 0)] * 3, [(0.0, 5), (1.0, 5)], [(0.0, 1), (60.0, 1e9),
                                                                      (120.0, 3)]):
                        sp.set_series(kind, pts, 120.0)
                        sp.grab()
        # Fewer than two samples: hidden. A kind change starts a new series.
        w.sample({**c, 'state': 'paused'}, 2000.0)
        self.assertTrue(w.spark.isHidden())
        w.sample(c, 2001.0)
        self.assertTrue(w.spark.isHidden())
        w.sample(c, 2003.0)
        self.assertFalse(w.spark.isHidden())

    def test_labels_never_cut(self):
        # At 900 px (and at 125/140/150% through DisplayScaling) every name
        # under the track is whole, inside the track and clear of the
        # others; the current stage's is always there; narrow: only it.
        from PyQt6.QtGui import QFontMetrics
        from PyQt6.QtWidgets import QVBoxLayout
        from nexus.gui.pipeline_view import PipelineCard
        host = QWidget()
        self.addCleanup(host.deleteLater)
        lay = QVBoxLayout(host)
        states = [self.card(), self.card(done=True, percent=100.0, state='seeding',
                                         install_started='2027-01-15T08:00:00',
                                         install_failed='2027-01-15T08:10:00'),
                  self.card(done=True, percent=100.0, installed=True,
                            installed_at='2027-01-15T08:00:00')]
        cards = []
        for c, r in states:
            w = PipelineCard(c, host)
            lay.addWidget(w)
            w.set(c, r, client='Transmission')
            cards.append(w)
        for width, narrow in ((900, False), (1440, False), (620, True)):
            host.resize(width, 900)
            host.show()
            for w in cards:
                w.set_narrow(narrow)
            for _ in range(3):
                QApplication.processEvents()
            for w in cards:
                t = w.track
                fm = QFontMetrics(t._font())
                labs = t.labels()
                with self.subTest(width=width, state=w.c['state']):
                    for lab in labs:
                        self.assertGreaterEqual(lab['w'], fm.horizontalAdvance(lab['text']))
                        self.assertGreaterEqual(lab['x'], 0)
                        self.assertLessEqual(lab['x'] + lab['w'], t.width() + 0.5)
                    spans = sorted((lab['x'], lab['x'] + lab['w']) for lab in labs)
                    for (_a, end), (start, _b) in zip(spans, spans[1:]):
                        self.assertLessEqual(end, start)
                    current = [x for x in t.segments() if x['status'] in ('active', 'failed')]
                    if current:
                        self.assertIn(current[0]['label'], [lab['text'] for lab in labs])
                    if narrow:
                        self.assertEqual(len(labs), len(current))

    def test_forecast_starts_hidden_and_hides_when_empty(self):
        from nexus.gui.pipeline_view import ForecastStrip
        host = QWidget()
        self.addCleanup(host.deleteLater)
        f = ForecastStrip(host)
        self.assertTrue(f.isHidden())
        f.set_drives([])
        self.assertTrue(f.isHidden())
        f.set_drives([{'id': '1', 'mount': '/', 'free': 1, 'total': 9, 'to_write': 1}])
        self.assertFalse(f.isHidden())
        f.set_drives([])
        self.assertTrue(f.isHidden())

    def test_forecast_short_by_in_danger(self):
        from nexus.gui.pipeline_view import ForecastStrip
        f = ForecastStrip()
        self.addCleanup(f.deleteLater)
        G = 1 << 30
        f.set_drives([{'id': '1', 'mount': '/home', 'free': 50 * G, 'total': 500 * G,
                       'to_write': 20 * G, 'free_after': 30 * G, 'short_by': 0},
                      {'id': '2', 'mount': '/games', 'free': 10 * G, 'total': 500 * G,
                       'to_write': 30 * G, 'free_after': -20 * G, 'short_by': 20 * G}])
        self.assertFalse(f.isHidden())
        _w, head, ok, bar = f.rows['1']
        self.assertEqual(head.text(), "/HOME FORECAST")
        self.assertIn("about", ok.text())
        self.assertIn("free after", ok.text())
        self.assertFalse(ok.property('short'))
        _w, _h, short, bar2 = f.rows['2']
        self.assertIn("short by", short.text())
        self.assertTrue(short.property('short'))
        from nexus.gui.theme import P
        self.assertIn(f'QLabel#PipeFigure[short="true"] {{ color: {P["danger"]}; }}',
                      nx.build_stylesheet())
        used, incoming = bar2.parts()
        self.assertAlmostEqual(incoming, 10 / 500)      # capped at the free space
        f.grab()
        f.set_drives([{'id': '1', 'mount': '/home', 'free': 50 * G, 'total': 500 * G,
                       'to_write': 0, 'free_after': 50 * G, 'short_by': 0}])
        self.assertTrue(f.isHidden())                    # nothing left to write
        self.assertEqual(list(f.rows), ['1'])

    def test_actions_per_state(self):
        from nexus.gui.pipeline_view import buttons
        def acts(c, **kw):
            return [(a, t) for a, t, _p in buttons(c, **kw)]
        for st in ('downloading', 'queued', 'checking'):
            self.assertEqual(acts({'state': st}), [('pause', "Pause")])
        self.assertEqual(acts({'state': 'downloading'}, stalled=True), [('pause', "Pause")])
        self.assertEqual(acts({'state': 'paused'}), [('resume', "Resume")])
        self.assertEqual(acts({'state': 'error'}), [('resume', "Try again")])
        failed = {'state': 'failed', 'reason': 'error'}
        self.assertEqual(acts(failed), [('retry', "Retry"), ('verify', "Verify & retry")])
        self.assertEqual(acts({**failed, 'reason': 'stopped'}, has_log=True),
                         [('retry', "Retry"), ('verify', "Verify & retry"), ('log', "Show log")])
        self.assertEqual(acts({**failed, 'reason': 'uac'}), [('retry', "Try again")])
        self.assertEqual(acts({**failed, 'reason': 'cancelled'}), [('retry', "Install again")])
        self.assertEqual(acts({'state': 'downloaded'}), [('install', "Install")])
        for st in ('waiting', 'verifying', 'installing', 'updating', 'finishing'):
            self.assertEqual(acts({'state': st}), [])
        self.assertEqual(acts({'state': 'ready'}), [('play', "Play"), ('show', "Show")])
        self.assertEqual(acts({'state': 'ready'}, has_game=False), [('show', "Show")])
        self.assertTrue(buttons({'state': 'downloaded'})[0][2])          # primary

    def _fits(self, w):
        """Every visible child inside the card, and no button or number
        narrower than it asks to be."""
        from PyQt6.QtWidgets import QLabel, QPushButton
        box = w.rect()
        for child in w.findChildren(QWidget):
            if not child.isVisibleTo(w):
                continue
            r = child.rect().translated(child.mapTo(w, QPoint(0, 0)))
            with self.subTest(child=child.objectName() or type(child).__name__):
                self.assertTrue(box.contains(r), (r, box))
                if isinstance(child, QPushButton) or child.objectName() in (
                        'PipePct', 'PipeEta', 'PipeFigure', 'PipeSource'):
                    self.assertGreaterEqual(child.width(), child.sizeHint().width())
                if isinstance(child, QLabel) and child.objectName() != 'PipePct':
                    self.assertGreaterEqual(child.height(), child.sizeHint().height())

    def test_fits_900_px_without_clipping(self):
        from nexus.gui.pipeline_view import PipelineCard
        host = QWidget()
        self.addCleanup(host.deleteLater)
        from PyQt6.QtWidgets import QVBoxLayout
        lay = QVBoxLayout(host)
        long = "The Very Long Title Of A Game: Definitive Anniversary Edition Plus Every DLC"
        c, r = self.card(title=long, name=long)
        failed, fr = self.card(done=True, percent=100.0, state='seeding',
                               install_started='2027-01-15T08:00:00',
                               install_failed='2027-01-15T08:10:00')
        cards = []
        for cc, rr, kw in ((c, r, {'stalled': True}), (failed, fr, {'has_log': True})):
            w = PipelineCard(cc, host)
            lay.addWidget(w)
            w.set(cc, rr, client='Transmission', **kw)
            cards.append(w)
        for width, narrow in ((900, False), (620, True)):
            host.resize(width, 600)
            host.show()
            for w in cards:
                w.set_narrow(narrow)
            for _ in range(3):
                QApplication.processEvents()
            for w in cards:
                with self.subTest(width=width, state=w.c['state']):
                    self._fits(w)
                    self.assertTrue(w.title.width() > 100)
        self.assertEqual(cards[0].warn.isHidden(), False)
        self.assertEqual(cards[1].step.text(), "The install didn't finish")
        self.assertEqual(cards[1].btns['verify'].text(), "Verify && retry")   # shows one &
        from nexus.gui.pipeline_view import span
        self.assertEqual((span(30), span(76), span(1500)), ("under a minute", "1 min", "25 min"))

    def test_queued_dims_and_menu_outlives_the_card(self):
        from nexus.gui.pipeline_view import PipelineCard
        got = []
        c, r = self.card(state='queued')
        w = PipelineCard(c, None, row_sink=lambda a, row: got.append((a, row['hash'])))
        w.set(c, r)
        self.assertIsNotNone(w.graphicsEffect())
        c2, r2 = self.card()
        w.set(c2, r2)
        self.assertIsNone(w.graphicsEffect())
        items = w.items()
        w.deleteLater()
        from PyQt6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        items[-1][2]()                                   # the sheet's choice, card gone
        self.assertEqual(got, [('delete', 'a' * 40)])

    def test_reorder_items(self):
        from nexus.gui.downloads import torrent_items
        from nexus.gui.pipeline_view import PipelineCard
        got = []
        emit = lambda a, row: got.append((a, row['hash']))      # noqa: E731
        _c, r = self.card(queue=2)

        def names(row, **kw):
            return [t for t, _g, _f in torrent_items(row, emit, **kw)]
        off = names(r)
        self.assertEqual(off, names(r, reorder=False))              # the default: no change
        on = names(r, reorder=True)
        self.assertEqual([t for t in on if t not in off],
                         ["Move to top", "Move up", "Move down", "Move to bottom"])
        for (text, _g, fn), want in zip([i for i in torrent_items(r, emit, reorder=True)
                                         if i[0].startswith("Move")],
                                        ('queue_top', 'queue_up', 'queue_down', 'queue_bottom')):
            fn()
            self.assertEqual(got[-1], (want, 'a' * 40), text)
        # First in line: nothing to move up. No position, finished, gone: no reorder at all.
        first = names({**r, 'queue': 0}, reorder=True)
        self.assertEqual([t for t in first if t.startswith("Move")],
                         ["Move down", "Move to bottom"])
        for odd in ({'queue': None}, {'done': True}, {'gone': True}):
            self.assertEqual([t for t in names({**r, **odd}, reorder=True)
                              if t.startswith("Move")], [], odd)
        # The card follows its flag; the sheet's choices are the same list.
        c, r = self.card(queue=1)
        w = PipelineCard(c, None, row_sink=emit)
        w.set(c, r)
        self.assertNotIn("Move up", [i[0] for i in w.items()])
        w.reorder = True
        self.assertIn("Move up", [i[0] for i in w.items()])

    def test_page_sends_queue_moves(self):
        page = nx.DownloadsPage()
        self.addCleanup(page.deleteLater)
        got = []
        page.queue_move.connect(lambda where, h: got.append((where, h)))
        page._row_action('queue_top', {'hash': 'a' * 40})
        page._row_action('queue_bottom', {'hash': 'b' * 40})
        self.assertEqual(got, [('top', 'a' * 40), ('bottom', 'b' * 40)])
        c, r = self.card(queue=1)
        page.reorder = True
        page.set_data({'torrents': [r], 'pipeline': [c], 'drives': [], 'client': {}})
        self.assertTrue(page.cards[c['key']].reorder)

    def test_restyle_keeps_focus_and_follows_size(self):
        from nexus.gui import theme
        from nexus.gui.pipeline_view import ART_PX
        page = nx.DownloadsPage()
        self.addCleanup(page.deleteLater)
        page.resize(1000, 700)
        page.show()
        c, r = self.card()
        page.set_data({'torrents': [r], 'pipeline': [c], 'drives': [], 'client': {}})
        QApplication.processEvents()
        card = page.cards[c['key']]
        page.activateWindow()
        card.btns['pause'].setFocus()
        QApplication.processEvents()
        old = dict(theme.LOOK)
        self.addCleanup(theme.LOOK.update, old)
        theme.LOOK['scale'] = 1.25
        page.restyle()
        QApplication.processEvents()
        self.assertIs(page.cards[c['key']], card)
        self.assertTrue(card.btns['pause'].hasFocus())
        self.assertEqual(card.art.width(), nx.scaled(ART_PX))
        theme.LOOK['scale'] = old['scale']
        page.restyle()

    def test_no_motion_starts_no_timer(self):
        from nexus.gui import art
        from nexus.gui.pipeline_view import StageTrack
        self.assertFalse(art.MOTION)                     # the tests run still
        page = nx.DownloadsPage()
        self.addCleanup(page.deleteLater)
        page.resize(900, 700)
        page.show()
        c, r = self.card()
        page.set_data({'torrents': [r], 'pipeline': [c], 'drives': [], 'client': {}})
        QApplication.processEvents()
        card = page.cards[c['key']]
        self.assertTrue(card.track.shimmer)              # it moves...
        self.assertFalse(page._shimmer.isActive())       # ...but nothing animates
        self.assertFalse(card.track._anim.state() == card.track._anim.State.Running)
        c2, r2 = self.card(percent=80.0)
        page.set_data({'torrents': [r2], 'pipeline': [c2], 'drives': [], 'client': {}})
        self.assertAlmostEqual(card.track._shown['download'], 0.8, places=3)  # at once
        with mock.patch.object(art, 'MOTION', True):
            page._sync_shimmer()
            self.assertTrue(page._shimmer.isActive())
            page.setWindowState(Qt.WindowState.WindowMinimized)   # isVisible stays True
            page._shimmer_tick()
            self.assertFalse(page._shimmer.isActive())
            page.setWindowState(Qt.WindowState.WindowNoState)
            page._sync_shimmer()
            self.assertTrue(page._shimmer.isActive())
            page.hide()
            self.assertFalse(page._shimmer.isActive())
        self.assertIsInstance(card.track, StageTrack)


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

    def test_grid_with_a_head_builds_only_near_rows(self):
        """w-98: a category page puts its header and chips over the grid
        (set_head) and More like this under it (set_foot), in the same
        scroll. The rows the head pushes off screen get no tiles."""
        host, view, games = self._big_grid()
        cols = view.flow_host.columns(view.flow_host.width())
        head, foot = QWidget(), QWidget()
        head.setFixedHeight(600)
        foot.setFixedHeight(120)
        view.set_head(head)
        view.set_foot(foot)
        _app.processEvents()
        self.assertIs(head.parentWidget(), view.container)
        self.assertEqual(view.flow_host.y(), 600)
        self.assertGreater(foot.y(), view.flow_host.y() + view.flow_host.height() - 1)
        self.assertLessEqual(len(self._shown(view)), 2 * cols)    # rows 0-1 only
        bar = view.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.assertIn('g2999', view.tiles)
        self.assertLess(len(self._shown(view)), 8 * cols)
        bar.setValue(0)
        self.assertIn('g0', view.tiles)
        view.set_head(None)                              # taken back out
        _app.processEvents()
        self.assertEqual(view.flow_host.y(), 0)
        self.assertEqual(view.container.layout().indexOf(head), -1)

    def test_row_title_click_is_see_all(self):
        from PyQt6.QtTest import QTest
        row = nx.ShelfRow("RPG", 150, see_all=True)
        plain = nx.ShelfRow("Installing", 150)
        self.addCleanup(row.deleteLater)
        self.addCleanup(plain.deleteLater)
        hits = []
        row.see_all.connect(lambda: hits.append('row'))
        plain.see_all.connect(lambda: hits.append('plain'))
        for r in (row, plain):
            r.show()
            QTest.mouseClick(r.name_label, Qt.MouseButton.LeftButton)
        self.assertEqual(hits, ['row'])
        self.assertEqual(row.name_label.cursor().shape(), Qt.CursorShape.PointingHandCursor)

    def test_keepplace_snapshot_restores_scroll_and_focus(self):
        """Opening a category page from Browse takes a snapshot; Back puts
        the page's scroll and the focused tile back."""
        from PyQt6.QtWidgets import QVBoxLayout
        from PyQt6.QtTest import QTest
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(900, 500)
        view = nx.HomeView('shelves')
        other = QWidget()
        other.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        lay = QVBoxLayout(host)
        lay.addWidget(view)
        lay.addWidget(other)
        other.hide()
        games = [{'slug': f'g{i}', 'display_name': f'G{i}', 'source': src, 'installed': True}
                 for i, src in enumerate(['nexus'] * 12 + ['steam'] * 12 + ['epic'] * 12)]
        view.set_games(games)
        view.set_groups(browse_groups(games))
        host.show()
        host.activateWindow()
        QTest.qWait(20)
        bar = view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        rows = [r for r in view.findChildren(nx.ShelfRow) if not r.isHidden()]
        tile = rows[-1].tiles[3]
        tile.setFocus()
        place = nx.KeepPlace(view).take()
        view.hide()                                    # the page opens in its place
        other.show()
        other.setFocus()
        bar.setValue(0)
        QTest.qWait(20)
        other.hide()
        view.show()
        place.put_back()
        QTest.qWait(30)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertIs(_app.focusWidget(), tile)

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
        """Browse (w-99): a reload re-points the rows it keeps, by key."""
        view = nx.HomeView('shelves')
        games = [{'slug': f'g{i}', 'display_name': f'G{i}', 'source': 'nexus' if i < 4
                  else 'steam', 'favorite': i < 3} for i in range(6)]

        def fill(gs):
            view.set_games(gs)
            view.set_groups(browse_groups(gs))
        fill(games)
        rows = {r.key: r for r in view.group_rows()}
        self.assertIn('src:nexus', rows)
        self.assertIn('fav', rows)
        first = {id(t): t for r in rows.values() for t in r.tiles}
        g1 = rows['src:nexus'].tiles[1]
        fresh = [dict(g) for g in games]                 # a reload: new dicts
        fill(fresh)
        now = {r.key: r for r in view.group_rows() if not r.isHidden()}
        self.assertEqual({k: id(v) for k, v in now.items()},
                         {k: id(v) for k, v in rows.items()})        # the same rows…
        self.assertEqual({id(t) for r in now.values() for t in r.tiles}, set(first))
        self.assertIs(now['src:nexus'].tiles[0].game, fresh[0])        # …on the new data
        self.assertEqual(len(view.tiles['g1']), sum(
            1 for r in now.values() for t in r.tiles if t.game['slug'] == 'g1'))
        # One repack gone, one new: the rest keep their tiles, the count follows.
        fresh2 = [dict(g) for g in fresh[1:]] + [{'slug': 'g9', 'display_name': 'G9',
                                                  'source': 'nexus'}]
        fill(fresh2)
        repacks = now['src:nexus']
        self.assertEqual(sorted(t.game['slug'] for t in repacks.tiles), ['g1', 'g2', 'g3', 'g9'])
        self.assertIn(g1, repacks.tiles)
        self.assertEqual(repacks.count_label.text(), '4')
        self.assertNotIn('g0', [s for s, ts in view.tiles.items() if ts])
        # No games: no rows, no head, the empty state.
        view.set_games([])
        QApplication.processEvents()
        self.assertEqual([r for r in view.findChildren(nx.ShelfRow) if not r.isHidden()], [])
        self.assertEqual(view.group_rows(), [])
        self.assertTrue(view.head.isHidden())
        view.deleteLater()

    def test_keepplace_matches_rows_by_key(self):
        """A row renamed in a rebuild (a shelf of yours) keeps its sideways
        offset and focus: KeepPlace knows rows by key, then by title."""
        from PyQt6.QtTest import QTest
        from nexus import categories
        view = nx.HomeView('shelves')
        self.addCleanup(view.deleteLater)
        view.resize(700, 600)
        games = [{'slug': f'g{i}', 'display_name': f'G{i}', 'source': 'steam',
                  'installed': True, 'collections': ['Old name']} for i in range(14)]
        pv = categories.PrefsView(collections=('Old name',))
        view.set_games(games)
        view.set_groups(categories.browse(categories.categories(games, {}, pv)))
        view.show()
        view.activateWindow()
        QTest.qWait(20)
        row = view.row_for('col:Old name')
        self.assertGreater(row.bar.maximum(), 0)
        row.bar.setValue(row.bar.maximum())
        focused = row.tiles[10]
        focused.setFocus()
        QTest.qWait(5)
        same = categories.lookup(categories.categories(games, {}, pv), 'col:Old name').games
        renamed = categories.Category('col:Old name', 'collection', "New name", '', '*', None,
                                      tuple(dict(g) for g in same))
        with nx.KeepPlace(view, scroll=True):
            view.set_games(games)
            view.set_groups([categories.Group('yours', "Yours", (renamed,))])
            row.bar.setValue(0)                    # as a rebuild that lost the place would
            focused.clearFocus()
        QTest.qWait(30)
        self.assertIs(view.row_for('col:Old name'), row)
        self.assertEqual(row.title, "New name")
        self.assertEqual(row.name_label.text(), "New name")
        self.assertEqual(row.bar.value(), row.bar.maximum())
        self.assertIs(_app.focusWidget(), focused)

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
