"""The Windows install flow (cli/install_win.py) in-process, with the
feature set of the Windows build and a fake elevated setup.exe that
"installs" a game wherever Inno's /DIR points. Runs on Linux too: only the
UAC prompt, the .lnk writer and Defender are faked. The real calls are
covered by test_platform's windows_only tests."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.oskind import linux_only, windows_only  # noqa: E402
from nexus import features, platform, saves  # noqa: E402
from nexus import cli as fi  # noqa: E402
from nexus.cli import entry  # noqa: E402
from nexus.cli import install_win as IW  # noqa: E402
from nexus.cli import savedata  # noqa: E402


MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">
  <Identity Name="39EA002F.FINALFANTASY15POCKETEDITION"
            Publisher="CN=83076AC8-252A-4004-9255-A4A444BA69F5" Version="1.4.524.2" />
  <Applications>
    <Application Id="App" Executable="Game.exe" EntryPoint="Project.App" />
  </Applications>
</Package>
"""
FF15 = '39EA002F.FINALFANTASY15POCKETEDITION_n746a19ndrrjg!App'
LATER = '2999-01-01T00:00:00'        # a Defender detection after any setup start


class FakeSetup:
    """What platform.run_elevated returns: the "installer" has already done
    its work by the first poll()."""

    def __init__(self, exe, args, cwd=None, *, make_game=True, rc=0, log_text=None, uwp=False):
        self.args = args
        opts = dict(a.split('=', 1) for a in args if '=' in a)
        game = Path(opts['/DIR'])
        if make_game:
            (game / 'bin').mkdir(parents=True, exist_ok=True)
            (game / 'bin' / 'TestGame.exe').write_bytes(b'\0' * 900_000)
            (game / 'unins000.exe').write_bytes(b'\0' * 700_000)
            if uwp:                 # a Store app's manifest, as FF XV Pocket Edition has
                (game / 'AppxManifest.xml').write_text(MANIFEST.replace('Game.exe', r'bin\TestGame.exe'),
                                                       encoding='utf-8')
        if log_text is not None and '/LOG' in opts:
            Path(opts['/LOG']).write_text(log_text, encoding='utf-8')
        self.returncode = rc
        self.pid = 4242

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        return False


