"""Settings export/import (--export-settings / --import-settings): what goes
in, what's left out (secrets, saves), and what an import will and won't
touch. In the test HOME only."""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import paths as NX  # noqa: E402
from nexus.cli import backup as B  # noqa: E402
from tests.oskind import why_not_private  # noqa: E402

CONFIG = """# mine
[steamgriddb]
api_key = "SECRET-KEY"

[install]
base = "~/Games/FitGirl"

[torrent]
client = "transmission"
password = "hunter2"
"""


class Backup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        from nexus.cli import common
        common._BACKED_UP.clear()          # one .bak per file per minute and process
        self.saved = {}
        for p in (NX.CONFIG_PATH, NX.CONFIG_DIR / 'library.json', NX.REGISTRY_PATH,
                  NX.DATA_DIR / 'sessions.jsonl'):
            self.saved[p] = p.read_bytes() if p.exists() else None
        self.addCleanup(self._restore)
        NX.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        NX.DATA_DIR.mkdir(parents=True, exist_ok=True)
        NX.CONFIG_PATH.write_text(CONFIG)
        (NX.CONFIG_DIR / 'library.json').write_text(json.dumps({'favorites': ['here']}))
        self.here = self.t / 'Games' / 'here'
        self.here.mkdir(parents=True)
        NX.REGISTRY_PATH.write_text(json.dumps({'version': 1, 'games': {
            'here': {'slug': 'here', 'display_name': 'Here', 'compatdata': str(self.here)},
            'gone': {'slug': 'gone', 'display_name': 'Gone', 'compatdata': str(self.t / 'nope')}}}))
        (NX.DATA_DIR / 'sessions.jsonl').write_text('{"slug": "here", "minutes": 5}\n')

    def _restore(self):
        for p, data in self.saved.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
        for bak in list(NX.CONFIG_DIR.glob('*.bak*')) + list(NX.DATA_DIR.glob('games.json.bak*')):
            bak.unlink()

    def test_secrets_are_left_out_and_kept_on_import(self):
        r = B.export_settings(self.t / 'out')
        self.assertEqual(sorted(r['secrets_left_out']), ['steamgriddb.api_key', 'torrent.password'])
        with tarfile.open(r['path']) as tf:
            cfg = tf.extractfile('config/config.toml').read().decode()
            names = tf.getnames()
        self.assertNotIn('SECRET-KEY', cfg)
        self.assertNotIn('hunter2', cfg)
        self.assertIn('client = "transmission"', cfg)
        self.assertIn('data/games.json', names)
        self.assertFalse([n for n in names if 'saves' in n])
        # A new machine with its own key: the import keeps it, and the old
        # library.json is kept as a .bak.
        NX.CONFIG_PATH.write_text('[steamgriddb]\napi_key = "NEW-MACHINE"\n')
        (NX.CONFIG_DIR / 'library.json').write_text('{"favorites": []}')
        plan = B.import_settings(Path(r['path']))
        text = NX.CONFIG_PATH.read_text()
        self.assertIn('api_key = "NEW-MACHINE"', text)
        self.assertIn('base = "~/Games/FitGirl"', text)
        self.assertEqual(json.loads((NX.CONFIG_DIR / 'library.json').read_text())['favorites'],
                         ['here'])
        self.assertTrue(list(NX.CONFIG_DIR.glob('library.json.bak*')))
        self.assertEqual(why_not_private(NX.CONFIG_PATH), '')
        self.assertEqual(plan['keep_secrets'], ['steamgriddb.api_key'])

    def test_games_only_where_their_folder_is(self):
        r = B.export_settings(self.t / 'out.tar.gz')
        NX.REGISTRY_PATH.write_text(json.dumps({'version': 1, 'games': {}}))
        plan = B.import_settings(Path(r['path']), dry_run=True)
        self.assertEqual(plan['games_added'], ['here'])
        self.assertEqual([g['slug'] for g in plan['games_missing']], ['gone'])
        self.assertEqual(json.loads(NX.REGISTRY_PATH.read_text())['games'], {})   # dry run
        B.import_settings(Path(r['path']))
        self.assertEqual(sorted(json.loads(NX.REGISTRY_PATH.read_text())['games']), ['here'])

    def test_a_hostile_archive_writes_nothing_outside(self):
        bad = self.t / 'bad.tar.gz'
        with tarfile.open(bad, 'w:gz') as tf:
            for name, data in (('manifest.json', b'{"app": "nexus", "format": 1}'),
                               ('../../evil.txt', b'x'), ('/tmp/evil2', b'x'),
                               ('art/../../x/picks.json', b'{}'), ('art/ok/run.sh', b'x'),
                               ('config/library.json', b'{"favorites": ["z"]}')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        plan = B.import_settings(bad)
        self.assertEqual(plan['art'], 0)
        self.assertFalse((self.t.parent / 'evil.txt').exists())
        self.assertFalse((NX.ARTWORK_DIR / 'ok' / 'run.sh').exists())
        self.assertEqual(json.loads((NX.CONFIG_DIR / 'library.json').read_text())['favorites'],
                         ['z'])

    def test_not_a_settings_file(self):
        junk = self.t / 'junk.tar.gz'
        with tarfile.open(junk, 'w:gz') as tf:
            info = tarfile.TarInfo('config/config.toml')
            info.size = 1
            tf.addfile(info, io.BytesIO(b'x'))
        with self.assertRaises(B.BackupError):
            B.import_settings(junk)
        (self.t / 'plain.txt').write_text('hi')
        with self.assertRaises(B.BackupError):
            B.import_settings(self.t / 'plain.txt')

    def test_broken_toml_is_refused_before_writing(self):
        bad = self.t / 'badtoml.tar.gz'
        with tarfile.open(bad, 'w:gz') as tf:
            for name, data in (('manifest.json', b'{"app": "nexus", "format": 1}'),
                               ('config/config.toml', b'[oops\nkey = ')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        with self.assertRaises(B.BackupError):
            B.import_settings(bad)
        self.assertEqual(NX.CONFIG_PATH.read_text(), CONFIG)


if __name__ == '__main__':
    unittest.main()
