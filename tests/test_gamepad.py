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
from tests.oskind import linux_only, windows_only  # noqa: E402


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


class FakeSDL(mock.MagicMock):
    """Every SDL call succeeds (0) unless a test says otherwise."""

    def _get_child_mock(self, **kw):
        return mock.MagicMock(return_value=0, **kw)


class OnWindows(unittest.TestCase):
    """The Windows settings through faked platform answers and a fake SDL."""

    def _pads(self, hints):
        lib = FakeSDL()
        with mock.patch.object(gamepad, '_load', return_value=lib), \
                mock.patch.object(gamepad.platform, 'sdl_hints', return_value=hints):
            g = gamepad.Gamepads()
        self.addCleanup(g.shutdown)
        return g, lib

    def test_platform_hints_are_set_before_init(self):
        hints = {b"SDL_JOYSTICK_HIDAPI": b"1", b"SDL_JOYSTICK_HIDAPI_PS5_RUMBLE": b"0"}
        g, lib = self._pads(hints)
        self.assertTrue(g.available)
        names = [c for c in lib.mock_calls if c[0] in ('SDL_SetHint', 'SDL_Init')]
        self.assertIn(mock.call.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI", b"1"), names)
        self.assertIn(mock.call.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_PS5_RUMBLE", b"0"), names)
        self.assertEqual(names[-1][0], 'SDL_Init')

    @linux_only
    def test_the_real_hints(self):
        from nexus.platform import linux
        self.assertEqual(linux.sdl_hints(), {b"SDL_JOYSTICK_HIDAPI": b"0"})

    @windows_only
    def test_the_real_windows_hints(self):
        from nexus.platform import windows
        self.assertEqual(windows.sdl_hints(), {b"SDL_JOYSTICK_HIDAPI": b"1",
                                               b"SDL_JOYSTICK_HIDAPI_PS5_RUMBLE": b"0"})

    def test_dll_beside_the_exe_is_tried_first(self):
        with mock.patch.object(gamepad.platform, 'sdl_library_paths',
                               return_value=[r'C:\nexus\SDL2.dll', 'SDL2.dll']), \
                mock.patch.object(gamepad.ctypes, 'CDLL', side_effect=OSError) as cdll:
            self.assertIsNone(gamepad._load())
        self.assertEqual(cdll.call_args_list[0], mock.call(r'C:\nexus\SDL2.dll'))

    def test_battery_from_sdl_where_sysfs_has_none(self):
        g, lib = self._pads({})
        g.pads = {1: ('h1', 'DualSense', 'ps'), 2: ('h2', 'Xbox', 'xbox'),
                  3: ('h3', 'Steam Virtual Gamepad', 'xbox'), 4: ('h4', 'Wired', 'xbox')}
        g.virtual = {3}
        levels = {'h1': 2, 'h2': 1, 'h3': 0, 'h4': 4}
        lib.SDL_GameControllerGetJoystick.side_effect = lambda h: h
        lib.SDL_JoystickCurrentPowerLevel.side_effect = lambda j: levels[j]
        self.assertTrue(g.steam_input)
        with mock.patch.object(gamepad, 'POWER_SUPPLY', Path('/nonexistent')):
            got = gamepad.battery(gamepad.POWER_SUPPLY)
        self.assertEqual(got, [{'percent': 20, 'charging': False},
                               {'percent': 70, 'charging': False}])
        g.shutdown()
        self.assertEqual(gamepad.battery(Path('/nonexistent')), [])



class HotPlug(unittest.TestCase):
    """ADDED carries the pad's GUID and whether it is a real hot-plug: not
    SDL listing what was there at start or resume, not a suspended pad coming
    back, not Steam's virtual pad."""

    GUIDS = {0: '030000004c050000e60c000011810000', 1: '03000000de280000ff11000001000000',
             2: '030000005e040000130b000011050000'}

    def setUp(self):
        self.now = 1000.0
        p = mock.patch.object(gamepad.time, 'monotonic', lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        self.lib = lib = FakeSDL()
        self.queue = []
        self.vendor = {}

        def poll(ref):
            if not self.queue:
                return 0
            ev = ref._obj
            ev.type, ev.which = self.queue.pop(0)
            return 1
        lib.SDL_PollEvent.side_effect = poll
        lib.SDL_JoystickGetDeviceGUID.side_effect = lambda i: i
        lib.SDL_JoystickGetGUIDString.side_effect = \
            lambda i, buf, n: setattr(buf, 'value', self.GUIDS[i].encode())
        lib.SDL_GameControllerOpen.side_effect = lambda i: 100 + i
        lib.SDL_GameControllerGetJoystick.side_effect = lambda h: h
        lib.SDL_JoystickInstanceID.side_effect = lambda j: j - 100
        lib.SDL_GameControllerName.return_value = b'Pad'
        lib.SDL_GameControllerGetVendor.side_effect = lambda h: self.vendor.get(h, (0, 0))[0]
        lib.SDL_GameControllerGetProduct.side_effect = lambda h: self.vendor.get(h, (0, 0))[1]
        with mock.patch.object(gamepad, '_load', return_value=lib), \
                mock.patch.object(gamepad.platform, 'sdl_hints', return_value={}):
            self.g = gamepad.Gamepads()
        self.addCleanup(self.g.shutdown)

    def add(self, index):
        self.queue.append((gamepad.CONTROLLERDEVICEADDED, index))
        return [e for e in self.g.poll() if e[0] == 'added']

    def remove(self, iid):
        self.queue.append((gamepad.CONTROLLERDEVICEREMOVED, iid))
        return self.g.poll()

    def test_added_carries_guid_and_first_enumeration_is_not_hotplug(self):
        self.assertEqual(self.add(0), [('added', 'Pad', 'xbox', self.GUIDS[0], False)])
        self.assertEqual(self.g.guids, {0: self.GUIDS[0]})
        self.assertEqual(self.g.physical, ['Pad'])
        self.now += 3                                       # past the settle window
        self.assertEqual(self.remove(0), [('removed', 'Pad')])
        self.assertEqual(self.g.guids, {})
        self.assertEqual(self.add(0)[0][3:], (self.GUIDS[0], True))

    def test_suspend_resume_same_pad_is_not_hotplug_a_new_one_is(self):
        self.add(0)
        self.now += 5
        self.g.suspend()
        self.g.resume()
        self.assertEqual(self.add(0)[0][4], False)          # re-listed in the settle window
        self.now += 3
        self.assertEqual(self.add(2)[0][4], True)           # a new pad, after it
        self.g.suspend()
        self.g.resume()
        self.now += 4                                       # a late Bluetooth reconnect
        self.assertEqual(self.add(0)[0][4], False)
        self.assertEqual(self.add(2)[0][4], False)

    def test_each_remembered_guid_matches_once_and_expires(self):
        self.add(0)
        self.now += 5
        self.g.suspend()
        self.g.resume()
        self.now += 3
        self.assertEqual(self.add(0)[0][4], False)          # the remembered one
        self.remove(0)
        self.assertEqual(self.add(0)[0][4], True)           # used up: this one is new
        self.g.suspend()
        self.g.resume()
        self.now += 11                                      # remembered GUIDs expired
        self.assertEqual(self.add(0)[0][4], True)

    def test_steam_virtual_pad_is_never_hotplug(self):
        self.now += 3
        self.vendor[101] = gamepad.STEAM_VIRTUAL
        self.assertEqual(self.add(1)[0][4], False)
        self.assertEqual(self.g.physical, [])
        self.assertEqual(self.add(0)[0][4], True)
        self.assertEqual(self.g.physical, ['Pad'])


if __name__ == '__main__':
    unittest.main()
