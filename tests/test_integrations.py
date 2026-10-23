"""The outside-world integrations: compatibility facts (Are We Anti-Cheat
Yet, Steam Deck, PCGamingWiki), Discord Rich Presence, phone pushes, the
controller battery, Sunshine entries and FitGirl's feed. No request leaves
the machine: every network call is mocked, and files live in temp dirs."""

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.oskind import linux_only  # noqa: E402
from tests.patching import linux_features, patch_cli  # noqa: E402
from nexus import compat, gamepad, net, notify, presence, repackfeed  # noqa: E402
from nexus import cli  # noqa: E402

PCGW_PAGE = """{{Infobox game
|steam appid  = 976730
}}
==Essential improvements==
===Skip intro videos===
{{Fixbox|description=...}}
===Force higher anisotropic filtering===
==Video==
{{Video
|ultrawidescreen           = limited
|ultrawidescreen notes     =
|4k ultra hd               = true
|hdr                       = false
|high frame rate           = unknown
|upscaling                 = hackable
}}
==API==
{{API
|direct3d versions      = 11.1
|vulkan versions        = false
}}
"""


class Compat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(compat, 'CACHE_DIR', Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_store_appid(self):
        self.assertEqual(compat.store_appid({'source': 'steam', 'appid': 570}), 570)
        self.assertEqual(compat.store_appid({'umu_gameid': 'umu-4231820'}), 4231820)
        self.assertIsNone(compat.store_appid({'umu_gameid': 'umu-0'}))
        self.assertIsNone(compat.store_appid({'source': 'steam', 'appid': 0x90000000}))
        self.assertIsNone(compat.store_appid({'source': 'heroic', 'app_name': 'x'}))

    def test_parse_pcgw(self):
        info = compat.parse_pcgw(PCGW_PAGE)
        self.assertEqual(info['support'], {'Ultrawide': 'limited', '4K': 'true',
                                           'HDR': 'false', 'Upscaling': 'hackable'})
        self.assertEqual(info['apis'], ['Direct3D 11.1'])
        self.assertEqual(info['fixes'], ['Skip intro videos',
                                         'Force higher anisotropic filtering'])

    def test_anticheat_index(self):
        games = [{'name': 'Halo', 'status': 'Broken', 'anticheats': ['Easy Anti-Cheat'],
                  'storeIds': {'steam': '976730'}, 'slug': 'halo',
                  'notes': [['Online is broken', 'https://x']]},
                 {'name': 'No steam id', 'status': 'Denied', 'storeIds': {}}]
        with mock.patch.object(net, 'cached', return_value=(games, 0, False)):
            got = compat.anticheat([976730, 1])
        self.assertEqual(list(got), [976730])
        self.assertEqual(got[976730]['status'], 'Broken')
        self.assertEqual(got[976730]['note'], 'Online is broken')
        self.assertTrue(got[976730]['url'].startswith('https://areweanticheatyet.com/'))

    def test_deck_report_cached_and_served_offline(self):
        body = json.dumps({'success': 1, 'results': {'resolved_category': 3,
                                                     'steamos_resolved_category': 2}})
        with mock.patch.object(net, 'get', return_value=body.encode()):
            self.assertEqual(compat.deck([10]), {10: {'deck': 'Verified',
                                                      'steamos': 'Compatible'}})
        with mock.patch.object(net, 'get', side_effect=net.NetError('offline')):
            self.assertEqual(compat.deck([10], refresh=True)[10]['deck'], 'Verified')

    def test_pcgw_lookup_follows_the_appid_redirect(self):
        parse = json.dumps({'parse': {'wikitext': PCGW_PAGE}}).encode()
        with mock.patch.object(net, 'final_url',
                               return_value='https://www.pcgamingwiki.com/wiki/Halo:_MCC'), \
                mock.patch.object(net, 'get', return_value=parse) as get:
            got = compat.pcgw([976730])[976730]
        self.assertIn('page=Halo%3A_MCC', get.call_args[0][0])
        self.assertEqual(got['page'], 'Halo: MCC')
        self.assertTrue(got['url'].startswith('https://www.pcgamingwiki.com/wiki/'))
        # No page on PCGamingWiki: nothing, remembered.
        with mock.patch.object(net, 'final_url', return_value='https://www.pcgamingwiki.com/'):
            self.assertEqual(compat.pcgw([5], refresh=True), {})


class FakeDiscord(threading.Thread):
    """Speaks just enough of Discord's IPC: READY after the handshake, then
    echoes SET_ACTIVITY back as a success. Listens on an AF_UNIX socket at
    `path`, or talks over `conn` (one end of a socketpair standing in for a
    Windows named pipe)."""

    def __init__(self, path: Path | None = None, conn=None):
        super().__init__(daemon=True)
        self.srv = None
        self.conn = conn
        if conn is None:
            self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.srv.bind(str(path))
            self.srv.listen(1)
        self.frames = []

    def _read(self, conn):
        head = conn.recv(8)
        if len(head) < 8:
            return None
        op, n = struct.unpack('<II', head)
        body = b''
        while len(body) < n:
            body += conn.recv(n - len(body))
        return op, json.loads(body or b'{}')

    def _write(self, conn, op, obj):
        data = json.dumps(obj).encode()
        conn.sendall(struct.pack('<II', op, len(data)) + data)

    def run(self):
        conn = self.conn or self.srv.accept()[0]
        with conn:
            while (frame := self._read(conn)) is not None:
                self.frames.append(frame)
                op, body = frame
                if op == 0:
                    self._write(conn, 1, {'evt': 'READY', 'cmd': 'DISPATCH'})
                elif op == 1:
                    self._write(conn, 1, {'evt': None, 'cmd': body['cmd'],
                                          'nonce': body['nonce']})
                else:
                    break


class Presence(unittest.TestCase):
    @linux_only
    def test_discord_handshake_and_activity(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeDiscord(Path(d, 'discord-ipc-0'))
            fake.start()
            with mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': d, 'TMPDIR': d}):
                client = presence.discord_presence('1234567890', 'Hades', 1145360)
            self.assertIsNotNone(client)
            client.close()
            fake.join(5)
            fake.srv.close()
        (op0, hello), (op1, act) = fake.frames[:2]
        self.assertEqual((op0, hello['client_id']), (0, '1234567890'))
        activity = act['args']['activity']
        self.assertEqual(activity['details'], 'Hades')
        self.assertIn('1145360', activity['assets']['large_image'])

    def test_discord_over_a_named_pipe(self):
        # Windows: the same frames, written to \\.\pipe\discord-ipc-N opened
        # as a file. A socketpair plays the pipe.
        ours, theirs = socket.socketpair()
        fake = FakeDiscord(conn=theirs)
        fake.start()
        pipe = ours.makefile('rwb', buffering=0)
        with mock.patch.object(presence.platform, 'discord_ipc_paths',
                               return_value=[r'\\.\pipe\discord-ipc-0']), \
                mock.patch.object(presence, '_open_pipe', return_value=pipe) as opened:
            client = presence.discord_presence('1234567890', 'Hades')
        self.assertIsNotNone(client)
        self.assertEqual(opened.call_args[0][0], r'\\.\pipe\discord-ipc-0')
        client.close()
        ours.close()
        fake.join(5)
        self.assertEqual([op for op, _ in fake.frames], [0, 1, 2])
        self.assertEqual(fake.frames[1][1]['args']['activity']['details'], 'Hades')

    def test_a_missing_pipe_is_skipped(self):
        with mock.patch.object(presence.platform, 'discord_ipc_paths',
                               return_value=[r'\\.\pipe\discord-ipc-0']), \
                mock.patch.object(presence, '_open_pipe', side_effect=FileNotFoundError):
            self.assertIsNone(presence.discord_presence('123', 'X'))

    def test_windows_keeps_awake_in_process(self):
        held = mock.Mock(spec=['release'])
        with mock.patch.object(presence.platform, 'inhibit_sleep', return_value=held), \
                mock.patch.object(presence.subprocess, 'Popen') as popen:
            got = presence.inhibit('Playing Hades')
            presence.release(got)
        self.assertIs(got, held)
        held.release.assert_called_once_with()
        popen.assert_not_called()

    def test_discord_needs_a_client_id_and_a_socket(self):
        self.assertIsNone(presence.discord_presence(None, 'X'))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': d, 'TMPDIR': d}):
            self.assertIsNone(presence.discord_presence('123', 'X'))

    def test_holder_lets_go_when_stdin_closes(self):
        with mock.patch.object(presence.shutil, 'which', return_value=None):
            proc = presence.inhibit('test')
        self.assertIsNotNone(proc)
        presence.release(proc)
        self.assertIsNotNone(proc.returncode)


class Notify(unittest.TestCase):
    def _settings(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'config.toml')
            p.write_text(text)
            with mock.patch.object(notify, 'CONFIG_PATH', p):
                return notify.settings()

    @linux_features
    def test_settings(self):
        s = self._settings('[notify]\nntfy = "http://insecure/x"\nkdeconnect = true\n')
        self.assertIsNone(s['ntfy'])                      # https only
        self.assertTrue(s['kdeconnect'])
        self.assertFalse(notify.configured(self._settings('')))

    def test_ntfy_push(self):
        s = {'ntfy': 'https://ntfy.sh/t', 'ntfy_token': 'tk', 'kdeconnect': False,
             'min_minutes': 3}
        with mock.patch.object(net, 'post') as post:
            self.assertEqual(notify.push("Hades installed", "Ready", True, s), [])
        url, body, headers = post.call_args[0]
        self.assertEqual((url, body), ('https://ntfy.sh/t', b'Ready'))
        self.assertEqual(headers['Authorization'], 'Bearer tk')
        with mock.patch.object(net, 'post', side_effect=net.NetError('down')):
            self.assertEqual(len(notify.push("t", "b", False, s)), 1)

    def test_post_refuses_http(self):
        with self.assertRaises(net.NetError):
            net.post('http://ntfy.sh/t', b'x')


@linux_only
class Battery(unittest.TestCase):
    def test_reads_controller_batteries_only(self):
        with tempfile.TemporaryDirectory() as d:
            for name, cap, status in (('ps-controller-battery-aa:bb', '64', 'Discharging'),
                                      ('xpadneo-1', '10', 'Charging'),
                                      ('BAT0', '90', 'Discharging')):
                p = Path(d, name)
                p.mkdir()
                (p / 'capacity').write_text(cap)
                (p / 'status').write_text(status)
            got = gamepad.battery(Path(d))
        self.assertEqual(got, [{'percent': 10, 'charging': True},
                               {'percent': 64, 'charging': False}])
        self.assertEqual(gamepad.battery(Path('/nonexistent')), [])


class Sunshine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.apps = self.tmp / 'apps.json'
        self.apps.write_text(json.dumps({'env': {}, 'apps': [{'name': 'Desktop'}]}))
        for value in ('SUNSHINE_APPS', 'SUNSHINE_ART'):
            p = patch_cli(value, self.apps if value == 'SUNSHINE_APPS' else self.tmp / 'art')
            p.start()
            self.addCleanup(p.stop)
        self.game = {'slug': 'hades', 'display_name': 'Hades', 'source': 'nexus'}
        p = patch_cli('_game_by_id', return_value=self.game)
        p.start()
        self.addCleanup(p.stop)

    def test_add_update_remove(self):
        cli.cmd_sunshine('hades')
        apps = json.loads(self.apps.read_text())['apps']
        self.assertEqual(len(apps), 2)
        self.assertTrue(apps[1]['cmd'].endswith('--launch hades --wait'))
        # The user's own additions in Sunshine survive an update.
        data = json.loads(self.apps.read_text())
        data['apps'][1]['prep-cmd'] = [{'do': 'x', 'undo': 'y'}]
        self.apps.write_text(json.dumps(data))
        cli.cmd_sunshine('hades')
        apps = json.loads(self.apps.read_text())['apps']
        self.assertEqual(len(apps), 2)
        self.assertEqual(apps[1]['prep-cmd'], [{'do': 'x', 'undo': 'y'}])
        self.assertTrue(cli.sunshine_has(self.game))
        cli.cmd_sunshine('hades', remove=True)
        self.assertEqual(json.loads(self.apps.read_text())['apps'], [{'name': 'Desktop'}])
        self.assertTrue(list(self.tmp.glob('apps.json.bak-*')))

    def test_entry_matches_whole_words_only(self):
        apps = [{'name': 'Other', 'cmd': "nexus-cli --launch hades-ii --wait"},
                {'name': 'Steam', 'detached': ["nexus-cli --open-url steam://rungameid/5700"]}]
        self.assertIsNone(cli._sunshine_find(apps, self.game))
        self.assertIsNone(cli._sunshine_find(apps, {'slug': 's', 'source': 'steam',
                                                    'launcher': 'steam://rungameid/570'}))
        apps.append({'name': 'Hades', 'cmd': "'/x y/nexus-cli' --launch hades --wait"})
        self.assertEqual(cli._sunshine_find(apps, self.game), 2)

    def test_steam_game_is_detached(self):
        e = cli.sunshine_entry({'slug': 'steam-570', 'name': 'Dota', 'source': 'steam',
                                'launcher': 'steam://rungameid/570'})
        self.assertNotIn('cmd', e)
        self.assertIn('--open-url steam://rungameid/570', e['detached'][0])

    def test_unreadable_apps_json_is_never_rewritten(self):
        self.apps.write_text('{ not json')
        with self.assertRaises(SystemExit):
            cli.cmd_sunshine('hades')
        self.assertEqual(self.apps.read_text(), '{ not json')


FEED = """<?xml version="1.0"?>
<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Frosthaven &#8211; v1.0.34024</title>
<link>https://fitgirl-repacks.site/frosthaven/</link>
<pubDate>Sat, 17 Oct 2026 02:05:21 +0000</pubDate><category>Lossless Repack</category></item>
<item><title>Updates Digest for October 15, 2026</title>
<link>https://fitgirl-repacks.site/updates-digest/</link>
<pubDate>Fri, 16 Oct 2026 11:46:36 +0000</pubDate><category>Updates Digest</category>
<content:encoded><![CDATA[
<div class="su-spoiler"><div class="su-spoiler-title" tabindex="0" role="button"><span class="su-spoiler-icon"></span>RIDE 6</div><div class="su-spoiler-content su-u-clearfix su-u-trim">
<a href="https://fitgirl-repacks.site/ride-6/">Repack page</a><br />
<a href="https://filecrypt.cc/Container/X.html">RIDE_6_Update.rar</a></div></div>
<div class="su-spoiler"><div class="su-spoiler-title" tabindex="0" role="button"><span class="su-spoiler-icon"></span>Evil</div><div class="su-spoiler-content su-u-clearfix su-u-trim">
<a href="https://fitgirl-repacks.evil.example/evil/">Repack page</a></div></div>
]]></content:encoded></item>
<item><title>Upcoming Repacks</title><link>https://fitgirl-repacks.site/upcoming/</link>
<pubDate>Sat, 17 Oct 2026 02:40:01 +0000</pubDate><category>Uncategorized</category></item>
</channel></rss>"""


class RepackFeed(unittest.TestCase):
    def test_parse(self):
        items = repackfeed.parse_feed(FEED)
        self.assertEqual([(i['kind'], i['slug']) for i in items],
                         [('repack', 'frosthaven'), ('update', 'ride-6')])
        self.assertEqual(items[0]['title'], 'Frosthaven – v1.0.34024')
        self.assertNotIn('filecrypt', json.dumps(items))          # no download links kept

    def test_base_title(self):
        for raw, want in (("Frosthaven – v1.0.34024", "Frosthaven"),
                          ("Bloodborne: GOTY Edition, v1.09 + All DLCs", "Bloodborne: GOTY Edition"),
                          ("Enshrouded: Deluxe Edition – v1.0 (Build 1)", "Enshrouded: Deluxe Edition"),
                          ("Kebab Chefs! - Restaurant Simulator", "Kebab Chefs! - Restaurant Simulator")):
            self.assertEqual(repackfeed.base_title(raw), want)

    def test_updates_only_when_posted_after_the_repack(self):
        posts = {p['slug']: p for p in repackfeed.parse_feed(FEED)}
        data = {'posts': posts}
        old = {'slug': 'ride-6', 'display_name': 'RIDE 6',
               'repack_installed_at': '2026-09-01T10:00:00'}
        new = {'slug': 'frost', 'display_name': 'Frosthaven',
               'repack_installed_at': '2026-10-17T09:00:00'}
        native = {'slug': 'frosthaven', 'display_name': 'Frosthaven'}   # not from a repack
        got = repackfeed.updates([old, new, native], data)
        self.assertEqual(list(got), ['ride-6'])
        self.assertEqual(got['ride-6']['url'], 'https://fitgirl-repacks.site/ride-6/')
        # Matched by title when the slugs differ.
        renamed = {**old, 'slug': 'ride6'}
        self.assertIn('ride6', repackfeed.updates([renamed], data))

    def test_refresh_merges_and_keeps_old_posts(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(repackfeed, 'STORE', Path(d, 'feed.json')):
            old = {'slug': 'gone', 'title': 'Gone', 'kind': 'repack', 'url': 'x',
                   'date': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            Path(d, 'feed.json').write_text(json.dumps({'posts': {'gone': old}}))
            with mock.patch.object(net, 'cached', return_value=(FEED, time.time(), False)):
                data = repackfeed.refresh()
            self.assertEqual(set(data['posts']), {'gone', 'frosthaven', 'ride-6'})
            self.assertTrue(data['fetched_at'])


try:
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ.setdefault('NEXUS_NO_GAMEPAD', '1')
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from nexus import gui as nx
except ImportError:
    nx = None


@unittest.skipIf(nx is None, "PyQt6 not available")
class Gui(unittest.TestCase):
    def test_compat_facts(self):
        tips = {'deck': {'deck': 'Verified', 'steamos': 'Compatible'},
                'anticheat': {'status': 'Denied', 'anticheats': ['BattlEye'],
                              'url': 'javascript:alert(1)', 'note': '<b>x</b>'},
                'pcgw': {'support': {'HDR': 'true'}, 'apis': [], 'fixes': ['Skip intros'],
                         'url': 'https://www.pcgamingwiki.com/wiki/X'}}
        lines, more = nx.compat_facts(tips)
        texts = [t for t, _c, _b in lines]
        self.assertIn('Steam Deck Verified', texts)
        self.assertIn('Anti-cheat denied', texts)
        self.assertNotIn('javascript:', more)                     # only https links
        self.assertNotIn('<b>x</b>', more)                        # escaped
        self.assertIn('blocked by the developer', nx.anticheat_warning(tips))
        self.assertIsNone(nx.anticheat_warning({'anticheat': {'status': 'Running'}}))
        self.assertEqual(nx.compat_facts({}), ([], ''))

    @linux_features
    def test_menu_offers_sunshine_and_the_posted_update(self):
        g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a', 'sunshine': True,
             'repack_online': {'url': 'https://fitgirl-repacks.site/a/'}}
        keys = [a.key for a in nx.game_actions(g)]
        self.assertIn('remove-sunshine', keys)
        self.assertEqual(keys[1], 'repack-page')
        g['repack_update'] = '/downloads/a'               # a local repack wins
        keys = [a.key for a in nx.game_actions(g)]
        self.assertEqual(keys[1], 'install-update')
        self.assertNotIn('repack-page', keys)
        self.assertNotIn('add-sunshine', [a.key for a in nx.game_actions({'slug': 'b'})])


if __name__ == '__main__':
    unittest.main()