class Harness(unittest.TestCase):
    """A repack, the Windows feature set and the fakes; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Resolved: a runner's TEMP can be an 8.3 name (RUNNER~1) that Nexus
        # resolves to the long one, and paths are compared below.
        t = self.root = Path(self.tmp.name).resolve()
        self.base = t / 'Games'
        self.repack = t / 'Downloads' / 'Test Game [FitGirl Repack]'
        (self.repack / 'MD5').mkdir(parents=True)
        (self.repack / 'setup.exe').write_bytes(b'MZ' + b'\0' * 100)
        (self.repack / 'fg-01.bin').write_bytes(b'x' * 10)
        self.calls, self.lnks, self.quiet = [], [], []
        self.setup_kw = {}
        for p in (mock.patch.object(features, 'FEATURES', features.WINDOWS),
                  mock.patch.object(platform, 'run_elevated', self.fake_elevated),
                  mock.patch.object(platform, 'create_shortcut', self.fake_lnk),
                  mock.patch.object(platform, 'defender_detections', return_value=[]),
                  mock.patch.object(platform, 'install_base_warnings', return_value=[]),
                  mock.patch.object(IW, '_install_session')):        # keep-awake helper
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.forget, 'test-game')

    def fake_elevated(self, exe, args, cwd=None, quiet=False):
        self.calls.append((Path(exe), list(args), cwd))
        self.quiet.append(quiet)
        return FakeSetup(exe, args, cwd, **self.setup_kw)

    def fake_lnk(self, path, target, args='', workdir=None, icon=None):
        self.lnks.append({'path': Path(path), 'target': Path(target), 'workdir': workdir})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b'lnk')

    def forget(self, slug):
        with fi.registry_lock():
            reg = fi.load_registry()
            if reg['games'].pop(slug, None) is not None:
                fi.save_registry(reg)
        for p in fi.APPLICATIONS_DIR.glob('*.lnk'):
            p.unlink()

    def install(self, *extra):
        """(exit code, stdout) of `nexus-cli <repack> --yes ...`."""
        args = entry.build_parser().parse_args(
            [str(self.repack), '--yes', '--no-artwork', '--no-steam', '--skip-mem-check',
             '--skip-disk-check', *extra])
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                IW.cmd_install_win(args, self.base)
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def events(self, out, name):
        return [json.loads(line.split(' ', 2)[2]) for line in out.splitlines()
                if line.startswith(f'@nexus {name} ')]


class WindowsInstall(Harness):
    def test_silent_install_registers_a_windows_game(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        game = self.base / 'test-game'
        exe, args, cwd = self.calls[0]
        self.assertEqual(exe.name, 'setup.exe')
        self.assertEqual(cwd, self.repack)
        self.assertEqual(args[:4], fi.INNO_SILENT)
        self.assertIn(f'/DIR={game}', args)
        self.assertTrue(any(a.startswith('/LOG=') and a.endswith('test-game-setup.log')
                            for a in args))
        e = fi.registry_get('test-game')
        self.assertEqual((e['platform'], e['install_dir'], e['exe'], e['uninstaller']),
                         ('windows', str(game), str(game / 'bin' / 'TestGame.exe'),
                          str(game / 'unins000.exe')))
        self.assertEqual((e['compatdata'], e['wine_prefix'], e['launcher']), (None, None, None))
        self.assertEqual(e['repack_source'], str(self.repack))
        self.assertFalse((game / fi.INCOMPLETE_MARKER).exists())
        lnk = self.lnks[0]
        self.assertEqual((lnk['path'].name, lnk['target'], lnk['workdir']),
                         ('Test Game.lnk', game / 'bin' / 'TestGame.exe', game / 'bin'))
        self.assertEqual(e['menu_entry'], str(lnk['path']))
        self.assertTrue(e['has_desktop'])
        self.assertEqual(self.events(out, 'installed')[0]['slug'], 'test-game')

    def test_interactive_shows_the_window_but_keeps_the_folder(self):
        code, out = self.install('--interactive')
        self.assertEqual(code, 0, out)
        args = self.calls[0][1]
        self.assertNotIn('/VERYSILENT', args)
        self.assertIn(f"/DIR={self.base / 'test-game'}", args)
        # Its window has a music button and a "visit the site" tick box:
        # the user's call, so it runs as it is.
        self.assertEqual(self.quiet, [False])

    def test_a_silent_setup_runs_quiet(self):
        # /VERYSILENT leaves FitGirl's music, closing site and hosts-file
        # console to the installer; quiet_setup is what keeps them away.
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.quiet, [True])
        self.assertNotIn('may play music', out)
        self.assertIn('music is muted', out)
        self.assertIn('web links open nothing', out)

    def test_declined_prompt_leaves_nothing(self):
        def declined(*a, **kw):
            raise platform.ElevationCancelled(1223, 'cancelled')
        with mock.patch.object(platform, 'run_elevated', declined):
            code, out = self.install()
        self.assertEqual(code, 1)
        self.assertIn('cancelled at the admin prompt', out)
        self.assertFalse((self.base / 'test-game').exists())
        self.assertIsNone(fi.registry_get('test-game'))
        self.assertEqual(self.events(out, 'install-cancelled'), [{'slug': 'test-game',
                                                                  'why': 'uac'}])

    def test_antivirus_is_named_when_the_install_comes_up_empty(self):
        self.setup_kw = {'make_game': False, 'rc': 4,
                         'log_text': 'Extracting...\nISDone.dll: An error occurred when '
                                     'unpacking! Unarc.dll returned an error code: -1\n'}
        caught = [{'file': str(self.repack / 'unarc.dll'), 'threat': 'Trojan:Win32/Wacatac',
                   'at': LATER}]
        with mock.patch.object(platform, 'defender_detections', return_value=caught) as dd:
            code, out = self.install()
        self.assertEqual(code, 1)
        self.assertIn('antivirus problem', out)
        self.assertIn('Trojan:Win32/Wacatac', out)
        self.assertIn(IW.FITGIRL_FAQ, out)
        self.assertEqual(dd.call_args.args[0], [self.repack, self.base / 'test-game'])
        ev = self.events(out, 'antivirus')[0]
        self.assertEqual(ev['detections'], caught)
        self.assertIn('ISDone.dll', ev['log_lines'][0])
        # Kept for a retry, as an unfinished install.
        self.assertTrue((self.base / 'test-game' / fi.INCOMPLETE_MARKER).exists())
        self.assertIsNone(fi.registry_get('test-game'))

    def test_a_quarantined_game_file_is_not_registered(self):
        # The VM: setup copied the game, Defender took vcruntime140_1_app.dll.
        self.setup_kw = {'rc': 1}
        dll = self.base / 'test-game' / 'vcruntime140_1_app.dll'
        with mock.patch.object(platform, 'defender_detections', return_value=[
                {'file': str(dll), 'threat': 'Trojan:Win32/Wacatac.H!ml', 'at': LATER}]):
            code, out = self.install()
        self.assertEqual(code, 1)
        self.assertIn("won't run: not adding it to the library", out)
        self.assertEqual(self.events(out, 'retry-interactive'), [])   # not the installer's fault
        self.assertIsNone(fi.registry_get('test-game'))
        self.assertTrue((self.base / 'test-game' / fi.INCOMPLETE_MARKER).exists())

    def test_detections_from_before_this_setup_are_ignored(self):
        old = [{'file': str(self.base / 'test-game' / 'x.dll'), 'threat': 'T',
                'at': '2000-01-01T00:00:00'}]
        with mock.patch.object(platform, 'defender_detections', return_value=old):
            code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertNotIn('antivirus', out)

    def test_quiet_when_nothing_points_at_antivirus(self):
        self.setup_kw = {'make_game': False, 'rc': 2, 'log_text': 'User cancelled.\n'}
        code, out = self.install()
        self.assertEqual(code, 1)
        self.assertNotIn('antivirus', out)

    def test_apply_and_list_handle_an_entry_without_prefix(self):
        self.assertEqual(self.install()[0], 0)
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
                fi.apply, 'game_has_steam_shortcut', return_value=False):
            fi.cmd_apply('test-game', {})
        self.assertEqual(len(self.lnks), 2)                    # re-written, not launch.sh
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fi.cmd_list(self.base, as_json=True)
        row = next(g for g in json.loads(out.getvalue())['games'] if g['slug'] == 'test-game')
        self.assertEqual((row['wine_prefix'], row['desktop']), (None, True))
        self.assertEqual(row['launcher'], row['exe'])       # the exe plays; see below
        self.assertIsNone(fi.registry_get('test-game')['launcher'])   # the registry stays clean

    def test_a_fresh_install_can_be_played(self):
        """The --library row is what the GUI's Play button reads: without a launcher
        it was disabled for every Windows install."""
        self.assertEqual(self.install()[0], 0)
        row = next(g for g in fi.managed_entries(self.base) if g['slug'] == 'test-game')
        self.assertEqual(row['launcher'], str(self.base / 'test-game' / 'bin' / 'TestGame.exe'))
        self.assertIsNone(fi.registry_get('test-game')['launcher'])
        try:
            from nexus.gui import gameinfo
        except ImportError:                                  # no PyQt6 here
            return
        self.assertTrue(gameinfo.play_enabled(row))

    def test_check_integrity_is_refused(self):
        with mock.patch.object(sys, 'argv', ['nexus-cli', '--check-integrity', 'test-game']), \
                self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            entry.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("isn't available", err.getvalue())

    def test_repair_launchers_recreates_a_missing_shortcut(self):
        self.assertEqual(self.install()[0], 0)
        Path(fi.registry_get('test-game')['menu_entry']).unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            fi.cmd_repair_launchers(self.base, {}, 'test-game')
        self.assertIn('Repaired 1 launcher(s).', out.getvalue())
        self.assertTrue(Path(fi.registry_get('test-game')['menu_entry']).exists())

    def test_a_rename_replaces_the_old_shortcut(self):
        self.assertEqual(self.install()[0], 0)
        old = self.lnks[0]['path']
        with contextlib.redirect_stdout(io.StringIO()):
            fi.write_desktop_entry('Test Game: Remastered', 'test-game',
                                   Path(fi.registry_get('test-game')['exe']), None)
        self.assertFalse(old.exists())
        self.assertEqual(self.lnks[1]['path'].name, 'Test Game Remastered.lnk')
        self.assertEqual(fi.registry_get('test-game')['menu_entry'], str(self.lnks[1]['path']))



class StoreApp(Harness):
    """A UWP repack: registered by its setup, started as its package."""

    def test_family_name_matches_windows(self):
        from nexus import appx
        # Both as Get-AppxPackage reported them in the VM.
        self.assertEqual(appx.publisher_id('CN=83076AC8-252A-4004-9255-A4A444BA69F5'),
                         'n746a19ndrrjg')
        self.assertEqual(appx.publisher_id('CN=Microsoft Corporation, O=Microsoft Corporation, '
                                           'L=Redmond, S=Washington, C=US'), '8wekyb3d8bbwe')

    def test_app_id_from_the_manifest(self):
        from nexus import appx
        g = self.root / 'g'
        (g / 'sub').mkdir(parents=True)
        (g / 'AppxManifest.xml').write_text(MANIFEST, encoding='utf-8')
        self.assertEqual(appx.app_id(g / 'Game.exe'), FF15)
        self.assertEqual(appx.app_id(g / 'GAME.EXE'), FF15)          # Windows ignores case
        self.assertIsNone(appx.app_id(g / 'Other.exe'))
        self.assertIsNone(appx.app_id(self.root / 'plain' / 'Game.exe'))

    def test_install_records_it_and_shortcuts_go_through_the_shell(self):
        self.setup_kw = {'uwp': True}
        with mock.patch.object(platform, 'appx_registered', return_value=True) as reg:
            code, out = self.install()
        self.assertEqual(code, 0, out)
        reg.assert_called_once_with(FF15.split('!')[0])
        self.assertEqual(fi.registry_get('test-game')['appx'], FF15)
        lnk = self.lnks[0]
        self.assertEqual(lnk['target'].name.lower(), 'explorer.exe')
        self.assertEqual(IW.steam_target(Path('x.exe'), FF15)[1], 'shell:AppsFolder\\' + FF15)

    def test_steam_shortcut_later_goes_through_explorer(self):
        # Steam was running at install time: the GUI adds it later with --steam.
        self.setup_kw = {'uwp': True}
        with mock.patch.object(platform, 'appx_registered', return_value=True):
            self.assertEqual(self.install()[0], 0)
        with mock.patch.object(fi.apply, 'add_steam_shortcut_native', return_value=0x80000001) as add, \
                contextlib.redirect_stdout(io.StringIO()):
            fi.cmd_add_steam('test-game', {})
        exe, opts = add.call_args.args[2], add.call_args.kwargs['launch_options']
        self.assertEqual((exe.name.lower(), opts), ('explorer.exe', 'shell:AppsFolder\\' + FF15))
        self.assertEqual(fi.registry_get('test-game')['steam_appid'], 0x80000001)

    def test_not_registered_runs_the_exe(self):
        self.setup_kw = {'uwp': True}
        with mock.patch.object(platform, 'appx_registered', return_value=False):
            self.assertEqual(self.install()[0], 0)
        self.assertIsNone(fi.registry_get('test-game')['appx'])
        self.assertEqual(self.lnks[0]['target'].name, 'TestGame.exe')

class WindowsUninstall(Harness):
    """--uninstall of a Windows install: unins000.exe as administrator, then
    the leftovers to the Recycle Bin, the Start Menu shortcut and the entry."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.install()[0], 0)
        self.game = self.base / 'test-game'
        self.trashed = []
        p = mock.patch.object(saves, 'load_index', return_value=saves.build_index([]))
        p.start()
        self.addCleanup(p.stop)
        for p in (mock.patch.object(platform, 'trash', self.fake_trash),
                  mock.patch.object(fi.uninstall, 'game_has_steam_shortcut', return_value=False),
                  mock.patch.object(fi.uninstall, 'remove_heroic_entry')):
            p.start()
            self.addCleanup(p.stop)
        self.calls.clear()

    def fake_trash(self, path):
        import shutil
        self.trashed.append(Path(path))
        shutil.rmtree(path)

    def uninstall(self):
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                fi.cmd_uninstall(self.base, 'test-game', yes=True, save_backup=True)
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def test_runs_the_uninstaller_then_clears_the_rest(self):
        def uninstaller(exe, args, cwd=None):
            self.calls.append((Path(exe), list(args)))
            Path(exe).unlink()                       # Inno removes itself last
            (Path(exe).parent / 'bin' / 'TestGame.exe').unlink()
            return mock.Mock(wait=lambda timeout=None: 0)
        lnk = Path(fi.registry_get('test-game')['menu_entry'])
        with mock.patch.object(platform, 'run_elevated', uninstaller):
            code, out = self.uninstall()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls, [(self.game / 'unins000.exe',
                                       ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'])])
        self.assertIn(fi.savedata.NO_SAVE_PATHS, out)                 # --save-backup
        self.assertEqual(self.trashed, [self.game])
        self.assertFalse(lnk.exists())
        self.assertIsNone(fi.registry_get('test-game'))

    def test_declined_prompt_removes_nothing(self):
        def declined(*a, **kw):
            raise platform.ElevationCancelled(1223, 'cancelled')
        with mock.patch.object(platform, 'run_elevated', declined):
            code, out = self.uninstall()
        self.assertEqual(code, 1)
        self.assertIn('cancelled at the admin prompt', out)
        self.assertTrue((self.game / 'bin' / 'TestGame.exe').exists())
        self.assertTrue(Path(fi.registry_get('test-game')['menu_entry']).exists())

    def test_no_uninstaller_removes_the_folder(self):
        (self.game / 'unins000.exe').unlink()
        code, out = self.uninstall()
        self.assertEqual(code, 0, out)
        self.assertIn('no uninstaller found', out)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.trashed, [self.game])
        self.assertIsNone(fi.registry_get('test-game'))

SAVE_MANIFEST = '''---
Test Game:
  files:
    "<base>/saves":
      tags:
        - save
    "<winAppData>/Test Studio/Test Game":
      when:
        - os: windows
  steam:
    id: 4242
'''


class WindowsSaves(Harness):
    """Saves of a Windows install through the Ludusavi manifest: backed up
    on --uninstall --save-backup, put back by --restore-saves (keeping the
    current ones first) and by --import-save on a reinstall."""

    def setUp(self):
        super().setUp()
        self.game = self.base / 'test-game'
        self.home = self.root / 'Users' / 'me'
        self.appdata = self.home / 'AppData' / 'Roaming' / 'Test Studio' / 'Test Game'
        self.index = saves.build_index(SAVE_MANIFEST.splitlines())
        folders = saves.windows_folders(self.home)
        for p in (mock.patch.object(saves, 'load_index', lambda *a, **k: self.index),
                  mock.patch.object(saves, 'windows_folders', lambda home=None: folders),
                  mock.patch.object(savedata, 'SAVES_DIR', self.root / 'saves'),
                  mock.patch.object(platform, 'trash', lambda p: __import__('shutil').rmtree(p)),
                  mock.patch.object(fi.uninstall, 'game_has_steam_shortcut', return_value=False),
                  mock.patch.object(fi.uninstall, 'remove_heroic_entry')):
            p.start()
            self.addCleanup(p.stop)

    def put(self, path: Path, data: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def run_cli(self, fn, *a, **kw):
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                fn(*a, **kw)
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def uninstall(self, save_backup=True):
        def uninstaller(exe, args, cwd=None):
            # The backup is taken before the game's own uninstaller runs.
            self.backed_up_first = savedata.latest_save_backup('test-game') is not None
            import shutil
            shutil.rmtree(Path(exe).parent)
            return mock.Mock(wait=lambda timeout=None: 0)
        with mock.patch.object(platform, 'run_elevated', uninstaller):
            return self.run_cli(fi.cmd_uninstall, self.base, 'test-game', yes=True,
                                save_backup=save_backup)

    def restore(self):
        args = entry.build_parser().parse_args(['--restore-saves', 'test-game'])
        return self.run_cli(entry._cmd_restore_saves, args, self.base)

    def install_game(self):
        self.assertEqual(self.install()[0], 0)

    def test_uninstall_backs_up_the_manifest_saves_first(self):
        self.install_game()
        self.put(self.game / 'saves' / 'slot1.sav', 'base')
        self.put(self.appdata / 'profile.sav', 'roaming')
        code, out = self.uninstall()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.backed_up_first)
        self.assertIn('Backed up 2 save file(s)', out)
        backup = savedata.latest_save_backup('test-game')
        mapping = json.loads((backup / savedata.SAVE_FILES_MAP).read_text())
        self.assertEqual(sorted(mapping.values()), sorted(
            [str(self.game / 'saves' / 'slot1.sav'), str(self.appdata / 'profile.sav')]))
        self.assertIsNone(fi.registry_get('test-game'))

    def test_uninstall_without_save_backup_does_not_look(self):
        self.install_game()
        self.put(self.appdata / 'profile.sav', 'roaming')
        with mock.patch.object(saves, 'load_index') as load:
            code, out = self.uninstall(save_backup=False)
        self.assertEqual(code, 0, out)
        load.assert_not_called()
        self.assertIsNone(savedata.latest_save_backup('test-game'))

    def test_a_game_the_manifest_does_not_know_is_not_guessed(self):
        self.index = saves.build_index([])
        self.install_game()
        self.put(self.game / 'saves' / 'slot1.sav', 'base')
        code, out = self.uninstall()
        self.assertEqual(code, 0, out)
        self.assertIn("Nexus doesn't know where this game keeps its saves", out)
        self.assertIsNone(savedata.latest_save_backup('test-game'))
        self.assertIsNone(fi.registry_get('test-game'))

    def test_restore_keeps_the_current_saves_first(self):
        self.install_game()
        sav = self.put(self.appdata / 'profile.sav', 'old')
        savedata.backup_manifest_saves('test-game', self.game, self.index['games']['Test Game'])
        sav.write_text('newer')
        code, out = self.restore()
        self.assertEqual(code, 0, out)
        self.assertEqual(sav.read_text(), 'old')
        before = [d for d in (self.root / 'saves' / 'test-game').iterdir()
                  if d.name.endswith(savedata.PRE_RESTORE)]
        self.assertEqual(len(before), 1)
        mapping = json.loads((before[0] / savedata.SAVE_FILES_MAP).read_text())
        rel = next(k for k, v in mapping.items() if v == str(sav))
        self.assertEqual((before[0] / rel).read_text(), 'newer')
        self.assertIn('Restored 1 save file(s)', out)

    def test_restore_of_an_unknown_game_changes_nothing(self):
        self.install_game()
        sav = self.put(self.appdata / 'profile.sav', 'old')
        savedata.backup_manifest_saves('test-game', self.game, self.index['games']['Test Game'])
        sav.write_text('newer')
        self.index = saves.build_index([])
        code, out = self.restore()
        self.assertEqual(code, 1)
        self.assertIn("Nexus doesn't know where this game keeps its saves", out)
        self.assertEqual(sav.read_text(), 'newer')

    def test_restore_refuses_a_prefix_backup(self):
        self.install_game()
        self.put(self.root / 'saves' / 'test-game' / '20260101-000000' / 'drive_c' / 'x.sav', 'x')
        code, out = self.restore()
        self.assertEqual(code, 1)
        self.assertIn('Wine prefix', out)

    def test_reinstall_imports_the_backup_only_when_asked(self):
        self.install_game()
        self.put(self.game / 'saves' / 'slot1.sav', 'kept')
        self.assertEqual(self.uninstall()[0], 0)
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn('NOT importing it', out)
        self.assertFalse((self.game / 'saves' / 'slot1.sav').exists())
        self.assertEqual(self.uninstall(save_backup=False)[0], 0)
        code, out = self.install('--import-save')
        self.assertEqual(code, 0, out)
        self.assertIn('Restored 1 save file(s)', out)
        self.assertEqual((self.game / 'saves' / 'slot1.sav').read_text(), 'kept')


class WindowsMove(Harness):
    """--move of a Windows install: install_dir, exe and uninstaller follow,
    and the Start Menu shortcut is rewritten (no prefix or launch.sh)."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.install()[0], 0)
        self.dest = self.root / 'Other Drive'
        p = mock.patch.object(fi.apply, 'game_has_steam_shortcut', return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def move(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fi.move.cmd_move(self.base, 'test-game', str(self.dest))

    def check_moved(self):
        new = self.dest / 'test-game'
        e = fi.registry_get('test-game')
        self.assertEqual((e['install_dir'], e['exe'], e['uninstaller'], e['install_base']),
                         (str(new), str(new / 'bin' / 'TestGame.exe'),
                          str(new / 'unins000.exe'), str(self.dest)))
        self.assertEqual(self.lnks[-1]['target'], new / 'bin' / 'TestGame.exe')
        self.assertTrue((new / 'bin' / 'TestGame.exe').is_file())

    def test_rename_on_the_same_drive(self):
        self.assertEqual(self.move(), 0)
        self.check_moved()
        self.assertFalse((self.base / 'test-game').exists())

    def test_copy_across_drives_keeps_the_old_copy(self):
        import errno
        real = fi.move.os.rename

        def rename(a, b):
            if Path(a) == self.base / 'test-game' and Path(b).parent == self.dest:
                raise OSError(errno.EXDEV, 'cross-device')
            return real(a, b)
        with mock.patch.object(fi.move.os, 'rename', rename):
            self.assertEqual(self.move(), 0)
        self.check_moved()
        self.assertEqual(len(list(self.base.glob('test-game.old-*'))), 1)

    @windows_only
    def test_rebase_windows_paths(self):
        e = {'install_dir': r'C:\Games\g', 'exe': r'c:\games\G\bin\g.exe',
             'uninstaller': 'C:/Games/g/unins000.exe', 'compatdata': None,
             'menu_entry': r'C:\Users\me\Start Menu\g.lnk', 'launcher': r'C:\Games\gx\x.exe'}
        r = fi.move.rebase(e, Path(r'C:\Games\g'), Path(r'D:\Games\g'))
        self.assertEqual(r, {'install_dir': r'D:\Games\g', 'exe': r'D:\Games\g\bin\g.exe',
                             'uninstaller': r'D:\Games\g/unins000.exe',
                             'install_base': r'D:\Games'})

    def test_a_running_game_stays(self):
        with mock.patch.object(fi.sunshine, 'dir_processes', return_value=[99]):
            self.assertEqual(self.move(), 1)
        self.assertTrue((self.base / 'test-game').is_dir())


class WindowsImport(Harness):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.forget, 'some-game')
        self.folder = self.root / 'Downloads' / 'Some_Game'
        (self.folder / 'bin').mkdir(parents=True)
        (self.folder / 'bin' / 'Game.exe').write_bytes(b'\0' * 700_000)
        (self.folder / 'unins000.exe').write_bytes(b'\0' * 700_000)

    def run_import(self, path, *extra):
        args = entry.build_parser().parse_args(
            ['--import', str(path), '--yes', '--no-artwork', '--no-steam', *extra])
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                fi.cmd_import(args, self.base)
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def test_a_folder_is_added_where_it_is(self):
        code, out = self.run_import(self.folder)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls, [])                    # nothing to run, nothing copied
        e = fi.registry_get('some-game')
        self.assertEqual((e['install_dir'], e['exe'], e['uninstaller'], e['wine_prefix']),
                         (str(self.folder), str(self.folder / 'bin' / 'Game.exe'), None, None))
        self.assertTrue(e['local'])
        self.assertFalse((self.base / 'some-game').exists())

    def test_add_exe_remembers_it_where_it_is(self):
        # A game from a launcher Nexus doesn't support (w-22): nothing runs,
        # nothing is copied, and Nexus owns no folder of it.
        args = entry.build_parser().parse_args(
            ['--add-exe', str(self.folder / 'bin' / 'Game.exe'), '--name', 'Some Game', '--yes',
             '--no-artwork', '--no-steam'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            fi.cmd_add_exe(args, self.base)
        self.assertEqual(self.calls, [])
        e = fi.registry_get('some-game')
        self.assertEqual((e['install_dir'], e['exe'], e['uninstaller']),
                         (str(self.folder / 'bin'), str(self.folder / 'bin' / 'Game.exe'), None))
        self.assertTrue(e['local'] and e['added_exe'])
        self.assertFalse((self.base / 'some-game').exists())

    def test_an_inno_installer_runs_silently_into_the_base(self):
        setup = self.root / 'setup_some_game.exe'
        setup.write_bytes(b'MZ')
        with mock.patch.object(IW, 'inno_header', return_value=b'Inno'):
            code, out = self.run_import(setup, '--name', 'Some Game')
        self.assertEqual(code, 0, out)
        exe, args, cwd = self.calls[0]
        self.assertEqual(exe, setup)
        self.assertEqual(args[:4], fi.INNO_SILENT)
        self.assertIn(f"/DIR={self.base / 'some-game'}", args)
        self.assertEqual(self.quiet, [True])
        e = fi.registry_get('some-game')
        self.assertEqual(e['uninstaller'], str(self.base / 'some-game' / 'unins000.exe'))

    def test_a_declined_prompt_says_so(self):
        setup = self.root / 'setup_some_game.exe'
        setup.write_bytes(b'MZ')

        def declined(*a, **kw):
            raise platform.ElevationCancelled(1223, 'cancelled')
        with mock.patch.object(platform, 'run_elevated', declined):
            code, out = self.run_import(setup, '--name', 'Some Game')
        self.assertEqual(code, 1)
        self.assertEqual(self.events(out, 'install-cancelled'), [{'slug': 'some-game',
                                                                  'why': 'uac'}])
        self.assertIsNone(fi.registry_get('some-game'))

class QuietSetupEntry(unittest.TestCase):
    """nexus-cli --quiet-setup is the elevated half of a silent install: it
    goes straight to platform.quiet_setup, before any option parsing."""

    def test_hands_over_to_quiet_setup_with_its_exit_code(self):
        argv = ['nexus-cli', platform.QUIET_SETUP_ARG, r'C:\Repack', r'C:\Repack\setup.exe',
                '/VERYSILENT', '/DIR=C:\\Games\\x']
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(platform, 'quiet_setup', return_value=7) as qs, \
                mock.patch.object(entry, 'build_parser') as parser, \
                self.assertRaises(SystemExit) as done:
            entry.main()
        self.assertEqual(done.exception.code, 7)
        qs.assert_called_once_with(argv[2:])
        parser.assert_not_called()

    @linux_only
    def test_linux_has_none(self):
        with self.assertRaises(NotImplementedError):
            platform.quiet_setup(['.', 'setup.exe'])


class WindowsDoctor(unittest.TestCase):
    def test_windows_set_and_no_linux_checks(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(features, 'FEATURES', features.WINDOWS), \
                mock.patch.object(platform, 'long_paths_enabled', return_value=False), \
                mock.patch.object(platform, 'defender_status', return_value={'realtime': True}), \
                mock.patch.object(platform, 'install_base_warnings',
                                  return_value=["it's inside OneDrive"]):
            checks = fi.doctor_checks(Path(d) / 'Games')
        titles = {c['title']: c for c in checks}
        for linux in ('Wine', 'umu-run', 'Proton', 'winetricks', 'Play Proton', 'protongen'):
            self.assertNotIn(linux, titles)
        self.assertEqual(titles['Long paths']['status'], 'warn')
        self.assertEqual(titles['Windows Defender']['actions'][0]['args'],
                         ['--open-url', 'windowsdefender://threatsettings'])
        # Every fix only opens a page the Health page (and --open-url) accept.
        fixes = [a['args'] for c in checks for a in c['actions']]
        self.assertIn(['--open-url', fi.doctor.LONG_PATHS_HELP], fixes)
        for args in fixes:
            if args[0] == '--open-url':
                self.assertTrue(platform.fix_page(args[1]), args)
        self.assertTrue(any(c['title'] == 'Install folder' and 'OneDrive' in c['detail']
                            and c['status'] == 'warn' for c in checks))

class Session(unittest.TestCase):
    """Play time on Windows: a launcher stub that exits at once hands over to
    the real game, which is followed by its folder until it's gone."""

    def run_session(self, rc, alive_polls):
        polls = iter([[7]] * alive_polls + [[]] * 1000)
        clock = [0.0]

        def sleep(n):
            clock[0] += n
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(fi.sunshine, 'game_processes', lambda e: next(polls)), \
                mock.patch.object(fi.sunshine.time, 'sleep', sleep), \
                mock.patch.object(fi.sunshine.time, 'monotonic', lambda: clock[0]), \
                mock.patch.object(fi.sunshine.subprocess, 'run',
                                  return_value=mock.Mock(returncode=rc)) as run:
            exe = Path(d) / 'Game' / 'Launcher.exe'
            got = fi.sunshine._run_session('g', str(exe), Path(d) / 'out.log',
                                           fi.sunshine.datetime.now())
            log = (Path(d) / 'out.log').read_text(encoding='utf-8')
        self.assertEqual(run.call_args.args[0], [str(exe)])
        self.assertEqual(run.call_args.kwargs['cwd'], exe.parent)
        self.elapsed = got[1]
        return got[0], clock[0], log

    def test_short_run_adds_no_playtime(self):
        """w-18: play time and sessions.jsonl use one rule, so a 45 s run is
        in neither and a 90 s run is in both."""
        from nexus import history
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(history, 'PATH', Path(d) / 'sessions.jsonl'):
            with fi.registry_lock():
                reg = fi.load_registry()
                reg['games']['short-run'] = {'display_name': 'Short Run'}
                fi.save_registry(reg)
            try:
                started = fi.sunshine.datetime.now()
                self.assertFalse(fi.sunshine._record_session('short-run', 'Short Run',
                                                             started, 45))
                self.assertNotIn('playtime_seconds', fi.load_registry()['games']['short-run'])
                self.assertEqual(history.load(), [])
                self.assertTrue(fi.sunshine._record_session('short-run', 'Short Run',
                                                            started, 90))
                self.assertEqual(fi.load_registry()['games']['short-run']['playtime_seconds'],
                                 90)
                self.assertEqual([s['slug'] for s in history.load()], ['short-run'])
            finally:
                with fi.registry_lock():
                    reg = fi.load_registry()
                    reg['games'].pop('short-run', None)
                    fi.save_registry(reg)

    def test_stub_hands_over_and_the_game_is_waited_for(self):
        rc, waited, log = self.run_session(0, alive_polls=3)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(waited, 10 + fi.sunshine.HANDOVER_GRACE)   # last seen at 10 s
        self.assertIn("waited for the game's processes", log)
        self.assertEqual(self.elapsed, 10)          # play time stops at last seen, not the grace

    def test_nothing_left_ends_after_the_grace(self):
        rc, waited, _ = self.run_session(0, alive_polls=0)
        self.assertLessEqual(waited, fi.sunshine.HANDOVER_GRACE + 1)
        self.assertEqual(self.elapsed, 0)           # the game never showed: no play time

    def test_a_store_app_is_opened_and_given_time_to_start(self):
        polls = iter([[]] * 3 + [[7]] * 2 + [[]] * 1000)        # appears after ~3 s
        clock = [0.0]

        def sleep(n):
            clock[0] += n
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(fi.sunshine, 'game_processes', lambda e: next(polls)), \
                mock.patch.object(fi.sunshine.time, 'sleep', sleep), \
                mock.patch.object(fi.sunshine.time, 'monotonic', lambda: clock[0]), \
                mock.patch.object(fi.sunshine.platform, 'open_url', return_value=True) as op:
            rc, played = fi.sunshine._run_session('g', 'shell:AppsFolder\\' + FF15,
                                             Path(d) / 'out.log', fi.sunshine.datetime.now())
        op.assert_called_once_with('shell:AppsFolder\\' + FF15)
        self.assertEqual((rc, played), (0, 8))      # seen from 3 s to 8 s
        self.assertGreaterEqual(clock[0], 3 + 5 + fi.sunshine.APPX_GRACE)

    def test_a_launch_that_never_comes_up_names_what_defender_took(self):
        clock = [0.0]

        def sleep(n):
            clock[0] += n
        hit = [{'file': r'C:\Games\g\vcruntime140_1_app.dll', 'threat': 'Trojan:Win32/Wacatac.H!ml',
                'at': LATER}]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(fi.sunshine, 'registry_get',
                                  return_value={'install_dir': r'C:\Games\g'}), \
                mock.patch.object(fi.sunshine, 'game_processes', lambda e: []), \
                mock.patch.object(fi.sunshine.time, 'sleep', sleep), \
                mock.patch.object(fi.sunshine.time, 'monotonic', lambda: clock[0]), \
                mock.patch.object(fi.sunshine.platform, 'open_url', return_value=True), \
                mock.patch.object(fi.sunshine.platform, 'defender_detections', return_value=hit):
            rc, played = fi.sunshine._run_session('g', 'shell:AppsFolder\\' + FF15,
                                                  Path(d) / 'out.log', fi.sunshine.datetime.now())
            log = (Path(d) / 'out.log').read_text(encoding='utf-8')
        self.assertEqual(played, 0)
        self.assertIn('Windows Defender removed C:\\Games\\g\\vcruntime140_1_app.dll', log)

    def test_a_store_app_that_doesnt_come_up_is_asked_once_more(self):
        polls = iter([[]] * 12 + [[7]] + [[]] * 1000)           # appears only after a retry
        clock = [0.0]

        def sleep(n):
            clock[0] += n
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(fi.sunshine, 'game_processes', lambda e: next(polls)), \
                mock.patch.object(fi.sunshine.time, 'sleep', sleep), \
                mock.patch.object(fi.sunshine.time, 'monotonic', lambda: clock[0]), \
                mock.patch.object(fi.sunshine.platform, 'open_url', return_value=True) as op:
            fi.sunshine._run_session('g', 'shell:AppsFolder\\' + FF15,
                                     Path(d) / 'out.log', fi.sunshine.datetime.now())
            log = (Path(d) / 'out.log').read_text(encoding='utf-8')
        self.assertEqual(op.call_count, 2)
        self.assertIn('asked Windows again', log)

    def test_a_crash_ends_at_once(self):
        rc, waited, _ = self.run_session(3, alive_polls=0)
        self.assertEqual((rc, waited), (3, 0))

if __name__ == '__main__':
    unittest.main()
