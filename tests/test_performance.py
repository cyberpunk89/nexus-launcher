"""Per-game performance switches (--perf), lsfg-vk profiles, session tuning
(scx / LACT), pausing a repack and benchmark logs. Everything runs against
temp files, fake processes and mocked commands."""

import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.oskind import linux_only  # noqa: E402
from tests.patching import patch_cli  # noqa: E402
from nexus import cli, session  # noqa: E402

PG_PROFILE = {'proton': 'proton-cachyos-slr',
              'env': [['PROTON_FSR4_UPGRADE', '1'], ['PROTON_USE_OPTISCALER', '1'],
                      ['PROTON_OPTISCALER_CONFIG', 'Menu.ShortcutKey=0xBF'],
                      ['MANGOHUD_CONFIG', 'fps']],
              'wrappers': ['game-performance', 'mangohud'], 'args': [],
              'source': "protongen preset 'X'"}


class Perf(unittest.TestCase):
    def test_state_is_read_from_the_profile(self):
        st = cli.perf_state(PG_PROFILE)
        self.assertEqual(st, {'fps': 0, 'mode': 'game-performance', 'mangohud': True,
                              'wayland': False, 'no_hidraw': False, 'upscaler': 'optiscaler',
                              'gamescope': None})
        self.assertEqual(cli.perf_state(None)['mode'], 'off')

    def test_apply_and_undo(self):
        ch = cli.parse_perf_args(['fps=60', 'mode=gamemoderun', 'wayland=on', 'hidraw=off',
                                  'upscaler=fsr4',
                                  'gamescope=2560x1440@144,fsr,fullscreen,render=1920x1080'])
        new = cli.apply_perf(PG_PROFILE, ch)
        st = cli.perf_state(new)
        self.assertEqual((st['fps'], st['mode'], st['upscaler'], st['wayland'], st['no_hidraw']),
                         (60, 'gamemoderun', 'fsr4', True, True))
        self.assertEqual(st['gamescope']['render_w'], 1920)
        opts = cli.format_launch_options(new)
        # gamescope outermost, then the power mode, mangohud innermost; the
        # OptiScaler-only config goes with OptiScaler; untouched env stays.
        self.assertIn('gamescope -W 2560 -H 1440 -r 144 -w 1920 -h 1080 -F fsr -f -- '
                      'gamemoderun mangohud %command%', opts)
        self.assertNotIn('OPTISCALER', opts)
        self.assertIn('MANGOHUD_CONFIG=fps', opts)
        self.assertEqual(new['proton'], 'proton-cachyos-slr')
        back = cli.apply_perf(new, cli.parse_perf_args(
            ['fps=0', 'mode=off', 'mangohud=off', 'wayland=off', 'hidraw=on', 'upscaler=off',
             'gamescope=off']))
        self.assertEqual(cli.format_launch_options(back), 'MANGOHUD_CONFIG=fps %command%')
        # The round trip through the parser keeps the profile's meaning.
        self.assertEqual(cli.perf_state(cli.parse_launch_options(opts)), st)

    def test_optiscaler_keeps_its_config(self):
        new = cli.apply_perf(PG_PROFILE, {'upscaler': 'optiscaler', 'fps': 90})
        self.assertIn(['PROTON_OPTISCALER_CONFIG', 'Menu.ShortcutKey=0xBF'], new['env'])

    def test_bad_input(self):
        for bad in (['fps=fast'], ['mode=turbo'], ['gamescope=big'], ['nope=1'], ['fps'],
                    ['lsfg=8'], ['gamescope=1920x1080,render=1x1@60']):
            with self.assertRaises(cli.LaunchParseError, msg=bad):
                cli.parse_perf_args(bad)
        self.assertEqual(cli.parse_perf_args(['lsfg=3', 'lsfg=off']), {'lsfg': None})


