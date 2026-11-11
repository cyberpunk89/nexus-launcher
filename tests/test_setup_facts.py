"""nexus-cli --setup-facts (w-103): what Setup shows in place, read-only."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from unittest import mock  # noqa: E402
from nexus import features, steamlib, torrent  # noqa: E402
from nexus.cli import common, setupfacts  # noqa: E402
import tests.sandbox  # noqa: E402
from tests.oskind import linux_only  # noqa: E402

try:
    import vdf
except ImportError:
    vdf = None


class Env(unittest.TestCase):
    """setup_facts against a temp tree: no real Steam, Heroic, Proton, client
    or drive is read."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.base = self.tmp / 'Games' / 'FitGirl'            # not created
        self.steam = self.tmp / 'Steam'
        self.cfg = self.tmp / 'config.toml'
        self.tor = {'client': 'transmission', 'url': 'http://127.0.0.1:1', 'username': '',
                    'password': '', 'download_dir': None, 'seed_ratio': None}
        for p in (mock.patch.object(setupfacts, 'STEAM_ROOT', self.steam),
                  mock.patch.object(setupfacts, 'CONFIG_PATH', self.cfg),
                  mock.patch.object(setupfacts, 'find_heroic_config', return_value=None),
                  mock.patch.object(setupfacts.shutil, 'which', return_value='/usr/bin/x'),
                  mock.patch.object(setupfacts, 'find_proton', return_value=Path('/p/proton')),
                  mock.patch.object(setupfacts, 'damaged_protons', return_value={}),
                  mock.patch.object(torrent, 'settings', side_effect=lambda: dict(self.tor)),
                  mock.patch.object(torrent, 'detect', return_value=None),
                  mock.patch.object(features, 'FEATURES', features.ALL)):
            p.start()
            self.addCleanup(p.stop)

    def facts(self):
        return setupfacts.setup_facts(self.base)


class Shape(Env):
    @linux_only
    def test_linux_shape_and_blockers(self):
        with mock.patch.object(setupfacts.shutil, 'which', return_value=None), \
                mock.patch.object(setupfacts, 'find_proton', return_value=None), \
                mock.patch.object(setupfacts, 'damaged_protons',
                                  return_value={'GE-Proton9-1': 'files changed',
                                                'Odd-1': 'files changed'}), \
                mock.patch.object(setupfacts, 'proton_repair_tag',
                                  side_effect=lambda n: 'GE-Proton9-1' if n.startswith('GE') else None):
            f = self.facts()
        self.assertEqual(set(f), {'edition', 'default_base', 'drives', 'torrent', 'needs',
                                  'repacks_was'})
        self.assertEqual(f['edition'], 'linux')
        self.assertEqual(f['default_base'], str(self.base))
        self.assertEqual([d['default'] for d in f['drives']][:1], [True])
        d = f['drives'][0]
        self.assertTrue({'id', 'path', 'fs', 'free', 'total', 'folder', 'steam_games',
                         'warnings'} <= set(d))
        self.assertEqual(d['folder'], str(self.base))
        by = {n['title']: n for n in f['needs']}
        self.assertEqual(list(by), ['Wine', 'Proton', 'GE-Proton9-1 is damaged',
                                    'Odd-1 is damaged'])
        self.assertEqual(by['Wine']['status'], 'fail')
        self.assertEqual(by['Wine']['actions'], [])
        self.assertEqual(by['Proton']['actions'][0]['args'], ['--proton-install', 'latest'])
        self.assertEqual(by['GE-Proton9-1 is damaged']['actions'][0]['args'],
                         ['--proton-install', 'GE-Proton9-1', '--force'])
        self.assertEqual(by['Odd-1 is damaged']['actions'], [])
        self.assertFalse(any(a['steam'] for n in f['needs'] for a in n['actions']))
        json.dumps(f)

    @linux_only
    def test_nothing_missing_gives_no_needs(self):
        self.assertEqual(self.facts()['needs'], [])

    def test_repacks_was_from_config(self):
        self.assertIsNone(self.facts()['repacks_was'])
        self.cfg.write_text('[repacks]\nenabled = false\n')
        self.assertIs(self.facts()['repacks_was'], False)
        self.cfg.write_text('[repacks]\nenabled = true\n')
        self.assertIs(self.facts()['repacks_was'], True)
        self.cfg.write_text('[repacks]\nfoo = 1\n')
        self.assertIs(self.facts()['repacks_was'], True)


