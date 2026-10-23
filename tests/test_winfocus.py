"""nexus/winfocus.py: picking the launched game's window out of niri's list,
and on Windows (faked: no niri feature) out of platform.top_windows()."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import winfocus  # noqa: E402

WINS = [
    {'id': 1, 'app_id': 'nexus', 'title': 'Nexus'},
    {'id': 2, 'app_id': 'zen', 'title': 'Browser'},
    {'id': 5, 'app_id': 'steam', 'title': 'Steam'},
    {'id': 7, 'app_id': 'steam_app_3669870', 'title': 'CONTROL Resonant'},
    {'id': 8, 'app_id': 'steamwebhelper', 'title': 'Launching…'},
]


class PickWindow(unittest.TestCase):
    def test_steam_game_by_app_id(self):
        g = {'source': 'steam', 'appid': 3669870, 'display_name': 'CONTROL Resonant'}
        self.assertEqual(winfocus.pick_game_window(WINS, {1, 2}, g), 7)

    def test_only_new_windows_and_never_launchers(self):
        g = {'source': 'steam', 'appid': 1, 'display_name': 'X'}
        self.assertIsNone(winfocus.pick_game_window(WINS[:3], {1, 2}, g))   # only steam is new

    def test_repack_by_exe_or_title(self):
        wins = WINS[:2] + [{'id': 9, 'app_id': 'unknown', 'title': 'something'},
                           {'id': 10, 'app_id': '007firstlight.exe', 'title': '007'}]
        g = {'source': 'nexus', 'exe': '/x/007FirstLight.exe', 'display_name': '007 First Light'}
        self.assertEqual(winfocus.pick_game_window(wins, {1, 2}, g), 10)

    def test_newest_unknown_window_as_fallback(self):
        wins = WINS[:2] + [{'id': 11, 'app_id': 'a', 'title': ''},
                           {'id': 12, 'app_id': 'b', 'title': ''}]
        self.assertEqual(winfocus.pick_game_window(wins, {1, 2}, {'display_name': 'Zz'}), 12)

    def test_own_window(self):
        self.assertEqual(winfocus.own_window(WINS), 1)

    def test_own_window_prefers_our_pid(self):
        wins = [{'id': 1, 'app_id': 'nexus', 'pid': 10},
                {'id': 4, 'app_id': 'nexus', 'pid': 20}]
        self.assertEqual(winfocus.own_window(wins, pid=20), 4)
        self.assertIsNone(winfocus.own_window(wins, pid=30))   # never someone else's

    def test_unavailable_is_inert(self):
        from unittest import mock
        with mock.patch.dict('os.environ', {'NIRI_SOCKET': ''}), \
                mock.patch.object(winfocus, 'has', return_value=True):
            self.assertFalse(winfocus.available())
            self.assertEqual(winfocus.windows(), [])
            self.assertFalse(winfocus.focus(3))


class OnWindows(unittest.TestCase):
    """No niri: the window list and focus go through the platform layer."""

    def setUp(self):
        from unittest import mock
        p = mock.patch.object(winfocus, 'has', return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_lists_through_the_platform_and_names_our_own_windows(self):
        from unittest import mock
        import os
        wins = [{'id': 100, 'pid': os.getpid(), 'app_id': 'python', 'title': 'Nexus'},
                {'id': 200, 'pid': 4242, 'app_id': 'hades', 'title': 'Hades',
                 'exe': r'D:\Games\Hades\x64\Hades.exe'}]
        with mock.patch.object(winfocus.platform, 'top_windows', return_value=wins):
            self.assertTrue(winfocus.available())
            got = winfocus.windows()
        self.assertEqual(winfocus.own_window(got, pid=os.getpid()), 100)
        self.assertEqual(got[1]['app_id'], 'hades')

    def test_focus_calls_the_platform(self):
        from unittest import mock
        with mock.patch.object(winfocus.platform, 'focus_window', return_value=True) as f:
            self.assertTrue(winfocus.focus(200))
        f.assert_called_once_with(200)
        with mock.patch.object(winfocus.platform, 'focus_window', side_effect=OSError):
            self.assertFalse(winfocus.focus(200))

    def test_a_window_from_the_install_folder_wins(self):
        wins = [{'id': 1, 'app_id': 'nexus', 'title': 'Nexus'},
                {'id': 30, 'app_id': 'unitycrashhandler64', 'title': 'Crash Handler',
                 'exe': r'C:\Program Files\Unity\UnityCrashHandler64.exe'},
                {'id': 20, 'app_id': 'game', 'title': 'Untitled',
                 'exe': r'D:\Games\Some Game\bin\game.exe'}]
        g = {'display_name': 'Zz', 'install_dir': 'D:/Games/Some Game/'}
        self.assertEqual(winfocus.pick_game_window(wins, {1}, g), 20)


if __name__ == '__main__':
    unittest.main()
