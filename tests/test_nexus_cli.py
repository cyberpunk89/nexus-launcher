"""Tests for nexus-cli (nexus/cli.py).

Run with:  python3 -m unittest discover -s tests -v

The unit tests exercise the pure helpers directly. The sandbox tests run the
real CLI as a subprocess with HOME pointed at a throwaway directory holding a
fake Steam / Heroic / Proton layout, so nothing on the real machine is touched.
"""

import base64
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / 'bin' / 'nexus-cli'


def load_cli():
    sys.path.insert(0, str(REPO))
    import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
    return importlib.import_module('nexus.cli')


fi = load_cli()
from tests.patching import patch_cli  # noqa: E402

try:
    import vdf
except ImportError:          # Steam sandbox tests need python-vdf
    vdf = None


class LaunchOptionsParsing(unittest.TestCase):
    def test_steam_string(self):
        p = fi.parse_launch_options(
            'PROTON_LOG=1 FOO="a b" gamescope -W 1920 -- mangohud %command% -dx11')
        self.assertEqual(p['env'], [['PROTON_LOG', '1'], ['FOO', 'a b']])
        self.assertEqual(p['wrappers'], ['gamescope', '-W', '1920', '--', 'mangohud'])
        self.assertEqual(p['args'], ['-dx11'])
        self.assertIsNone(p['proton'])

    def test_umu_command_takes_protonpath_and_drops_reserved(self):
        p = fi.parse_launch_options(
            'PROTONPATH=/x/GE-Proton10-1/ WINEPREFIX=/p DXVK_HUD=fps umu-run /g/Game.exe -w')
        self.assertEqual(p['proton'], 'GE-Proton10-1')
        self.assertEqual(p['env'], [['DXVK_HUD', 'fps']])
        self.assertEqual(p['wrappers'], [])
        self.assertEqual(p['args'], ['-w'])

    def test_no_command_marker_follows_steam_semantics(self):
        p = fi.parse_launch_options('PROTON_LOG=1 -windowed')
        self.assertEqual(p['env'], [['PROTON_LOG', '1']])
        self.assertEqual(p['args'], ['-windowed'])

    def test_bad_quotes(self):
        with self.assertRaises(fi.LaunchParseError):
            fi.parse_launch_options('FOO="unterminated %command%')

    def test_format_round_trips(self):
        text = "A=1 B='x y' gamescope -W 1 -- mangohud %command% '-a b'"
        p = fi.parse_launch_options(text)
        self.assertEqual(fi.format_launch_options(p), text)
        self.assertEqual(fi.parse_launch_options(fi.format_launch_options(p)), p)

    def test_empty_profile_formats_empty(self):
        self.assertEqual(fi.format_launch_options(fi.empty_profile()), '')


class HeroicMapping(unittest.TestCase):
    def test_native_toggles_and_wrappers(self):
        p = fi.parse_launch_options(
            'X=1 gamescope -W 1920 -- game-performance gamemoderun mangohud %command% -a')
        h = fi.heroic_launch_fields(p)
        self.assertEqual(h['enviromentOptions'], [{'key': 'X', 'value': '1'}])
        self.assertEqual(h['wrapperOptions'], [{'exe': 'gamescope', 'args': '-W 1920 --'},
                                               {'exe': 'game-performance', 'args': ''}])
        self.assertTrue(h['showMangohud'])
        self.assertTrue(h['useGameMode'])
        self.assertEqual(h['launcherArgs'], '-a')


class ProtongenImport(unittest.TestCase):
    CFG = {'umu': False, 'runtime': 'proton-cachyos-slr',
           'env': [['PROTON_LOG', '1'], ['MANGOHUD_CONFIG', ''], ['WINEPREFIX', '/x']],
           'wrappers': [['mangohud', ''], ['gamescope', '-W 2560'], ['game-performance', '']],
           'extra_env': 'DXVK_STATE_CACHE=1 PROTON_LOG=0', 'game_args': '/Flag:False'}

    def test_config_to_profile(self):
        p = fi.profile_from_protongen_config(self.CFG)
        # empty values and reserved vars dropped; extra_env doesn't override env
        self.assertEqual(p['env'], [['PROTON_LOG', '1'], ['DXVK_STATE_CACHE', '1']])
        # protongen's outer->inner order: gamescope, game-performance, mangohud
        self.assertEqual(p['wrappers'],
                         ['gamescope', '-W', '2560', '--', 'game-performance', 'mangohud'])
        self.assertEqual(p['args'], ['/Flag:False'])
        self.assertEqual(p['proton'], 'proton-cachyos-slr')

    def test_share_code(self):
        blob = json.dumps({'name': 'Mine', 'config': self.CFG}).encode()
        code = fi.PROTONGEN_CODE_PREFIX + base64.b64encode(blob).decode()
        name, p = fi.decode_protongen_code(code[:20] + '\n ' + code[20:])
        self.assertEqual(name, 'Mine')
        self.assertEqual(p['proton'], 'proton-cachyos-slr')
        with self.assertRaises(fi.LaunchParseError):
            fi.decode_protongen_code(fi.PROTONGEN_CODE_PREFIX + 'not-base64!')

    def test_share_code_refuses_code_execution(self):
        def code(cfg):
            blob = json.dumps({'name': 'x', 'config': {**self.CFG, **cfg}}).encode()
            return fi.PROTONGEN_CODE_PREFIX + base64.b64encode(blob).decode()
        for cfg in ({'env': [['LD_PRELOAD', '/tmp/x.so']]},
                    {'env': [['BASH_ENV', '$(id)']]},
                    {'extra_env': 'PYTHONWARNINGS=x BROWSER=sh'},
                    {'wrappers': [['/tmp/evil', '']]}):
            with self.subTest(cfg=cfg), self.assertRaises(fi.LaunchParseError):
                fi.decode_protongen_code(code(cfg))
        # a user's own protongen state is trusted as before
        p = fi.profile_from_protongen_config({'env': [['LD_PRELOAD', 'x.so']]})
        self.assertEqual(p['env'], [['LD_PRELOAD', 'x.so']])

    def test_heroic_id_matches_protongen(self):
        # Vectors taken from a real protongen state.toml (games.rs heroic_app_id).
        self.assertEqual(fi.protongen_heroic_id('g5-RoqR1AZ3roKLPv7o3AF'), 2234504493)
        self.assertEqual(fi.protongen_heroic_id('hWptBInU-uoxxZhosLVSSg'), 3123563496)