class Libs(Env):
    def lib(self, name, ids):
        lib = self.tmp / name
        (lib / 'steamapps').mkdir(parents=True)
        for i in ids:
            (lib / 'steamapps' / f'appmanifest_{i}.acf').write_text(vdf.dumps({'AppState': {
                'appid': str(i), 'name': f'G{i}', 'StateFlags': '4', 'installdir': f'G{i}'}}))
        return lib

    def steam_libs(self, *libs):
        (self.steam / 'steamapps').mkdir(parents=True, exist_ok=True)
        (self.steam / 'steamapps' / 'libraryfolders.vdf').write_text(vdf.dumps(
            {'libraryfolders': {str(i): {'path': str(p)} for i, p in enumerate(libs)}}))


@unittest.skipIf(vdf is None, "python-vdf not installed")
class Drives(Libs):
    def test_dedup_by_drive_and_steam_counts(self):
        a, b = self.lib('libA', [1, 2]), self.lib('libB', [3])
        self.steam_libs(a, b)
        drives = self.facts()['drives']
        self.assertEqual(len(drives), 1)             # all under one temp dir, one drive
        self.assertTrue(drives[0]['default'])
        self.assertEqual(drives[0]['steam_games'], 3)
        self.assertGreater(drives[0]['total'], 0)
        self.assertFalse(self.base.exists())

    def test_other_drive_gets_games_folder(self):
        a = self.lib('libA', [1])
        self.steam_libs(a)
        real = setupfacts.drive_of

        def fake(path):
            got = real(path)
            if got and str(path).startswith(str(a)):
                return {'id': 'other', 'mount': str(a)}
            return got
        with mock.patch.object(setupfacts, 'drive_of', fake):
            drives = self.facts()['drives']
        self.assertEqual([d['id'] == 'other' for d in drives], [False, True])
        self.assertEqual(drives[1]['folder'], str(a / 'Games'))
        self.assertFalse(drives[1]['default'])
        self.assertEqual(drives[1]['steam_games'], 1)

    def test_slow_drive_reported_not_waited_on(self):
        a = self.lib('libA', [1])
        self.steam_libs(a)
        real, gate = setupfacts.drive_of, threading.Event()
        self.addCleanup(gate.set)

        def fake(path):
            if str(path).startswith(str(a)):
                gate.wait(10)
            got = real(path)
            return got and dict(got, mount='/')       # not a narrower mount: nothing hides it
        t0 = time.monotonic()
        with mock.patch.object(setupfacts, 'drive_of', fake), \
                mock.patch.object(setupfacts, 'DRIVE_TIMEOUT', 0.3):
            drives = self.facts()['drives']
        self.assertLess(time.monotonic() - t0, 3)
        slow = [d for d in drives if d['free'] is None]
        self.assertEqual(len(slow), 1)
        self.assertIn("didn't answer within", slow[0]['warnings'][0])
        self.assertTrue(drives[0]['default'])
        self.assertIsNotNone(drives[0]['free'])


