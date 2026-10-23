"""Moving a game, adding any Windows game, btrfs snapshots and the one-run
Proton try. The CLI runs against a throwaway HOME (the Sandbox of
test_nexus_cli); btrfs is a stand-in that copies folders, unless
NEXUS_TEST_BTRFS names a writable folder on a real btrfs volume."""

import errno
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.oskind import linux_only  # noqa: E402
from tests.patching import patch_cli  # noqa: E402
from tests import test_nexus_cli as T  # noqa: E402
from nexus import cli  # noqa: E402
from nexus.cli import move as MV, snapshots as SN, importgame as IG  # noqa: E402


@linux_only                     # Windows: test_install_win.WindowsMove/WindowsImport
class MoveCli(T.Sandbox):
    def test_move_rewires_everything_and_keeps_heroic_entry(self):
        if T.steam_running():
            self.skipTest("Steam is running")
        self.cli('--heroic', 'test-game')
        app = self.registry()['heroic_app_name']
        dest_base = self.home / 'Other Drive'
        r = self.cli('--move', 'test-game', str(dest_base), '--json')
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(out['ok'] and out['renamed'], out)
        new = dest_base / 'test-game'
        e = self.registry()
        self.assertEqual(e['compatdata'], str(new))
        self.assertEqual(e['wine_prefix'], str(new / 'pfx'))
        self.assertTrue(e['exe'].startswith(str(new) + '/'))
        self.assertEqual(e['install_base'], str(dest_base))
        self.assertFalse(self.game_dir.exists())
        self.assertIn(str(new / 'pfx'), (new / 'launch.sh').read_text())
        lib = json.loads((self.heroic / 'sideload_apps/library.json').read_text())['games']
        mine = [g for g in lib if g['app_name'] == app]
        self.assertEqual(len(mine), 1)                          # updated, not duplicated
        self.assertEqual(mine[0]['install']['executable'], e['exe'])
        gc = json.loads((self.heroic / 'GamesConfig' / f'{app}.json').read_text())
        self.assertEqual(gc[app]['winePrefix'], str(new / 'pfx'))

    def test_move_refuses_a_taken_place(self):
        dest_base = self.home / 'Other'
        (dest_base / 'test-game').mkdir(parents=True)
        r = self.cli('--move', 'test-game', str(dest_base), check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn('already exists', r.stderr)
        self.assertTrue(self.game_dir.exists())

    def test_import_a_game_folder(self):
        src = self.home / 'Downloads' / 'Some_Game'
        (src / 'bin').mkdir(parents=True)
        (src / 'bin' / 'Game.exe').write_bytes(b'\0' * 700_000)
        r = self.cli('--import', str(src), '--yes', '--no-steam', '--no-heroic',
                     '--no-artwork', '--no-desktop')
        reg = json.loads((tests.sandbox.data_dir(self.home) / 'games.json').read_text())['games']
        e = reg['some-game']
        self.assertEqual(e['display_name'], 'Some Game')
        self.assertIs(e['local'], True)
        self.assertTrue(Path(e['exe']).is_file(), r.stdout)
        self.assertIn('/pfx/drive_c/Games/some-game/bin/Game.exe', e['exe'])
        self.assertTrue(src.exists())                           # copied, not taken
        again = self.cli('--import', str(src), '--yes', check=False)
        self.assertEqual(again.returncode, 1)                   # never over an existing one


# Only these tests: not every Sandbox test once more.
for _name in dir(T.Sandbox):
    if _name.startswith('test_') and _name not in MoveCli.__dict__:
        setattr(MoveCli, _name, None)


@linux_only                     # a Wine prefix; Windows: test_install_win.WindowsMove
class MoveCopy(unittest.TestCase):
    """Across drives: a copy, checked file by file, the old one set aside."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.base, self.dest = t / 'A', t / 'B'
        self.game = self.base / 'g'
        (self.game / 'pfx/drive_c/G').mkdir(parents=True)
        (self.game / 'pfx/drive_c/G/g.exe').write_bytes(b'x' * 1000)
        os.symlink('.', self.game / 'pfx' / 'pfx')
        self.entry = {'slug': 'g', 'display_name': 'G', 'exe': str(self.game / 'pfx/drive_c/G/g.exe'),
                      'compatdata': str(self.game), 'wine_prefix': str(self.game / 'pfx'),
                      'launcher': str(self.game / 'launch.sh'), 'source': 'install',
                      'install_base': str(self.base)}
        self.updates = {}

    def _move(self, corrupt=False):
        real_rename = os.rename

        def rename(a, b):
            if Path(a) == self.game and Path(b).parent == self.dest:
                raise OSError(errno.EXDEV, "cross-device")
            return real_rename(a, b)
        real_copy = MV._copy

        def copy(src, dest, total):
            real_copy(src, dest, total)
            if corrupt:
                (dest / 'pfx/drive_c/G/g.exe').write_bytes(b'x')
        with mock.patch.object(MV.os, 'rename', rename), \
                mock.patch.object(MV, '_copy', copy), \
                patch_cli('registry_get', side_effect=lambda s: dict(self.entry)), \
                patch_cli('update_registry_entry',
                          side_effect=lambda s, **kw: self.updates.update(kw)), \
                patch_cli('update_registry_size'), patch_cli('cmd_apply'), \
                patch_cli('prefix_processes', return_value=[]), \
                patch_cli('steam_is_running', return_value=False), \
                patch_cli('make_game_dir', side_effect=lambda p: p.mkdir() or False), \
                patch_cli('emit_event'), mock.patch('sys.stdout'):
            return MV.cmd_move(self.base, 'g', str(self.dest), as_json=True)

    def test_copies_checks_and_keeps_the_old_copy(self):
        self.assertEqual(self._move(), 0)
        self.assertTrue((self.dest / 'g/pfx/drive_c/G/g.exe').is_file())
        self.assertTrue(os.path.islink(self.dest / 'g/pfx/pfx'))
        self.assertEqual(self.updates['wine_prefix'], str(self.dest / 'g/pfx'))
        self.assertFalse(self.game.exists())
        self.assertEqual(len(list(self.base.glob('g.old-*'))), 1)

    def test_a_bad_copy_changes_nothing(self):
        self.assertEqual(self._move(corrupt=True), 1)
        self.assertTrue(self.game.is_dir())
        self.assertFalse((self.dest / 'g').exists())
        self.assertEqual(self.updates, {})

    def test_rebase_only_touches_paths_inside(self):
        e = {'compatdata': '/a/g', 'wine_prefix': '/a/g/pfx', 'exe': '/a/gx/other.exe',
             'launcher': '/a/g/launch.sh'}
        r = MV.rebase(e, Path('/a/g'), Path('/b/g'))
        self.assertEqual(r, {'compatdata': '/b/g', 'wine_prefix': '/b/g/pfx',
                             'launcher': '/b/g/launch.sh', 'install_base': '/b'})


class FakeBtrfs:
    """Subvolumes as plain folders, snapshots as copies."""

    def __call__(self, *args):
        if args[:2] == ('subvolume', 'create'):
            Path(args[2]).mkdir()
        elif args[:2] == ('subvolume', 'snapshot'):
            shutil.copytree(args[2], args[3], symlinks=True)
        else:
            raise AssertionError(args)


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.game = self.base / 'g'
        self.game.mkdir()
        (self.game / 'save.dat').write_text('v1')
        self.entry = {'slug': 'g', 'display_name': 'G', 'compatdata': str(self.game),
                      'wine_prefix': str(self.game / 'pfx'), 'source': 'install',
                      'install_base': str(self.base)}
        for p in (mock.patch.object(SN, '_btrfs', FakeBtrfs()),
                  mock.patch.object(SN, 'is_subvolume', lambda p: True),
                  mock.patch.object(SN.shutil, 'which', lambda n: '/usr/bin/' + n),
                  patch_cli('registry_get', side_effect=lambda s: dict(self.entry)),
                  patch_cli('prefix_processes', return_value=[]),
                  patch_cli('load_config', return_value={'snapshots_keep': 2})):
            p.start()
            self.addCleanup(p.stop)
        self.n = 0
        real_dt = SN.datetime

        class Clock(real_dt):                          # a new second for every snapshot
            @classmethod
            def now(cls, tz=None):
                self.n += 1
                return real_dt(2026, 10, 20, 12, 0, self.n)
        p = mock.patch.object(SN, 'datetime', Clock)
        p.start()
        self.addCleanup(p.stop)

    def test_take_keeps_the_newest_and_lists_newest_first(self):
        for reason in ('manual', 'update', 'proton'):
            SN.take('g', reason, self.base)
        snaps = SN.list_snapshots(self.entry, 'g')
        self.assertEqual([s['reason'] for s in snaps], ['proton', 'update'])
        self.assertEqual(snaps[0]['label'], "Before a Proton change")
        self.assertTrue((self.base / '.nexus-snapshots' / 'g').is_dir())

    def test_rollback_restores_and_can_itself_be_undone(self):
        snap = SN.take('g', 'update', self.base)
        (self.game / 'save.dat').write_text('v2')
        SN.rollback('g', snap.name, self.base)
        self.assertEqual((self.game / 'save.dat').read_text(), 'v1')
        before = [s for s in SN.list_snapshots(self.entry, 'g') if s['reason'] == 'before-rollback']
        self.assertEqual(len(before), 1)
        self.assertEqual((Path(before[0]['path']) / 'save.dat').read_text(), 'v2')
        self.assertEqual([p.name for p in self.base.iterdir() if 'rollback-' in p.name], [])

    def test_bad_names_and_a_running_game_are_refused(self):
        with self.assertRaises(SN.SnapshotError):
            SN.rollback('g', '../../etc', self.base)
        snap = SN.take('g', 'manual', self.base)
        with patch_cli('prefix_processes', return_value=[42]):
            with self.assertRaises(SN.SnapshotError):
                SN.rollback('g', snap.name, self.base)
        with self.assertRaises(SN.SnapshotError):
            SN.take('g', 'Bad Reason', self.base)

    def test_not_a_subvolume_means_no_snapshot_not_an_error(self):
        with mock.patch.object(SN, 'is_subvolume', lambda p: False):
            self.assertIsNone(SN.take('g', 'update', self.base))

    def test_delete_removes_one(self):
        snap = SN.take('g', 'manual', self.base)
        SN.delete('g', snap.name, self.base)
        self.assertEqual(SN.list_snapshots(self.entry, 'g'), [])


@linux_only
class FsType(unittest.TestCase):
    def test_reads_mountinfo_longest_match(self):
        info = ("22 1 0:21 / / rw - ext4 /dev/a rw\n"
                "40 22 0:35 /@games /home/u/Games rw,noatime - btrfs /dev/b rw\n")
        with mock.patch.object(SN.Path, 'read_text', lambda self, **kw: info), \
                mock.patch.object(SN, 'existing_ancestor', lambda p: p), \
                mock.patch.object(SN.os.path, 'realpath', lambda p: str(p)):
            self.assertEqual(SN.fs_type(Path('/home/u/Games/FitGirl/x')), 'btrfs')
            self.assertEqual(SN.fs_type(Path('/home/u/other')), 'ext4')


@unittest.skipUnless(os.environ.get('NEXUS_TEST_BTRFS'), "set NEXUS_TEST_BTRFS=<dir on btrfs>")
class RealBtrfs(unittest.TestCase):
    def test_subvolume_snapshot_rollback_delete(self):
        base = Path(os.environ['NEXUS_TEST_BTRFS']) / f'nexus-test-{os.getpid()}'
        base.mkdir()
        self.addCleanup(lambda: SN.remove_game_dir(base))
        game = base / 'g'
        self.assertTrue(SN.make_game_dir(game, {'subvolumes': True}))
        self.assertTrue(SN.is_subvolume(game))
        (game / 'f').write_text('one')
        entry = {'slug': 'g', 'display_name': 'G', 'compatdata': str(game),
                 'source': 'install', 'install_base': str(base)}
        with patch_cli('registry_get', return_value=entry), \
                patch_cli('prefix_processes', return_value=[]):
            snap = SN.take('g', 'manual', base, keep=3)
            (game / 'f').write_text('two')
            SN.rollback('g', snap.name, base)
            self.assertEqual((game / 'f').read_text(), 'one')
            self.assertTrue(SN.is_subvolume(game))
            for s in SN.list_snapshots(entry, 'g'):
                SN.delete('g', s['name'], base)
        self.assertEqual(SN.remove_game_dir(game), [])


class Names(unittest.TestCase):
    def test_guess_name(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, 'setup_hollow_knight_1.5.78.11833_(64bit)_(43546).exe')
            f.write_bytes(b'MZ')
            with patch_cli('setup_product_name', return_value=None):
                self.assertEqual(IG.guess_name(f), 'Hollow knight')
            with patch_cli('setup_product_name', return_value='Hollow Knight'):
                self.assertEqual(IG.guess_name(f), 'Hollow Knight')
            folder = Path(d, 'Celeste_v1.4')
            folder.mkdir()
            self.assertTrue(IG.guess_name(folder).startswith('Celeste'))


@linux_only
class TryProton(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launcher = Path(self.tmp.name, 'launch.sh')
        self.proton = Path(self.tmp.name, 'GE-Proton11-9', 'proton')
        self.proton.parent.mkdir()
        self.proton.write_text('')

    def _launch(self, text):
        self.launcher.write_text(text)
        started = []
        entry = {'slug': 'g', 'launcher': str(self.launcher)}
        with patch_cli('registry_get', return_value=entry), \
                patch_cli('prefix_processes', return_value=[]), \
                patch_cli('proton_by_name', return_value=self.proton), \
                mock.patch('nexus.steamlib.running', return_value={'slugs': set()}), \
                mock.patch.object(cli.sunshine.subprocess, 'Popen',
                                  side_effect=lambda cmd, **k: started.append(k.get('env'))
                                  or mock.Mock(wait=mock.Mock(side_effect=cli.sunshine.subprocess.TimeoutExpired('x', 3)))), \
                mock.patch('sys.stdout'), mock.patch('sys.stderr'):
            try:
                cli.cmd_launch(Path(self.tmp.name), 'g', try_proton='GE-Proton11-9')
            except SystemExit as e:
                return started, e.code
        return started, 0

    def test_umu_launcher_gets_protonpath_for_one_run(self):
        started, code = self._launch('DEFAULT_PROTONPATH=/x\nexport PROTONPATH=...\n')
        self.assertEqual(code, 0)
        self.assertEqual(started[0]['PROTONPATH'], str(self.proton.parent))

    def test_other_runners_say_how_to_pin_it_instead(self):
        started, code = self._launch('"/x/proton" run game.exe\n')
        self.assertEqual((started, code), ([], 1))


if __name__ == '__main__':
    unittest.main()
