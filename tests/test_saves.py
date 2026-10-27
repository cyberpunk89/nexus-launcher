"""Saves of a native Windows install, found through the Ludusavi manifest
(nexus/saves.py) and backed up and restored by cli/savedata.py. On a small
synthetic manifest, in the test HOME, with a fake user-folder layout — so it
runs the same on Linux and Windows, offline."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import net, saves  # noqa: E402
from nexus.cli import savedata  # noqa: E402

MANIFEST = '''---
"! Bastard Gold !":
  installDir:
    "! Bastard Gold !": {}
  registry:
    HKEY_CURRENT_USER/SOFTWARE/Gold:
      tags:
        - save
  steam:
    id: 449940
"Anyway: The Game":
  cloud:
    steam: true
  files:
    "<winLocalAppData>/anyway":
      tags:
        - save
      when:
        - os: windows
    "<winLocalAppData>/anyway/settings.ini":
      tags:
        - config
    "<xdgData>/anyway":
      when:
        - os: linux
  id:
    steamExtra:
      - 867990
  steam:
    id: 866510
Hollow Knight™:
  files:
    "<home>/AppData/LocalLow/Team Cherry/Hollow Knight/*.dat":
      tags:
        - save
      when:
        - os: windows
          store: steam
    "<winDocuments>/My Games/HK/<storeUserId>/user.sav":
      when:
        - os: windows
    "<base>/saves":
      tags:
        - save
    "<root>/userdata/367520/remote": {}
  steam:
    id: 367520
'It''s Quoted':
  files:
    "<winAppData>/Quoted \\"Co\\"/save.bin": {}
HK Alias:
  alias: Hollow Knight™
'''


def index():
    return saves.build_index(MANIFEST.splitlines())


class Index(unittest.TestCase):
    def test_keeps_windows_save_paths_only(self):
        g = index()['games']
        self.assertNotIn('! Bastard Gold !', g)          # registry only
        self.assertEqual(g['Anyway: The Game'], [['<winLocalAppData>/anyway', ['windows']]])
        paths = [p for p, _ in g['Hollow Knight™']]
        self.assertIn('<base>/saves', paths)
        self.assertIn('<winDocuments>/My Games/HK/<storeUserId>/user.sav', paths)
        self.assertEqual(g["It's Quoted"], [['<winAppData>/Quoted "Co"/save.bin', []]])

    def test_lookups(self):
        idx = index()
        self.assertEqual(saves.find_game(idx, 866510), 'Anyway: The Game')
        self.assertEqual(saves.find_game(idx, '867990'), 'Anyway: The Game')  # steamExtra
        self.assertIsNone(saves.find_game(idx, 449940))  # no save files
        self.assertEqual(saves.find_game(idx, None, 'Hollow Knight'), 'Hollow Knight™')
        self.assertEqual(saves.find_game(idx, None, 'hk alias'), 'Hollow Knight™')
        self.assertEqual(saves.find_game(idx, 1, 'anyway - the game'), 'Anyway: The Game')
        self.assertIsNone(saves.find_game(idx, None, 'Nope'))
        self.assertIsNone(saves.find_game(None, 866510))


class Fetch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for name, val in (('INDEX_PATH', d / 'index.json'), ('ETAG_PATH', d / 'etag')):
            p = mock.patch.object(saves, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

    def fetch(self, result):
        def f(headers):
            self.calls.append(headers)
            if isinstance(result, Exception):
                raise result
            return result
        return mock.patch.object(saves, '_fetch', f)

    def test_downloads_once_then_serves_cache(self):
        with self.fetch((MANIFEST.encode(), '"e1"')):
            idx = saves.load_index()
            self.assertEqual(saves.load_index(), idx)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(saves.ETAG_PATH.read_text(), '"e1"')
        self.assertIn('Anyway: The Game', idx['games'])

    def test_stale_index_revalidates_with_etag(self):
        with self.fetch((MANIFEST.encode(), '"e1"')):
            saves.load_index()
        old = saves.INDEX_PATH.stat().st_mtime - 8 * 86400
        os.utime(saves.INDEX_PATH, (old, old))
        with self.fetch(net.NetError('not modified', 304)):
            idx = saves.load_index()
        self.assertEqual(self.calls[-1], {'If-None-Match': '"e1"'})
        self.assertIn('Anyway: The Game', idx['games'])
        self.assertGreater(saves.INDEX_PATH.stat().st_mtime, old + 86400)

    def test_failure_serves_last_index_and_is_remembered(self):
        with self.fetch(net.NetError('offline')):
            self.assertIsNone(saves.load_index())
            self.assertIsNone(saves.load_index())
        self.assertEqual(len(self.calls), 1)        # not retried within FAIL_TTL


class BackupRestore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name)
        self.home = self.t / 'Users' / 'me'
        self.folders = saves.windows_folders(self.home)
        self.game = self.t / 'Games' / 'HK'
        p = mock.patch.object(savedata, 'SAVES_DIR', self.t / 'saves')
        p.start()
        self.addCleanup(p.stop)
        self.entry = index()['games']['Hollow Knight™']

    def put(self, path: Path, data='x'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def test_resolve_expands_globs_and_drops_noise(self):
        lowlow = self.home / 'AppData' / 'LocalLow' / 'Team Cherry' / 'Hollow Knight'
        a = self.put(lowlow / 'user1.dat')
        self.put(lowlow / 'player.log')
        b = self.put(self.home / 'Documents' / 'My Games' / 'HK' / '7656' / 'user.sav')
        c = self.put(self.game / 'saves' / 'slot1.sav')
        self.put(self.game / 'saves' / 'logs' / 'today.txt')
        got = sorted(f for f, _ in saves.resolve(self.entry, self.game, self.folders,
                                                  noise=savedata._is_save_noise))
        self.assertEqual(got, sorted([a, b, c]))

    def test_backup_and_restore_round_trip(self):
        a = self.put(self.game / 'saves' / 'slot1.sav', 'one')
        b = self.put(self.home / 'Documents' / 'My Games' / 'HK' / '42' / 'user.sav', 'two')
        dest, n, failed = savedata.backup_manifest_saves('hk', self.game, self.entry, self.folders)
        self.assertEqual((n, failed), (2, []))
        self.assertEqual(dest.parent, self.t / 'saves' / 'hk')
        mapping = json.loads((dest / savedata.SAVE_FILES_MAP).read_text())
        self.assertEqual(sorted(mapping.values()), sorted([str(a), str(b)]))
        a.unlink()
        b.write_text('changed')
        self.assertEqual(savedata.restore_manifest_saves('hk'), (2, []))
        self.assertEqual((a.read_text(), b.read_text()), ('one', 'two'))

    def test_nothing_to_back_up(self):
        self.assertIsNone(savedata.backup_manifest_saves('hk', self.game, self.entry, self.folders))
        self.assertIsNone(savedata.restore_manifest_saves('hk'))

    def test_restore_ignores_a_prefix_backup(self):
        self.put(self.t / 'saves' / 'hk' / '20260101-000000' / 'drive_c' / 'x.sav')
        self.assertIsNone(savedata.restore_manifest_saves('hk'))


if __name__ == '__main__':
    unittest.main()