class Timeout(unittest.TestCase):
    def test_call_timeout_tells_failure_from_result(self):
        self.assertEqual(common.call_timeout(lambda: None, 1), (True, None))
        ok, why = common.call_timeout(lambda: 1 / 0, 1)
        self.assertFalse(ok)
        self.assertIn('ZeroDivisionError', why)
        gate = threading.Event()
        self.addCleanup(gate.set)
        ok, why = common.call_timeout(lambda: gate.wait(5), 0.1)
        self.assertEqual((ok, why), (False, "didn't finish within 0.1 s"))

    def test_slow_call_returns_default_fast(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        t0 = time.monotonic()
        out = common.with_timeout(lambda: gate.wait(5) and 'late', 0.1, 'dflt')
        self.assertEqual(out, 'dflt')
        self.assertLess(time.monotonic() - t0, 2)

    def test_fast_call_and_errors(self):
        self.assertEqual(common.with_timeout(lambda: 7, 1, None), 7)
        def boom():
            raise OSError('x')
        self.assertEqual(common.with_timeout(boom, 1, 'd'), 'd')


class Torrent(unittest.TestCase):
    QB = {'client': 'qbittorrent', 'url': 'http://127.0.0.1:1', 'username': '', 'password': ''}

    def _info(self, prefs):
        def req(self, path, form=None, **k):
            if path == '/app/version':
                return 200, {}, b'v5.0'
            if path == '/transfer/info':
                return 200, {}, b'{}'
            if path == '/app/preferences':
                return prefs
            raise AssertionError(path)
        with mock.patch.object(torrent.QBittorrent, '_req', req):
            return torrent.QBittorrent(self.QB).info()

    def test_qbittorrent_info_reads_save_path(self):
        got = self._info((200, {}, json.dumps({'save_path': '/dl/qb'}).encode()))
        self.assertEqual(got['download_dir'], '/dl/qb')

    def test_qbittorrent_preferences_refused_gives_none(self):
        self.assertIsNone(self._info((403, {}, b'Forbidden'))['download_dir'])


class TorrentFacts(Env):
    def info(self, **kw):
        info = {'client': 'Transmission', 'version': '4', 'download_dir': '/dl',
                'down': 0, 'up': 0}
        return mock.patch.object(torrent, 'client', return_value=mock.Mock(
            info=mock.Mock(**kw or {'return_value': info})))

    def test_configured(self):
        with self.info():
            t = self.facts()['torrent']
        self.assertEqual(t, {'client': 'transmission', 'url': 'http://127.0.0.1:1',
                             'configured': True, 'download_dir': '/dl', 'error': None})

    def test_detected_when_unconfigured(self):
        self.tor.update(client=None, url='')
        with mock.patch.object(torrent, 'detect',
                               return_value={'client': 'qbittorrent', 'url': 'http://127.0.0.1:8080'}), \
                self.info() as made:
            t = self.facts()['torrent']
        self.assertEqual((t['client'], t['url'], t['configured']),
                         ('qbittorrent', 'http://127.0.0.1:8080', False))
        self.assertEqual(made.call_args.args[0]['client'], 'qbittorrent')

    def test_none_when_nothing_answers(self):
        self.tor.update(client=None, url='')
        self.assertIsNone(self.facts()['torrent'])

    def test_error_keeps_the_dir_from_config(self):
        self.tor['download_dir'] = '/cfg'
        with self.info(side_effect=torrent.TorrentError('refused')):
            t = self.facts()['torrent']
        self.assertEqual((t['error'], t['download_dir']), ('refused', '/cfg'))

    def test_slow_client_times_out(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        t0 = time.monotonic()
        with self.info(side_effect=lambda: gate.wait(10)), \
                mock.patch.object(setupfacts, 'TORRENT_TIMEOUT', 0.3):
            t = self.facts()['torrent']
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("didn't answer", t['error'])


class Windows(Env):
    def test_winfeat_no_wine_rows_long_paths_free_space(self):
        with mock.patch.object(features, 'FEATURES', features.WINDOWS), \
                mock.patch.object(setupfacts.platform, 'long_paths_enabled', return_value=False), \
                mock.patch.object(setupfacts.shutil, 'which', return_value=None), \
                mock.patch.object(setupfacts, 'LOW_SPACE', 1e30):
            f = self.facts()
        self.assertEqual(f['edition'], 'windows')
        by = {n['title']: n for n in f['needs']}
        self.assertEqual(list(by), ['Long paths', 'Free space'])
        self.assertEqual(by['Long paths']['status'], 'warn')
        self.assertEqual(by['Long paths']['actions'][0]['args'],
                         ['--open-url', setupfacts.LONG_PATHS_HELP])
        self.assertEqual(by['Free space']['actions'][0]['args'],
                         ['--open-url', 'ms-settings:storagesense'])

    def test_winfeat_all_fine_gives_nothing(self):
        with mock.patch.object(features, 'FEATURES', features.WINDOWS), \
                mock.patch.object(setupfacts.platform, 'long_paths_enabled', return_value=True), \
                mock.patch.object(setupfacts, 'LOW_SPACE', 0):
            self.assertEqual(self.facts()['needs'], [])


class Failures(Env):
    def test_needs_failure_is_a_warn_row_not_silence(self):
        with mock.patch.object(setupfacts, '_needs', side_effect=OSError('boom')):
            needs = self.facts()['needs']
        self.assertEqual(len(needs), 1)
        self.assertEqual((needs[0]['status'], needs[0]['title'], needs[0]['fix']),
                         ('warn', "Some checks didn't finish", 'Open Health for the full list'))
        self.assertIn('boom', needs[0]['detail'])

    def test_needs_timeout_says_so(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(setupfacts, '_needs', side_effect=lambda d: gate.wait(10)), \
                mock.patch.object(setupfacts, 'GATHER_TIMEOUT', 0.2):
            needs = self.facts()['needs']
        self.assertIn("didn't finish within", needs[0]['detail'])

    def test_torrent_settings_error_is_reported(self):
        with mock.patch.object(torrent, 'settings', side_effect=ValueError('bad toml')):
            t = self.facts()['torrent']
        self.assertIn('bad toml', t['error'])
        self.assertNotIn("didn't answer", t['error'])

    def test_detect_error_is_reported(self):
        self.tor.update(client=None, url='')
        with mock.patch.object(torrent, 'detect', side_effect=RuntimeError('nope')):
            t = self.facts()['torrent']
        self.assertIn('nope', t['error'])

    def test_gathering_timeout_is_visible(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(setupfacts, '_candidates', side_effect=lambda b: gate.wait(10)), \
                mock.patch.object(setupfacts, 'GATHER_TIMEOUT', 0.2):
            drives = self.facts()['drives']
        self.assertTrue(drives[0]['default'])
        self.assertTrue(any("couldn't list" in w for d in drives for w in d['warnings']))

    def test_slow_warnings_are_flagged(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(setupfacts.platform, 'install_base_warnings',
                               side_effect=lambda p: gate.wait(10)), \
                mock.patch.object(setupfacts, 'DRIVE_TIMEOUT', 0.5):
            t0 = time.monotonic()
            drives = self.facts()['drives']
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(drives[0]['warnings'], ["couldn't check this folder in time"])
        self.assertIsNotNone(drives[0]['free'])


class BaseWritable(unittest.TestCase):
    def test_windows_probes_with_a_scratch_file(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(common.nxplatform, 'OS_NAME', 'windows'), \
                mock.patch.object(common.os, 'access', side_effect=AssertionError('ACLs')):
            base = Path(d) / 'Games' / 'x'
            self.assertTrue(common.base_writable(base))
            self.assertEqual(list(Path(d).iterdir()), [])        # nothing left behind
            with mock.patch.object(tempfile, 'TemporaryFile',
                                   side_effect=PermissionError('acl')):
                self.assertFalse(common.base_writable(base))


@unittest.skipIf(vdf is None, "python-vdf not installed")
class DeadDrives(Libs):
    def test_dead_library_is_one_timed_out_entry(self):
        a = self.lib('libA', [1])
        self.steam_libs(a, a)                         # named twice in the vdf
        real_isdir, gate = Path.is_dir, threading.Event()
        self.addCleanup(gate.set)

        def isdir(p):
            if p == a:
                gate.wait(10)
            return real_isdir(p)
        real = setupfacts.drive_of
        t0 = time.monotonic()
        with mock.patch.object(Path, 'is_dir', isdir), \
                mock.patch.object(setupfacts, 'DRIVE_TIMEOUT', 0.3), \
                mock.patch.object(setupfacts, 'drive_of',
                                  lambda p: dict(real(p), mount='/')):
            drives = self.facts()['drives']
        self.assertLess(time.monotonic() - t0, 3)
        stuck = [d for d in drives if str(d['id']).startswith('timeout:')]
        self.assertEqual([d['id'] for d in stuck], [f'timeout:{a}'])
        self.assertIn("didn't answer", stuck[0]['warnings'][0])

    def test_stuck_candidate_under_a_resolved_mount_is_skipped(self):
        a = self.lib('libA', [1])
        self.steam_libs(a)
        real, gate = setupfacts.drive_of, threading.Event()
        self.addCleanup(gate.set)

        def fake(path):
            if str(path).startswith(str(a)):
                gate.wait(10)
            return dict(real(path), mount=str(self.tmp))      # base resolves to a narrow mount
        with mock.patch.object(setupfacts, 'drive_of', fake), \
                mock.patch.object(setupfacts, 'DRIVE_TIMEOUT', 0.3):
            drives = self.facts()['drives']
        self.assertEqual(len(drives), 1)
        self.assertTrue(drives[0]['default'])


class NoMkdir(Env):
    def test_setup_facts_creates_no_folder(self):
        self.facts()
        self.assertFalse(self.base.exists())
        self.assertFalse(self.base.parent.exists())

    def test_base_writable_checks_nearest_parent_only(self):
        self.assertTrue(common.base_writable(self.base))
        self.assertFalse(self.base.exists())

    @linux_only
    def test_unwritable_parent_is_not_writable(self):
        # /proc is read-only on Linux; on Windows the nearest parent is C:\ (writable as admin)
        self.assertFalse(common.base_writable(Path('/proc/nexus-nope/Games')))

    def test_doctor_checks_creates_no_folder(self):
        from nexus import cli
        with mock.patch.object(cli.doctor, 'load_config', return_value={}), \
                mock.patch.object(torrent, 'detect', return_value=None), \
                mock.patch.object(torrent, 'installed_clients', return_value=[]):
            for edition in (features.ALL, features.WINDOWS):
                with mock.patch.object(features, 'FEATURES', edition):
                    checks = cli.doctor_checks(self.base)
                self.assertFalse(self.base.parent.exists(), edition)
                row = next(c for c in checks if c['title'] == 'Install folder')
                self.assertEqual(row['status'], 'ok')


class Cli(unittest.TestCase):
    def test_cli_json_in_fresh_home(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cfg = tests.sandbox.config_dir(home)
            cfg.mkdir(parents=True)
            (cfg / 'config.toml').write_text(      # port 1: no real client is ever probed
                '[torrent]\nclient = "transmission"\nurl = "http://127.0.0.1:1"\n')
            env = {**os.environ, **tests.sandbox.home_env(home), 'STEAMGRIDDB_API_KEY': ''}
            r = subprocess.run([sys.executable, str(REPO / 'bin' / 'nexus-cli'),
                                '--setup-facts', '--json'], env=env, text=True,
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out['torrent']['client'], 'transmission')
            self.assertTrue(out['torrent']['error'])
            self.assertTrue(out['torrent']['configured'])
            self.assertTrue(out['drives'][0]['default'])
            self.assertFalse((home / 'Games').exists())          # nothing created
            text = subprocess.run([sys.executable, str(REPO / 'bin' / 'nexus-cli'),
                                   '--setup-facts'], env=env, text=True, capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertIn('Install folder:', text.stdout)

    def test_flag_is_not_gated(self):
        from nexus.cli import entry
        self.assertNotIn('setup_facts', {d for ds in entry.GATED.values() for d in ds})


if __name__ == '__main__':
    unittest.main()