class ChildOutput(unittest.TestCase):
    def test_child_cannot_fake_gui_events(self):
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out):
            fi.Logger().write_raw('ok\n@nexus installed {"slug": "x"}\n')
            fi.Logger().write_raw('50%\r@nexus exe-candidates {}\n')
        self.assertNotIn('\n@nexus', out.getvalue())
        self.assertNotIn('\r@nexus', out.getvalue())
        self.assertFalse(out.getvalue().startswith('@nexus'))

    def test_wine_env_drops_secrets(self):
        env = fi.wine_env({'GITHUB_TOKEN': 't', 'NEXUS_GAMEDB_TOKEN': 'g',
                           'STEAMGRIDDB_API_KEY': 'k', 'HOME': '/h'})
        self.assertEqual(env, {'HOME': '/h'})


class SteamRoot(unittest.TestCase):
    def test_finds_debian_and_flatpak_steam(self):
        from nexus import paths
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertEqual(paths.find_steam_root(home), home / '.local/share/Steam')
            deb = home / '.steam/debian-installation'
            (deb / 'steamapps').mkdir(parents=True)
            self.assertEqual(paths.find_steam_root(home), deb)
            flat = home / '.var/app/com.valvesoftware.Steam/.local/share/Steam'
            (flat / 'steamapps').mkdir(parents=True)
            self.assertEqual(paths.find_steam_root(home), deb)       # first one wins
            (home / '.local/share/Steam/steamapps').mkdir(parents=True)
            self.assertEqual(paths.find_steam_root(home), home / '.local/share/Steam')


class FileSafety(unittest.TestCase):
    def test_atomic_write_and_backup_pruning(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'shortcuts.vdf'
            fi.atomic_write(p, b'one')
            os.chmod(p, 0o600)
            fi.atomic_write(p, 'two')
            self.assertEqual(p.read_text(), 'two')
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            for i in range(8):
                (Path(d) / f'shortcuts.vdf.bak-2026010{i}-000000').write_text(str(i))
            (Path(d) / 'shortcuts.vdf.bak-user-copy').write_text('keep me')
            fi.backup_file(p, keep=3)
            baks = sorted(x.name for x in Path(d).glob('shortcuts.vdf.bak-2*'))
            self.assertEqual(len(baks), 3)
            self.assertTrue((Path(d) / 'shortcuts.vdf.bak-user-copy').exists())
            self.assertEqual(sorted(os.listdir(d)).count('.shortcuts.vdf.tmp'), 0)

    def test_backup_keeps_the_state_before_the_first_rewrite(self):
        # One operation rewriting a file several times within a second used to
        # overwrite the pristine backup with an intermediate state.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.vdf'
            p.write_text('original')
            for i in range(8):
                fi.backup_file(p, keep=3)
                fi.atomic_write(p, f'step {i}')
            baks = list(Path(d).glob('config.vdf.bak-*'))
            self.assertEqual([b.read_text() for b in baks], ['original'])
            self.assertRegex(baks[0].name, fi._BACKUP_TS_RE)

    def test_sgdb_refetch_replaces_old_art(self):
        # A refetch that comes back as .png must not leave the old cover.jpg
        # winning sorted(glob)[0], nor pick up a half-downloaded .part file.
        with tempfile.TemporaryDirectory() as d, \
                patch_cli('ARTWORK_DIR', Path(d)):
            art = Path(d) / 'game'
            art.mkdir()
            (art / 'cover.jpg').write_text('old')
            (art / 'hero.aaa.part').write_text('partial')

            def sgdb(path, key):
                return [{'id': 7}] if 'search' in path else [{'url': f'https://cdn/x{path[:3]}.png'}]

            def download(url, dest):
                dest.write_text('new')
                return dest
            with patch_cli('_sgdb_get', sgdb), \
                    patch_cli('_download', download):
                got = fi.fetch_artwork('Game', 'game', {'api_key': 'k'})
            self.assertEqual(got['cover'], art / 'cover.png')
            self.assertEqual(sorted(x.name for x in art.glob('cover.*')), ['cover.png'])
            self.assertEqual(fi._cover_path(art), str(art / 'cover.png'))
            (art / 'hero.png').unlink()
            self.assertNotIn('hero', fi._local_artwork('game'))

    def test_read_cache_follows_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.toml'
            p.write_text('a = 1\n')
            self.assertEqual(fi._cached_read(p, fi._load_toml), {'a': 1})
            p.write_text('a = 22\n')                       # new size: re-read
            self.assertEqual(fi._cached_read(p, fi._load_toml), {'a': 22})
            os.utime(p, ns=(1, 1))                          # same size, new mtime
            p.write_text('a = 33\n')
            self.assertEqual(fi._cached_read(p, fi._load_toml), {'a': 33})

    def test_proton_scan_is_cached_until_a_build_changes(self):
        with tempfile.TemporaryDirectory() as d, \
                patch_cli('PROTON_SEARCH_DIRS', [Path(d)]), \
                patch_cli('_proton_wine_is_native', lambda p: True), \
                patch_cli('_PROTON_SCAN', None):
            def build(name):
                (Path(d) / name).mkdir()
                (Path(d) / name / 'proton').write_text('#!/bin/sh\n')
            build('GE-Proton10-1')
            calls = []
            real_glob = Path.glob
            with mock.patch.object(Path, 'glob',
                                   lambda self, pat: calls.append(pat) or real_glob(self, pat)):
                self.assertEqual(list(fi.list_protons()), ['GE-Proton10-1'])
                fi.find_proton()
                fi.damaged_protons()
                self.assertEqual(calls.count('*/proton'), 1)       # scanned once
                build('GE-Proton10-2')                              # the dir changed
                self.assertEqual(sorted(fi.list_protons()), ['GE-Proton10-1', 'GE-Proton10-2'])
                self.assertEqual(calls.count('*/proton'), 2)
            fi.list_protons().clear()                               # callers get a copy
            self.assertEqual(len(fi.list_protons()), 2)

    def test_split_wrappers(self):
        self.assertEqual(fi.split_wrappers(['gamescope', '-f', '--', 'mangohud', 'strangle', '60']),
                         [['gamescope', '-f', '--'], ['mangohud'], ['strangle', '60']])


class ProtonByName(unittest.TestCase):
    def test_whole_words_only(self):
        p = {'GE-Proton9-20': Path('/x/GE-Proton9-20'),
             'proton-cachyos-slr': Path('/y/proton-cachyos-slr')}
        with patch_cli('proton_tool_name', lambda p: p.name):
            self.assertIsNone(fi.proton_by_name('GE-Proton9-2', p))      # not 9-20
            self.assertEqual(fi.proton_by_name('ge-proton9-20', p).name, 'GE-Proton9-20')
            self.assertEqual(fi.proton_by_name('proton-cachyos', p).name,
                             'proton-cachyos-slr')


class Running(unittest.TestCase):
    def test_heroic_dir_needs_a_separator(self):
        from nexus import steamlib
        self.assertTrue(steamlib.in_dir_cmdline('/g/Foo/', ['wine Z:\\g\\Foo\\a.exe']))
        self.assertFalse(steamlib.in_dir_cmdline('/g/Foo', ['/g/FooBar/a.exe']))


class ExeDetection(unittest.TestCase):
    def test_largest_non_helper_exe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'Game').mkdir()
            (root / 'Game' / 'Game.exe').write_bytes(b'\0' * 900_000)
            (root / 'Game' / 'unins000.exe').write_bytes(b'\0' * 2_000_000)
            (root / 'windows').mkdir()
            (root / 'windows' / 'big.exe').write_bytes(b'\0' * 3_000_000)
            self.assertEqual(fi.find_exe_in_dir(root), root / 'Game' / 'Game.exe')

    def test_detect_game_name(self):
        name, slug = fi.detect_game_name(Path('/x/Cool Game [FitGirl Repack]'))
        self.assertEqual((name, slug), ('Cool Game', 'cool-game'))


