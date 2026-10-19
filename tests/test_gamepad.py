"""nexus/gamepad.py must be inert, never crash, when SDL is missing, and its
event struct must match SDL's 56-byte SDL_Event union."""

import ctypes
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import gamepad  # noqa: E402


class Gamepad(unittest.TestCase):
    def test_event_struct_size(self):
        self.assertEqual(ctypes.sizeof(gamepad._Event), 56)

    def test_no_sdl_is_inert(self):
        with mock.patch.object(gamepad, '_load', return_value=None):
            g = gamepad.Gamepads()
        self.assertFalse(g.available)
        self.assertEqual(g.poll(), [])
        g.suspend()
        g.shutdown()
        self.assertIsNone(g.kind)

    def test_button_table_matches_sdl_order(self):
        # SDL_GameControllerButton: A=0 ... DPAD_RIGHT=14
        self.assertEqual(gamepad.BUTTONS[0], 'a')
        self.assertEqual(gamepad.BUTTONS[9:11], ['lb', 'rb'])
        self.assertEqual(gamepad.BUTTONS[11:15], ['up', 'down', 'left', 'right'])

    def test_steam_virtual_pad_is_shadowed_by_a_real_one(self):
        with mock.patch.object(gamepad, '_load', return_value=None):
            g = gamepad.Gamepads()
        g.pads = {1: (None, 'Steam Virtual Gamepad', 'xbox')}
        g.virtual = {1}
        self.assertFalse(g.shadowed(1))              # alone, it's the only pad
        g.pads[2] = (None, 'DualSense', 'ps')
        self.assertTrue(g.shadowed(1))
        self.assertFalse(g.shadowed(2))


class PlayStationMapping(unittest.TestCase):
    def test_circle_is_back_and_cross_is_accept(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            line = gamepad.playstation_mapping('guid', 0x0ce6, module=Path(d))
        fields = dict(f.split(':', 1) for f in line.split(',')[2:] if ':' in f)
        # hid-playstation: button 0 is cross, 1 is circle.
        self.assertEqual((fields['a'], fields['b']), ('b0', 'b1'))
        self.assertEqual((fields['x'], fields['y']), ('b3', 'b2'))
        self.assertEqual((fields['lefttrigger'], fields['rightx']), ('a2', 'a3'))

    def test_only_for_sony_pads_on_that_driver(self):
        from pathlib import Path
        self.assertIsNone(gamepad.playstation_mapping('g', 0x0ce6, module=Path('/nope')))
        self.assertIsNone(gamepad.playstation_mapping('g', 0x1234, module=Path('/')))
        self.assertIsNone(gamepad.playstation_mapping('g', 0x0ce6, vendor=0x045e,
                                                      module=Path('/')))


if __name__ == '__main__':
    unittest.main()
