"""Tests for nexus/gamedb.py: settings, the no-redirect authenticated fetch,
the private stale-tolerant cache, cache-only lookups, and the CLI on top."""

import email.message
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from nexus import gamedb  # noqa: E402

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
        self.assertEqual(s['url'], gamedb.DEFAULT_URL)
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
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
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
        (h / '.config/nexus').mkdir(parents=True)
        (h / '.config/nexus/config.toml').write_text(
            f'[gamedb]\nenabled = true\nurl = "https://gdb.invalid"\ntoken = "{TOKEN}"\n')
        cache = h / '.cache/nexus/gamedb'
        cache.mkdir(parents=True)
        (cache / 'summary.json').write_text(json.dumps({
            'fingerprint': gamedb._fingerprint('https://gdb.invalid', TOKEN),
            'fetched_at': time.time(), 'data': SUMMARY}))
        self.env = {**os.environ, 'HOME': str(h), 'XDG_CACHE_HOME': str(h / '.cache')}
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
        data = Path(self.env['HOME']) / '.local/share/nexus'
        data.mkdir(parents=True)
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

    def test_not_configured_exits_2(self):
        (Path(self.env['HOME']) / '.config/nexus/config.toml').write_text('')
        r = self.cli('--deals')
        self.assertEqual(r.returncode, 2)
        self.assertIn('/account', r.stderr)


if __name__ == '__main__':
    unittest.main()
