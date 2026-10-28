"""Tests for nexus/gamedb.py: settings, the no-redirect authenticated fetch,
the private stale-tolerant cache, cache-only lookups, the v2 meta/games sync
against a fake server, the cache-first ProtonDB/HLTB/review lookups on top,
and the CLI."""

import email.message
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import gamedb, hltb, protondb, storeinfo  # noqa: E402
from tests.oskind import why_not_private  # noqa: E402

TOKEN = 'gdb_' + 'A' * 43

# Mirrors the shape of Game-db's GET /api/nexus/v1/summary (schema 1).
PRICE = {'current': 12.49, 'currentStr': '€12.49', 'historicalLow': 12.49,
         'historicalLowStr': '€12.49', 'cutPct': 75, 'dealRatio': 1.0, 'onSale': True,
         'atHistoricalLow': True, 'preorder': False,
         'badge': {'kind': 'atl', 'text': 'All-time low'},
         'best': {'price': 12.49, 'priceStr': '€12.49', 'url': 'https://store.example/x',
                  'category': 'official', 'shop': 'Steam', 'cutPct': 75},
         'keyshop': 10.2, 'ggUrl': 'https://gg.deals/game/x/'}
SUMMARY = {
    'schema': 1, 'generatedAt': '2026-10-01T18:00:00.000Z', 'lastSyncAt': None,
    'user': {'handle': 'player', 'displayName': 'Player', 'steamId64': '76561197972611406'},
    'region': 'eu', 'currency': 'EUR',
    'radar': {'onSaleCount': 1, 'totalNow': 12.49, 'totalSavings': 37.47, 'currency': 'EUR'},
    'backlog': {'beaten': 1, 'inProgress': 1, 'played': 0, 'notStarted': 2, 'backlogHours': 40},
    'wishlist': [
        {'appid': 1086940, 'title': "Baldur's Gate 3", 'headerImage': None, 'priority': 1,
         'favorite': False, 'dateAdded': None, 'comingSoon': False, 'releaseDate': None,
         'releaseDateText': 'Aug 3, 2023', 'price': PRICE, 'inBundle': False,
         'bundleTitle': None, 'protonTier': 'platinum',
         'hltb': {'main': 75, 'mainExtra': 120, 'completionist': 180},
         'reviews': {'steamPercent': 96, 'steamDesc': 'Overwhelmingly Positive',
                     'metacritic': 96},
         'steamUrl': 'https://store.steampowered.com/app/1086940'},
        {'appid': 999, 'title': 'Future Game', 'headerImage': None, 'priority': 2,
         'favorite': False, 'dateAdded': None, 'comingSoon': True,
         'releaseDate': '2027-04-01T00:00:00.000Z', 'releaseDateText': 'Q2 2027',
         'price': None, 'inBundle': False, 'bundleTitle': None, 'protonTier': None,
         'hltb': None, 'reviews': {}, 'steamUrl': 'https://store.steampowered.com/app/999'},
    ],
    'upcoming': [{'appid': 999, 'title': 'Future Game', 'headerImage': None,
                  'releaseDate': '2027-04-01T00:00:00.000Z', 'releaseDateText': 'Q2 2027',
                  'countdown': 'Apr 2027', 'owned': False, 'protonTier': None,
                  'steamUrl': 'https://store.steampowered.com/app/999'}],
    'library': [{'appid': 620, 'title': 'Portal 2', 'headerImage': None,
                 'playtimeMinutes': 300, 'playtime2weeks': 0, 'lastPlayedAt': None,
                 'favorite': False, 'protonTier': 'platinum',
                 'hltb': {'main': 8.5, 'mainExtra': 13, 'completionist': 22},
                 'achievements': None,
                 'completion': {'state': 'in-progress', 'fraction': 0.59,
                                'label': 'In progress'},
                 'price': None}],
    'tonight': {'lt1h': [], '1to3': [{'appid': 620, 'title': 'Portal 2', 'headerImage': None,
                                      'reasons': ['~3.5h left to beat']}],
                '3plus': [], 'long': []},
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.result, Exception):
            raise self.result
        return FakeResponse(json.dumps(self.result).encode())