# ---------------------------------------------------------------------------
# Sandbox: run the real CLI against a fake HOME
# ---------------------------------------------------------------------------

def steam_running() -> bool:
    return fi.steam_is_running()


@unittest.skipIf(vdf is None, "python-vdf not installed")
def fi_sandbox(test, fn, *a):
    """Call a cli function with its Steam/registry paths pointed at the
    test's fake HOME (module globals are bound at import)."""
    from unittest import mock
    steam = test.steam
    data = test.home / '.local/share/nexus'
    with patch_cli('STEAM_ROOT', steam), \
            patch_cli('REGISTRY_PATH', data / 'games.json'), \
            patch_cli('find_steam_userdata', lambda *a, **k: test.cfg.parent), \
            patch_cli('steam_accounts',
                              lambda: [{'config_dir': test.cfg, 'accountid': 111}]), \
            patch_cli('_steam_libraries', lambda: [steam]):
        return fn(*a)


class CommandTable(unittest.TestCase):
    def test_every_command_flag_exists_and_none_fires_by_default(self):
        parser = fi.build_parser()
        defaults = parser.parse_args(['/some/repack'])
        for when, handler in fi.COMMANDS:
            if not callable(when):
                self.assertTrue(hasattr(defaults, when), when)
            self.assertTrue(callable(handler), when)
            fired = when(defaults) if callable(when) else getattr(defaults, when)
            self.assertFalse(fired, when)             # a bare repack dir means install
        dests = {w for w, _h in fi.COMMANDS if not callable(w)}
        self.assertEqual(len(dests), len([w for w, _h in fi.COMMANDS if not callable(w)]))

    def test_first_flag_wins_and_status_passes_through(self):
        import signal
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        calls = []
        with patch_cli('cmd_doctor', lambda base, as_json: calls.append('doctor')), \
                patch_cli('cmd_prune', lambda yes, as_json: 3), \
                mock.patch('sys.argv', ['nexus-cli', '--doctor', '--prune']):
            with self.assertRaises(SystemExit) as cm:
                fi._main()
        self.assertEqual((calls, cm.exception.code), (['doctor'], 0))
        with patch_cli('cmd_prune', lambda yes, as_json: 3), \
                mock.patch('sys.argv', ['nexus-cli', '--prune']):
            with self.assertRaises(SystemExit) as cm:
                fi._main()
        self.assertEqual(cm.exception.code, 3)


class Slugs(unittest.TestCase):
    def test_symbol_only_names_still_get_a_slug(self):
        for name in ('!!!', '™', ''):
            slug = fi.make_slug(name)
            self.assertTrue(fi.valid_slug(slug), (name, slug))
        self.assertEqual(fi.make_slug('The Witcher® 3: Wild Hunt'), 'the-witcher-3-wild-hunt')

    def test_valid_slug(self):
        for ok in ('end-of-abyss', 'steam-993090', 'heroic-legendary-Snowdrop', 'pokémon'):
            self.assertTrue(fi.valid_slug(ok), ok)
        for bad in ('', '.', '..', '../x', 'a/b', '-x', '_x', 'a b', '.hidden', 'a\0'):
            self.assertFalse(fi.valid_slug(bad), bad)


class UmuGameId(unittest.TestCase):
    CSV = ("TITLE,STORE,CODENAME,UMU_ID,COMMON ACRONYM (Optional),NOTE (Optional)\n"
           "Borderlands 3,egs,Catnip,umu-397540,bl3,\n"
           "Some Game™,gog,1,umu-111,,\n"
           "Some Game,steam,2,umu-222,,\n")

    def lookup(self, name, search=None):
        from nexus import net

        def cached(url, cache_name, max_age, refresh=False, as_json=True, timeout=20):
            if 'umu-database' in url:
                return self.CSV, 0, False
            if search is None:
                raise net.NetError('offline')
            return search, 0, False
        with mock.patch.object(net, 'cached', cached):
            return fi.resolve_umu_gameid(name)

    def test_database_first_steam_entry_preferred(self):
        self.assertEqual(self.lookup('Borderlands 3'), 'umu-397540')
        self.assertEqual(self.lookup('Some Game'), 'umu-222')

    def test_steam_store_exact_title_only(self):
        hit = {'items': [{'id': 5064340, 'name': 'DinoBones: Tidy Up the Museum'}]}
        self.assertEqual(self.lookup('Dinobones: Tidy Up the Museum', hit), 'umu-5064340')
        near = {'items': [{'id': 1, 'name': 'The Witcher 3: Wild Hunt — Remastered'}]}
        self.assertIsNone(self.lookup('The Witcher 3: Wild Hunt', near))
        self.assertIsNone(self.lookup('Unknown Game'))                    # offline

    def test_launch_script_exports_it(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            proton = d / 'GE-Proton10-1' / 'proton'
            out = fi.write_launch_script(d / 'launch.sh', d, d / 'pfx', d / 'g.exe', 'g',
                                         proton, 'G', runner='umu', gameid='umu-222')
            self.assertIn('export GAMEID="${GAMEID:-umu-222}"', out.read_text())
            out = fi.write_launch_script(d / 'launch.sh', d, d / 'pfx', d / 'g.exe', 'g',
                                         proton, 'G', runner='umu', gameid='$(rm -rf /)')
            self.assertIn('export GAMEID="${GAMEID:-umu-0}"', out.read_text())


class SaveData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.users = self.root / 'game/pfx/drive_c/users/steamuser'

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, data=b'x'):
        p = self.users / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_caches_shortcuts_and_logs_are_not_saves(self):
        self.put('AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Game.lnk')
        self.put('AppData/Local/Temp/x.tmp')
        self.put('AppData/Local/D3DSCache/abc/blob')
        self.put('AppData/Local/Game/Saved/Logs/Game.log')
        self.put('AppData/Local/Game/ShaderCache/a.bin')
        self.put('AppData/Local/Game/GPUCache/data_0')
        self.put('Documents/desktop.ini')
        self.assertEqual(fi.find_prefix_savedata(self.root / 'game'), [])
        save = self.put('AppData/Local/Game/Saved/SaveGames/slot1.sav')
        doc = self.put('Documents/My Games/Game/profile.dat')
        self.assertEqual(sorted(fi.find_prefix_savedata(self.root / 'game')),
                         sorted([save, doc]))

    def test_backups_dont_merge_and_restore_reports_failures(self):
        self.put('Documents/My Games/Game/slot1.sav', b'one')
        with patch_cli('SAVES_DIR', self.root / 'saves'):
            a = fi.backup_prefix_saves('game', self.root / 'game')
            b = fi.backup_prefix_saves('game', self.root / 'game')
            self.assertEqual((a[1], a[2]), (1, []))
            self.assertNotEqual(a[0], b[0])          # same second, separate folders
            self.assertEqual(fi.latest_save_backup('game'), b[0])
            # A file where the restore needs a folder: that copy fails.
            target = self.root / 'new/pfx'
            (target / 'drive_c').mkdir(parents=True)
            (target / 'drive_c/users').write_text('in the way')
            n, failed = fi.restore_prefix_saves('game', target)
        self.assertEqual(n, 0)
        self.assertEqual(len(failed), 1)


