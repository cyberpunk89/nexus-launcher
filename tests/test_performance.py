"""Per-game performance switches (--perf), lsfg-vk profiles, session tuning
(scx / LACT), pausing a repack and benchmark logs. Everything runs against
temp files, fake processes and mocked commands."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
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
