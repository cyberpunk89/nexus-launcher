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
from nexus import (facts, gamedb, gamedb_upload, hltb, protondb, steamlib,  # noqa: E402
                   storeinfo)
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


def http_error(code, headers=None, body=None):
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    raw = json.dumps(body).encode() if body is not None else b''
    return urllib.error.HTTPError('https://x/api', code, 'err', h, io.BytesIO(raw))


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
    """urllib's opener, routed by `"METHOD path"`, then by path: a route is a
    dict (JSON 200), an exception, or a callable(query) returning either; a
    request's JSON body is in query['__body__']."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        u = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if req.data is not None:
            q['__body__'] = json.loads(req.data)
        r = self.routes.get(f"{req.get_method()} {u.path}", self.routes.get(u.path))
        if r is None:
            r = http_error(404)
        if callable(r):
            r = r(q)
        if isinstance(r, Exception):
            raise r
        return FakeResponse(json.dumps(r).encode())

    def paths(self):
        return [urllib.parse.urlsplit(r.full_url).path for r in self.requests]

    def bodies(self, method, path):
        return [json.loads(r.data) for r in self.requests
                if r.get_method() == method and urllib.parse.urlsplit(r.full_url).path == path]

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

    def paged(self, pages, since_games=None, removed=(), removed_keys=()):
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
                        'removedKeys': list(removed_keys),
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

    def test_facts_index_takes_the_synced_rows(self):
        self.sync(game(241930, title='Shadow of Mordor', status='early_access',
                       igdbSlug='middle-earth-shadow-of-mordor',
                       playLength={'main': None, 'mainExtra': 17.9, 'completionist': 56.7,
                                   'source': 'igdb', 'count': 10, 'fetchedAt': iso(60)}))
        lib = [{'slug': 'mordor', 'appid': 241930, 'name': 'Shadow of Mordor'},
               {'slug': 'other', 'appid': 5, 'name': 'Not on Game-db'}]
        with self.no_network():
            rows = facts.gamedb_rows(lib)
            idx = facts.build(lib, rows)
            _appids, titles = facts.todo(lib)
        self.assertEqual(set(rows), {241930})
        f = idx['games']['mordor']
        self.assertEqual((f['status'], f['igdbSlug'], f['length']['source'],
                          f['length']['plus']), ('early_access',
                                                 'middle-earth-shadow-of-mordor', 'igdb', 17.9))
        self.assertNotIn('other', idx['games'])
        self.assertEqual([t for t, _y in titles], ['Not on Game-db'])   # no HLTB for ours

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


SCOPED_META = {**META, 'features': ['summary', 'games', 'sessions', 'library'],
               'scopes': ['read', 'write:sessions', 'write:library']}


class Writes(V2Base):
    """Phase 2: scopes, the scope error, and the write backoff."""

    def test_scope_403_is_its_own_kind_and_never_leaks_the_token(self):
        _fake, p = self.server({
            '/api/nexus/v2/meta': SCOPED_META,
            'POST /api/nexus/v2/sessions': http_error(
                403, body={'error': 'scope', 'need': 'write:sessions'})})
        with p:
            gamedb.server_meta()
            self.assertEqual(gamedb.write_status('sessions', gamedb.SCOPE_SESSIONS), 'ok')
            with self.assertRaises(gamedb.GameDbError) as cm:
                gamedb.write('/api/nexus/v2/sessions', {'sessions': []})
        self.assertEqual((cm.exception.kind, cm.exception.need), ('scope', 'write:sessions'))
        self.assertIn("can only read", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        # remembered without asking the server again
        self.assertEqual(gamedb.write_status('sessions', gamedb.SCOPE_SESSIONS), 'read_only')
        self.assertEqual(gamedb.write_status('library', gamedb.SCOPE_LIBRARY), 'ok')

    def test_plain_403_stays_auth(self):
        _fake, p = self.server({'POST /api/nexus/v2/sessions': http_error(403)})
        with p, self.assertRaises(gamedb.GameDbError) as cm:
            gamedb.write('/api/nexus/v2/sessions', {'sessions': []})
        self.assertEqual(cm.exception.kind, 'auth')

    def test_400_on_a_write_is_invalid(self):
        _fake, p = self.server({'POST /api/nexus/v2/sessions': http_error(
            400, body={'error': 'invalid', 'detail': 'minutes out of range'})})
        with p, self.assertRaises(gamedb.GameDbError) as cm:
            gamedb.write('/api/nexus/v2/sessions', {'sessions': []})
        self.assertEqual(cm.exception.kind, 'invalid')
        self.assertIn('minutes out of range', str(cm.exception))

    def test_write_sends_json_with_the_bearer(self):
        fake, p = self.server({'POST /api/nexus/v2/sessions': {'accepted': 1, 'duplicate': 0}})
        with p:
            got = gamedb.write('/api/nexus/v2/sessions', {'sessions': [{'id': 'x'}]})
        self.assertEqual(got, {'accepted': 1, 'duplicate': 0})
        req = fake.requests[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Content-type'), 'application/json')
        self.assertEqual(req.get_header('Authorization'), f'Bearer {TOKEN}')
        self.assertEqual(fake.bodies('POST', '/api/nexus/v2/sessions'),
                         [{'sessions': [{'id': 'x'}]}])

    def test_429_on_a_write_backs_off_writes_only(self):
        fake, p = self.server({
            'POST /api/nexus/v2/sessions': http_error(429, {'Retry-After': '120'}),
            '/api/nexus/v1/summary': SUMMARY})
        with p:
            with self.assertRaises(gamedb.GameDbError) as cm:
                gamedb.write('/api/nexus/v2/sessions', {'sessions': []})
            self.assertEqual(cm.exception.kind, 'rate')
            n = len(fake.requests)
            with self.assertRaises(gamedb.GameDbError) as cm2:
                gamedb.write('/api/nexus/v2/sessions', {'sessions': []})
            self.assertEqual(len(fake.requests), n)            # not sent while waiting
            self.assertEqual(cm2.exception.kind, 'rate')
            self.assertFalse(gamedb.summary()['stale'])        # reads are not held back
        st = gamedb.last_state()
        self.assertGreater(st['write_retry_at'], time.time() + 100)
        self.assertFalse(st.get('retry_at'))
        with mock.patch.object(gamedb.time, 'time', return_value=time.time() + 130), p:
            with self.assertRaises(gamedb.GameDbError):
                gamedb.write('/api/nexus/v2/sessions', {'sessions': []})
        self.assertEqual(len(fake.requests), n + 2)            # after Retry-After: asked again

    def test_meta_scopes_and_features(self):
        self.assertEqual(gamedb.write_status('sessions', gamedb.SCOPE_SESSIONS), 'unknown')
        self.assertIsNone(gamedb.token_scopes())
        _fake, p = self.server({'/api/nexus/v2/meta': META})       # read-only, no sessions
        with p:
            gamedb.server_meta()
        self.assertEqual(gamedb.token_scopes(), ['read'])
        self.assertIs(gamedb.offers('sessions'), False)
        self.assertEqual(gamedb.write_status('sessions', gamedb.SCOPE_SESSIONS), 'unavailable')
        _fake, p = self.server({'/api/nexus/v2/meta': {**SCOPED_META, 'scopes': ['read']}})
        with p:
            gamedb.server_meta(refresh=True)
        self.assertEqual(gamedb.write_status('sessions', gamedb.SCOPE_SESSIONS), 'read_only')

    def test_old_server_means_every_write_feature_off(self):
        _fake, p = self.server({'/api/nexus/v1/summary': SUMMARY})    # v1 only
        with p:
            gamedb.server_meta()
        for feature, scope in (('sessions', gamedb.SCOPE_SESSIONS),
                               ('library', gamedb.SCOPE_LIBRARY)):
            self.assertEqual(gamedb.write_status(feature, scope), 'unavailable')
        self.assertIsNone(gamedb.token_scopes())
        # a server from before scopes (no `scopes` in meta) is read-only
        meta = {k: v for k, v in SCOPED_META.items() if k != 'scopes'}
        _fake, p = self.server({'/api/nexus/v2/meta': meta})
        with p:
            gamedb.server_meta(refresh=True)
        self.assertEqual(gamedb.token_scopes(), ['read'])


def session_line(slug, source, name, start='2026-10-20T20:00:00', minutes=30.0):
    end = datetime.fromisoformat(start).timestamp() + minutes * 60
    return json.dumps({'slug': slug, 'source': source, 'name': name, 'start': start,
                       'end': datetime.fromtimestamp(end).isoformat(timespec='seconds'),
                       'minutes': minutes}) + '\n'


class SessionsServer:
    """POST/DELETE v2/sessions the way Game-db answers them: upsert-ignore
    on the session id, so a resend counts as duplicate."""

    def __init__(self):
        self.ids = {}
        self.fail_after = None          # fail every POST after this many

    def post(self, q):
        if self.fail_after is not None:
            if self.fail_after <= 0:
                return urllib.error.URLError('dropped')
            self.fail_after -= 1
        new = dup = 0
        for it in q['__body__']['sessions']:
            if it['id'] in self.ids:
                dup += 1
            else:
                self.ids[it['id']] = it
                new += 1
        return {'accepted': new, 'duplicate': dup}

    def delete(self, q):
        n, self.ids = len(self.ids), {}
        return {'deleted': n}


class SessionsUpload(V2Base):
    """Phase 3: the opt-in play-session upload queue."""

    ON = ('[gamedb]\nenabled = true\nurl = "https://gdb.example"\n'
          f'token = "{TOKEN}"\nupload_sessions = true\n')

    def setUp(self):
        super().setUp()
        self.data = Path(self.tmp.name) / 'data'
        p = mock.patch.object(gamedb.paths, 'DATA_DIR', self.data)
        p.start()
        self.addCleanup(p.stop)
        self.file = self.data / 'sessions.jsonl'
        self.data.mkdir()
        self.srv = SessionsServer()

    def routes(self, meta=SCOPED_META):
        return {'/api/nexus/v2/meta': meta,
                'POST /api/nexus/v2/sessions': self.srv.post,
                'DELETE /api/nexus/v2/sessions': self.srv.delete}

    def add(self, *lines):
        with open(self.file, 'a', encoding='utf-8') as f:
            f.writelines(lines)

    def upload(self, routes=None):
        fake, p = self.server(routes or self.routes())
        with p:
            return gamedb_upload.upload_sessions(path=self.file), fake

    def test_off_by_default_sends_nothing(self):
        self.add(session_line('steam-620', 'steam', 'Portal 2'))
        res, fake = self.upload()
        self.assertEqual(res['status'], 'off')
        self.assertEqual(fake.requests, [])
        self.assertFalse(gamedb.settings()['upload_other'])

    def test_store_games_go_up_with_ids_repacks_stay_home(self):
        self.write_config(self.ON)
        self.add(session_line('steam-620', 'steam', 'Portal 2'),
                 session_line('heroic-legendary-Fortnite', 'heroic', 'Fortnite',
                              start='2026-10-21T10:00:00'),
                 session_line('heroic-nile-amzn1.adg.product.x', 'heroic', 'Amazon Game',
                              start='2026-10-21T11:00:00'),
                 session_line('elden-ring', 'nexus',
                              'ELDEN RING (v1.12 + DLC, MULTi14) [FitGirl Repack]',
                              start='2026-10-22T10:00:00'),
                 session_line('epic-Sugar', 'epic', 'Sugar', start='2026-10-22T12:00:00'))
        res, fake = self.upload()
        self.assertEqual((res['status'], res['sent'], res['accepted']), ('uploaded', 4, 4))
        sent = fake.bodies('POST', '/api/nexus/v2/sessions')[0]['sessions']
        self.assertEqual([(i['store'], i['storeId']) for i in sent],
                         [('steam', '620'), ('epic', 'Fortnite'),
                          ('amazon', 'amzn1.adg.product.x'), ('epic', 'Sugar')])
        body = json.dumps(sent)
        self.assertNotIn('FitGirl', body)
        self.assertNotIn('elden', body.lower())
        for i in sent:
            self.assertRegex(i['id'], r'^[0-9a-f]{32}$')
            self.assertEqual(i['minutes'], 30)
            self.assertTrue(i['start'].endswith('+00:00'))
        self.assertEqual(why_not_private(self.cache / 'sessions-upload.json'), '')
        self.assertNotIn(TOKEN, (self.cache / 'sessions-upload.json').read_text())
        # nothing new: no request
        res, fake = self.upload()
        self.assertEqual(res['status'], 'idle')
        self.assertNotIn('/api/nexus/v2/sessions', fake.paths())

    def test_other_games_only_with_the_switch_and_title_only(self):
        self.write_config(self.ON + 'upload_other = true\n')
        self.add(session_line('elden-ring', 'nexus',
                              'ELDEN RING (v1.12 + DLC, MULTi14) [FitGirl Repack]'),
                 session_line('odd', 'desktop', '/home/me/Games/odd/run.sh',
                              start='2026-10-22T10:00:00'))
        res, fake = self.upload()
        sent = fake.bodies('POST', '/api/nexus/v2/sessions')[0]['sessions']
        self.assertEqual(res['sent'], 1)                       # a path is never a title
        self.assertEqual(sent[0]['store'], 'other')
        self.assertIsNone(sent[0]['storeId'])
        self.assertEqual(sent[0]['title'], 'ELDEN RING')
        self.assertNotIn('/home', json.dumps(sent))

    def test_retry_adds_nothing_twice(self):
        self.write_config(self.ON)
        self.add(*[session_line(f'steam-{i}', 'steam', f'Game {i}',
                                start=f'2026-10-01T{i // 60:02d}:{i % 60:02d}:00')
                   for i in range(1, 451)])
        self.srv.fail_after = 1                     # the 2nd batch of 200 fails
        res, fake = self.upload()
        self.assertEqual((res['status'], res['accepted']), ('failed', 200))
        self.assertNotIn(TOKEN, json.dumps(res))
        self.srv.fail_after = None
        res, fake = self.upload()
        self.assertEqual((res['status'], res['accepted'], res['duplicate']), ('uploaded', 250, 0))
        self.assertEqual(len(self.srv.ids), 450)
        self.assertEqual([len(b['sessions']) for b in fake.bodies(
            'POST', '/api/nexus/v2/sessions')], [200, 50])
        # A lost mark (cache cleared) or a new token resends; the ids keep it harmless.
        (self.cache / 'sessions-upload.json').unlink()
        res, _fake = self.upload()
        self.assertEqual((res['accepted'], res['duplicate']), (0, 450))
        self.assertEqual(len(self.srv.ids), 450)

    def test_a_half_written_line_waits(self):
        self.write_config(self.ON)
        self.add(session_line('steam-620', 'steam', 'Portal 2'))
        with open(self.file, 'a', encoding='utf-8') as f:
            f.write('{"slug": "steam-70", "sou')
        res, _fake = self.upload()
        self.assertEqual(res['sent'], 1)
        with open(self.file, 'a', encoding='utf-8') as f:
            f.write('rce": "steam", "name": "Half-Life", "start": "2026-10-23T10:00:00", '
                    '"end": "2026-10-23T11:00:00", "minutes": 60.0}\n')
        res, fake = self.upload()
        self.assertEqual(res['sent'], 1)
        self.assertEqual(fake.bodies('POST', '/api/nexus/v2/sessions')[0]['sessions'][0][
            'storeId'], '70')

    def test_read_only_token_or_old_server_sends_nothing(self):
        self.write_config(self.ON)
        self.add(session_line('steam-620', 'steam', 'Portal 2'))
        res, fake = self.upload({**self.routes(), '/api/nexus/v2/meta': META})
        self.assertEqual(res['status'], 'unavailable')        # META has no sessions
        self.assertNotIn('/api/nexus/v2/sessions', fake.paths())
        gamedb.clear_cache()
        res, fake = self.upload({**self.routes(), '/api/nexus/v2/meta': {
            **SCOPED_META, 'scopes': ['read']}})
        self.assertEqual(res['status'], 'read_only')
        self.assertIn("can only read", res['error']['message'])
        self.assertNotIn('/api/nexus/v2/sessions', fake.paths())
        gamedb.clear_cache()
        res, fake = self.upload({'/api/nexus/v1/summary': SUMMARY})     # no v2 at all
        self.assertEqual(res['status'], 'unavailable')

    def test_scope_403_mid_upload_turns_into_the_hint(self):
        self.write_config(self.ON)
        self.add(session_line('steam-620', 'steam', 'Portal 2'))
        res, _fake = self.upload({**self.routes(), 'POST /api/nexus/v2/sessions': http_error(
            403, body={'error': 'scope', 'need': 'write:sessions'})})
        self.assertEqual((res['status'], res['error']['kind']), ('failed', 'scope'))
        res, fake = self.upload()
        self.assertEqual(res['status'], 'read_only')            # no second try
        self.assertNotIn('/api/nexus/v2/sessions', fake.paths())

    def test_429_waits_for_retry_after(self):
        self.write_config(self.ON)
        self.add(session_line('steam-620', 'steam', 'Portal 2'))
        res, _fake = self.upload({**self.routes(), 'POST /api/nexus/v2/sessions': http_error(
            429, {'Retry-After': '300'})})
        self.assertEqual(res['error']['kind'], 'rate')
        res, fake = self.upload()
        self.assertEqual(res['error']['kind'], 'rate')
        self.assertNotIn('/api/nexus/v2/sessions', fake.paths())
        later = time.time() + 400
        with mock.patch.object(gamedb.time, 'time', return_value=later), \
                mock.patch.object(gamedb_upload.time, 'time', return_value=later):
            res, _fake = self.upload()
        self.assertEqual(res['status'], 'uploaded')

    def test_offline_fails_soft(self):
        self.write_config(self.ON)
        self.add(session_line('steam-620', 'steam', 'Portal 2'))
        res, _fake = self.upload({'/api/nexus/v2/meta': urllib.error.URLError('down')})
        self.assertEqual(res['status'], 'unknown')

    def test_delete_and_the_device_id_stays(self):
        self.write_config(self.ON)
        self.add(session_line('steam-620', 'steam', 'Portal 2'))
        self.upload()
        dev = gamedb_upload.device_id()
        self.assertEqual(why_not_private(self.data / 'device-id'), '')
        fake, p = self.server(self.routes())
        with p:
            self.assertEqual(gamedb_upload.delete_sessions(), {'deleted': 1})
        self.assertEqual(fake.requests[-1].get_method(), 'DELETE')
        self.assertEqual(self.srv.ids, {})
        gamedb.clear_cache()
        self.assertEqual(gamedb_upload.device_id(), dev)
        self.assertEqual(gamedb_upload.session_id(dev, 'steam-620', '2026-10-20T20:00:00'),
                         gamedb_upload.session_id(dev, 'steam-620', '2026-10-20T20:00:00'))

    def test_helpers(self):
        st = gamedb_upload.store_of
        self.assertEqual(st('steam-620', 'steam'), ('steam', '620'))
        self.assertEqual(st('heroic-gog-1207658924', 'heroic'), ('gog', '1207658924'))
        self.assertIsNone(st('heroic-legendary-', 'heroic'))
        self.assertIsNone(st('steam-620', 'nexus'))
        ct = gamedb_upload.clean_title
        self.assertEqual(ct('Cyberpunk 2077: Ultimate Edition (v2.13 + All DLCs) '
                            '[FitGirl Repack]'), 'Cyberpunk 2077: Ultimate Edition')
        self.assertEqual(ct('Hades II - v1.0.4 - DODI Repack'), 'Hades II')
        self.assertIsNone(ct('C:\\Games\\x'))
        self.assertIsNone(ct('[FitGirl Repack]'))


def store_game(store, store_id, appid=None, **kw):
    """A v2/games row for a game Nexus sent from Heroic (Phase 4 fields)."""
    return game(appid, title=kw.pop('title', f'{store} {store_id}'),
                stores=[{'store': store, 'storeId': store_id}], igdbId=1000,
                description='A short summary.', gameModes=['Single player', 'Co-operative'],
                genres=['Shooter', 'Adventure'], developers=['Studio'], **kw)


class OtherStoreLibrary(V2Base):
    """w-44: Heroic's owned games go to POST v2/library; their rows come
    back in v2/games keyed by appid or store:storeId."""

    ON = ('[gamedb]\nenabled = true\nurl = "https://gdb.example"\n'
          f'token = "{TOKEN}"\n')

    def setUp(self):
        super().setUp()
        self.got = []

    def post(self, q):
        self.got.append(q['__body__'])
        n = len(q['__body__']['games'])
        return {'accepted': n, 'resolved': n - 1, 'pending': 1}

    def routes(self, meta=SCOPED_META, games=()):
        return {'/api/nexus/v2/meta': meta, 'POST /api/nexus/v2/library': self.post,
                '/api/nexus/v2/games': self.paged([list(games)])}

    def heroic(self, n):
        return [{'store': 'epic', 'storeId': f'app{i}', 'title': f'Game {i}'} for i in range(n)]

    def test_pages_of_1000_complete_only_on_the_last(self):
        fake, p = self.server(self.routes())
        with p:
            res = gamedb_upload.upload_library(games=self.heroic(2300))
        self.assertEqual((res['status'], res['sent'], res['resolved']), ('uploaded', 2300, 2297))
        self.assertEqual([(len(b['games']), b['complete']) for b in self.got],
                         [(1000, False), (1000, False), (300, True)])
        self.assertEqual(why_not_private(self.cache / 'library-upload.json'), '')
        # once a day
        with p:
            self.assertEqual(gamedb_upload.upload_library(games=self.heroic(5))['status'],
                             'fresh')
        self.assertEqual(len(self.got), 3)

    def test_nothing_without_heroic_or_scope_or_server(self):
        _fake, p = self.server(self.routes())
        with p:
            self.assertEqual(gamedb_upload.upload_library(games=[])['status'], 'idle')
        self.assertEqual(self.got, [])                     # never an empty "complete"
        for meta, want in (({**SCOPED_META, 'scopes': ['read', 'write:sessions']}, 'read_only'),
                           (META, 'unavailable')):
            gamedb.clear_cache()
            _fake, p = self.server(self.routes(meta=meta))
            with p:
                self.assertEqual(gamedb_upload.upload_library(games=self.heroic(3))['status'],
                                 want)
        self.assertEqual(self.got, [])
        self.write_config(self.ON + 'upload_library = false\n')
        self.assertEqual(gamedb_upload.upload_library(games=self.heroic(3))['status'], 'off')

    def test_failure_is_retried_on_the_next_tick(self):
        _fake, p = self.server({**self.routes(), 'POST /api/nexus/v2/library':
                                urllib.error.URLError('down')})
        with p:
            res = gamedb_upload.upload_library(games=self.heroic(3))
        self.assertEqual(res['status'], 'failed')
        self.assertNotIn(TOKEN, json.dumps(res))
        _fake, p = self.server(self.routes())
        with p:
            self.assertEqual(gamedb_upload.upload_library(games=self.heroic(3))['status'],
                             'uploaded')

    def test_heroic_library_reads_the_store_caches(self):
        owned = [{'runner': 'legendary', 'app_name': 'Fortnite', 'display_name': 'Fortnite',
                  'install_dir': '/home/me/Games/Fortnite'},
                 {'runner': 'gog', 'app_name': '1207658924', 'display_name': 'Witcher'},
                 {'runner': 'nile', 'app_name': 'amzn1.x', 'display_name': 'Amazon'},
                 {'runner': 'legendary', 'app_name': 'Fortnite', 'display_name': 'Fortnite'},
                 {'runner': 'sideload', 'app_name': 'x', 'display_name': 'Mine'}]
        with mock.patch.object(steamlib, 'heroic_store_games', return_value=owned):
            got = gamedb_upload.heroic_library()
        self.assertEqual(got, [{'store': 'epic', 'storeId': 'Fortnite', 'title': 'Fortnite'},
                               {'store': 'gog', 'storeId': '1207658924', 'title': 'Witcher'},
                               {'store': 'amazon', 'storeId': 'amzn1.x', 'title': 'Amazon'}])
        self.assertNotIn('/home', json.dumps(got))

    def test_rows_keyed_by_appid_or_store_and_found_both_ways(self):
        rows = [game(620), store_game('epic', 'Sugar'),
                store_game('gog', '1207658924', appid=292030, title='The Witcher 3')]
        fake, p = self.server(self.routes(games=rows))
        with p:
            gamedb.sync_games()
        c = json.loads((self.cache / 'games.json').read_text())
        self.assertEqual(sorted(c['games']), ['292030', '620', 'epic:Sugar'])
        self.assertEqual(gamedb.store_meta('epic', 'Sugar')['description'], 'A short summary.')
        self.assertIsNone(gamedb.store_meta('epic', 'Sugar')['url'])
        w3 = gamedb.store_meta('gog', '1207658924')
        self.assertEqual((w3['appid'], w3['url']), (292030, 'https://gdb.example/game/292030'))
        self.assertIsNone(gamedb.store_meta('epic', 'Nope'))
        self.assertIsNone(gamedb.meta('epic:Sugar'))           # meta() is appids only
        # a delta can remove a store-keyed row
        self.age_games()
        fake.routes['/api/nexus/v2/games'] = self.paged([rows], removed_keys=['epic:Sugar'])
        with p:
            self.assertEqual(gamedb.sync_games()['removed'], 1)
        self.assertIsNone(gamedb.store_meta('epic', 'Sugar'))

    def test_heroic_game_gets_facts_from_its_row(self):
        _fake, p = self.server(self.routes(games=[
            store_game('epic', 'Sugar', title='Sugar Rush', status='early_access',
                       releaseDate='2024-03-01T00:00:00Z',
                       playLength={'main': 12, 'mainExtra': 18, 'completionist': 30,
                                   'source': 'igdb', 'count': 4, 'fetchedAt': iso(60)})]))
        with p:
            gamedb.sync_games()
        lib = [{'slug': 'heroic-legendary-Sugar', 'appid': None, 'name': 'Sugar',
                'store': 'epic', 'store_id': 'Sugar'},
               {'slug': 'heroic-gog-1', 'appid': None, 'name': 'Unknown', 'store': 'gog',
                'store_id': '1'}]
        boom = AssertionError('network!')
        with mock.patch.multiple(gamedb.net, get=mock.Mock(side_effect=boom),
                                 post=mock.Mock(side_effect=boom)):
            idx = facts.build(lib, facts.gamedb_rows(lib))
        self.assertEqual(set(idx['games']), {'heroic-legendary-Sugar'})
        f = idx['games']['heroic-legendary-Sugar']
        self.assertEqual((f['name'], f['developers'], f['genres'], f['description']),
                         ('Sugar Rush', ['Studio'], ['Shooter', 'Adventure'], 'A short summary.'))
        self.assertEqual((f['modes'], f['src']['modes']), (['single', 'coop'], 'gamedb'))
        self.assertEqual((f['length']['main'], f['length']['source']), (12, 'igdb'))
        self.assertEqual((f['status'], f['year'], f['appid']), ('early_access', 2024, None))
        self.assertEqual(idx['missing'], {'steam': 0, 'hltb': 0})

    def test_facts_library_adds_heroic_games_game_db_knows(self):
        from nexus.cli import info
        _fake, p = self.server(self.routes(games=[
            store_game('epic', 'Sugar'),
            store_game('gog', '1207658924', appid=292030, title='The Witcher 3')]))
        with p:
            gamedb.sync_games()
        owned = [{'slug': 'heroic-legendary-Sugar', 'runner': 'legendary', 'app_name': 'Sugar',
                  'display_name': 'Sugar'},
                 {'slug': 'heroic-gog-1207658924', 'runner': 'gog', 'app_name': '1207658924',
                  'display_name': 'The Witcher 3'},
                 {'slug': 'heroic-nile-x', 'runner': 'nile', 'app_name': 'x',
                  'display_name': 'Not resolved yet'}]
        with mock.patch.object(steamlib, 'heroic_store_games', return_value=owned), \
                mock.patch.object(steamlib, 'scan', return_value=[]), \
                mock.patch.object(info, 'load_registry', return_value={'games': {}}):
            lib = info._facts_library()
        heroic = {r['slug']: r for r in lib if r['slug'].startswith('heroic-')}
        self.assertEqual(set(heroic), {'heroic-legendary-Sugar', 'heroic-gog-1207658924'})
        self.assertEqual(heroic['heroic-legendary-Sugar']['appid'], None)
        self.assertEqual(heroic['heroic-gog-1207658924']['appid'], 292030)   # IGDB's link


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


class SyncCommand(unittest.TestCase):
    """--gamedb-sync's text output with a read-only token: the sessions hint
    shows, the library upload (on by default) skips quietly, no crash
    (found in the live run against Game-db dev, 2026-10-28)."""

    def test_read_only_token_prints_the_hint_and_does_not_crash(self):
        from types import SimpleNamespace
        from nexus.cli import entry
        hint = gamedb.SCOPE_HINT[gamedb.SCOPE_SESSIONS]
        ups = {'sessions': {'status': 'read_only', 'sent': 0,
                            'error': {'kind': 'scope', 'message': hint}},
               'library': {'status': 'read_only', 'sent': 0, 'error': None}}
        err = io.StringIO()
        with mock.patch.object(gamedb, 'sync_games', return_value={
                'status': 'synced', 'count': 3, 'changed': 3, 'removed': 0, 'error': None}), \
                mock.patch.object(gamedb_upload, 'upload_all', return_value=ups), \
                mock.patch('sys.stdout', io.StringIO()), mock.patch('sys.stderr', err), \
                self.assertRaises(SystemExit) as cm:
            entry._cmd_gamedb_sync(SimpleNamespace(refresh=False, json=False), None)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(hint, err.getvalue())
        self.assertNotIn('library', err.getvalue())


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

    def test_delete_sessions_offline_and_unconfigured(self):
        r = self.cli('--gamedb-delete-sessions', '--json')   # gdb.invalid never resolves
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(json.loads(r.stdout)['error']['kind'], 'net')
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        (tests.sandbox.config_dir(Path(self.env['HOME'])) / 'config.toml').write_text('')
        self.assertEqual(self.cli('--gamedb-delete-sessions').returncode, 2)

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
