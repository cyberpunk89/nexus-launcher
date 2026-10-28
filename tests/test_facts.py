"""The game facts index (nexus/facts.py): genre and mode maps, Steam's
release dates, the merge order and its Game-db merge point, the cache-only
build, and the fill's hourly budget and backoff. Network calls are mocked;
caches live in temp dirs."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import facts, hltb, net, storeinfo  # noqa: E402


def appdetails(appid, **data):
    base = {'name': 'Elden Ring', 'short_description': 'Rise, Tarnished.',
            'genres': [{'description': 'Action'}, {'description': 'RPG'}],
            'developers': ['FromSoftware'], 'publishers': ['Bandai Namco'],
            'categories': [{'id': 2, 'description': 'Single-player'},
                           {'id': 38, 'description': 'Online Co-op'}],
            'release_date': {'coming_soon': False, 'date': '24 Feb, 2022'}}
    return json.dumps({str(appid): {'success': True, 'data': {**base, **data}}}).encode()


class TempCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        for mod in (facts, storeinfo, hltb):
            p = mock.patch.object(mod, 'CACHE_DIR', self.cache)
            p.start()
            self.addCleanup(p.stop)

    def store(self, appid, d, age=0):
        path = self.cache / 'store' / f'{appid}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d), encoding='utf-8')
        if age:
            t = time.time() - age
            os.utime(path, (t, t))

    def hltb_hit(self, name, main, plus=None, complete=None):
        path = hltb._path(hltb.title_key(name))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'hit': {'id': 1, 'name': name, 'main': main, 'plus': plus,
                                            'complete': complete, 'url': None}}),
                        encoding='utf-8')


class Genres(unittest.TestCase):
    def test_steam_and_igdb_names_map_to_nexus_genres(self):
        self.assertEqual(facts.nexus_genres(['Action', 'RPG', 'Indie', 'Early Access',
                                             'Free to Play', 'Action']),
                         ['Action', 'RPG', 'Indie'])
        self.assertEqual(facts.nexus_genres(['Role-playing (RPG)', 'Shooter', 'Platform',
                                             'Real Time Strategy (RTS)', 'Simulator', 'Sport',
                                             'Puzzle', 'Racing', 'Adventure']),
                         ['RPG', 'Shooter', 'Platformer', 'Strategy', 'Simulation', 'Sports',
                          'Puzzle', 'Racing', 'Adventure'])
        self.assertEqual(facts.nexus_genres(None), [])
        self.assertTrue(set(facts.GENRE_MAP.values()) <= set(facts.NEXUS_GENRES))


class Modes(unittest.TestCase):
    def test_categories_by_id_and_description(self):
        self.assertEqual(facts.modes([{'id': 2}, {'id': 38}]), ['single', 'coop', 'online'])
        self.assertEqual(facts.modes([{'id': 39}, {'id': 24}]), ['coop', 'local'])
        self.assertEqual(facts.modes([{'id': 9999, 'name': 'Online Co-op'}]), ['coop', 'online'])
        self.assertEqual(facts.modes([]), [])

    def test_plain_multiplayer_is_online(self):
        self.assertEqual(facts.modes([{'id': 2}, {'id': 1}]), ['single', 'online'])
        self.assertEqual(facts.modes([{'id': 1}, {'id': 24}]), ['local'])

    def test_v1_cache_modes_unknown(self):
        self.assertIsNone(facts.modes(None))


class Release(unittest.TestCase):
    def test_steam_date_formats(self):
        for text, want in (('17 Sep, 2020', (2020, '2020-09-17')),
                           ('Sep 17, 2020', (2020, '2020-09-17')),
                           ('Oct 2026', (2026, '2026-10')),
                           ('October, 2026', (2026, '2026-10')),
                           ('Q2 2027', (2027, '2027')), ('2027', (2027, '2027')),
                           ('2014-09-30T00:00:00.000Z', (2014, '2014-09-30')),
                           ('Coming soon', (None, None)), ('To be announced', (None, None)),
                           ('', (None, None)), (None, (None, None)), ('1 Jan, 1800', (None, None))):
            self.assertEqual(facts.parse_release(text), want, text)


class Status(unittest.TestCase):
    def test_coming_soon_early_access_released(self):
        def layer(**k):
            return facts.from_steam({'v': storeinfo.STORE_V, 'genres': [], **k})
        self.assertEqual(facts.status_from_steam(layer(coming_soon=True)), 'upcoming')
        self.assertEqual(facts.status_from_steam(
            layer(coming_soon=False, genres=['Indie', 'Early Access'])), 'early_access')
        self.assertEqual(facts.status_from_steam(layer(coming_soon=False)), 'released')
        self.assertIsNone(facts.status_from_steam(facts.from_steam({'genres': []})))   # v1
        self.assertIsNone(facts.status_from_steam(None))


STEAM = {'v': storeinfo.STORE_V, 'name': 'Elden Ring', 'blurb': 'Rise.',
         'genres': ['Action', 'RPG'], 'developers': ['FromSoftware'],
         'publishers': ['Bandai Namco'], 'release': '24 Feb, 2022', 'coming_soon': False,
         'categories': [{'id': 2, 'name': 'Single-player'}], 'metacritic': {'score': 94}}
GDB_ROW = {'appid': 1245620, 'title': 'ELDEN RING', 'genres': ['Role-playing (RPG)'],
           'developers': ['FromSoftware Inc.'], 'publishers': [],
           'releaseDate': '2022-02-25T00:00:00.000Z', 'comingSoon': False,
           'status': 'early_access', 'statusLabel': 'Early Access',
           'playLength': {'main': 50, 'mainExtra': 90, 'completionist': 133,
                          'source': 'igdb', 'count': 12},
           'igdbSlug': 'elden-ring', 'reviews': {'metacritic': 90}}


class Merge(unittest.TestCase):
    def local(self, **k):
        return {'appid': 1245620, 'name': 'Elden Ring repack', 'steam': facts.from_steam(STEAM),
                'hltb': facts.from_hltb({'main': 59.5, 'plus': 100, 'complete': 133.4}), **k}

    def test_local_only_names_every_source(self):
        r = facts.merge(self.local())
        self.assertEqual((r['name'], r['developers'], r['publishers'], r['year'], r['release']),
                         ('Elden Ring', ['FromSoftware'], ['Bandai Namco'], 2022, '2022-02-24'))
        self.assertEqual((r['genres'], r['modes'], r['status']),
                         (['Action', 'RPG'], ['single'], 'released'))
        self.assertEqual((r['description'], r['metacritic'], r['igdbSlug']), ('Rise.', 94, None))
        self.assertEqual(r['src'], {'name': 'steam', 'description': 'steam',
                                    'developers': 'steam', 'publishers': 'steam',
                                    'releaseText': 'steam', 'year': 'steam', 'release': 'steam',
                                    'metacritic': 'steam', 'genresRaw': 'steam',
                                    'genres': 'steam', 'modes': 'steam', 'status': 'steam',
                                    'length': 'hltb'})

    def test_hltb_length_has_null_count(self):
        self.assertEqual(facts.merge(self.local())['length'],
                         {'main': 59.5, 'plus': 100, 'complete': 133.4, 'source': 'hltb',
                          'count': None})
        self.assertIsNone(facts.merge(self.local(hltb=None))['length'])
        self.assertIsNone(facts.from_hltb({'main': None, 'plus': None, 'complete': None}))

    def test_none_row_changes_nothing(self):
        self.assertEqual(facts.merge(self.local(), None), facts.merge(self.local()))

    def test_gamedb_row_status_length_and_slug_win(self):
        r = facts.merge(self.local(hltb=None), GDB_ROW)
        self.assertEqual((r['status'], r['src']['status']), ('early_access', 'gamedb'))
        self.assertEqual(r['length'], {'main': 50, 'plus': 90, 'complete': 133,
                                       'source': 'igdb', 'count': 12})
        self.assertEqual((r['igdbSlug'], r['src']['igdbSlug']), ('elden-ring', 'gamedb'))
        self.assertEqual(r['developers'], ['FromSoftware'])              # Steam still first
        # Local HowLongToBeat beats IGDB's length, as on Game-db's own site.
        self.assertEqual(facts.merge(self.local(), GDB_ROW)['length']['source'], 'hltb')
        # With no Steam layer, Game-db fills the rest.
        r = facts.merge({'appid': 1, 'name': 'x', 'steam': None, 'hltb': None}, GDB_ROW)
        self.assertEqual((r['name'], r['genres'], r['year'], r['src']['genres']),
                         ('ELDEN RING', ['RPG'], 2022, 'gamedb'))
        self.assertIsNone(r['modes'])
        for key, want in (('alpha', 'early_access'), ('cancelled', 'cancelled'),
                          ('delisted', None), (None, None)):
            self.assertEqual(facts.from_gamedb({**GDB_ROW, 'status': key})['status'], want)


class Build(TempCache):
    def test_cache_only_and_no_store_id_no_entry(self):
        self.store(10, {**STEAM, 'name': 'Hades'})
        self.store(20, {'name': 'Old', 'genres': ['Action'], 'developers': ['A'],
                        'release': 'Sep 17, 2020'})                     # a v1 file
        self.hltb_hit('Hades', 22.5)
        lib = [{'slug': 'steam-10', 'appid': 10, 'name': 'Hades'},
               {'slug': 'repack', 'appid': 20, 'name': 'Old Game'},
               {'slug': 'no-id', 'appid': None, 'name': 'Sideloaded'},
               {'slug': 'steam-30', 'appid': 30, 'name': 'Not fetched'}]
        with mock.patch.object(net, 'get', side_effect=AssertionError('network')), \
                mock.patch.object(hltb, 'search', side_effect=AssertionError('network')):
            idx = facts.build(lib)
        self.assertEqual(set(idx['games']), {'steam-10', 'repack'})
        self.assertEqual(idx['games']['steam-10']['length']['main'], 22.5)
        old = idx['games']['repack']
        self.assertEqual((old['year'], old['publishers'], old['modes'], old['status']),
                         (2020, None, None, None))
        self.assertEqual(idx['missing'], {'steam': 2, 'hltb': 2})        # 20 (v1) and 30
        facts.write_index(idx)
        self.assertEqual(facts.read_index()['games'], idx['games'])
        facts.index_path().write_text('{"v": 0}', encoding='utf-8')
        self.assertEqual(facts.read_index()['games'], {})


class StoreSchema(TempCache):
    def test_v1_cache_is_refetched_and_served_offline(self):
        self.store(10, {'name': 'Old'})
        with mock.patch.object(net, 'get', return_value=appdetails(10)) as get:
            d = storeinfo.details(10)
        self.assertEqual(get.call_count, 1)
        self.assertEqual((d['v'], d['publishers'], d['coming_soon']), (2, ['Bandai Namco'], False))
        self.assertEqual(d['categories'][1], {'id': 38, 'name': 'Online Co-op'})
        self.store(11, {'name': 'Old'})
        with mock.patch.object(net, 'get', side_effect=net.NetError('offline')):
            self.assertEqual(storeinfo.details(11), {'name': 'Old'})
        self.assertEqual(storeinfo.cached_details(10)[0], True)
        self.assertEqual(storeinfo.cached_details(11), (False, {'name': 'Old'}))
        self.assertEqual(storeinfo.cached_details(12), (False, None))

    def test_null_appdetails_is_a_failure(self):
        errs = []
        with mock.patch.object(net, 'get', return_value=b'null'):
            self.assertIsNone(storeinfo.details(10, errors=errs))
        self.assertEqual(len(errs), 1)
        self.assertFalse((self.cache / 'store' / '10.json').exists())   # not "no such app"

    def test_errors_list_carries_status(self):
        errs = []
        with mock.patch.object(net, 'get', side_effect=net.NetError('slow down', 429)):
            storeinfo.details(10, errors=errs)
        self.assertEqual(errs[0].status, 429)


class Budget(TempCache):
    def test_forty_per_hour_then_wait(self):
        now = 1_000_000.0
        self.assertTrue(all(facts.take('steam', now) for _ in range(facts.BUDGET)))
        self.assertFalse(facts.take('steam', now + 10))
        self.assertAlmostEqual(facts.wait_seconds('steam', now + 10), facts.WINDOW - 10)
        self.assertTrue(facts.take('hltb', now))                       # its own budget

    def test_window_resets(self):
        now = 1_000_000.0
        for _ in range(facts.BUDGET):
            facts.take('steam', now)
        self.assertTrue(facts.take('steam', now + facts.WINDOW))
        self.assertEqual(facts.wait_seconds('steam', now + facts.WINDOW), 0)

    def test_429_blocks_an_hour(self):
        now = 1_000_000.0
        facts.report('steam', net.NetError('x', 429), now)
        self.assertFalse(facts.take('steam', now + 60))
        self.assertAlmostEqual(facts.wait_seconds('steam', now + 60), facts.BACKOFF - 60)
        self.assertTrue(facts.take('steam', now + facts.BACKOFF + 1))
        self.assertTrue(facts.take('hltb', now + 60))

    def test_failure_streak_blocks(self):
        now = 1_000_000.0
        facts.report('hltb', net.NetError('down'), now)
        facts.report('hltb', None, now)
        facts.report('hltb', net.NetError('down'), now)
        facts.report('hltb', net.NetError('down'), now)
        self.assertEqual(facts.wait_seconds('hltb', now), 0)
        facts.report('hltb', net.NetError('down'), now)
        self.assertGreater(facts.wait_seconds('hltb', now), 0)


class Fill(TempCache):
    LIB = [{'slug': 'steam-10', 'appid': 10, 'name': 'Elden Ring'},
           {'slug': 'repack', 'appid': 20, 'name': 'Some Repack'},
           {'slug': 'twin', 'appid': 10, 'name': 'Elden Ring'}]

    def gamedb_off(self):
        from nexus import gamedb
        return mock.patch.object(gamedb, 'lookup', return_value={})

    def test_steam_first_then_hltb(self):
        order = []
        with self.gamedb_off(), \
                mock.patch.object(net, 'get', side_effect=lambda url, **k: order.append(url)
                                  or appdetails(int(url.split('appids=')[1].split('&')[0]))), \
                mock.patch.object(hltb, 'search',
                                  side_effect=lambda n: order.append(n) or
                                  [{'game_id': 5, 'game_name': n, 'comp_main': 36000}]):
            res = facts.fill(self.LIB, 10, sleep=lambda s: None)
        self.assertEqual(res, {'steam': 2, 'hltb': 1, 'stopped': [], 'left': 0})
        self.assertEqual(len(order), 3)
        self.assertIn('appids=10', order[0])
        self.assertIn('appids=20', order[1])
        self.assertEqual(order[2], 'Elden Ring')            # the store's name, asked once
        idx = facts.build(self.LIB)
        self.assertEqual(idx['games']['repack']['length']['main'], 10.0)
        self.assertEqual(idx['missing'], {'steam': 0, 'hltb': 0})

    def test_n_caps_each_site(self):
        with self.gamedb_off(), mock.patch.object(net, 'get', return_value=appdetails(10)), \
                mock.patch.object(hltb, 'search', return_value=[]):
            res = facts.fill(self.LIB, 1, sleep=lambda s: None)
        self.assertEqual((res['steam'], res['hltb']), (1, 1))   # a miss is an answer
        self.assertGreater(res['left'], 0)

    def test_gamedb_length_skips_hltb(self):
        from nexus import gamedb
        self.store(10, STEAM)
        self.store(20, {**STEAM, 'name': 'Other'})
        with mock.patch.object(gamedb, 'lookup', return_value={10: {'hltb': {'main': 50}},
                                                               20: {'hltb': {'main': 9}}}):
            self.assertEqual(facts.todo(self.LIB), ([], []))

    def test_stops_on_rate_limit(self):
        with self.gamedb_off(), \
                mock.patch.object(net, 'get', side_effect=net.NetError('slow', 429)) as get, \
                mock.patch.object(hltb, 'search', side_effect=net.NetError('down')):
            res = facts.fill(self.LIB, 10, sleep=lambda s: None)
        self.assertEqual(get.call_count, 1)
        self.assertEqual((res['steam'], res['stopped'][0]), (0, 'steam'))
        self.assertGreater(facts.wait_seconds('steam'), 0)

    def test_failed_fetch_stays_stale(self):
        self.store(10, STEAM, age=8 * 86400)
        with self.gamedb_off(), mock.patch.object(net, 'get', side_effect=net.NetError('down')), \
                mock.patch.object(hltb, 'search', side_effect=net.NetError('down')):
            res = facts.fill(self.LIB[:1], 10, sleep=lambda s: None)
        self.assertEqual(res['steam'], 0)
        self.assertEqual(storeinfo.cached_details(10), (False, STEAM))
        self.assertEqual(facts.todo(self.LIB[:1])[0], [10])


class NetPeek(unittest.TestCase):
    def test_cached_json_valid_and_peek(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'x.json')
            path.write_text('{"v": 1}', encoding='utf-8')
            ok = (lambda v: v.get('v') == 2)
            self.assertEqual(net.cached_json(path, 3600, lambda: {'v': 2}, valid=ok), {'v': 2})
            self.assertEqual(net.peek_json(path, 3600, ok), (True, {'v': 2}))
            path.write_text('{"v": 1}', encoding='utf-8')
            self.assertEqual(net.peek_json(path, 3600, ok), (False, {'v': 1}))

            def down():
                raise net.NetError('offline')
            self.assertEqual(net.cached_json(path, 3600, down, valid=ok), {'v': 1})
            self.assertEqual(net.peek_json(Path(d, 'none.json'), 3600), (False, None))


if __name__ == '__main__':
    unittest.main()
