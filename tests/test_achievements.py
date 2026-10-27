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

    def test_rarest_order(self):
        pct = {APPID: {'A0': 50.0, 'A3': 2.5, 'A31': 2.5, 'A5': 0.1}, 7: {}}
        got = A.rarest(self.root, ACCOUNT, pct, n=5)
        # A5 isn't unlocked; the tie at 2.5 % goes newest first.
        self.assertEqual([a['title'] for a in got], ['First 3', 'Secret', 'First 0'])
        self.assertEqual(got[0]['percent'], 2.5)
        self.assertEqual(A.rarest(self.root, ACCOUNT, {APPID: {}}), [])

    def test_corrupt_file_is_ignored(self):
        (self.stats / f'UserGameStatsSchema_{APPID}.bin').write_bytes(b'\x00garbage')
        self.assertIsNone(A.achievements(self.root, ACCOUNT, APPID))


class GlobalPercentages(unittest.TestCase):
    """Rarest needs Steam's public global percentages: parsed, cached like
    the community schema, and cache-only for a while once offline."""
    BODY = json.dumps({'achievementpercentages': {'achievements': [
        {'name': 'A1', 'percent': '12.5'}, {'name': 'A2', 'percent': 0.4},
        {'name': 'bad', 'percent': 'x'}, {'percent': 3}]}}).encode()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'achpct'

    def test_parse_percentages(self):
        self.assertEqual(A.parse_percentages(self.BODY), {'A1': 12.5, 'A2': 0.4})
        self.assertEqual(A.parse_percentages(b'<html>'), {})
        self.assertEqual(A.parse_percentages(b'{"achievementpercentages": {}}'), {})

    def test_closest_order(self):
        counts = {'a': [9, 10], 'b': [1, 2], 'c': [10, 10], 'd': [0, 5], 'e': [18, 20]}
        self.assertEqual(A.closest(counts, 3), ['a', 'e', 'b'])     # done and unstarted left out

    def test_cache_empty_and_offline(self):
        import os
        import time
        from nexus import net
        calls = []

        def fetch(body):
            def f():
                calls.append(1)
                if isinstance(body, Exception):
                    raise body
                return body
            return f
        self.assertEqual(A.global_percentages(5, self.dir, fetch=fetch(self.BODY)),
                         {'A1': 12.5, 'A2': 0.4})
        self.assertFalse(A.needs_fetch(5, self.dir))
        self.assertEqual(A.global_percentages(5, self.dir, fetch=fetch(b'')), {'A1': 12.5,
                                                                                'A2': 0.4})
        self.assertEqual(len(calls), 1)                         # fresh: not asked again
        old = time.time() - A.PERCENT_AGE - 10
        os.utime(self.dir / '5.json', (old, old))
        self.assertTrue(A.needs_fetch(5, self.dir))
        # A stale good copy is never replaced by an empty answer.
        self.assertEqual(A.global_percentages(5, self.dir, fetch=fetch(b'{}')), {'A1': 12.5,
                                                                                  'A2': 0.4})
        self.assertFalse(A.needs_fetch(5, self.dir))
        # An empty answer for a new game is trusted only EMPTY_PERCENT_AGE.
        self.assertEqual(A.global_percentages(6, self.dir, fetch=fetch(b'{}')), {})
        old = time.time() - A.EMPTY_PERCENT_AGE - 10
        os.utime(self.dir / '6.json', (old, old))
        self.assertTrue(A.needs_fetch(6, self.dir))
        # Offline: the marker makes the next lookups cache-only.
        n = len(calls)
        self.assertEqual(A.global_percentages(6, self.dir, fetch=fetch(net.NetError('down'))), {})
        self.assertEqual(A.global_percentages(7, self.dir, fetch=fetch(self.BODY)), {})
        self.assertEqual(len(calls), n + 1)
        self.assertEqual(A.global_percentages(7, self.dir, allow_fetch=False), {})


if __name__ == '__main__':
    unittest.main()
