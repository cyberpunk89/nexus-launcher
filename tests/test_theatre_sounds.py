"""Theatre's menu sounds and the art-tinted background (w-65).

Never opens real audio: tests/sandbox.py sets NEXUS_NO_SOUND=1, and the
tests that check what would play hand UiSounds a fake QSoundEffect class
(`factory`), so QtMultimedia is never asked for a device."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import set_gui  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QColor, QImage, QKeyEvent
    from PyQt6.QtWidgets import QApplication, QPushButton
    from nexus.gui import art, sounds
    from nexus.gui.input import focus_and_reveal
    from nexus.gui.sounds import gain, NAMES, UiSounds
    from nexus.gui.theme import LOOK
    from nexus.gui.widgets import GradientBackground


class FakeEffect:
    """Stands in for QSoundEffect: records instead of opening a device."""
    made: list = []

    def __init__(self, parent=None):
        self.source = None
        self.volume = None
        self.plays = 0
        self.stopped = 0
        self.deleted = False
        FakeEffect.made.append(self)

    def setSource(self, url):
        self.source = url.toLocalFile()

    def setVolume(self, v):
        self.volume = v

    def play(self):
        self.plays += 1

    def stop(self):
        self.stopped += 1

    def deleteLater(self):
        self.deleted = True


def fake_sounds(parent, theatre=True):
    """A UiSounds that would play: not disabled, app active, fake effects."""
    flag = {'on': theatre}
    s = UiSounds(parent, lambda: flag['on'])
    s.disabled = False
    s.app_active = True
    s.factory = lambda: FakeEffect
    s.flag = flag
    return s


def played(s) -> dict:
    """name -> times played, from the fake effects UiSounds holds."""
    return {n: e.plays for n, e in s.effects.items() if e.plays}


@unittest.skipIf(nx is None, "PyQt6 not available")
class Sounds(unittest.TestCase):
    def setUp(self):
        FakeEffect.made = []
        from PyQt6.QtWidgets import QWidget
        self.host = QWidget()
        self.addCleanup(self.host.deleteLater)

    def test_gain_curve(self):
        self.assertEqual(gain(0), 0.0)
        self.assertAlmostEqual(gain(100), 0.7)
        steps = [gain(v) for v in range(10, 101, 10)]
        self.assertEqual(steps, sorted(steps))                     # louder each step
        self.assertTrue(all(0 < g <= 0.7 for g in steps))
        # Log: each step multiplies, so the low end isn't all the same.
        self.assertAlmostEqual(steps[1] / steps[0], steps[-1] / steps[-2], places=6)
        self.assertLess(gain(50), 0.7 / 2)
        self.assertEqual(gain(None), 0.0)
        self.assertAlmostEqual(gain(250), 0.7)                     # clamped

    def test_wavs_ship(self):
        for n in NAMES:
            p = sounds.SOUND_DIR / f'{n}.wav'
            self.assertTrue(p.exists(), p)
        total = sum((sounds.SOUND_DIR / f'{n}.wav').stat().st_size for n in NAMES)
        self.assertLess(total, 200_000)

    def test_silent_under_no_sound(self):
        """The sandbox sets NEXUS_NO_SOUND=1: no effect is ever made, no key
        filter installed, even with sounds on in Theatre."""
        self.assertEqual(os.environ.get('NEXUS_NO_SOUND'), '1')
        asked = []
        s = UiSounds(self.host, lambda: True)
        s.factory = lambda: asked.append(1) or FakeEffect
        s.app_active = True
        s.configure(True, 100)
        self.assertTrue(s.disabled)
        self.assertFalse(s.play('select'))
        s.preload()
        self.assertEqual((asked, FakeEffect.made, s.effects, s._watching), ([], [], {}, False))

    def test_off_by_default_and_theatre_only(self):
        s = fake_sounds(self.host, theatre=False)
        self.assertFalse(s.on)
        self.assertFalse(s.play('select'))                         # off
        s.configure(True, 50)
        self.assertFalse(s.play('select'))                         # not in Theatre
        self.assertEqual(FakeEffect.made, [])                      # nothing made yet: lazy
        s.flag['on'] = True
        self.assertTrue(s.play('select'))
        e = s.effects['select']
        self.assertTrue(e.source.endswith('select.wav'))
        self.assertAlmostEqual(e.volume, gain(50))
        s.configure(True, 0)
        self.assertFalse(s.play('back'))                           # volume 0 is silent
        s.configure(True, 80)
        self.assertAlmostEqual(e.volume, gain(80))                 # held effects follow
        s.configure(False, 80)
        self.assertEqual(s.effects, {})                            # off lets go
        self.assertTrue(e.deleted)
        self.assertFalse(s._watching)

    def test_released_when_nexus_goes_inactive(self):
        s = fake_sounds(self.host)
        s.configure(True, 50)
        s.preload()
        held = list(s.effects.values())
        self.assertEqual(sorted(s.effects), sorted(NAMES))
        s._app_state(Qt.ApplicationState.ApplicationInactive)
        self.assertEqual(s.effects, {})
        self.assertTrue(all(e.deleted and e.stopped for e in held))
        self.assertFalse(s.play('move'))                           # a game has the device
        s._app_state(Qt.ApplicationState.ApplicationActive)
        self.assertTrue(s.play('move'))                            # made again, lazily
        self.assertEqual(list(s.effects), ['move'])

    def test_launch_replaces_select_and_moves_stay_quiet_after(self):
        s = fake_sounds(self.host)
        s.configure(True, 50)
        self.assertTrue(s.play('select'))
        self.assertFalse(s.play('move'))                           # the page it opened
        self.assertTrue(s.play('launch'))
        self.assertEqual(s.effects['select'].stopped, 1)
        self.assertFalse(s.play('launch'))                         # doubled key: once
        s._last_any = ('', 0.0)
        s._last = {}
        self.assertTrue(s.play('move'))
        self.assertFalse(s.play('move'))                           # 60 ms apart at most
        self.assertFalse(s.play('nope'))

    def test_no_qtmultimedia(self):
        s = fake_sounds(self.host)
        s.factory = lambda: None
        s.configure(True, 50)
        self.assertFalse(s.active())
        self.assertFalse(s.play('select'))
        with mock.patch.dict('sys.modules', {'PyQt6.QtMultimedia': None}):
            self.assertIsNone(sounds._sound_effect_class())

    def test_accept_key_selects(self):
        s = fake_sounds(self.host)
        s.configure(True, 50)
        btn = QPushButton("x", self.host)
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        with mock.patch.object(QApplication, 'focusWidget', return_value=btn):
            s.eventFilter(self.host, ev)                           # a parent: not again
            self.assertEqual(played(s), {})
            s.eventFilter(btn, ev)
            self.assertEqual(played(s), {'select': 1})
            arrow = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
            s.eventFilter(btn, arrow)                              # moves come from SpatialNav
            self.assertEqual(played(s), {'select': 1})


@unittest.skipIf(nx is None, "PyQt6 not available")
class Wash(unittest.TestCase):
    def test_wash_colour(self):
        red = QColor.fromHsvF(0.0, 0.9, 0.9)
        with mock.patch.dict(LOOK, {'glow': 1.0}):
            dark = GradientBackground.wash_color(red, light=False)
            self.assertAlmostEqual(dark.alphaF(), 0.30, places=2)
            light = GradientBackground.wash_color(red, light=True)
            self.assertAlmostEqual(light.alphaF(), 0.10, places=2)
            self.assertLessEqual(light.hsvSaturationF(), 0.351)      # Dawn stays cream
            grey = GradientBackground.wash_color(QColor('#808080'), light=True)
            self.assertAlmostEqual(grey.alphaF(), 0.10, places=2)
        with mock.patch.dict(LOOK, {'glow': 1.9}):
            self.assertLessEqual(GradientBackground.wash_color(red, False).alphaF(), 0.401)
        with mock.patch.dict(LOOK, {'glow': 0.0}):                 # Glow Off: none
            self.assertEqual(GradientBackground.wash_color(red, False).alphaF(), 0.0)
            self.assertEqual(GradientBackground.wash_color(red, True).alphaF(), 0.0)

    def test_tint_instant_without_motion_and_paints(self):
        bg = GradientBackground()
        self.addCleanup(bg.deleteLater)
        bg.resize(320, 180)
        red = QColor('#d03030')
        with mock.patch.object(art, 'MOTION', False), mock.patch.dict(LOOK, {'art_tint': True}):
            bg.set_tint(red)
            self.assertEqual(bg._tint, red)                        # no cross-fade
            self.assertFalse(bg._anim.state() == bg._anim.State.Running)
        bg.set_wash(True)
        img = bg.grab().toImage()                                  # paints without aborting
        self.assertFalse(img.isNull())
        with mock.patch.dict(LOOK, {'art_tint': False}):
            bg.set_tint(red)
        self.assertNotEqual(bg._to, red)                           # art tint off: the accent


def _png(path: Path, color: str):
    img = QImage(32, 18, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    img.save(str(path))
    return str(path)


@unittest.skipIf(nx is None, "PyQt6 not available")
class TheatreWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        nx.LibraryPrefs.save = lambda self: None
        set_gui('CliQuery', lambda *a, **k: None)
        cls.tmp = tempfile.mkdtemp(prefix='nexus-w65-')
        cls.red = _png(Path(cls.tmp) / 'red.png', '#c02020')
        cls.blue = _png(Path(cls.tmp) / 'blue.png', '#2040c0')

    @classmethod
    def tearDownClass(cls):
        set_gui('cli_json', cls._saved[0])
        nx.LibraryPrefs.save = cls._saved[1]
        set_gui('CliQuery', cls._saved[2])

    def setUp(self):
        FakeEffect.made = []
        for name, ret in (('power_caps', set()), ('power', True)):
            p = mock.patch(f'nexus.platform.{name}', return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        self.win = nx.MainWindow()
        self.win.resize(1300, 850)
        self.win.show()
        games = [
            {'slug': 'alpha', 'display_name': 'Alpha', 'source': 'nexus', 'launcher': '/a',
             'last_played': '2026-09-30T10:00:00', 'playtime_minutes': 120, 'cover': self.blue},
            {'slug': 'gamma', 'display_name': 'Gamma', 'source': 'nexus', 'launcher': '/g',
             'cover': self.red},
        ]
        self.win._on_library({'games': games, 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def _fake(self):
        s = self.win.sounds
        s.disabled = False
        s.app_active = True
        s.factory = lambda: FakeEffect
        return s

    def test_sounds_off_by_default_and_follow_prefs(self):
        w = self.win
        self.assertFalse(w.prefs.controls['sounds'])
        self.assertFalse(w.sounds.on)
        w._apply_controls({'sounds': True, 'volume': 30})
        self.assertEqual((w.sounds.on, w.sounds.volume), (True, 30))
        self.assertTrue(w.sounds.disabled)                         # sandbox: still silent
        w.set_theatre(True)
        self.assertFalse(w.sounds.play('select'))
        self.assertEqual(FakeEffect.made, [])

    def test_theatre_hooks(self):
        w = self.win
        s = self._fake()
        w.set_theatre(True)
        QApplication.processEvents()
        # Turning sounds on from the couch says so; the volume plays a sample.
        w.open_theatre_settings()
        rows = w.theatre_settings.rows_by_key
        rows['sounds'].click()
        self.assertTrue(s.on)
        self.assertEqual(played(s), {'select': 1})
        rows['volume'].step(1)
        self.assertEqual(played(s), {'select': 2})
        self.assertAlmostEqual(s.effects['select'].volume, gain(60))
        # B that closes something: back.
        w._escape()
        self.assertFalse(w.theatre_settings.isVisible())
        self.assertEqual(played(s).get('back'), 1)
        s._last_any = ('', 0.0)
        w._escape()                                                # Home, nothing to close
        self.assertEqual(played(s).get('back'), 1)
        # A D-pad move.
        w.spatial.moved.emit()
        self.assertEqual(played(s).get('move'), 1)
        # Play: launch (the CLI is never run).
        with mock.patch.object(w, 'on_game_action'):
            w.theatre.play.emit(w.game('alpha'))
        self.assertEqual(played(s).get('launch'), 1)
        # A refusal: error.
        w.toast("Alpha is running — close it first.", 'warn', None, 10)
        self.assertEqual(played(s).get('error'), 1)
        # Leaving Theatre lets go of the device; the desktop is silent.
        held = list(s.effects.values())
        w.set_theatre(False)
        self.assertEqual(s.effects, {})
        self.assertTrue(all(e.deleted for e in held))
        w.toast("Oops", 'error', None, 10)
        self.assertEqual(s.effects, {})

    def test_wash_and_tint_follow_focus(self):
        w = self.win
        bg = w.background
        self.assertFalse(bg.wash)
        with mock.patch.object(art, 'MOTION', False), mock.patch.dict(LOOK, {'art_tint': True}):
            w.set_theatre(True)
            self.assertTrue(bg.wash)
            w.activateWindow()
            QApplication.processEvents()
            tile = next(t for t in w.theatre.findChildren(nx.GameTile)
                        if t.game['slug'] == 'gamma' and t.isVisible())
            focus_and_reveal(tile)
            QApplication.processEvents()
            if QApplication.focusWidget() is not tile:            # offscreen lost activation
                w.theatre._focus_changed(None, tile)
            red = art.dominant_color(self.red)
            self.assertEqual(bg._to, red)
            self.assertEqual(bg._tint.name(), red.name())          # instant: no motion
            w.set_theatre(False)
            self.assertFalse(bg.wash)
            self.assertNotEqual(bg._to, red)                       # the desktop's own colour

    def test_tint_waits_for_focus_to_rest(self):
        w = self.win
        got = []
        w.theatre.focus_game.connect(got.append)
        with mock.patch.object(art, 'MOTION', True):
            w.set_theatre(True)
            got.clear()
            tile = next(t for t in w.theatre.findChildren(nx.GameTile)
                        if t.game['slug'] == 'gamma' and t.isVisible())
            w.theatre._focus_changed(None, tile)
            self.assertEqual(got, [])                              # not yet: 250 ms dwell
            self.assertTrue(w.theatre._tint_timer.isActive())
            self.assertEqual(w.theatre._tint_timer.interval(), 250)
            w.theatre._tint_timer.stop()
            w.theatre._tint_settle()
            self.assertEqual([g['slug'] for g in got], ['gamma'])
            w.theatre._focus_changed(None, tile)
            w.theatre._tint_settle()
            self.assertEqual(len(got), 1)                          # same game: once


if __name__ == '__main__':
    unittest.main()