def http_error(code, headers=None):
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError('https://x/api', code, 'err', h, io.BytesIO(b''))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / 'config.toml'
        self.cache = root / 'cache' / 'gamedb'
        self.patches = [
            mock.patch.object(gamedb.paths, 'CONFIG_PATH', self.config),
            mock.patch.object(gamedb.paths, 'GAMEDB_CACHE', self.cache),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop('NEXUS_GAMEDB_TOKEN', None)
        os.environ.pop('NEXUS_GAMEDB_URL', None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def write_config(self, text='[gamedb]\nenabled = true\nurl = "https://gdb.example"\n'
                                f'token = "{TOKEN}"\n'):
        self.config.write_text(text)

    def opener(self, result):
        fake = FakeOpener(result)
        return fake, mock.patch.object(gamedb.urllib.request, 'build_opener',
                                       return_value=fake)


class Settings(Base):
    def test_defaults_when_missing(self):
        s = gamedb.settings()
        self.assertFalse(s['enabled'])
        self.assertIsNone(s['url'])            # no built-in server
        self.assertIsNone(s['url_error'])
        self.assertIsNone(s['token'])
        self.assertFalse(gamedb.configured(s))

    def test_config_and_env_override(self):
        self.write_config()
        s = gamedb.settings()
        self.assertTrue(gamedb.configured(s))
        self.assertEqual((s['url'], s['token_source']), ('https://gdb.example', 'config'))
        os.environ['NEXUS_GAMEDB_TOKEN'] = 'gdb_' + 'B' * 43
        os.environ['NEXUS_GAMEDB_URL'] = 'http://localhost:3000/'
        s = gamedb.settings()
        self.assertEqual((s['url'], s['token_source']), ('http://localhost:3000', 'env'))

    def test_broken_toml_is_disabled(self):
        self.config.write_text('[gamedb\nnope')
        self.assertFalse(gamedb.settings()['enabled'])

    def test_url_rules(self):
        self.assertEqual(gamedb.check_url('https://a.example/'), 'https://a.example')
        self.assertEqual(gamedb.check_url('http://127.0.0.1:3000'), 'http://127.0.0.1:3000')
        for bad in ('http://evil.example', 'ftp://a', 'not a url', ''):
            with self.assertRaises(gamedb.GameDbError):
                gamedb.check_url(bad)


class Fetch(Base):
    def test_sends_bearer_and_parses(self):
        fake, p = self.opener(SUMMARY)
        with p:
            data = gamedb._fetch('https://gdb.example', TOKEN)
        self.assertEqual(data['region'], 'eu')
        req = fake.requests[0]
        self.assertEqual(req.full_url, 'https://gdb.example/api/nexus/v1/summary')
        self.assertEqual(req.get_header('Authorization'), f'Bearer {TOKEN}')

    def test_error_kinds_never_leak_token(self):
        cases = [(http_error(401), 'auth'), (http_error(403), 'auth'),
                 (http_error(500), 'net'), (urllib.error.URLError('down'), 'net'),
                 (http_error(429, {'Retry-After': '120'}), 'rate')]
        for exc, kind in cases:
            _fake, p = self.opener(exc)
            with p, self.assertRaises(gamedb.GameDbError) as cm:
                gamedb._fetch('https://gdb.example', TOKEN)
            self.assertEqual(cm.exception.kind, kind)
            self.assertNotIn(TOKEN, str(cm.exception))
        self.assertEqual(cm.exception.retry_after, 120)

    def test_schema_mismatch(self):
        _fake, p = self.opener({**SUMMARY, 'schema': 2})
        with p, self.assertRaises(gamedb.GameDbError) as cm:
            gamedb._fetch('https://gdb.example', TOKEN)
        self.assertEqual(cm.exception.kind, 'schema')

    def test_redirects_are_refused(self):
        h = gamedb._no_redirect()()
        req = urllib.request.Request('https://gdb.example/api/nexus/v1/summary',
                                     headers={'Authorization': f'Bearer {TOKEN}'})
        with self.assertRaises(gamedb.GameDbError) as cm:
            h.redirect_request(req, None, 302, 'Found', {}, 'http://evil.example/steal')
        self.assertEqual(cm.exception.kind, 'net')
        self.assertNotIn(TOKEN, str(cm.exception))


class Cache(Base):
    def setUp(self):
        super().setUp()
        self.write_config()

    def test_fresh_cache_is_reused_and_private(self):
        fake, p = self.opener(SUMMARY)
        with p:
            first = gamedb.summary()
            second = gamedb.summary()
        self.assertEqual(len(fake.requests), 1)
        self.assertFalse(first['stale'] or second['stale'])
        f = self.cache / 'summary.json'
        self.assertEqual(why_not_private(f), '')
        self.assertNotIn(TOKEN, f.read_text())

    def test_refresh_bypasses_and_failure_serves_stale(self):
        _fake, p = self.opener(SUMMARY)
        with p:
            gamedb.summary()
        c = json.loads((self.cache / 'summary.json').read_text())
        c['fetched_at'] -= gamedb.MAX_AGE + 10
        (self.cache / 'summary.json').write_text(json.dumps(c))
        _fake, p = self.opener(http_error(401))
        with p:
            res = gamedb.summary()
        self.assertTrue(res['stale'])
        self.assertEqual(res['error']['kind'], 'auth')
        self.assertEqual(res['data']['region'], 'eu')

    def test_no_cache_and_no_network_raises(self):
        _fake, p = self.opener(urllib.error.URLError('down'))
        with p, self.assertRaises(gamedb.GameDbError):
            gamedb.summary()

    def test_token_change_invalidates_cache(self):
        _fake, p = self.opener(SUMMARY)
        with p:
            gamedb.summary()
        os.environ['NEXUS_GAMEDB_TOKEN'] = 'gdb_' + 'C' * 43
        self.assertEqual(gamedb.lookup([620]), {})
        fake, p = self.opener(SUMMARY)
        with p:
            gamedb.summary()
        self.assertEqual(len(fake.requests), 1)

    def test_retry_after_is_respected_even_with_refresh(self):
        _fake, p = self.opener(SUMMARY)
        with p:
            gamedb.summary()
        _fake, p = self.opener(http_error(429, {'Retry-After': '300'}))
        with p:
            res = gamedb.summary(refresh=True)
        self.assertEqual(res['error']['kind'], 'rate')
        fake, p = self.opener(SUMMARY)
        with p:
            res = gamedb.summary(refresh=True)
        self.assertEqual(fake.requests, [])
        self.assertEqual(res['error']['kind'], 'rate')


class Lookup(Base):
    def test_lookup_is_cache_only(self):
        self.write_config()
        with mock.patch.object(gamedb, '_fetch', side_effect=AssertionError('network!')):
            self.assertEqual(gamedb.lookup([1086940]), {})
        _fake, p = self.opener(SUMMARY)
        with p:
            gamedb.summary()
        with mock.patch.object(gamedb, '_fetch', side_effect=AssertionError('network!')):
            info = gamedb.lookup([1086940, 620, 999, 5])
        self.assertEqual(info[1086940]['list'], 'wishlist')
        self.assertTrue(info[1086940]['price']['atHistoricalLow'])
        self.assertEqual(info[620]['completion']['state'], 'in-progress')
        self.assertEqual(info[999]['release']['releaseDateText'], 'Q2 2027')
        self.assertEqual(info[620]['url'], 'https://gdb.example/game/620')
        self.assertNotIn(5, info)


# ---- v2 ----------------------------------------------------------------------

def iso(ago: float = 0) -> str:
    return datetime.fromtimestamp(time.time() - ago, timezone.utc).isoformat()


META = {'schema': 2, 'apiVersion': 2, 'features': ['summary', 'games'],
        'sources': {'steam': True, 'protondb': True, 'hltb': True, 'igdb': True},
        'games': {'pageSize': 500, 'sinceOverlapSeconds': 300}, 'scopes': ['read']}


def game(appid, **kw):
    g = {'appid': appid, 'title': f'Game {appid}', 'list': 'library', 'updatedAt': iso(),
         'genres': [], 'status': None, 'statusLabel': None,
         'reviews': {'steamPercent': 93, 'steamDesc': 'Very Positive', 'steamTotal': 1000,
                     'metacritic': 84},
         'proton': {'tier': 'platinum', 'trendingTier': 'gold', 'total': 134,
                    'fetchedAt': iso(3600)},
         'playLength': {'main': 10.5, 'mainExtra': 15, 'completionist': 30, 'source': 'hltb',
                        'count': None, 'fetchedAt': iso(3600)},
         'igdbSlug': None, 'steamUrl': f'https://store.steampowered.com/app/{appid}'}
    g.update(kw)
    return g


class FakeServer:
    """urllib's opener, routed by path: `routes[path]` is a dict (JSON 200),
    an exception, or a callable(query) returning either."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        u = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(u.query))
        r = self.routes[u.path] if u.path in self.routes else http_error(404)
        if callable(r):
            r = r(q)
        if isinstance(r, Exception):
            raise r
        return FakeResponse(json.dumps(r).encode())

    def paths(self):
        return [urllib.parse.urlsplit(r.full_url).path for r in self.requests]

    def queries(self, path):
        return [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.full_url).query))
                for r in self.requests if urllib.parse.urlsplit(r.full_url).path == path]


class V2Base(Base):
    def setUp(self):
        super().setUp()
        self.write_config()

    def server(self, routes):
        fake = FakeServer(routes)
        return fake, mock.patch.object(gamedb.urllib.request, 'build_opener',
                                       return_value=fake)

    def paged(self, pages, since_games=None, removed=()):
        """A games route: `pages` lists of games for a full fetch (cursor
        c1, c2…), one delta page for `?since=`."""
        def route(q):
            if 'cursor' in q:
                i = int(q['cursor'][1:])
                return {'schema': 2, 'full': True, 'games': pages[i],
                        'nextCursor': f'c{i + 1}' if i + 1 < len(pages) else None}
            if 'since' in q:
                return {'schema': 2, 'full': False, 'since': q['since'],
                        'nextSince': 'S2', 'nextCursor': None, 'removed': list(removed),
                        'games': since_games or []}
            return {'schema': 2, 'full': True, 'nextSince': 'S1', 'games': pages[0],
                    'nextCursor': 'c1' if len(pages) > 1 else None, 'removed': []}
        return route

    def age_games(self, by=gamedb.GAMES_AGE + 10):
        f = self.cache / 'games.json'
        c = json.loads(f.read_text())
        c['synced_at'] -= by
        f.write_text(json.dumps(c))


class Headers(V2Base):
    def test_v1_and_v2_say_who_is_asking(self):
        self.write_config('[gamedb]\nenabled = true\nurl = "https://gdb.example"\n'
                          f'token = "{TOKEN}"\ndevice = "living room"\n')
        fake, p = self.server({'/api/nexus/v1/summary': SUMMARY,
                               '/api/nexus/v2/meta': META,
                               '/api/nexus/v2/games': self.paged([[game(620)]])})
        with p:
            gamedb.summary()
            gamedb.sync_games()
        self.assertEqual(len(fake.requests), 3)
        for req in fake.requests:
            self.assertRegex(req.get_header('X-nexus-client'),
                             r'^nexus/\d+\.\d+\.\d+\S* (linux|windows|\w+)$')
            self.assertEqual(req.get_header('X-nexus-device'), 'living room')

    def test_device_is_never_a_path(self):
        host = gamedb.device_label()
        self.assertTrue(host and '/' not in host and '\\' not in host)
        self.assertEqual(gamedb.device_label('/home/me'), host)
        self.assertEqual(gamedb.device_label('C:\\Users\\me'), host)
        self.assertEqual(gamedb.device_label('  desk\x00top  '), 'desktop')
        self.assertEqual(len(gamedb.device_label('x' * 200)), 64)
        self.assertEqual(gamedb.device_label('café'), 'caf')     # an HTTP header: ASCII


class UnknownFields(V2Base):
    def test_summary_ignores_unknown_fields(self):
        extra = {**SUMMARY, 'brandNew': {'x': 1},
                 'wishlist': [{**SUMMARY['wishlist'][0], 'alerts': {'muted': True}}]}
        _fake, p = self.server({'/api/nexus/v1/summary': extra})
        with p:
            res = gamedb.summary()
        self.assertFalse(res['stale'])
        self.assertEqual(gamedb.lookup([1086940])[1086940]['list'], 'wishlist')


class Meta(V2Base):
    def test_asked_once_a_day(self):
        fake, p = self.server({'/api/nexus/v2/meta': META})
        with p:
            self.assertEqual(gamedb.server_meta()['features'], ['summary', 'games'])
            gamedb.server_meta()
        self.assertEqual(len(fake.requests), 1)
        self.assertIs(gamedb.games_available(), True)
        self.assertEqual(why_not_private(self.cache / 'meta.json'), '')

    def test_old_server_404_turns_the_feature_off(self):
        fake, p = self.server({'/api/nexus/v1/summary': SUMMARY})      # no v2 at all
        with p:
            res = gamedb.sync_games()
            again = gamedb.sync_games(force=True)
            gamedb.summary()                                           # v1 still fine
        self.assertEqual((res['status'], again['status']), ('off', 'off'))
        self.assertEqual(fake.paths().count('/api/nexus/v2/meta'), 1)  # remembered
        self.assertNotIn('/api/nexus/v2/games', fake.paths())
        self.assertIs(gamedb.games_available(), False)
        self.assertIsNone(gamedb.meta(620))

    def test_server_without_games_feature(self):
        fake, p = self.server({'/api/nexus/v2/meta': {**META, 'features': ['summary']}})
        with p:
            self.assertEqual(gamedb.sync_games()['status'], 'off')
        self.assertNotIn('/api/nexus/v2/games', fake.paths())

    def test_switched_off_in_settings_makes_no_request(self):
        self.write_config('[gamedb]\nenabled = true\nurl = "https://gdb.example"\n'
                          f'token = "{TOKEN}"\ngames = false\n')
        fake, p = self.server({'/api/nexus/v2/meta': META})
        with p:
            self.assertEqual(gamedb.sync_games()['status'], 'off')
        self.assertEqual(fake.requests, [])

    def test_unreachable_is_a_failure_not_off(self):
        _fake, p = self.server({'/api/nexus/v2/meta': urllib.error.URLError('down')})
        with p:
            res = gamedb.sync_games()
        self.assertEqual(res['status'], 'failed')
        self.assertIsNone(gamedb.games_available())


class GamesSync(V2Base):
    def test_full_fetch_follows_every_page(self):
        pages = [[game(10), game(20)], [game(30)], [game(40)]]
        fake, p = self.server({'/api/nexus/v2/meta': META,
                               '/api/nexus/v2/games': self.paged(pages)})
        with p:
            res = gamedb.sync_games()
        self.assertEqual((res['status'], res['count']), ('synced', 4))
        self.assertEqual(fake.queries('/api/nexus/v2/games'),
                         [{}, {'cursor': 'c1'}, {'cursor': 'c2'}])
        f = self.cache / 'games.json'
        self.assertEqual(why_not_private(f), '')
        self.assertNotIn(TOKEN, f.read_text())
        c = json.loads(f.read_text())
        self.assertEqual(c['since'], 'S1')                    # the first page's nextSince
        self.assertEqual(sorted(c['games']), ['10', '20', '30', '40'])

    def test_delta_upserts_and_removes_at_most_every_6h(self):
        fake, p = self.server({
            '/api/nexus/v2/meta': META,
            '/api/nexus/v2/games': self.paged(
                [[game(10), game(20), game(30)]],
                since_games=[game(20, title='Renamed'), game(50)], removed=[30, 999])})
        with p:
            gamedb.sync_games()
            self.assertEqual(gamedb.sync_games()['status'], 'fresh')   # within 6 h
            self.assertEqual(len(fake.queries('/api/nexus/v2/games')), 1)
            self.age_games()
            res = gamedb.sync_games()
        self.assertEqual((res['status'], res['changed'], res['removed']), ('synced', 2, 1))
        self.assertEqual(fake.queries('/api/nexus/v2/games')[-1], {'since': 'S1'})
        c = json.loads((self.cache / 'games.json').read_text())
        self.assertEqual(sorted(c['games']), ['10', '20', '50'])
        self.assertEqual(c['games']['20']['title'], 'Renamed')
        self.assertEqual(c['since'], 'S2')

    def test_failed_page_applies_nothing(self):
        def route(q):
            if 'cursor' in q:
                return urllib.error.URLError('dropped')
            return {'schema': 2, 'full': True, 'nextSince': 'S1', 'nextCursor': 'c1',
                    'games': [game(10)]}
        _fake, p = self.server({'/api/nexus/v2/meta': META, '/api/nexus/v2/games': route})
        with p:
            res = gamedb.sync_games()
        self.assertEqual(res['status'], 'failed')
        self.assertNotIn(TOKEN, json.dumps(res))
        self.assertFalse((self.cache / 'games.json').exists())

    def test_rejected_since_starts_over(self):
        def route(q):
            if 'since' in q:
                return http_error(400)
            return {'schema': 2, 'full': True, 'nextSince': 'S9', 'nextCursor': None,
                    'games': [game(77)]}
        fake, p = self.server({'/api/nexus/v2/meta': META,
                               '/api/nexus/v2/games': self.paged([[game(10)]])})
        with p:
            gamedb.sync_games()
        self.age_games()
        fake.routes['/api/nexus/v2/games'] = route
        with p:
            res = gamedb.sync_games()
        self.assertEqual((res['status'], res['count']), ('synced', 1))
        self.assertIsNotNone(gamedb.meta(77))
        self.assertIsNone(gamedb.meta(10))

    def test_429_backs_off_v1_and_v2(self):
        fake, p = self.server({
            '/api/nexus/v2/meta': META,
            '/api/nexus/v2/games': http_error(429, {'Retry-After': '300'}),
            '/api/nexus/v1/summary': SUMMARY})
        with p:
            res = gamedb.sync_games()
            n = len(fake.requests)
            again = gamedb.sync_games(force=True)
            summ = gamedb.summary(refresh=True) if (self.cache / 'summary.json').exists() \
                else None
        self.assertEqual(res['error']['kind'], 'rate')
        self.assertEqual(again['error']['kind'], 'rate')
        self.assertEqual(len(fake.requests), n)                # nothing sent while waiting
        self.assertIsNone(summ)
        self.assertGreater(gamedb.last_state()['retry_at'], time.time() + 200)
        with p, self.assertRaises(gamedb.GameDbError) as cm:
            gamedb.summary()                                   # no copy: raises 'rate'
        self.assertEqual(cm.exception.kind, 'rate')
        self.assertEqual(len(fake.requests), n)

    def test_token_change_hides_the_old_copy(self):
        _fake, p = self.server({'/api/nexus/v2/meta': META,
                                '/api/nexus/v2/games': self.paged([[game(620)]])})
        with p:
            gamedb.sync_games()
        self.assertIsNotNone(gamedb.meta(620))
        os.environ['NEXUS_GAMEDB_TOKEN'] = 'gdb_' + 'C' * 43
        self.assertIsNone(gamedb.meta(620))


class CacheFirst(V2Base):
    """protondb, hltb and storeinfo reviews answer from games.json and send
    nothing upstream while Game-db's copy is fresh; else their own fetch."""

    def sync(self, *games):
        _fake, p = self.server({'/api/nexus/v2/meta': META,
                                '/api/nexus/v2/games': self.paged([list(games)])})
        with p:
            self.assertEqual(gamedb.sync_games()['status'], 'synced')

    def no_network(self):
        boom = AssertionError('network!')
        return mock.patch.multiple(gamedb.net, get=mock.Mock(side_effect=boom),
                                   post=mock.Mock(side_effect=boom))

    def test_owned_game_page_asks_no_one(self):
        self.sync(game(620, status='early_access', statusLabel='Early Access'))
        with self.no_network():
            pdb = protondb.tiers([620])[620]
            hl = hltb.lookup('Portal 2', appid=620)
            rv = storeinfo.reviews(620)
            row = gamedb.meta(620)
        self.assertEqual((pdb['tier'], pdb['trending'], pdb['total'], pdb['source']),
                         ('platinum', 'gold', 134, 'gamedb'))
        self.assertEqual((hl['main'], hl['plus'], hl['complete'], hl['source']),
                         (10.5, 15, 30, 'hltb'))
        self.assertEqual((rv['desc'], rv['percent'], rv['total'], rv['positive']),
                         ('Very Positive', 93, 1000, 930))
        self.assertEqual((row['status'], row['statusLabel']), ('early_access', 'Early Access'))
        self.assertEqual(row['url'], 'https://gdb.example/game/620')

    def test_igdb_play_length_keeps_its_source(self):
        self.sync(game(241930, playLength={'main': None, 'mainExtra': 17.9,
                                           'completionist': 56.7, 'source': 'igdb',
                                           'count': 10, 'fetchedAt': iso(60)}))
        with self.no_network():
            hl = hltb.lookup('Shadow of Mordor', appid=241930)
        self.assertEqual((hl['main'], hl['plus'], hl['complete']), (None, 17.9, 56.7))
        self.assertEqual((hl['source'], hl['count']), ('igdb', 10))

    def test_missing_or_stale_rows_fall_back(self):
        self.sync(game(1, proton={'tier': 'gold', 'trendingTier': None, 'total': 3,
                                  'fetchedAt': iso(3 * 86400)},
                       playLength={'main': 5, 'mainExtra': None, 'completionist': None,
                                   'source': 'hltb', 'count': None,
                                   'fetchedAt': iso(hltb.HIT_AGE + 60)}),
                  game(2, proton=None, playLength=None, reviews={}))
        self.assertIsNone(protondb.from_gamedb(1))         # older than its TTL
        self.assertIsNone(hltb.from_gamedb(1))
        self.assertIsNone(protondb.from_gamedb(2))         # no report cached
        self.assertIsNone(hltb.from_gamedb(2))
        self.assertIsNone(storeinfo.reviews_from_gamedb(2))
        self.assertIsNone(protondb.from_gamedb(3))         # not your game
        with mock.patch.object(gamedb.net, 'get',
                               return_value=json.dumps({'tier': 'silver'}).encode()) as get:
            self.assertEqual(protondb.tiers([1])[1]['tier'], 'silver')
        get.assert_called_once()
        self.age_games(storeinfo.REVIEWS_AGE + 10)          # reviews age with the sync
        self.assertIsNone(storeinfo.reviews_from_gamedb(1))

    def test_refresh_skips_gamedb(self):
        self.sync(game(620))
        with mock.patch.object(gamedb.net, 'get',
                               return_value=json.dumps({'tier': 'gold'}).encode()) as get:
            self.assertEqual(protondb.tiers([620], refresh=True)[620]['tier'], 'gold')
        get.assert_called_once()

    def test_game_db_off_or_old_changes_nothing(self):
        self.sync(game(620))
        self.write_config('[gamedb]\nenabled = false\nurl = "https://gdb.example"\n'
                          f'token = "{TOKEN}"\n')
        self.assertIsNone(gamedb.meta(620))
        self.write_config()
        (self.cache / 'meta.json').write_text(json.dumps({
            'fingerprint': gamedb._fingerprint('https://gdb.example', TOKEN),
            'fetched_at': time.time(), 'data': {'off': True}}))
        self.assertIsNone(gamedb.meta(620))                 # the server dropped v2


class Helpers(unittest.TestCase):
    NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    def test_days_and_labels(self):
        self.assertEqual(gamedb.days_until('2026-10-13T00:00:00.000Z', self.NOW), 12)
        self.assertIsNone(gamedb.days_until(None))
        lab = gamedb.release_label
        self.assertEqual(lab({'releaseDate': '2026-10-13T00:00:00Z',
                              'releaseDateText': 'Oct 13, 2026'}, self.NOW), 'in 12 days')
        self.assertEqual(lab({'releaseDate': '2026-11-01T00:00:00Z',
                              'releaseDateText': 'November 2026'}, self.NOW), 'November 2026')
        self.assertEqual(lab({'releaseDate': None, 'releaseDateText': 'Q2 2027'}), 'Q2 2027')
        self.assertEqual(lab({'releaseDate': None, 'releaseDateText': None}), 'TBA')
        self.assertEqual(lab({'releaseDate': '2026-10-02T00:00:00Z',
                              'releaseDateText': '2 Oct, 2026'}, self.NOW), 'tomorrow')

    def test_has_day(self):
        self.assertTrue(gamedb.has_day({'releaseDate': '2026-10-14T00:00:00Z',
                                        'releaseDateText': 'Oct 14, 2026'}))
        self.assertFalse(gamedb.has_day({'releaseDate': '2026-10-01T00:00:00Z',
                                         'releaseDateText': 'October 2026'}))
        self.assertFalse(gamedb.has_day({'releaseDate': None, 'releaseDateText': 'Q2 2027'}))

    def test_upcoming_merges_and_orders(self):
        price = {'currentStr': '€55.50', 'preorder': False,
                 'best': {'priceStr': '€41.14', 'category': 'keyshop', 'shop': None}}
        summary = {
            'upcoming': [
                {'appid': 1, 'title': 'Later', 'releaseDate': '2026-12-01T00:00:00Z',
                 'releaseDateText': 'Dec 1, 2026', 'protonTier': 'gold', 'owned': False},
                {'appid': 2, 'title': 'Soon', 'releaseDate': '2026-10-14T00:00:00Z',
                 'releaseDateText': 'Oct 14, 2026', 'protonTier': None},
            ],
            'wishlist': [
                {'appid': 1, 'title': 'Later', 'comingSoon': True, 'price': price,
                 'protonTier': None, 'releaseDate': '2026-12-01T00:00:00Z',
                 'releaseDateText': 'Dec 1, 2026'},
                # Dropped by the server once Oct 1 passed; kept here.
                {'appid': 3, 'title': 'Month', 'comingSoon': True, 'price': None,
                 'releaseDate': '2026-10-01T00:00:00Z', 'releaseDateText': 'October 2026'},
                {'appid': 4, 'title': 'Zed', 'comingSoon': True, 'releaseDate': None,
                 'releaseDateText': None},
                {'appid': 5, 'title': 'Quarter', 'comingSoon': True, 'releaseDate': None,
                 'releaseDateText': 'Q2 2027'},
                {'appid': 6, 'title': 'Out', 'comingSoon': False,
                 'releaseDate': '2026-01-01T00:00:00Z'},
            ]}
        got = gamedb.upcoming(summary)
        self.assertEqual([g['appid'] for g in got], [2, 1, 3, 5, 4])
        later = got[1]
        self.assertEqual(later['price'], price)              # from the wishlist
        self.assertEqual(later['protonTier'], 'gold')        # None never overwrites
        self.assertEqual(gamedb.upcoming(None), [])

    def test_price_line(self):
        pl = gamedb.price_line
        self.assertEqual(pl({'price': None}), ("No price yet", False))
        self.assertEqual(pl({'price': {'currentStr': '€55.50', 'best': {
            'priceStr': '€41.14', 'category': 'keyshop'}}}), ("€41.14 · keyshop", False))
        self.assertEqual(pl({'price': {'currentStr': '€69.99', 'preorder': True, 'best': {
            'priceStr': '€69.99', 'category': 'official', 'shop': 'Steam'}}}),
            ("€69.99 · Steam", True))
        self.assertEqual(pl({'price': {'currentStr': '€9.99'}}), ("€9.99", False))

    def test_steamid64(self):
        self.assertEqual(gamedb.steamid64(12345678), '76561197972611406')

    def test_safe_url(self):
        self.assertEqual(gamedb.safe_url('https://a/b'), 'https://a/b')
        self.assertIsNone(gamedb.safe_url('javascript:alert(1)'))
        self.assertIsNone(gamedb.safe_url('http://evil/x'))
        self.assertEqual(gamedb.safe_url('http://localhost:3000/game/1',
                                         'http://localhost:3000'),
                         'http://localhost:3000/game/1')


class Cli(unittest.TestCase):
    """The real CLI against a fake HOME with a pre-seeded cache: proves the
    lookup/deals paths work offline and never need the network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        h = Path(self.tmp.name)
        tests.sandbox.config_dir(h).mkdir(parents=True)
        (tests.sandbox.config_dir(h) / 'config.toml').write_text(
            f'[gamedb]\nenabled = true\nurl = "https://gdb.invalid"\ntoken = "{TOKEN}"\n')
        cache = tests.sandbox.cache_dir(h) / 'gamedb'
        cache.mkdir(parents=True)
        (cache / 'summary.json').write_text(json.dumps({
            'fingerprint': gamedb._fingerprint('https://gdb.invalid', TOKEN),
            'fetched_at': time.time(), 'data': SUMMARY}))
        self.env = {**os.environ, **tests.sandbox.home_env(h), 'XDG_CACHE_HOME': str(h / '.cache')}
        self.env.pop('NEXUS_GAMEDB_TOKEN', None)
        self.env.pop('NEXUS_GAMEDB_URL', None)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(REPO / 'bin' / 'nexus-cli'), *args],
                              env=self.env, text=True, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def test_lookup_json(self):
        r = self.cli('--gamedb-lookup', '1086940', '620')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['1086940']['price']['currentStr'], '€12.49')
        self.assertEqual(out['620']['list'], 'library')

    def test_deals_text_and_backlog_json(self):
        r = self.cli('--deals')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Baldur's Gate 3", r.stdout)
        self.assertIn('ATL', r.stdout)
        r = self.cli('--backlog', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['tonight'][0]['title'], 'Portal 2')
        self.assertIn('installed_appids', out)

    def test_wishlist_repacks_offline(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        data = tests.sandbox.data_dir(Path(self.env['HOME']))
        data.mkdir(parents=True, exist_ok=True)       # Windows: the cache is inside
        (data / 'repack-index.json').write_text(json.dumps({
            'posts': {'baldurs-gate-3': {
                'id': 1, 'slug': 'baldurs-gate-3', 'modified': '2026-01-01T00:00:00',
                'title': "Baldur’s Gate 3 – Digital Deluxe Edition, v4.1 + Bonus Content"}},
            'built_at': now, 'synced_at': now, 'max_modified': None, 'rejected': {}}))
        r = self.cli('--gamedb', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)['repacks']
        self.assertEqual(rep['1086940']['url'], 'https://fitgirl-repacks.site/baldurs-gate-3/')
        self.assertEqual(rep['1086940']['confidence'], 'exact')
        # Synced moments ago: no request; the first answer is a summary,
        # after that a match is news only once.
        first = json.loads(self.cli('--repack-index', '--json').stdout)
        self.assertTrue(first['first'])
        self.assertEqual([m['appid'] for m in first['new_matches']], ['1086940'])
        again = json.loads(self.cli('--repack-index', '--json').stdout)
        self.assertEqual((again['first'], again['new_matches'], again['matches']),
                         (False, [], 1))
        r = self.cli('--repack-reject', '1086940', 'baldurs-gate-3')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.cli('--gamedb', '--json').stdout)['repacks'], {})

    def test_sync_offline_fails_soft(self):
        r = self.cli('--gamedb-sync', '--json')          # gdb.invalid never resolves
        self.assertEqual(r.returncode, 1, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual(res['status'], 'failed')
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_not_configured_exits_2(self):
        (tests.sandbox.config_dir(Path(self.env['HOME'])) / 'config.toml').write_text('')
        r = self.cli('--deals')
        self.assertEqual(r.returncode, 2)
        self.assertIn("server's address", r.stderr)     # no built-in server to point at
        (tests.sandbox.config_dir(Path(self.env['HOME'])) / 'config.toml').write_text(
            '[gamedb]\nurl = "https://gamedb.test"\n')
        r = self.cli('--deals')
        self.assertEqual(r.returncode, 2)
        self.assertIn('https://gamedb.test/account', r.stderr)


if __name__ == '__main__':
    unittest.main()
