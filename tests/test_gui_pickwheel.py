"""Pick for me's Wheel style (w-142): the style option, the wheel being built
only in wheel mode, the spin landing on the decided pick, Motion off."""

import time
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx

if nx is not None:
    from PyQt6.QtWidgets import QApplication
    from nexus.gui import dashboard as D
    from nexus.gui import pick_wheel as W


def games(n=10):
    return [{'slug': f'g{i}', 'display_name': f'Game {i}', 'launcher': f'/g{i}'}
            for i in range(n)]


@unittest.skipIf(nx is None, "PyQt6 not available")
class StyleOption(unittest.TestCase):
    def test_style_reaches_tables_and_edit_bar(self):
        self.assertEqual(nx.DASH_OPTIONS['pick'],
                         {'vibe': ('any', 'short', 'coop', 'unfinished', 'almost', 'never'),
                          'style': ('chips', 'wheel')})
        self.assertEqual(D.DASH_STYLE_LABELS['pick'], {'chips': "Chips", 'wheel': "Wheel"})
        self.assertIn('style', D.OPTION_BAR)

    def test_saved_layouts_stay_valid(self):
        out = nx.dash_normalize([{'id': 'pick', 'vibe': 'coop'}])
        pick = next(i for i in out if i['id'] == 'pick')
        self.assertEqual(pick['vibe'], 'coop')
        self.assertEqual(pick['style'], 'chips')           # an old layout: Chips
        out = nx.dash_normalize([{'id': 'pick', 'style': 'wheel', 'vibe': 'coop'}])
        pick = next(i for i in out if i['id'] == 'pick')
        self.assertEqual((pick['style'], pick['vibe']), ('wheel', 'coop'))
        out = nx.dash_normalize([{'id': 'pick', 'style': 'bogus'}])
        self.assertEqual(next(i for i in out if i['id'] == 'pick')['style'], 'chips')


@unittest.skipIf(nx is None, "PyQt6 not available")
class WheelWidget(unittest.TestCase):
    def setUp(self):
        self.spun = []
        self.decided = games()[6]
        self.hub = SimpleNamespace(_games=games(), spin=lambda vibe, g=None: self.spun.append((vibe, g)),
                                   pick_for=lambda vibe: self.decided)

    def pick(self, **options):
        w = D.PickWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = 'small'
        w.options = options
        w.bind(self.hub)
        w.fill(self.hub)
        return w

    def test_chips_mode_has_no_wheel(self):
        w = self.pick()
        self.assertIsNone(w.wheel)
        w = self.pick(style='chips')
        self.assertIsNone(w.wheel)

    def test_wheel_mode_builds_the_wheel(self):
        w = self.pick(style='wheel')
        self.assertIsInstance(w.wheel, W.PickWheel)
        self.assertEqual(len(w.wheel.names), W.SEGMENTS)
        self.assertTrue(w.wheel.accessibleName())
        self.assertTrue(w.wheel.toolTip())
        self.assertTrue(w.btn.isEnabled())                 # a real button for the pad
        self.assertNotEqual(w.btn.focusPolicy(), W.Qt.FocusPolicy.NoFocus)
        w.options = {'style': 'chips'}                     # cycled back
        w.fill(self.hub)
        self.assertTrue(w.wheel.isHidden())

    def test_spin_lands_on_the_decided_pick(self):
        w = self.pick(style='wheel', vibe='any')
        with mock.patch.object(W, 'anim_ms', lambda ms: 0):
            w.btn.click()
        self.assertEqual(self.spun, [('any', self.decided)])
        wheel = w.wheel
        self.assertEqual(wheel.names[wheel.landed], self.decided['display_name'])
        self.assertIn(self.decided['display_name'], wheel.accessibleDescription())

    def test_animated_spin_lands_after_the_animation(self):
        w = self.pick(style='wheel')
        with mock.patch.object(W, 'anim_ms', lambda ms: 40):
            w.btn.click()
            self.assertTrue(w.wheel.spinning)
            self.assertFalse(w.btn.isEnabled())
            self.assertEqual(self.spun, [])                # not yet
            for _ in range(200):
                QApplication.processEvents()
                if not w.wheel.spinning:
                    break
                time.sleep(0.01)
        self.assertFalse(w.wheel.spinning)
        self.assertEqual(self.spun, [('any', self.decided)])
        self.assertEqual(w.wheel.names[w.wheel.landed], self.decided['display_name'])
        self.assertTrue(w.btn.isEnabled())

    def test_motion_off_starts_no_animation(self):
        w = self.pick(style='wheel')
        with mock.patch.object(W, 'anim_ms', lambda ms: 0), \
                mock.patch.object(W, 'QVariantAnimation') as anim:
            w.btn.click()
        anim.assert_not_called()
        self.assertFalse(w.wheel.spinning)
        self.assertIsNotNone(w.wheel.landed)
        self.assertEqual(self.spun, [('any', self.decided)])

    def test_landing_angle_puts_the_segment_under_the_pointer(self):
        for count in (3, 8):
            for i in range(count):
                for cur in (0.0, 123.0, 700.0):
                    a = W.landing_angle(i, count, cur)
                    self.assertGreaterEqual(a, cur + 360.0 * W.TURNS - 1e-6)
                    r = (a + i * 360.0 / count) % 360.0
                    self.assertAlmostEqual(min(r, 360.0 - r), 0.0, places=6)

    def test_no_decision_no_spin(self):
        self.hub.pick_for = lambda vibe: None
        w = self.pick(style='wheel')
        w.btn.click()
        self.assertEqual(self.spun, [])
        self.assertFalse(w.wheel.spinning)


if __name__ == '__main__':
    unittest.main()