class Sandbox(unittest.TestCase):
    ACCOUNT = '111'
    OTHER = {'appid': -5, 'AppName': 'Unrelated Game', 'Exe': '"/opt/x.exe"',
             'StartDir': '/opt', 'LaunchOptions': 'KEEP=1 %command%', 'tags': {'0': 'Fav'}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        h = self.home
        self.steam = h / '.local/share/Steam'
        self.cfg = self.steam / 'userdata' / self.ACCOUNT / 'config'
        self.cfg.mkdir(parents=True)
        (self.cfg / 'localconfig.vdf').write_text('"UserLocalConfigStore"\n{\n}\n')
        (self.steam / 'config').mkdir(parents=True)
        (self.steam / 'config' / 'config.vdf').write_text(vdf.dumps(
            {'InstallConfigStore': {'Software': {'Valve': {'Steam': {
                'CompatToolMapping': {}}}}}}, pretty=True))
        self.sc = self.cfg / 'shortcuts.vdf'
        self.sc.write_bytes(vdf.binary_dumps({'shortcuts': {'0': dict(self.OTHER)}}))
        for name, internal in (('GE-Proton10-1', 'GE-Proton10-1'),
                               ('proton-custom', 'custom-internal')):
            d = self.steam / 'compatibilitytools.d' / name
            d.mkdir(parents=True)
            (d / 'proton').write_text('#!/bin/sh\n')
            (d / 'proton').chmod(0o755)
            (d / 'compatibilitytool.vdf').write_text(
                f'"compatibilitytools"\n{{\n "compat_tools"\n {{\n  "{internal}"\n'
                f'  {{\n   "install_path" "."\n  }}\n }}\n}}\n')
        self.heroic = h / '.config/heroic'
        (self.heroic / 'sideload_apps').mkdir(parents=True)
        (self.heroic / 'GamesConfig').mkdir()
        (self.heroic / 'config.json').write_text('{}')
        (self.heroic / 'sideload_apps' / 'library.json').write_text(json.dumps(
            {'games': [{'app_name': 'otherapp', 'title': 'Other', 'runner': 'sideload',
                        'install': {'executable': '/opt/other.exe'}}]}))
        self.game_dir = h / 'Games/FitGirl/test-game'
        exe_dir = self.game_dir / 'pfx/drive_c/Test Game'
        exe_dir.mkdir(parents=True)
        self.exe = exe_dir / 'Test$Game.exe'
        self.exe.write_bytes(b'\0' * 600_000)
        (h / '.config/nexus').mkdir(parents=True)
        (h / '.config/nexus/config.toml').write_text(
            '[integrations]\nsteam = true\ndesktop = false\nheroic = true\n'
            '[runner]\nbackend = "proton"\n')
        data = h / '.local/share/nexus'
        data.mkdir(parents=True)
        (data / 'games.json').write_text(json.dumps({'version': 1, 'games': {'test-game': {
            'slug': 'test-game', 'display_name': 'Test Game', 'exe': str(self.exe),
            'compatdata': str(self.game_dir), 'wine_prefix': str(self.game_dir / 'pfx'),
            'launcher': str(self.game_dir / 'launch.sh'), 'source': 'install',
            'install_base': str(h / 'Games/FitGirl'), 'steam_appid': None,
            'has_desktop': False, 'artwork_dir': None, 'heroic_app_name': None}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, check=True):
        env = {**os.environ, 'HOME': str(self.home), 'STEAMGRIDDB_API_KEY': ''}
        r = subprocess.run([sys.executable, str(CLI), *args], env=env, text=True,
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        if check and r.returncode != 0:
            self.fail(f"{args} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def shortcuts(self):
        with open(self.sc, 'rb') as f:
            return list(vdf.binary_load(f)['shortcuts'].values())

    def registry(self):
        return json.loads((self.home / '.local/share/nexus/games.json')
                          .read_text())['games']['test-game']

    def test_corrupt_shortcuts_is_never_overwritten(self):
        if steam_running():
            self.skipTest("Steam is running")
        self.sc.write_bytes(b'\x00garbage\x08\x08')
        r = self.cli('--steam', 'test-game', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.sc.read_bytes(), b'\x00garbage\x08\x08')

    def test_truncated_shortcuts_still_gives_valid_json(self):
        # python-vdf raises SyntaxError on a file Steam is halfway through
        # rewriting; every --json command must still print JSON.
        self.sc.write_bytes(vdf.binary_dumps({'shortcuts': {'0': dict(self.OTHER)}})[:-7])
        for args in (['--list', '--json'], ['--library', '--json'], ['--running', '--json']):
            r = self.cli(*args)
            json.loads(r.stdout)                    # raises if it isn't JSON

    def test_unreadable_shortcuts_orphan_nothing(self):
        cd = self.steam / 'steamapps' / 'compatdata' / str(0x9000_0001)
        cd.mkdir(parents=True)
        base = self.home / 'Games/FitGirl'
        self.assertEqual([o['appid'] for o in fi_sandbox(self, fi.find_orphan_compatdata, base)],
                         [0x9000_0001])
        self.sc.write_bytes(b'\x00garbage\x08\x08')
        self.assertEqual(fi_sandbox(self, fi.find_orphan_compatdata, base), [])

    def test_json_guard_reports_internal_errors(self):
        r = self.cli('--library', '--json', check=False)
        self.assertEqual(r.returncode, 0)
        # An exception anywhere ends in one JSON error object, not a traceback.
        env = {**os.environ, 'HOME': str(self.home)}
        code = ("import sys; sys.argv=['nexus-cli','--list','--json'];"
                "import nexus.cli.entry as c; c._main=lambda: 1/0; c.main()")
        r = subprocess.run([sys.executable, '-c', code], env=env, text=True,
                           capture_output=True, cwd=str(REPO), timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)['error']['kind'], 'internal')
        self.assertIn("ZeroDivisionError", r.stderr.splitlines()[-1])

    def test_malformed_heroic_entries_are_skipped_not_fatal(self):
        # Valid JSON with nulls where Heroic usually has objects must not crash
        # --heroic / --remove-heroic, and the odd entries are left as they were.
        lib = self.heroic / 'sideload_apps' / 'library.json'
        lib.write_text(json.dumps({'games': [None, 'junk', {'app_name': 'x', 'install': None},
                                             {'app_name': 'otherapp',
                                              'install': {'executable': '/opt/other.exe'}}]}))
        self.cli('--heroic', 'test-game')
        games = json.loads(lib.read_text())['games']
        self.assertEqual(games[:3], [None, 'junk', {'app_name': 'x', 'install': None}])
        self.assertEqual(len(games), 5)
        app = self.registry()['heroic_app_name']
        (self.heroic / 'GamesConfig' / f'{app}.json').write_text('[]')
        self.cli('--heroic', 'test-game')               # a non-object GamesConfig too
        self.cli('--remove-heroic', 'test-game')
        self.assertEqual(len(json.loads(lib.read_text())['games']), 4)

    def test_launch_profile_everywhere_and_preserved_fields(self):
        if steam_running():
            self.skipTest("Steam is running")
        self.cli('--steam', 'test-game')
        self.cli('--heroic', 'test-game')
        ours = [e for e in self.shortcuts() if e['AppName'] == 'Test Game']
        self.assertEqual(len(ours), 1)
        appid = ours[0]['appid'] & 0xFFFFFFFF
        self.assertEqual(self.registry()['steam_appid'], appid)

        # The user edits the shortcut inside Steam...
        data = {'shortcuts': {str(i): e for i, e in enumerate(self.shortcuts())}}
        for e in data['shortcuts'].values():
            if e['AppName'] == 'Test Game':
                e['AppName'] = 'Renamed In Steam'
                e['tags'] = {'0': 'RPG'}
                e['LaunchOptions'] = 'USER=1 %command%'
        self.sc.write_bytes(vdf.binary_dumps(data))
        # ...then re-adds it: matched by appid, user fields kept.
        self.cli('--steam', 'test-game')
        ours = [e for e in self.shortcuts() if (e['appid'] & 0xFFFFFFFF) == appid]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]['tags'], {'0': 'RPG'})
        self.assertEqual(ours[0]['LaunchOptions'], 'USER=1 %command%')
        other = [e for e in self.shortcuts() if e['AppName'] == 'Unrelated Game'][0]
        self.assertEqual(other['LaunchOptions'], 'KEEP=1 %command%')

        # Set a launch profile: it lands in all three launchers.
        opts = 'PROTON_LOG=1 gamemoderun mangohud %command% -dx11'
        self.cli('--set-launch', 'test-game', opts)
        ours = [e for e in self.shortcuts() if (e['appid'] & 0xFFFFFFFF) == appid][0]
        self.assertEqual(ours['LaunchOptions'], opts)
        sh = (self.game_dir / 'launch.sh').read_text()
        self.assertIn("export PROTON_LOG=1", sh)
        self.assertIn("exec gamemoderun mangohud", sh)
        self.assertIn("'" + str(self.exe) + "'", sh)          # $ in the path stays literal
        app = self.registry()['heroic_app_name']
        gc = json.loads((self.heroic / 'GamesConfig' / f'{app}.json').read_text())[app]
        self.assertEqual(gc['enviromentOptions'], [{'key': 'PROTON_LOG', 'value': '1'}])
        self.assertTrue(gc['showMangohud'] and gc['useGameMode'])
        self.assertEqual(gc['launcherArgs'], '-dx11')
        self.assertTrue(gc['enableFsync'])                     # untouched default kept
        lib = json.loads((self.heroic / 'sideload_apps/library.json').read_text())
        self.assertIn('otherapp', [g['app_name'] for g in lib['games']])

        # Pin a Proton whose compat-tool name differs from its directory.
        self.cli('--proton', 'test-game', 'proton-custom')
        cfgvdf = vdf.loads((self.steam / 'config/config.vdf').read_text())
        mapping = cfgvdf['InstallConfigStore']['Software']['Valve']['Steam']['CompatToolMapping']
        self.assertEqual(mapping[str(appid)]['name'], 'custom-internal')
        gc = json.loads((self.heroic / 'GamesConfig' / f'{app}.json').read_text())[app]
        self.assertTrue(gc['wineVersion']['bin'].endswith('proton-custom/proton'))
        self.assertIn('proton-custom', (self.game_dir / 'launch.sh').read_text())

        # Clearing the options clears them everywhere but keeps the Proton pin.
        self.cli('--set-launch', 'test-game', '')
        ours = [e for e in self.shortcuts() if (e['appid'] & 0xFFFFFFFF) == appid][0]
        self.assertEqual(ours['LaunchOptions'], '')
        self.assertEqual(self.registry()['launch']['proton'], 'proton-custom')

        info = json.loads(self.cli('--show-launch', 'test-game', '--json').stdout)
        self.assertEqual(info['proton'], 'proton-custom')

        # Backups are capped.
        for _ in range(8):
            self.cli('--set-launch', 'test-game', f'N={_} %command%')
        self.assertLessEqual(len(list(self.cfg.glob('shortcuts.vdf.bak-*'))), fi.BACKUP_KEEP)

    def test_from_protongen_reads_state_read_only(self):
        if steam_running():
            self.skipTest("Steam is running")
        self.cli('--heroic', 'test-game')
        app = self.registry()['heroic_app_name']
        state = self.home / '.config/protongen/state.toml'
        state.parent.mkdir(parents=True)
        state.write_text(
            f'[game_memory.{fi.protongen_heroic_id(app)}]\n'
            'umu = false\nruntime = "GE-Proton10-1"\n'
            'env = [["PROTON_FSR4_UPGRADE", "1"]]\nwrappers = [["mangohud", ""]]\n'
            'extra_env = ""\ngame_args = ""\n')
        before = state.read_bytes()
        r = self.cli('--from-protongen', 'test-game')
        self.assertIn('Heroic entry', r.stdout)
        self.assertEqual(self.registry()['launch']['env'], [['PROTON_FSR4_UPGRADE', '1']])
        self.assertEqual(state.read_bytes(), before)

    def test_protongen_default_profile(self):
        r = self.cli('--protongen-default', 'test-game', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('no default profile', r.stderr)

        self.cli('--heroic', 'test-game')
        state = self.home / '.config/protongen/state.toml'
        state.parent.mkdir(parents=True)
        # The runtime is named by its compat-tool name; Nexus stores the folder.
        state.write_text(
            '[global_profile]\numu = false\nruntime = "custom-internal"\n'
            'env = [["PROTON_FSR4_UPGRADE", "1"]]\n'
            'wrappers = [["mangohud", ""], ["game-performance", ""]]\n'
            'extra_env = ""\numu_exe = ""\ngame_args = ""\n')
        before = state.read_bytes()
        info = json.loads(self.cli('--show-launch', 'test-game', '--json').stdout)
        self.assertFalse(info['protongen_default']['applied'])

        self.cli('--protongen-default', 'test-game')
        launch = self.registry()['launch']
        self.assertEqual(launch['env'], [['PROTON_FSR4_UPGRADE', '1']])
        self.assertEqual(launch['wrappers'], ['game-performance', 'mangohud'])
        self.assertEqual(launch['proton'], 'proton-custom')
        self.assertEqual(launch['source'], 'protongen default profile')
        self.assertIn('proton-custom', (self.game_dir / 'launch.sh').read_text())
        app = self.registry()['heroic_app_name']
        gc = json.loads((self.heroic / 'GamesConfig' / f'{app}.json').read_text())[app]
        self.assertTrue(gc['showMangohud'])
        info = json.loads(self.cli('--show-launch', 'test-game', '--json').stdout)
        self.assertTrue(info['protongen_default']['applied'])
        self.assertEqual(state.read_bytes(), before)

        # 'all' never overwrites a game that has options of its own.
        self.cli('--set-launch', 'test-game', 'MINE=1 %command%')
        r = self.cli('--protongen-default', 'all')
        self.assertIn('nothing to do', r.stdout)
        self.assertEqual(self.registry()['launch']['env'], [['MINE', '1']])

    def test_uninstall_keeps_going_only_when_safe(self):
        self.cli('--uninstall', 'test-game', '--yes')
        self.assertFalse(self.game_dir.exists())
        self.assertIn('Unrelated Game', [e['AppName'] for e in self.shortcuts()])

    def test_uninstall_refuses_anything_but_a_slug(self):
        base = self.home / 'Games/FitGirl'
        for bad in ('..', '.', 'test-game/..', '../FitGirl'):
            r = self.cli('--uninstall', bad, '--yes', check=False)
            self.assertEqual(r.returncode, 2, (bad, r.stdout, r.stderr))
        self.assertTrue(self.game_dir.is_dir())
        self.assertTrue((self.home / '.local/share/nexus/games.json').exists())
        self.assertTrue(base.is_dir())

    def test_prune_lists_then_removes_games_deleted_by_hand(self):
        import shutil
        shutil.rmtree(self.game_dir)
        data = json.loads(self.cli('--prune', '--json').stdout)
        self.assertEqual([s['slug'] for s in data['stale']], ['test-game'])
        self.assertEqual(data['removed'], [])
        self.assertIn('test-game', json.loads(
            (self.home / '.local/share/nexus/games.json').read_text())['games'])
        data = json.loads(self.cli('--prune', '--yes', '--json').stdout)
        self.assertEqual(data['removed'], ['test-game'])
        self.assertNotIn('test-game', json.loads(
            (self.home / '.local/share/nexus/games.json').read_text())['games'])

    def test_prune_keeps_games_on_an_unmounted_drive(self):
        import shutil
        shutil.rmtree(self.home / 'Games')                   # the whole base is gone
        data = json.loads(self.cli('--prune', '--yes', '--json').stdout)
        self.assertEqual(data['removed'], [])
        self.assertEqual(data['skipped'][0]['slug'], 'test-game')

    def test_uninstall_leaves_a_symlinked_game_folder_alone(self):
        elsewhere = self.home / 'elsewhere'
        self.game_dir.rename(elsewhere)
        self.game_dir.symlink_to(elsewhere)
        self.cli('--uninstall', 'test-game', '--yes')
        self.assertTrue((elsewhere / 'pfx/drive_c/Test Game').is_dir())

    def test_doctor_json_is_structured(self):
        data = json.loads(self.cli('--doctor', '--json').stdout)
        groups = {c['group'] for c in data['checks']}
        self.assertIn('Requirements', groups)
        for c in data['checks']:
            self.assertIn(c['status'], ('ok', 'warn', 'fail', 'info'))
            for a in c['actions']:
                self.assertIsInstance(a['args'], list)

    def test_launch_reports_immediate_crash_and_tracks(self):
        launcher = self.game_dir / 'launch.sh'
        launcher.write_text('#!/bin/bash\necho boom\nexit 3\n')
        r = self.cli('--launch', 'test-game', check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn('exited immediately with code 3', r.stderr)
        self.assertIn('boom', r.stderr)
        self.assertIsNotNone(self.registry().get('last_played'))
        self.assertFalse(self.registry().get('playtime_seconds'))   # a crash isn't play time

    def test_list_json_has_disk_and_play_stats(self):
        data = json.loads(self.cli('--list', '--json').stdout)
        self.assertGreater(data['disk']['total'], 0)
        g = data['games'][0]
        self.assertIn('playtime_minutes', g)
        self.assertIn('last_played', g)

    def test_legacy_dirs_are_migrated_once(self):
        new_data = self.home / '.local/share/nexus'
        old_data = self.home / '.local/share/fitgirl-install'
        new_cfg = self.home / '.config/nexus'
        old_cfg = self.home / '.config/fitgirl-install'
        new_data.rename(old_data)
        new_cfg.rename(old_cfg)
        self.cli('--list', '--json')
        for old, new in ((old_data, new_data), (old_cfg, new_cfg)):
            self.assertTrue(new.is_dir() and not new.is_symlink())
            self.assertTrue(old.is_symlink())
            self.assertEqual(old.resolve(), new.resolve())
        self.assertEqual(self.registry()['slug'], 'test-game')
        self.cli('--list', '--json')                  # idempotent
        self.assertTrue(old_data.is_symlink())

    def test_open_url_only_accepts_launcher_schemes(self):
        for bad in ('file:///etc/passwd', 'https://example.com', 'steam://x; rm -rf ~',
                    'steam://rungameid/440//-console', 'steam://exit', 'heroic://launch/../x'):
            r = self.cli('--open-url', bad, check=False)
            self.assertEqual(r.returncode, 2, bad)

    def test_library_json_merges_sources(self):
        data = json.loads(self.cli('--library', '--json').stdout)
        self.assertEqual([g['source'] for g in data['games']], ['nexus'])
        self.assertTrue(data['disks'])

    def test_library_owned_adds_uninstalled_and_installing_games(self):
        sys.path.insert(0, str(REPO))
        from nexus import gamedb
        apps = self.steam / 'steamapps'
        apps.mkdir(parents=True, exist_ok=True)
        for appid, name, flags, extra in ((10, 'Ten Installed', 4, ''),
                                          (20, 'Twenty Downloading', 1026,
                                           '"BytesDownloaded" "50"\n"BytesToDownload" "200"\n')):
            (apps / f'appmanifest_{appid}.acf').write_text(
                f'"AppState"\n{{\n"appid" "{appid}"\n"name" "{name}"\n'
                f'"StateFlags" "{flags}"\n"installdir" "{name}"\n{extra}}}\n')
        token = 'gdb_' + 'a' * 43
        url = gamedb.check_url(gamedb.DEFAULT_URL)
        cache = self.home / 'cache/nexus/gamedb'
        cache.mkdir(parents=True)
        (cache / 'summary.json').write_text(json.dumps({
            'fingerprint': gamedb._fingerprint(url, token), 'fetched_at': 0,
            'data': {'library': [{'appid': a, 'title': t, 'playtimeMinutes': 5}
                                 for a, t in ((10, 'Ten'), (20, 'Twenty'), (30, 'Thirty'),
                                              (228980, 'Steamworks Common Redistributables'))]}}))
        store = self.heroic / 'store_cache'
        store.mkdir()
        (store / 'legendary_library.json').write_text(json.dumps({'library': [
            {'app_name': 'Inst', 'title': 'Installed Epic', 'is_installed': True,
             'install': {'install_path': '/x', 'install_size': '1 GiB', 'is_dlc': False}},
            {'app_name': 'Own', 'title': 'Owned Epic', 'is_installed': False,
             'install': {'install_size': '2 GiB', 'is_dlc': False}},
            {'app_name': 'Dlc', 'title': 'Some DLC', 'is_installed': False,
             'install': {'is_dlc': True}}]}))
        (self.heroic / 'store').mkdir()
        (self.heroic / 'store' / 'download-manager.json').write_text(json.dumps(
            {'finished': [], 'queue': [{'params': {'appName': 'Own', 'runner': 'legendary'}}]}))
        env = {**os.environ, 'HOME': str(self.home), 'STEAMGRIDDB_API_KEY': '',
               'XDG_CACHE_HOME': str(self.home / 'cache'), 'NEXUS_GAMEDB_TOKEN': token,
               'NEXUS_GAMEDB_URL': ''}

        def library(*extra):
            r = subprocess.run([sys.executable, str(CLI), '--library', '--json', *extra],
                               env=env, text=True, capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return {g['slug']: g for g in json.loads(r.stdout)['games']}
        plain = library()
        self.assertEqual(sorted(plain), ['heroic-legendary-Inst', 'steam-10', 'test-game'])
        self.assertTrue(all(g['installed'] for g in plain.values()))
        games = library('--owned')
        self.assertTrue(games['steam-10']['installed'])
        self.assertEqual(games['steam-20']['installing'],
                         {'done': 50, 'total': 200, 'paused': False})
        self.assertFalse(games['steam-20']['installed'])
        self.assertFalse(games['steam-30']['installed'])
        self.assertEqual(games['steam-30']['install_url'], 'steam://install/30')
        self.assertEqual(games['steam-30']['playtime_minutes'], 5)
        self.assertNotIn('steam-228980', games)                  # a tool, not a game
        own = games['heroic-legendary-Own']
        self.assertFalse(own['installed'])
        self.assertIn('installing', own)                          # in Heroic's queue
        self.assertIsNone(own['disk_bytes'])                      # not on disk yet
        self.assertEqual(own['download_bytes'], 2 << 30)
        self.assertNotIn('heroic-legendary-Dlc', games)
        self.assertTrue(games['heroic-legendary-Inst']['installed'])

    def test_cleanup_quarantine_and_trash(self):
        cd = self.steam / 'steamapps' / 'compatdata'
        stray = cd / '2999999999'
        (stray / 'pfx').mkdir(parents=True)
        (stray / 'pfx' / 'junk').write_bytes(b'x' * 1000)
        sc = self.steam / 'steamapps' / 'shadercache' / '555'
        sc.mkdir(parents=True)
        (sc / 'cache').write_bytes(b'y' * 500)
        data = json.loads(self.cli('--cleanup-scan', '--json').stdout)
        ids = {c['id']: c for c in data['candidates']}
        pid = f"orphan-prefix:{stray}"
        sid = f"shadercache:{sc}"
        self.assertIn(pid, ids)
        self.assertIn(sid, ids)
        self.assertEqual(ids[pid]['action'], 'quarantine')
        # Unknown ids are refused, nothing touched.
        r = self.cli('--cleanup', 'shadercache:/etc', '--yes', check=False)
        self.assertEqual(r.returncode, 1)
        self.cli('--cleanup', pid, sid, '--yes')
        self.assertFalse(stray.exists())
        self.assertFalse(sc.exists())
        q = json.loads(self.cli('--cleanup-scan', '--json').stdout)['quarantine']
        self.assertEqual([x['name'] for x in q], ['2999999999'])
        self.cli('--quarantine-restore', '2999999999')
        self.assertTrue((stray / 'pfx' / 'junk').exists())
        # The shader cache went to the (sandbox) Trash, not into the void.
        trash = self.home / '.local/share/Trash/files'
        self.assertTrue(any(trash.glob('555*')))

    def test_running_json(self):
        data = json.loads(self.cli('--running', '--json').stdout)
        self.assertIsInstance(data['playing'], list)

    def test_corrupt_registry_is_quarantined(self):
        reg = self.home / '.local/share/nexus/games.json'
        reg.write_text('{not json')
        r = self.cli('--list', '--json')
        # Falls back to a filesystem scan instead of an empty library.
        self.assertEqual([g['source'] for g in json.loads(r.stdout)['games']], ['scan'])
        self.assertTrue(list(reg.parent.glob('games.json.corrupt-*')))


if __name__ == '__main__':
    unittest.main()


class SteamGridDB(unittest.TestCase):
    """Variants, picks and art overlays with the network mocked out and every
    path pointed at a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch_cli('ARTWORK_DIR', root / 'art'),
            patch_cli('SGDB_CACHE', root / 'cache'),
            patch_cli('load_config', lambda: {'api_key': 'k'}),
        ]
        for p in self.patches:
            p.start()
        self.calls = []

        def fake_json(path, key):
            self.calls.append(path)
            if path.startswith('/games/steam/'):
                return {'id': 42}, None
            if path.startswith('/search/autocomplete/'):
                return [{'id': 7}], None
            if path.startswith('/heroes/game/42'):
                return [{'id': 1, 'url': 'https://cdn2.steamgriddb.com/hero/a.png',
                         'thumb': 'https://cdn2.steamgriddb.com/hero_thumb/a.jpg',
                         'width': 1920, 'height': 620, 'style': 'alternate'},
                        {'id': 2, 'url': 'https://evil.example/x.png'}], None
            return [], None
        self.patches.append(patch_cli('_sgdb_json', fake_json))

        def fake_download(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'img')
            return dest
        self.patches.append(patch_cli('_download', fake_download))
        for p in self.patches[3:]:
            p.start()
        self.steam = {'slug': 'steam-10', 'source': 'steam', 'appid': 10,
                      'display_name': 'Ten', 'hero': '/steam/librarycache/hero.jpg'}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_game_id_by_appid_then_cached(self):
        self.assertEqual(fi.sgdb_game_id(self.steam, 'k'), (42, None))
        self.assertEqual(fi.sgdb_game_id(self.steam, 'k'), (42, None))
        self.assertEqual(self.calls.count('/games/steam/10'), 1)

    def test_pinned_match_wins_and_reset_clears_art(self):
        d = fi.art_dir_for(self.steam)
        d.mkdir(parents=True)
        (d / 'sgdb.json').write_text('{"id": 99, "name": "Right"}')
        self.assertEqual(fi.sgdb_game_id(self.steam, 'k'), (99, None))
        self.assertEqual(self.calls, [])
        (d / 'hero.png').write_bytes(b'x')
        (d / 'picks.json').write_text('{"hero": {"url": "u"}}')
        with patch_cli('_game_by_id', lambda i: self.steam):
            fi.cmd_set_sgdb('steam-10', None, None, True)
        self.assertFalse((d / 'sgdb.json').exists())
        self.assertFalse((d / 'hero.png').exists())
        self.assertFalse((d / 'picks.json').exists())
        self.assertEqual(fi.sgdb_game_id(self.steam, 'k'), (42, None))

    def test_set_sgdb_pins_id(self):
        with patch_cli('_game_by_id', lambda i: self.steam):
            fi.cmd_set_sgdb('steam-10', 5, 'Five', False)
        self.assertEqual(fi.read_sgdb_match(fi.art_dir_for(self.steam)),
                         {'id': 5, 'name': 'Five'})

    def test_search_shapes_results(self):
        with patch_cli('_sgdb_json', lambda p, k: (
                [{'id': 3, 'name': 'A', 'release_date': 1500000000, 'verified': True},
                 {'name': 'no id'}], None)):
            res = fi.sgdb_search('a b', 'k')
        self.assertEqual(res['items'], [{'id': 3, 'name': 'A', 'year': 2017, 'verified': True}])

    def test_repack_uses_name_not_shortcut_id(self):
        repack = {'slug': 'r', 'source': 'nexus', 'display_name': 'Some Game',
                  'steam_appid': 3000000000}
        self.assertEqual(fi.sgdb_game_id(repack, 'k'), (7, None))
        self.assertTrue(self.calls[0].startswith('/search/autocomplete/'))

    def test_variants_only_cdn_urls(self):
        res = fi.art_variants(self.steam, 'hero', 0, 'k')
        self.assertEqual([v['id'] for v in res['items']], [1])
        self.assertEqual(res['items'][0]['style'], 'alternate')
        fi.art_variants(self.steam, 'hero', 0, 'k')              # cached for a day
        self.assertEqual(sum(c.startswith('/heroes/') for c in self.calls), 1)

    def test_pick_overrides_source_art_and_reset_restores(self):
        from unittest import mock
        with patch_cli('_game_by_id', lambda ident: dict(self.steam)):
            fi.cmd_set_art('steam-10', 'hero', 'https://cdn2.steamgriddb.com/hero/a.png', False)
            d = fi.ARTWORK_DIR / 'steam-10'
            self.assertEqual(json.loads((d / 'picks.json').read_text())['hero']['url'],
                             'https://cdn2.steamgriddb.com/hero/a.png')
            g = dict(self.steam)
            fi.overlay_art(g)
            self.assertEqual(g['hero'], str(d / 'hero.png'))     # pick beats Steam's file
            fi.cmd_set_art('steam-10', 'hero', None, True)
            g = dict(self.steam)
            fi.overlay_art(g)
            self.assertEqual(g['hero'], '/steam/librarycache/hero.jpg')
            self.assertFalse(list(d.glob('hero.*')))

    def test_set_art_refuses_other_hosts(self):
        from unittest import mock
        with patch_cli('_game_by_id', lambda ident: dict(self.steam)), \
                self.assertRaises(SystemExit):
            fi.cmd_set_art('steam-10', 'hero', 'https://evil.example/x.png', False)

    def test_miss_is_remembered(self):
        from unittest import mock
        with patch_cli('_sgdb_json', lambda p, k: (None, 'none')):
            self.assertEqual(fi.sgdb_game_id({'slug': 'x', 'display_name': 'Nope'}, 'k'),
                             (None, 'none'))
        # A second lookup doesn't hit the network for a week.
        with patch_cli('_sgdb_json', side_effect=AssertionError):
            self.assertEqual(fi.sgdb_game_id({'slug': 'x', 'display_name': 'Nope'}, 'k'),
                             (None, 'none'))

    def test_fill_art_fills_only_gaps_once_a_day(self):
        from unittest import mock
        from nexus import steamlib
        bare = {'slug': 'steam-10', 'source': 'steam', 'appid': 10, 'display_name': 'Ten'}
        full = {'slug': 'steam-11', 'source': 'steam', 'appid': 11, 'display_name': 'Eleven',
                'hero_url': 'https://cdn/x', 'logo_url': 'https://cdn/y', 'wide_url': 'https://z'}
        with patch_cli('managed_entries', lambda *a, **k: []), \
                mock.patch.object(steamlib, 'scan', lambda *a, **k: [dict(bare), dict(full)]), \
                mock.patch.object(steamlib, 'heroic_store_games', lambda *a, **k: []), \
                patch_cli('find_heroic_config', lambda: None), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            fi.cmd_fill_art(Path(self.tmp.name), True, True)
            first = json.loads(out.getvalue().splitlines()[-1])
            fi.cmd_fill_art(Path(self.tmp.name), True, True)
            second = json.loads(out.getvalue().splitlines()[-1])
        # Only the bare game's hero exists on SGDB in the fake: 1 image.
        self.assertEqual(first['filled'], 1)
        self.assertTrue((fi.ARTWORK_DIR / 'steam-10' / 'hero.png').exists())
        self.assertFalse((fi.ARTWORK_DIR / 'steam-11').exists())
        self.assertTrue(second['skipped'])
