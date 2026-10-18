"""Tests for nexus/cleanup.py rules and removal."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from nexus import cleanup as C  # noqa: E402


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_logs_keep_newest_per_game(self):
        logs = self.d / 'logs'
        logs.mkdir()
        old = time.time() - 60 * 86400
        for name in ('game-20250101-000000.log', 'game-20250102-000000.log',
                     'other-20250101-000000.log'):
            p = logs / name
            p.write_text('x')
            os.utime(p, (old, old))
        os.utime(logs / 'game-20250102-000000.log', (old + 10, old + 10))
        got = sorted(Path(c['path']).name for c in C.old_logs(logs))
        self.assertEqual(got, ['game-20250101-000000.log'])   # newest per game survives

    def test_save_backups_keep_three_and_never_preselect(self):
        g = self.d / 'saves' / 'g'
        for ts in ('20250101-000000', '20250102-000000', '20250103-000000',
                   '20250104-000000', '20250105-000000'):
            (g / ts).mkdir(parents=True)
        got = C.old_save_backups(self.d / 'saves')
        self.assertEqual(sorted(Path(c['path']).name for c in got),
                         ['20250101-000000', '20250102-000000'])
        self.assertFalse(any(c['preselect'] for c in got))

    def test_shader_caches_only_for_missing_apps(self):
        sc = self.d / 'lib' / 'steamapps' / 'shadercache'
        for i in ('10', '20', 'x'):
            (sc / i).mkdir(parents=True)
        got = C.shader_caches([self.d / 'lib'], keep_ids={10})
        self.assertEqual([Path(c['path']).name for c in got], ['20'])

    def test_saves_block_preselect(self):
        base = self.d / 'base'
        (base / 'g.old-20250101-000000').mkdir(parents=True)
        got = C.force_leftovers(base, lambda p: True)
        self.assertTrue(got[0]['saves'])
        self.assertFalse(got[0]['preselect'])

    def test_stale_data(self):
        (self.d / 'art' / 'gone').mkdir(parents=True)
        (self.d / 'art' / 'kept').mkdir(parents=True)
        (self.d / 'integ').mkdir()
        (self.d / 'integ' / 'gone.json').write_text('{}')
        got = C.stale_data({'kept'}, self.d / 'art', self.d / 'integ')
        self.assertEqual(len(got), 2)

    def test_manual_trash_writes_trashinfo(self):
        victim = self.d / 'victim dir'
        victim.mkdir()
        (victim / 'f').write_text('data')
        with mock.patch.object(C, 'TRASH', self.d / 'Trash'), \
                mock.patch.object(C.shutil, 'which', return_value=None):
            C.trash(victim)
        self.assertFalse(victim.exists())
        self.assertTrue((self.d / 'Trash' / 'files' / 'victim dir' / 'f').exists())
        info = (self.d / 'Trash' / 'info' / 'victim dir.trashinfo').read_text()
        self.assertIn('Path=', info)
        self.assertIn('victim%20dir', info)
        self.assertIn('DeletionDate=', info)


if __name__ == '__main__':
    unittest.main()
