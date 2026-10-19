"""Steam achievements from the client's stats cache (nexus/achievements.py),
against fixture files written with python-vdf's binary format."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import achievements as A  # noqa: E402

try:
    import vdf
except ImportError:
    vdf = None

APPID, ACCOUNT = 4242, 111


def ach(name, title, hidden=0, icon='on.jpg'):
    return {'name': name, 'display': {'name': {'english': title, 'german': title + ' (de)'},
                                      'desc': {'english': f'Do {title}'}, 'hidden': hidden,
                                      'icon': icon, 'icon_gray': 'off.jpg'}}


@unittest.skipIf(vdf is None, "python-vdf not installed")
class Achievements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stats = self.root / 'appcache' / 'stats'
        self.stats.mkdir(parents=True)
        bits1 = {str(i): ach(f'A{i}', f'First {i}') for i in range(32)}
        bits1['31'] = ach('A31', 'Secret', hidden=1)
        schema = {str(APPID): {'gamename': 'Test', 'stats': {
            '1': {'type': 'ACHIEVEMENTS', 'bits': bits1},
            '2': {'type': '4', 'bits': {'0': ach('B0', 'Second 0')}},
            '9': {'type': '1', 'name': 'kills'}}}}            # a plain stat: no bits
        (self.stats / f'UserGameStatsSchema_{APPID}.bin').write_bytes(vdf.binary_dumps(schema))
        self.user = self.stats / f'UserGameStats_{ACCOUNT}_{APPID}.bin'
        # Bits 0, 3 and 31 (the sign bit: Steam stores data as a signed int32).
        data = (1 << 0) | (1 << 3) | (1 << 31)
        self.user.write_bytes(vdf.binary_dumps({'cache': {
            'crc': 1, 'PendingChanges': 0,
            '1': {'data': data - (1 << 32),
                  'AchievementTimes': {'0': 100, '3': 300, '31': 200}},
            '9': {'data': 17}}}))

    def tearDown(self):
        self.tmp.cleanup()
        A._CACHE.clear()

    def test_schema_and_progress(self):
        r = A.achievements(self.root, ACCOUNT, APPID)
        self.assertEqual((r['unlocked'], r['total']), (3, 33))
        self.assertEqual([a['name'] for a in r['items'][:3]], ['A3', 'A31', 'A0'])  # newest first
        self.assertEqual(r['items'][0]['time'], 300)
        secret = next(a for a in r['items'] if a['name'] == 'A31')
        self.assertTrue(secret['hidden'])
        locked = next(a for a in r['items'] if not a['unlocked'])
        self.assertTrue(locked['icon_url'].endswith(f'/{APPID}/off.jpg'))
        self.assertTrue(r['items'][0]['icon_url'].endswith(f'/{APPID}/on.jpg'))

    def test_language(self):
        r = A.achievements(self.root, ACCOUNT, APPID, lang='german')
        self.assertTrue(r['items'][0]['title'].endswith('(de)'))
        r = A.achievements(self.root, ACCOUNT, APPID, lang='french')    # falls back
        self.assertEqual(r['items'][0]['title'], 'First 3')

    def test_no_progress_and_no_game(self):
        self.user.unlink()
        r = A.achievements(self.root, ACCOUNT, APPID)
        self.assertEqual((r['unlocked'], r['total']), (0, 33))
        self.assertIsNone(A.achievements(self.root, ACCOUNT, 1))
        self.assertEqual(A.counts(self.root, ACCOUNT, [1]), {})

    def test_counts_cache_and_recent(self):
        cache = self.root / 'cache.json'
        self.assertEqual(A.counts(self.root, ACCOUNT, [APPID, 7], cache), {APPID: [3, 33]})
        self.assertIn(f'{ACCOUNT}:{APPID}', json.loads(cache.read_text()))
        A._CACHE.clear()
        # A cache hit doesn't parse anything.
        orig, A.schema = A.schema, lambda *a, **k: self.fail("parsed despite cache")
        try:
            self.assertEqual(A.counts(self.root, ACCOUNT, [APPID], cache), {APPID: [3, 33]})
        finally:
            A.schema = orig
        recent = A.recent(self.root, ACCOUNT, [APPID], n=2)
        self.assertEqual([a['title'] for a in recent], ['First 3', 'Secret'])

    def test_corrupt_file_is_ignored(self):
        (self.stats / f'UserGameStatsSchema_{APPID}.bin').write_bytes(b'\x00garbage')
        self.assertIsNone(A.achievements(self.root, ACCOUNT, APPID))


if __name__ == '__main__':
    unittest.main()