LSFG = """version = 2

[global]
dll = "/x/Lossless Scaling/"

[[profile]]
active_in = "Other.exe"
multiplier = 2
name = "Mine"

[[profile]]
multiplier = 2
name = "default"
pacing_mode = "vsync"
override_present_mode = true
"""


class Lsfg(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conf = self.tmp / 'conf.toml'
        self.conf.write_text(LSFG)
        for name, val in (('LSFG_CONF', self.conf), ('lsfg_installed', lambda: True)):
            p = patch_cli(name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch('shutil.which', return_value=None)   # no lsfg-vk-cli
        p.start()
        self.addCleanup(p.stop)
        self.entry = {'slug': 'hades', 'display_name': 'Hades',
                      'exe': str(self.tmp / 'Hades.exe')}

    def test_add_change_remove_keeps_the_rest(self):
        self.assertEqual(cli.lsfg_state(self.entry), {})
        cli.lsfg_set(self.entry, 3)
        self.assertEqual(cli.lsfg_state(self.entry)['multiplier'], 3)
        text = self.conf.read_text()
        self.assertIn('active_in = "Hades.exe"', text)
        self.assertIn('pacing_mode = "vsync"', text)          # copied from the default profile
        cli.lsfg_set(self.entry, 2)
        self.assertEqual(self.conf.read_text().count('Nexus: Hades'), 1)
        cli.lsfg_set(self.entry, None)
        self.assertEqual(self.conf.read_text(), LSFG)

    def test_unparseable_file_is_left_alone(self):
        self.conf.write_text('version = [')
        with self.assertRaises(ValueError):
            cli.lsfg_set(self.entry, 2)
        self.assertEqual(self.conf.read_text(), 'version = [')


class Session(unittest.TestCase):
    def _fake(self, outputs):
        calls = []

        def run(cmd, timeout=15):
            calls.append(cmd)
            out = outputs.get(tuple(cmd[:3]), '')
            return subprocess.CompletedProcess(cmd, 0, out, '')
        return calls, run

    def test_starts_then_stops_scx_and_restores_lact(self):
        calls, run = self._fake({('scxctl', 'get'): 'no scx scheduler running',
                                 ('lact', 'cli', 'profile'): 'Default\n'})
        with mock.patch.object(session, '_run', side_effect=run):
            undo = session.enter({'scx': 'lavd', 'lact_profile': 'Gaming'}, say=lambda _m: None)
            session.leave(undo, say=lambda _m: None)
        self.assertIn(['scxctl', 'start', '-s', 'lavd', '-m', 'gaming'], calls)
        self.assertIn(['lact', 'cli', 'profile', 'set', 'Gaming'], calls)
        self.assertEqual(calls[-2:], [['scxctl', 'stop'],
                                      ['lact', 'cli', 'profile', 'set', 'Default']])

    def test_switches_a_running_scheduler_back(self):
        calls, run = self._fake({('scxctl', 'get'): 'running Lavd in PowerSave mode'})
        with mock.patch.object(session, '_run', side_effect=run):
            undo = session.enter({'scx': 'bpfland', 'lact_profile': None}, say=lambda _m: None)
            session.leave(undo, say=lambda _m: None)
        self.assertIn(['scxctl', 'switch', '-s', 'lavd', '-m', 'gaming'], calls)
        self.assertEqual(calls[-1], ['scxctl', 'switch', '-s', 'lavd', '-m', 'powersave'])

    def test_nothing_configured_runs_nothing(self):
        with mock.patch.object(session, '_run') as run:
            self.assertEqual(session.enter({'scx': None, 'lact_profile': None}), {})
        run.assert_not_called()


@linux_only
class Pause(unittest.TestCase):
    def test_pause_and_resume_everything_in_the_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, 'WINEPREFIX': d}
            procs = [subprocess.Popen(['sleep', '30'], env=env) for _ in range(2)]
            self.addCleanup(lambda: [(p.kill(), p.wait()) for p in procs])
            reg = {'games': {'g': {'slug': 'g', 'wine_prefix': d}}}
            with patch_cli('load_registry', return_value=reg):
                cli.cmd_pause(None, 'toggle')
                time.sleep(0.2)
                self.assertTrue(all(cli._proc_state(p.pid) == 'T' for p in procs))
                cli.cmd_pause('g', 'toggle')
                time.sleep(0.2)
                self.assertFalse(any(cli._proc_state(p.pid) == 'T' for p in procs))

    def test_nothing_running(self):
        with patch_cli('load_registry', return_value={'games': {}}), \
                self.assertRaises(SystemExit):
            cli.cmd_pause(None, 'toggle')


def _proc(pid, cmd='', ppid=1, exe='', env=None):
    """A fake platform.processes() record: nothing here is a real process."""
    env = {k.encode(): v.encode() for k, v in (env or {}).items()}
    return SimpleNamespace(pid=pid, ppid=ppid, cmdline=cmd.split() if cmd else [], exe=exe,
                           name='x', environ=lambda: env)


class Stop(unittest.TestCase):
    """--stop: which processes make up a running game, and TERM then KILL.
    The process list and stop_process are fakes; os.kill raises if anything
    ever reaches it."""

    def setUp(self):
        from nexus import platform
        from nexus.cli import running
        self.platform, self.running = platform, running
        self.procs: list = []
        self.sent: list[tuple[int, bool]] = []
        for target, new in ((platform, 'processes'), (platform, 'stop_process'),
                            (platform, 'steam_running_appid'), (os, 'kill')):
            fake = {'processes': lambda: iter(list(self.procs)),
                    'stop_process': lambda pid, force=False: self.sent.append((pid, force))
                    or True,
                    'steam_running_appid': lambda: None,
                    'kill': mock.Mock(side_effect=AssertionError("os.kill reached"))}[new]
            p = mock.patch.object(target, new, fake)
            p.start()
            self.addCleanup(p.stop)
        self.clock = [0.0]
        fake_time = SimpleNamespace(monotonic=lambda: self.clock[0],
                                    sleep=lambda s: self.clock.__setitem__(0, self.clock[0] + s))
        p = mock.patch.object(running, 'time', fake_time)
        p.start()
        self.addCleanup(p.stop)

    def targets(self, g):
        return self.running.stop_targets(g)

    def test_steam_matches_the_exact_appid_only(self):
        self.procs = [
            _proc(100, 'reaper SteamLaunch AppId=12 -- wrapper'), _proc(101, 'game12', 100),
            _proc(200, 'reaper SteamLaunch AppId=123 -- wrapper'), _proc(201, 'wrap', 200),
            _proc(202, 'wine64 Game.exe', 201), _proc(203, 'wineserver', 200),
            _proc(300, 'reaper SteamLaunch AppId=1234 -- wrapper'), _proc(301, 'g', 300),
            _proc(400, 'steam -silent', 1), _proc(401, 'steamwebhelper', 400)]
        self.assertEqual(self.targets({'source': 'steam', 'appid': 123}), {201, 202, 203})
        self.assertEqual(self.targets({'source': 'steam', 'appid': 12}), {101})
        self.assertEqual(self.targets({'source': 'steam', 'appid': 1}), set())
        self.assertEqual(self.targets({'source': 'steam', 'appid': 0}), set())

    def test_steam_on_windows_uses_the_folder_while_steam_says_it_runs(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            self.procs = [_proc(10, exe=os.path.join(d, 'G', 'game.exe')),
                          _proc(11, exe=os.path.join(d, 'Other', 'x.exe'))]
            g = {'source': 'steam', 'appid': 77, 'install_dir': os.path.join(d, 'G')}
            self.assertEqual(self.targets(g), set())          # Steam says nothing runs
            with mock.patch.object(self.platform, 'steam_running_appid', lambda: 77):
                self.assertEqual(self.targets(g), {10})
            with mock.patch.object(self.platform, 'steam_running_appid', lambda: 78):
                self.assertEqual(self.targets(g), set())

    def test_repack_prefix_shortcut_reaper_and_never_the_tracker(self):
        with tempfile.TemporaryDirectory() as d:
            pfx = os.path.realpath(d)
            self.procs = [_proc(10, 'wine64 Game.exe', env={'WINEPREFIX': pfx}),
                          _proc(11, 'wineserver', env={'WINEPREFIX': pfx + '/'}),
                          _proc(12, 'python nexus-cli --track-session g /x/launch.sh',
                                env={'WINEPREFIX': pfx}),
                          _proc(13, 'wine64 Other.exe', env={'WINEPREFIX': pfx + '2'}),
                          _proc(os.getpid(), 'nexus-cli --stop g', env={'WINEPREFIX': pfx}),
                          _proc(20, 'reaper SteamLaunch AppId=3000000001 -- x'),
                          _proc(21, 'proton run', 20)]
            g = {'slug': 'g', 'source': 'nexus', 'wine_prefix': pfx}
            self.assertEqual(self.targets(g), {10, 11})
            self.assertEqual(self.targets({**g, 'steam_appid': 3000000001}), {10, 11, 21})

    def test_repack_on_windows_by_folder(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            self.procs = [_proc(10, exe=os.path.join(d, 'Foo', 'bin', 'game.exe')),
                          _proc(11, exe=os.path.join(d, 'FooBar', 'game.exe'))]
            g = {'slug': 'foo', 'source': 'nexus', 'install_dir': os.path.join(d, 'Foo')}
            self.assertEqual(self.targets(g), {10})

    def test_heroic_by_install_folder_wine_command_lines_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            foo = os.path.join(d, 'Foo')
            self.procs = [_proc(10, f"wine {os.path.join(foo, 'game.exe')}"),
                          _proc(11, f"kate {os.path.join(foo, 'readme.txt')}"),
                          _proc(12, f"wine {os.path.join(d, 'FooBar', 'game.exe')}"),
                          _proc(13, exe=os.path.join(foo, 'native-game'))]
            for src in ('heroic', 'epic'):
                self.assertEqual(self.targets({'source': src, 'install_dir': foo}), {10, 13})
            self.assertEqual(self.targets({'source': 'heroic'}), set())

    def stop(self, g, stays=()):
        """cmd_stop on `g`; after a TERM the process is gone unless it's in
        `stays` (those ignore TERM; KILL ends them unless stays is 'all')."""
        def stop_process(pid, force=False):
            self.sent.append((pid, force))
            if not (stays == 'all' or (pid in stays and not force)):
                self.procs = [p for p in self.procs if p.pid != pid]
            return True
        with mock.patch.object(self.platform, 'stop_process', stop_process), \
                patch_cli('_game_by_id', return_value=g), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
                mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            try:
                self.running.cmd_stop(g['slug'] if g else 'nope')
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()

    def steam_game(self):
        # 202 hangs straight off the reaper: a subreaper adopts what's orphaned.
        self.procs = [_proc(200, 'reaper SteamLaunch AppId=123 -- w'), _proc(201, 'w', 200),
                      _proc(202, 'game', 200)]
        return {'slug': 'steam-123', 'source': 'steam', 'appid': 123,
                'display_name': 'Test Game'}

    def test_term_is_enough(self):
        code, text = self.stop(self.steam_game())
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [(201, False), (202, False)])
        self.assertIn("Stopped 'Test Game'", text)
        self.assertLess(self.clock[0], 1.0)

    def test_kill_after_the_grace(self):
        code, text = self.stop(self.steam_game(), stays={202})
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [(201, False), (202, False), (202, True)])
        self.assertGreaterEqual(self.clock[0], self.running.STOP_GRACE)
        self.assertIn("ended after 10 s", text)

    def test_still_running_after_kill_fails(self):
        code, text = self.stop(self.steam_game(), stays='all')
        self.assertEqual(code, 1)
        self.assertIn("still running", text)

    def test_not_running_and_unknown(self):
        self.procs = [_proc(1, 'init')]
        code, text = self.stop({'slug': 'steam-5', 'source': 'steam', 'appid': 5})
        self.assertEqual(code, 1)
        self.assertIn("isn't running", text)
        self.assertEqual(self.sent, [])
        code, text = self.stop(None)
        self.assertEqual(code, 2)

    def test_cli_flag(self):
        from nexus.cli import entry
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        with patch_cli('cmd_stop') as stop, self.assertRaises(SystemExit) as e, \
                mock.patch('sys.argv', ['nexus-cli', '--stop', 'steam-123']):
            entry._main()
        self.assertEqual(e.exception.code, 0)
        stop.assert_called_once_with('steam-123')
        with mock.patch('sys.argv', ['nexus-cli', '--stop', '../x']), \
                mock.patch('sys.stderr', new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as e:
            entry._main()
        self.assertEqual(e.exception.code, 2)


CSV = """os,cpu,gpu,ram,kernel,driver,cpuscheduler
Linux,Ryzen,RX 9060 XT,32,7.2,Mesa,lavd
--------------------FRAME METRICS--------------------
fps,frametime,cpu_load,gpu_load,elapsed
""" + "".join(f"60,{16.0 if i % 100 else 50.0},10,90,{i * 16_000_000}\n" for i in range(1000))


class Benchmark(unittest.TestCase):
    def test_parse_mangohud_csv(self):
        r = cli.parse_mangohud_csv(CSV)
        self.assertAlmostEqual(r['avg_fps'], 1000 * 1000 / (990 * 16 + 10 * 50), places=0)
        self.assertEqual(r['low_1'], 20.0)                 # the 1% slowest are 50 ms
        self.assertEqual(r['frames'], 1000)
        self.assertIsNone(cli.parse_mangohud_csv("no,header\n1,2\n"))

    def test_launcher_carries_the_hook_and_old_ones_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            sh = cli.write_launch_script(Path(d, 'launch.sh'), None, Path(d, 'pfx'),
                                         Path(d, 'G.exe'), 'g', None, runner='wine')
            self.assertIn(cli.BENCH_HOOK, sh.read_text())
            sh.write_text('#!/bin/bash\nexec wine G.exe\n')
            with patch_cli('_require_entry',
                                   return_value={'slug': 'g', 'launcher': str(sh)}), \
                    self.assertRaises(SystemExit):
                cli.cmd_benchmark(Path(d), 'g', 60, 30)


try:
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ.setdefault('NEXUS_NO_GAMEPAD', '1')
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from nexus import gui as nx
except ImportError:
    nx = None


@unittest.skipIf(nx is None, "PyQt6 not available")
class PerfPanelRoundTrip(unittest.TestCase):
    def test_items_reproduce_the_state(self):
        prof = cli.apply_perf(PG_PROFILE, cli.parse_perf_args(
            ['fps=144', 'gamescope=2560x1440@165,fsr,render=1920x1080', 'wayland=on']))
        st = cli.perf_state(prof)
        panel = nx.PerfPanel()
        panel.set_state(st, {'gamescope': True, 'mangohud': True, 'gamemoderun': True,
                             'game-performance': True}, niri=True, lsfg={'multiplier': 3})
        items = panel.items()
        self.assertIn('lsfg=3', items)
        again = cli.apply_perf(PG_PROFILE, {k: v for k, v in cli.parse_perf_args(items).items()
                                            if k != 'lsfg'})
        self.assertEqual(cli.perf_state(again), st)
        self.assertFalse(panel.ck_hdr.isEnabled())          # niri: no HDR
        panel.set_state({}, {}, lsfg=None)
        self.assertFalse(panel.cb_fg.isEnabled())
        self.assertNotIn('lsfg', ' '.join(panel.items()))
        self.assertIn('gamescope=off', panel.items())
        panel.deleteLater()


if __name__ == '__main__':
    unittest.main()
