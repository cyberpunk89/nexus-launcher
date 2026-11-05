"""Steam friends (nexus/cli/friends.py, `--friends`): the Web API client, its
cache and TTLs, the by_app join, private friends, and every way Steam can
fail (429, a refused key, no network, an empty or unreadable list) — against
a fake Steam API on 127.0.0.1, never the real one. Ids and keys are made up."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402  (HOME -> a temp dir; before any nexus import)
from nexus import paths as NX  # noqa: E402
from nexus.cli import friends as F  # noqa: E402

KEY = 'ABCDEF0123456789ABCDEF0123456789'          # not a real key
ME = '76561190000000001'
A, B, C, D = (f'7656119000000010{i}' for i in range(4))   # made-up friends


def player(sid, name, cvs=3, game=None):
    p = {'steamid': sid, 'personaname': name, 'communityvisibilitystate': cvs,
         'profilestate': 1, 'personastate': 1,
         'avatarmedium': f'https://avatars.example/{name}.jpg'}
    if game:
        p['gameid'], p['gameextrainfo'] = game
    return p


class FakeSteam(BaseHTTPRequestHandler):
    """Answers the three calls from `self.server.world`; `status` maps an
    interface name to an HTTP status to fail it with."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        name = url.path.strip('/').split('/')[1] if url.path.count('/') > 1 else ''
        srv.calls.append((name, q))
        w = srv.world
        code = srv.status.get(name) or srv.status.get('*')
        if code:
            self.send_response(code)
            if code == 429:
                self.send_header('Retry-After', '30')
            self.end_headers()
            self.wfile.write(b'<html>no</html>')
            return
        if q.get('key') != KEY:
            self.send_response(403)
            self.end_headers()
            return
        if name == 'GetFriendList':
            if w.get('list_private'):
                self.send_response(401)
                self.end_headers()
                return
            body = w['list_body'] if 'list_body' in w else {'friendslist': {'friends': [
                {'steamid': s, 'relationship': 'friend', 'friend_since': 1600000000}
                for s in w['friends']]}}
        elif name == 'GetPlayerSummaries':
            ids = q['steamids'].split(',')
            srv.batches.append(len(ids))
            body = {'response': {'players': [w['players'][s] for s in ids
                                             if s in w['players']]}}
        elif name == 'GetOwnedGames':
            got = w['owned'].get(q['steamid'])
            body = {'response': {}} if got is None else {'response': {
                'game_count': len(got),
                'games': [{'appid': a, 'playtime_forever': m} for a, m in got.items()]}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def world():
    return {'friends': [A, B, C, D],
            'players': {ME: player(ME, 'Me', cvs=1),
                        A: player(A, 'Ana', game=('10', 'Ten')),
                        B: player(B, 'Ben'),
                        C: player(C, 'Cy', cvs=1),           # private profile
                        D: player(D, 'Dee')},                # game details friends-only
            'owned': {A: {10: 120, 20: 5}, B: {10: 600, 30: 0}}}


class _FakeSteamCase(unittest.TestCase):
    """A fake Steam API on 127.0.0.1 and an empty friends cache."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), FakeSteam)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.srv.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.srv.world, self.srv.status = world(), {}
        self.srv.calls, self.srv.batches = [], []
        self.cfg = {'key': KEY, 'enabled': True, 'steamid': ME}
        for p in (mock.patch.dict(os.environ, {F.BASE_ENV: self.base}),
                  mock.patch.object(F, 'settings', lambda: dict(self.cfg)),
                  mock.patch.object(F, 'OWNED_GAP', 0.0)):
            p.start()
            self.addCleanup(p.stop)
        F.cache_path().unlink(missing_ok=True)
        self.addCleanup(F.cache_path().unlink, missing_ok=True)

    def names(self):
        return [n for n, _q in self.srv.calls]

    def age(self, **fields):
        """Move cache times back by these many seconds."""
        st = json.loads(F.cache_path().read_text())
        for k, secs in fields.items():
            if k == 'owned':
                for r in st['friends'].values():
                    r['owned_at'] = max(0, (r['owned_at'] or 0) - secs)
            else:
                st['meta'][k] -= secs
        F.cache_path().write_text(json.dumps(st))


class Friends(_FakeSteamCase):
    def test_refresh_lists_friends_flags_private_and_joins_by_app(self):
        data = F.friends()
        self.assertIsNone(data['error'])
        self.assertTrue(data['enabled'])
        self.assertEqual(data['me'], ME)
        self.assertEqual(data['counts'], {'total': 4, 'public': 2, 'private': 2, 'playing': 1})
        rows = {r['steamid']: r for r in data['friends']}
        self.assertEqual(data['friends'][0]['steamid'], A)          # playing first
        self.assertEqual(rows[A]['playing']['appid'], 10)
        self.assertEqual(rows[A]['playing']['name'], 'Ten')
        self.assertTrue(rows[A]['playing']['since'])
        self.assertEqual(rows[A]['owned'], {'10': 120, '20': 5})
        self.assertEqual((rows[C]['visible'], rows[C]['private_reason']), (False, 'profile'))
        self.assertEqual((rows[D]['visible'], rows[D]['private_reason']),
                         (False, 'game-details'))
        self.assertEqual(rows[B]['avatar'], 'https://avatars.example/Ben.jpg')
        self.assertEqual(data['by_app'], {'10': [B, A], '20': [A], '30': [B]})
        # The raw evidence for the three Steam facts
        self.assertEqual(data['evidence']['friendlist_status'], 200)
        self.assertEqual(data['evidence']['my_communityvisibilitystate'], 1)
        self.assertTrue(rows[A]['evidence']['gameid_present'])
        self.assertTrue(rows[A]['evidence']['gameextrainfo_present'])
        self.assertFalse(rows[B]['evidence']['gameid_present'])
        self.assertEqual(rows[D]['evidence']['owned_answer'], 'empty')
        self.assertEqual(rows[D]['evidence']['communityvisibilitystate'], 3)
        self.assertEqual(rows[B]['evidence']['owned_answer'], 'games')
        # The private profile's games are never asked for
        owned_for = [q['steamid'] for n, q in self.srv.calls if n == 'GetOwnedGames']
        self.assertNotIn(C, owned_for)
        self.assertEqual(sorted(owned_for), sorted([A, B, D]))
        self.assertTrue(data['updated'])
        if os.name == 'posix':
            self.assertEqual(F.cache_path().stat().st_mode & 0o777, 0o600)

    def test_ttls_ask_only_for_what_is_stale(self):
        F.friends()
        self.srv.calls.clear()
        F.friends()
        self.assertEqual(self.names(), [])                         # all fresh
        self.age(summary_at=F.PLAYING_TTL + 1)
        F.friends()
        self.assertEqual(self.names(), ['GetPlayerSummaries'])
        self.srv.calls.clear()
        self.age(list_at=F.LIST_TTL + 1)
        F.friends()
        self.assertEqual(self.names(), ['GetFriendList'])           # summaries still fresh
        self.srv.calls.clear()
        self.age(owned=F.OWNED_TTL + 1)
        F.friends()
        self.assertEqual(sorted(self.names()), ['GetOwnedGames'] * 3)
        self.srv.calls.clear()
        F.friends(force=True)
        self.assertEqual(self.names()[:2], ['GetFriendList', 'GetPlayerSummaries'])
        self.assertEqual(self.names().count('GetOwnedGames'), 3)

    def test_presence_asks_only_for_summaries(self):
        F.friends()
        self.srv.calls.clear()
        self.age(summary_at=30)
        F.friends(presence=True)
        self.assertEqual(self.names(), [])                         # under a minute
        self.age(summary_at=31)
        data = F.friends(presence=True)
        self.assertEqual(self.names(), ['GetPlayerSummaries'])
        self.assertEqual(data['counts']['playing'], 1)
        self.srv.calls.clear()
        # A stale list and stale owned games are left for the plain call
        self.age(summary_at=F.PRESENCE_TTL + 1, list_at=F.LIST_TTL + 1,
                 owned=F.OWNED_TTL + 1)
        F.friends(presence=True)
        self.assertEqual(self.names(), ['GetPlayerSummaries'])
        self.srv.calls.clear()
        self.age(summary_at=F.PRESENCE_TTL + 1)
        self.cfg['enabled'] = False
        F.friends(presence=True)
        self.assertEqual(self.names(), [])                         # switched off

    def test_summaries_go_100_at_a_time(self):
        many = [f'765611910000{i:05d}' for i in range(150)]
        self.srv.world['friends'] = many
        self.srv.world['players'].update({s: player(s, f'P{i}', cvs=1)
                                          for i, s in enumerate(many)})
        data = F.friends()
        self.assertEqual(data['counts']['total'], 150)
        self.assertEqual(self.srv.batches, [100, 51])               # me + 150

    def test_owned_games_stop_at_the_time_budget(self):
        st = F.load_cache(ME)
        clock = iter(range(10 ** 9, 10 ** 9 + 1000, 10))
        err = F.refresh(st, KEY, ME, budget=5, now=lambda: next(clock), sleep=lambda s: None)
        self.assertIsNone(err)
        self.assertEqual(self.names().count('GetOwnedGames'), 1)   # the rest next time
        later = F._owned_due(st, False, 10 ** 9 + 50)
        self.assertEqual(len(later), 2)

    def test_429_keeps_the_last_good_copy_and_backs_off(self):
        good = F.friends()
        self.age(summary_at=F.PLAYING_TTL + 1)
        self.srv.status['GetPlayerSummaries'] = 429
        data = F.friends()
        self.assertEqual(data['error']['kind'], 'rate-limited')
        self.assertEqual(data['counts']['total'], 4)
        self.assertEqual(data['by_app'], good['by_app'])
        st = json.loads(F.cache_path().read_text())
        self.assertGreater(st['meta']['backoff_until'], time.time() + 20)
        self.srv.calls.clear()
        self.srv.status.clear()
        data = F.friends(force=True)                               # still waiting
        self.assertEqual(self.names(), [])
        self.assertEqual(data['error']['kind'], 'rate-limited')

    def test_a_refused_key_is_not_cached_as_no_friends(self):
        F.friends()
        self.cfg['key'] = 'F' * 32
        data = F.friends(force=True)
        self.assertEqual(data['error']['kind'], 'bad-key')
        self.assertEqual(data['counts']['total'], 4)
        self.assertNotIn('F' * 32, json.dumps(data))
        st = json.loads(F.cache_path().read_text())
        self.assertEqual(len(st['friends']), 4)
        self.assertEqual(st['meta']['last_error']['kind'], 'bad-key')

    def test_401_on_the_list_is_a_private_list_when_the_key_works(self):
        self.srv.world['list_private'] = True
        data = F.friends()
        self.assertEqual(data['error']['kind'], 'private-list')
        self.assertIn('friends list public', data['error']['message'])
        self.assertEqual(data['evidence']['friendlist_status'], 401)
        self.assertEqual(data['friends'], [])
        self.srv.status['GetPlayerSummaries'] = 401                # and when it doesn't
        self.assertEqual(F.friends(force=True)['error']['kind'], 'bad-key')

    def test_offline_serves_the_last_good_copy(self):
        good = F.friends()
        with mock.patch.dict(os.environ, {F.BASE_ENV: 'http://127.0.0.1:9'}):
            data = F.friends(force=True)
        self.assertEqual(data['error']['kind'], 'offline')
        self.assertEqual(data['counts'], good['counts'])
        self.assertEqual(data['by_app'], good['by_app'])

    def test_unreadable_answer_never_empties_the_list(self):
        F.friends()
        self.srv.world['list_body'] = {}                            # no friendslist at all
        data = F.friends(force=True)
        self.assertEqual(data['error']['kind'], 'offline')
        self.assertEqual(data['counts']['total'], 4)

    def test_empty_list(self):
        self.srv.world['friends'] = []
        data = F.friends()
        self.assertIsNone(data['error'])
        self.assertEqual(data['counts'], {'total': 0, 'public': 0, 'private': 0, 'playing': 0})
        self.assertEqual(data['evidence']['friendlist_answer'], 'empty')
        self.assertEqual(data['by_app'], {})

    def test_all_private(self):
        for s in (A, B, D):
            self.srv.world['players'][s]['communityvisibilitystate'] = 1
        data = F.friends()
        self.assertEqual(data['counts']['private'], 4)
        self.assertEqual(data['counts']['public'], 0)
        self.assertEqual(data['by_app'], {})
        self.assertNotIn('GetOwnedGames', self.names())

    def test_no_key_and_switched_off_never_ask_steam(self):
        self.cfg['key'] = None
        data = F.friends(force=True)
        self.assertEqual(data['error']['kind'], 'no-key')
        self.cfg.update(key=KEY, enabled=False)
        data = F.friends()
        self.assertFalse(data['enabled'])
        self.assertIsNone(data['error'])
        self.assertEqual(self.names(), [])
        F.friends(force=True)                                      # Refresh now still asks
        self.assertIn('GetFriendList', self.names())

    def test_stale_presence_is_not_shown_as_playing(self):
        F.friends()
        self.cfg['enabled'] = False                                # no refresh
        self.age(summary_at=F.PLAYING_STALE + 1)
        data = F.friends()
        self.assertEqual(data['counts']['playing'], 0)
        self.assertTrue(all(r['playing'] is None for r in data['friends']))

    def test_another_account_starts_over(self):
        F.friends()
        self.cfg['steamid'] = '76561190000000002'
        self.srv.world['friends'] = []
        self.assertEqual(F.friends()['counts']['total'], 0)

    def test_key_is_masked(self):
        self.assertEqual(F.mask(f'https://x/?key={KEY}&steamid=1', KEY),
                         'https://x/?key=***&steamid=1')
        self.assertNotIn(KEY, F.mask(f'boom {KEY}', KEY))
        err = F.FriendsError('offline', 'x')
        with mock.patch.object(F.net, '_open', side_effect=OSError(f'refused key={KEY}')):
            with self.assertRaises(F.FriendsError) as cm:
                F._get('ISteamUser/GetFriendList/v1', {'steamid': ME}, KEY)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(err.kind, 'offline')

    def test_base_url_only_https_or_this_machine(self):
        for base, want in (('https://steam.example', 'https://steam.example'),
                           ('http://127.0.0.1:5', 'http://127.0.0.1:5'),
                           ('http://evil.example', F.API_BASE), ('ftp://x', F.API_BASE)):
            with mock.patch.dict(os.environ, {F.BASE_ENV: base}):
                self.assertEqual(F.api_base(), want)


class Whose(unittest.TestCase):
    def test_override_then_the_pinned_account(self):
        self.assertEqual(F.whose({'steamid': ME})['steamid'], ME)
        bad = F.whose({'steamid': '123'})
        self.assertIsNone(bad['steamid'])
        self.assertIn('SteamID64', bad['problem'])
        from nexus.cli import steam
        acct = {'accountid': 5, 'steamid64': None, 'persona': 'Me'}
        with mock.patch.object(steam, 'resolve_steam_account', return_value=(acct, 'x')):
            w = F.whose({'steamid': None})
        self.assertEqual(w, {'steamid': str(76561197960265728 + 5), 'persona': 'Me',
                             'source': 'steam'})
        with mock.patch.object(steam, 'resolve_steam_account', return_value=(None, 'none')):
            self.assertIsNone(F.whose({'steamid': None})['steamid'])

    def test_settings_read_config_and_env(self):
        cfg = NX.CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        old = cfg.read_text() if cfg.exists() else None
        self.addCleanup(lambda: cfg.write_text(old) if old is not None
                        else cfg.unlink(missing_ok=True))
        cfg.write_text(f'[steam]\nweb_api_key = "{KEY}"\n\n[friends]\nenabled = true\n'
                       f'steamid = "{ME}"\n')
        with mock.patch.dict(os.environ, {F.KEY_ENV: ''}):
            self.assertEqual(F.settings(), {'key': KEY, 'enabled': True, 'steamid': ME})
        with mock.patch.dict(os.environ, {F.KEY_ENV: 'E' * 32}):
            self.assertEqual(F.settings()['key'], 'E' * 32)


class FriendsCli(_FakeSteamCase):
    """The real `nexus-cli --friends` against the fake server."""

    def test_cli_json_shape_and_no_key_in_output(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = Path(tests.sandbox.config_dir(home))
            cfg.mkdir(parents=True)
            (cfg / 'config.toml').write_text(
                f'[friends]\nenabled = true\nsteamid = "{ME}"\n')
            env = {**os.environ, **tests.sandbox.home_env(home), F.BASE_ENV: self.base,
                   F.KEY_ENV: KEY}
            r = subprocess.run([sys.executable, str(REPO / 'bin' / 'nexus-cli'), '--friends',
                                '--refresh', '--json'], env=env, text=True,
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(KEY, r.stdout + r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(set(data), {'enabled', 'me', 'account', 'friends', 'by_app',
                                         'counts', 'updated', 'evidence', 'error'})
            self.assertEqual(set(data['counts']), {'total', 'public', 'private', 'playing'})
            self.assertEqual(data['counts']['total'], 4)
            self.assertIsNone(data['error'])
            f = data['friends'][0]
            for k in ('steamid', 'name', 'avatar', 'visible', 'playing', 'owned', 'owned_at',
                      'fetched_at', 'evidence'):
                self.assertIn(k, f)
            # A refused key: an error in the JSON, the key nowhere
            env[F.KEY_ENV] = 'F' * 32
            r = subprocess.run([sys.executable, str(REPO / 'bin' / 'nexus-cli'), '--friends',
                                '--refresh', '--json'], env=env, text=True,
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
            self.assertNotIn('F' * 32, r.stdout + r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(data['error']['kind'], 'bad-key')
            self.assertEqual(data['counts']['total'], 4)        # the last good copy


if __name__ == '__main__':
    unittest.main()
