"""Launching through Steam, Theatre's screensaver, the repack safety look,
achievements from Steam-emulator files, and the torrent-client hand-off.
Files live in temp dirs; the torrent client is a fake local HTTP server."""

import io
import json
import shutil
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.oskind import linux_only  # noqa: E402
from tests.patching import patch_cli, patch_gui  # noqa: E402
from nexus import cli, emuach, safety, torrent  # noqa: E402


class LaunchViaSteam(unittest.TestCase):
    def test_shortcut_url(self):
        url = cli.steam_shortcut_url({'steam_appid': 2399106986})
        self.assertEqual(url, f"steam://rungameid/{(2399106986 << 32) | 0x02000000}")
        self.assertIsNone(cli.steam_shortcut_url({'steam_appid': 570}))   # a store game
        self.assertIsNone(cli.steam_shortcut_url({}))

    def test_launch_goes_through_steam_without_a_tracker(self):
        entry = {'slug': 'g', 'steam_appid': 2399106986, 'launch_via': 'steam'}
        from nexus import steamlib
        with patch_cli('registry_get', return_value=entry), \
                mock.patch.object(steamlib, 'running', return_value={'slugs': set()}), \
                patch_cli('prefix_processes', return_value=[]), \
                patch_cli('touch_last_played'), \
                mock.patch.object(steamlib, 'open_url', return_value=True) as op, \
                mock.patch('subprocess.Popen') as popen:
            cli.cmd_launch(Path('/nowhere'), 'g')
        op.assert_called_once_with(cli.steam_shortcut_url(entry))
        popen.assert_not_called()

    def test_needs_a_shortcut(self):
        with patch_cli('_require_entry', return_value={'slug': 'g'}), \
                self.assertRaises(SystemExit):
            cli.cmd_launch_via('g', 'steam')


def make_repack(d: Path, extra: dict | None = None) -> Path:
    r = d / 'Game [FitGirl Repack]'
    (r / 'MD5').mkdir(parents=True)
    (r / 'setup.exe').write_bytes(b'MZ')
    (r / 'fg-01.bin').write_bytes(b'x')
    (r / 'fg-optional-bonus.bin').write_bytes(b'x')
    (r / 'MD5' / 'QuickSFV.EXE').write_bytes(b'MZ')
    (r / 'MD5' / 'QuickSFV.ini').write_text('')
    (r / 'MD5' / 'fitgirl-bins.md5').write_text('')
    (r / 'Verify BIN files before installation.bat').write_text(
        '@echo off\ncd /d "%~dp0"\ncd MD5\nstart QuickSFV.EXE fitgirl-bins.md5\n')
    for name, data in (extra or {}).items():
        (r / name).write_text(data)
    return r


class Safety(unittest.TestCase):
    def check(self, extra=None, header=b'Inno https://download.microsoft.com/x'):
        with tempfile.TemporaryDirectory() as d:
            return safety.check(make_repack(Path(d), extra), header_reader=lambda _p: header)

    def test_normal_repack(self):
        self.assertEqual(self.check()['verdict'], 'ok')

    def test_extra_program_is_a_warning(self):
        r = self.check({'crack.exe': 'MZ'})
        self.assertEqual(r['verdict'], 'warn')
        self.assertEqual(r['findings'][0]['file'], 'crack.exe')

    def test_disguised_names_are_dangerous(self):
        self.assertEqual(self.check({'readme.pdf.exe': 'MZ'})['verdict'], 'danger')
        self.assertEqual(self.check({'setup‮txt.exe': 'MZ'})['verdict'], 'danger')

    def test_scripts_and_installer_commands(self):
        bad_bat = {'Verify BIN files before installation.bat':
                   '@echo off\npowershell -enc AAAA\n'}
        self.assertEqual(self.check(bad_bat)['verdict'], 'danger')
        self.assertEqual(self.check(header=b'... powershell -EncodedCommand ...')['verdict'],
                         'danger')
        self.assertEqual(self.check(header=None)['verdict'], 'warn')     # not Inno
        r = self.check(header=b'see https://github.com/someone/project')
        self.assertEqual((r['verdict'], r['findings'][0]['level']), ('ok', 'info'))

    def test_cli_refuses_a_tampered_repack(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(safety, 'check', return_value={
                    'verdict': 'danger', 'findings': [], 'clamav': None}), \
                self.assertRaises(SystemExit) as ex:
            cli.cmd_check_repack(d, False, False)
        self.assertEqual(ex.exception.code, 2)


SSR = ('<script type="application/json" id="valve-ssr-data" nonce="x">'
       + json.dumps({'renderContext': {'queryData': json.dumps({'queries': [{'state': {'data': {
           'groups': [{'achievements': [
               {'api_name': 'ACH_A', 'name': 'First &amp; Last', 'description': 'Do it',
                'hidden': False, 'icon_achieved': 'https://shared.fastly.steamstatic.com/a.jpg',
                'icon_unachieved': 'https://shared.fastly.steamstatic.com/a_g.jpg'},
               {'api_name': 'ACH_B', 'name': 'Secret', 'description': '', 'hidden': True},
               {'api_name': 'OLD', 'name': 'Gone', 'archived': True}]}]}}}]})}})
       + '</script>')


class Achievements(unittest.TestCase):
    def test_parse_formats(self):
        with tempfile.TemporaryDirectory() as d:
            j = Path(d, 'a.json')
            j.write_text(json.dumps({'ACH_A': {'earned': True, 'earned_time': 1700000000},
                                     'ACH_B': {'earned': False, 'earned_time': 0}}))
            self.assertEqual(emuach.parse(j, 'json'), {'ACH_A': 1700000000})
            codex = Path(d, 'c.ini')
            codex.write_text("[SteamAchievements]\nCount=1\n00000=ACH_A\n\n"
                             "[ACH_A]\nAchieved=1\nUnlockTime=1700000001\n"
                             "[ACH_B]\nAchieved=0\n")
            self.assertEqual(emuach.parse(codex, 'ini'), {'ACH_A': 1700000001})
            of = Path(d, 'o.ini')
            of.write_text("[ACH_B]\nachieved=true\ntimestamp=5\n")
            self.assertEqual(emuach.parse(of, 'ini'), {'ACH_B': 5})
            sk = Path(d, 's.ini')
            sk.write_text("[AchievementsUnlockTimes]\nACH_A=7\nACH_B=0\n")
            self.assertEqual(emuach.parse(sk, 'ini'), {'ACH_A': 7})

    def test_community_schema(self):
        items = emuach.parse_community(SSR)
        self.assertEqual([a['name'] for a in items], ['ACH_A', 'ACH_B'])
        self.assertEqual(items[0]['title'], 'First & Last')
        self.assertIsNone(items[1]['icon_url'])

    def test_empty_community_schema_is_not_trusted_for_a_month(self):
        page = {'body': SSR}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(emuach, 'CACHE_DIR', Path(d)), \
                mock.patch.object(emuach.net, 'get', lambda *a, **k: page['body'].encode()):
            self.assertEqual(len(emuach.schema(7)), 2)
            cache = Path(d, 'achschema', '7.json')
            os.utime(cache, (1, 1))                        # stale: refetch
            page['body'] = '<html>age check</html>'
            self.assertEqual(len(emuach.schema(7)), 2)     # the good copy survives
            self.assertGreater(cache.stat().st_mtime, 1)   # ...and isn't re-asked every call
            cache.write_text('[]')                         # a cached empty schema
            old = time.time() - emuach.EMPTY_SCHEMA_AGE - 60
            os.utime(cache, (old, old))
            page['body'] = SSR
            self.assertEqual(len(emuach.schema(7)), 2)     # retried after hours, not a month

    def test_status_and_watcher(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d)
            f = prefix / 'drive_c/users/Public/Documents/Steam/RUNE/4231820/achievements.ini'
            f.parent.mkdir(parents=True)
            f.write_text("[SteamAchievements]\nCount=0\n")
            schema = emuach.parse_community(SSR)
            with mock.patch.object(emuach, 'schema', return_value=[dict(a) for a in schema]):
                st = emuach.status(prefix)
                self.assertEqual((st['appid'], st['emulator'], st['unlocked'], st['total']),
                                 (4231820, 'RUNE', 0, 2))
                got = []
                w = emuach.Watcher(prefix, got.append)
                w._poll()                                  # baseline
                f.write_text("[ACH_A]\nAchieved=1\nUnlockTime=9\n")
                os.utime(f, (1, 1))
                w._poll()
                self.assertEqual([a['title'] for a in got], ['First & Last'])
                st = emuach.status(prefix)
                self.assertEqual(st['unlocked'], 1)
                self.assertTrue(st['items'][0]['unlocked'])
        self.assertIsNone(emuach.status(Path('/nonexistent')))


class FakeTransmission(BaseHTTPRequestHandler):
    torrents = {}
    removed = []
    moves = []
    posts = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        type(self).posts += 1
        # (read before any answer, as Transmission does: Windows resets a
        # connection closed with unread data, and the client sees WinError 10053)
        if self.headers.get('X-Transmission-Session-Id') != 'sid':
            self.send_response(409)
            self.send_header('X-Transmission-Session-Id', 'sid')
            self.end_headers()
            return
        req = json.loads(raw)
        args = req['arguments']
        if req['method'] == 'torrent-add':
            h = torrent.infohash(args['filename'])
            self.torrents[h] = {'hashString': h, 'name': 'Game [FitGirl Repack]',
                                'percentDone': 1.0, 'eta': -1, 'rateDownload': 0, 'status': 6,
                                'downloadDir': args.get('download-dir'), 'error': 0}
            out = {'torrent-added': {'id': 1, 'hashString': h, 'name': 'Game [FitGirl Repack]'}}
        elif req['method'] == 'torrent-get':
            ids = args.get('ids', list(self.torrents))
            out = {'torrents': [self.torrents[i] for i in ids if i in self.torrents]}
        elif req['method'] in ('torrent-start', 'torrent-stop'):
            for i in args['ids']:
                self.torrents[i]['status'] = 0 if req['method'] == 'torrent-stop' else 6
            out = {}
        elif req['method'] == 'torrent-remove':
            self.removed.append((args['ids'], args['delete-local-data']))
            for i in args['ids']:
                self.torrents.pop(i, None)
            out = {}
        elif req['method'].startswith('queue-move-'):
            self.moves.append((req['method'][len('queue-move-'):], args['ids']))
            out = {}
        elif req['method'] == 'session-get':
            out = {'version': '4.1.3', 'download-dir': '/dl', 'config-dir': '/cfg/daemon'}
        elif req['method'] == 'session-stats':
            out = {'downloadSpeed': 10, 'uploadSpeed': 2}
        else:
            out = {}
        body = json.dumps({'result': 'success', 'arguments': out}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Torrents(unittest.TestCase):
    MAGNET = 'magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Game'

    def test_infohash(self):
        self.assertEqual(torrent.infohash(self.MAGNET), '0123456789abcdef0123456789abcdef01234567')
        b32 = 'magnet:?xt=urn:btih:' + 'A' * 32
        self.assertEqual(torrent.infohash(b32), '00' * 20)
        self.assertIsNone(torrent.infohash('magnet:?dn=nothing'))

    def test_content_path_stays_in_folder(self):
        self.assertEqual(torrent._content_path('/dl', 'Game [FitGirl Repack]'),
                         '/dl/Game [FitGirl Repack]')
        for name in ('..', '.', '../home', '/etc', 'a/b', ''):
            self.assertIsNone(torrent._content_path('/dl', name), name)
        # A Windows client's folder stays Windows-shaped, wherever Nexus runs.
        self.assertEqual(torrent._content_path('D:\\Downloads', 'Game'), 'D:\\Downloads\\Game')
        for name in ('a\\b', 'C:x', '..'):
            self.assertIsNone(torrent._content_path('D:\\Downloads', name), name)

    def test_installed_clients_from_their_settings(self):
        with tempfile.TemporaryDirectory() as d:
            tr = Path(d, 'settings.json')
            tr.write_text('{"rpc-enabled": false, "rpc-port": 9092}')
            qb = Path(d, 'qBittorrent.ini')
            qb.write_text('[Preferences]\nWebUI\\Enabled=true\nWebUI\\Port=8181\n')
            with mock.patch.object(torrent.platform, 'torrent_client_settings',
                                   return_value=[('transmission', tr), ('qbittorrent', qb),
                                                 ('transmission', Path(d, 'missing.json'))]):
                got = torrent.installed_clients()
        self.assertEqual(got, [
            {'client': 'transmission', 'remote': False, 'url': 'http://127.0.0.1:9092'},
            {'client': 'qbittorrent', 'remote': True, 'url': 'http://127.0.0.1:8181'}])

    def test_only_loopback(self):
        with self.assertRaises(torrent.TorrentError):
            torrent._http('http://192.168.1.5:9091/transmission/rpc')

    def test_add_follow_and_finish(self):
        srv = HTTPServer(('127.0.0.1', 0), FakeTransmission)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)      # cleanups run last-first: stop serving, then close
        self.addCleanup(srv.shutdown)
        s = {'client': 'transmission', 'url': f'http://127.0.0.1:{srv.server_port}',
             'username': '', 'password': '', 'download_dir': '/games/Install',
             'seed_ratio': None}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'TRACKED', Path(d, 't.json')), \
                mock.patch.object(torrent, 'settings', return_value=s):
            got = torrent.add(self.MAGNET, auto_install=True, s=s)
            self.assertEqual(got['name'], 'Game [FitGirl Repack]')
            rows = torrent.status(s)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]['done'] and rows[0]['auto_install'])
            self.assertEqual(rows[0]['path'], '/games/Install/Game [FitGirl Repack]')
            with self.assertRaises(torrent.TorrentError):
                torrent.add('not a link', s=s)

    @linux_only
    def test_windows_only_env_never_reaches_wine(self):
        from nexus import cli
        with mock.patch.dict(os.environ, {'NoDefaultCurrentDirectoryInExePath': '1',
                                          'KEEP_ME': 'x'}):
            env = cli.wine_env()
            self.assertNotIn('NoDefaultCurrentDirectoryInExePath', env)
            self.assertEqual(env['KEEP_ME'], 'x')
            self.assertNotIn('NODEFAULTCURRENTDIRECTORYINEXEPATH',
                             cli.wine_env({'NODEFAULTCURRENTDIRECTORYINEXEPATH': '1'}))
            out = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, out, True)
            probe = out / 'env.txt'
            rc = cli.run(['sh', '-c', f'env > {probe}'],
                         extra_env={'NoDefaultCurrentDirectoryInExePath': '1'})
            self.assertEqual(rc, 0)
            self.assertNotIn('NoDefaultCurrentDirectoryInExePath', probe.read_text())

    QB = {'client': 'qbittorrent', 'url': 'http://127.0.0.1:1', 'username': '',
          'password': '', 'download_dir': None, 'seed_ratio': None}

    def test_qbittorrent_torrent_file_is_tracked(self):
        # qBittorrent's upload API doesn't name the torrent it added; the hash
        # comes from the file itself (SHA-1 of the bencoded info dict).
        import hashlib
        info = (b'd6:lengthi5e4:name4:Game12:piece lengthi16384e6:pieces20:'
                + b'\x01' * 20 + b'e')
        data = b'd8:announce9:udp://x/a13:creation datei1e4:info' + info + b'e'
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'TRACKED', Path(d, 't.json')), \
                mock.patch.object(torrent.QBittorrent, '_req',
                                  lambda *a, **k: (200, {}, b'Ok.')):
            f = Path(d, 'game.torrent')
            f.write_bytes(data)
            got = torrent.add(str(f), auto_install=True, s=self.QB)
            self.assertEqual(got['hash'], hashlib.sha1(info).hexdigest())
            self.assertTrue(torrent.load_tracked()[got['hash']]['auto_install'])
            f.write_bytes(b'd8:announce3:urle')
            with self.assertRaises(torrent.TorrentError):
                torrent.add(str(f), s=self.QB)
            f.write_bytes(b'd4:infod4:name')                      # truncated
            with self.assertRaises(torrent.TorrentError):
                torrent.add(str(f), s=self.QB)

    def test_bad_client_replies_are_torrent_errors(self):
        import http.client
        with mock.patch.object(torrent.QBittorrent, '_req',
                               lambda *a, **k: (200, {}, b'<html>login</html>')):
            with self.assertRaises(torrent.TorrentError):
                torrent.client(self.QB).list_all()

        def drop(*a, **k):
            raise http.client.IncompleteRead(b"ab")      # not an OSError
        with mock.patch('urllib.request.OpenerDirector.open', drop):
            with self.assertRaises(torrent.TorrentError):
                torrent.client(self.QB).list_all()

    def test_tracked_edits_dont_lose_updates(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'TRACKED', Path(d, 't.json')):
            def worker(n):
                for i in range(25):
                    with torrent.edit_tracked() as t:
                        t[f'{n}-{i}'] = {}
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(torrent.load_tracked()), 100)
            self.assertEqual([p.name for p in Path(d).glob('*.part')], [])

    def test_install_marks_its_download(self):
        from nexus import cli
        h = 'd' * 40
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'TRACKED', Path(d, 't.json')):
            torrent.save_tracked({h: {'name': 'G', 'auto_install': True, 'installed': False}})
            cli._torrent_mark(h.upper(), install_started='now')
            self.assertFalse(torrent.load_tracked()[h]['installed'])     # not yet
            self.assertEqual(torrent.load_tracked()[h]['install_started'], 'now')
            cli._torrent_mark(h, installed=True, slug='g')
            self.assertTrue(torrent.load_tracked()[h]['installed'])
            cli._torrent_mark('not-a-hash', installed=True)               # ignored
            cli._torrent_mark('e' * 40, installed=True)                   # not tracked: ignored
            self.assertEqual(list(torrent.load_tracked()), [h])

    def test_remote_controls_every_torrent(self):
        FakeTransmission.torrents = {'f' * 40: {
            'hashString': 'F' * 40, 'name': 'Other', 'percentDone': 0.25, 'eta': 60,
            'rateDownload': 5, 'rateUpload': 1, 'status': 4, 'downloadDir': '/dl', 'error': 0,
            'uploadRatio': -1, 'addedDate': 100}}
        FakeTransmission.removed = []
        srv = HTTPServer(('127.0.0.1', 0), FakeTransmission)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)      # cleanups run last-first: stop serving, then close
        self.addCleanup(srv.shutdown)
        s = {'client': 'transmission', 'url': f'http://127.0.0.1:{srv.server_port}',
             'username': '', 'password': '', 'download_dir': None, 'seed_ratio': None}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'TRACKED', Path(d, 't.json')):
            torrent.add(self.MAGNET, auto_install=True, s=s, meta={'slug': 'game', 'appid': 7})
            FakeTransmission.torrents[torrent.infohash(self.MAGNET)]['addedDate'] = 200
            rows = torrent.all_status(s)
            self.assertEqual([r['name'] for r in rows], ['Game [FitGirl Repack]', 'Other'])
            self.assertTrue(rows[0]['tracked'] and rows[0]['appid'] == 7)
            self.assertEqual((rows[1]['tracked'], rows[1]['state'], rows[1]['ratio']),
                             (False, 'downloading', 0.0))
            torrent.control('pause', ['F' * 40], s=s)
            self.assertEqual(torrent.all_status(s)[1]['state'], 'paused')
            torrent.control('resume', ['f' * 40], s=s)
            self.assertEqual(torrent.all_status(s)[1]['state'], 'seeding')
            h = torrent.infohash(self.MAGNET)
            torrent.control('remove', [h], delete_data=True, s=s)
            self.assertEqual(FakeTransmission.removed, [([h], True)])
            self.assertNotIn(h, torrent.load_tracked())
            with self.assertRaises(torrent.TorrentError):
                torrent.control('pause', ['nothex'], s=s)
            info = torrent.client(s).info()
            self.assertEqual((info['version'], info['down']), ('4.1.3', 10))
            self.assertEqual(info['config_dir'], '/cfg/daemon')
            # A poll (--torrents --all) shares one client: one session-id
            # handshake, not one per call.
            FakeTransmission.posts = 0
            c = torrent.client(s)
            torrent.all_status(s, c), c.info()
            self.assertEqual(FakeTransmission.posts, 4)       # 409, torrent-get, session-get, stats

    def serve(self, torrents=None):
        """A FakeTransmission on a free port: its settings dict."""
        FakeTransmission.torrents = dict(torrents or {})
        FakeTransmission.removed = []
        srv = HTTPServer(('127.0.0.1', 0), FakeTransmission)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return {'client': 'transmission', 'url': f'http://127.0.0.1:{srv.server_port}',
                'username': '', 'password': '', 'download_dir': None, 'seed_ratio': None}

    def test_grab_keeps_sizes_never_magnet(self):
        from nexus import repackindex
        s = self.serve()
        page = {'url': 'https://fitgirl-repacks.site/test-game/', 'title': 'Test Game',
                'magnets': [{'hash': torrent.infohash(self.MAGNET), 'uri': self.MAGNET}],
                'repack_size': 'from 12.5 GB [Selective Download]',
                'original_size': '38 GB', 'hv': None}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'TRACKED', Path(d, 't.json')), \
                mock.patch.object(torrent, 'settings', return_value=s), \
                mock.patch.object(repackindex, 'fetch_page', return_value=page), \
                mock.patch('sys.stdout'):
            cli.cmd_repack_grab('test-game', '7', True, True)
            meta = torrent.load_tracked()[torrent.infohash(self.MAGNET)]
            raw = Path(d, 't.json').read_text()
        self.assertEqual(meta['repack_bytes'], int(12.5 * (1 << 30)))
        self.assertEqual(meta['original_bytes'], 38 << 30)
        self.assertNotIn('magnet', raw)
        self.assertNotIn('urn:btih', raw)
        # A page with no "Original Size" leaves the key out.
        page['original_size'] = None
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'TRACKED', Path(d, 't.json')), \
                mock.patch.object(torrent, 'settings', return_value=s), \
                mock.patch.object(repackindex, 'fetch_page', return_value=page), \
                mock.patch('sys.stdout'):
            cli.cmd_repack_grab('test-game', None, False, True)
            meta = torrent.load_tracked()[torrent.infohash(self.MAGNET)]
        self.assertNotIn('original_bytes', meta)

    def test_progress_file_roundtrip_ignores_junk_prunes_old(self):
        # Reading (every poll) never deletes; pruning is its own call (made
        # when an install starts).
        h, old = 'a' * 40, 'b' * 40
        now = time.time()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'PIPELINE_DIR', Path(d, 'pipeline')):
            self.assertEqual(torrent.load_progress(), {})               # no folder yet
            torrent.prune_progress(now)                                 # nor here: fine
            torrent.write_progress(h, {'hash': h, 'stage': 'install', 'updated_at': now})
            torrent.write_progress(old, {'hash': old, 'stage': 'install',
                                         'updated_at': now - 2 * 86400})
            pipe = Path(d, 'pipeline')
            (pipe / ('c' * 40 + '.json')).write_text('{not json')
            (pipe / ('e' * 40 + '.json')).write_text('[1, 2]')
            (pipe / ('f' * 40 + '.json')).write_text(json.dumps({'hash': 'd' * 40}))
            (pipe / 'notes.json').write_text('{}')
            stale_junk = pipe / ('9' * 40 + '.json')
            stale_junk.write_text('{')
            os.utime(stale_junk, (now - 2 * 86400, now - 2 * 86400))
            got = torrent.load_progress()
            self.assertEqual(sorted(got), [h, old])
            self.assertEqual(got[h]['stage'], 'install')
            self.assertTrue(stale_junk.exists())                       # read only
            torrent.prune_progress(now)
            self.assertEqual(list(torrent.load_progress()), [h])
            self.assertFalse((pipe / f'{old}.json').exists())          # a day old: pruned
            self.assertFalse(stale_junk.exists())
            self.assertTrue((pipe / ('c' * 40 + '.json')).exists())    # fresh junk: left
            self.assertEqual([p.name for p in pipe.glob('*.part')], [])
            torrent.clear_progress(h)
            torrent.clear_progress(h)                                  # gone already: fine
            self.assertEqual(torrent.load_progress(), {})

    def poll(self, *flags):
        """`--torrents --all [flags]` against a FakeTransmission with three
        tracked downloads: one downloading, one installing (a fresh progress
        file), one whose install was killed (a stale file), plus one untracked."""
        from nexus.cli import entry
        GBb = 1 << 30
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        dl, base = d / 'dl', d / 'Games'
        dl.mkdir()
        a, b, c, other = 'a' * 40, 'b' * 40, 'c' * 40, 'f' * 40

        def t(h, name, pct, status):
            return {'hashString': h, 'name': name, 'percentDone': pct, 'eta': 120 if pct < 1
                    else -1, 'rateDownload': 1 << 20, 'status': status,
                    'downloadDir': str(dl), 'error': 0, 'sizeWhenDone': 10 * GBb,
                    'addedDate': 100}
        s = self.serve({a: t(a, 'Test Game', 0.4, 4), b: t(b, 'Test Game Two', 1.0, 6),
                        c: t(c, 'Test Game Three', 1.0, 6), other: t(other, 'Other', 0.5, 4)})
        s['download_dir'] = str(dl)
        now = time.time()
        tracked = {a: {'name': 'Test Game', 'title': 'Test Game', 'installed': False,
                       'original_bytes': 30 * GBb},
                   b: {'name': 'Test Game Two', 'title': 'Test Game Two', 'installed': False,
                       'install_started': '2026-01-01T00:00:00', 'original_bytes': 20 * GBb},
                   c: {'name': 'Test Game Three', 'title': 'Test Game Three',
                       'installed': False, 'install_started': '2026-01-01T00:00:00'}}
        prog = {'v': 1, 'hash': b, 'slug': 'test-game-two', 'stage': 'install',
                'step': 'Installing', 'written': 5 * GBb, 'expected': 20 * GBb,
                'rate': 50 << 20, 'done': None, 'total': None, 'update': False,
                'dest': str(base / 'test-game-two'), 'started_at': now - 100,
                'updated_at': now - 1, 'pid': os.getpid()}
        with mock.patch.object(torrent, 'TRACKED', d / 't.json'), \
                mock.patch.object(torrent, 'PIPELINE_DIR', d / 'pipeline'), \
                mock.patch.object(torrent, 'settings', return_value=s):
            torrent.save_tracked(tracked)
            torrent.write_progress(b, prog)
            torrent.write_progress(c, {**prog, 'hash': c, 'updated_at': now - 300})
            args = entry.build_parser().parse_args(['--torrents', '--all', *flags])
            out = io.StringIO()
            with mock.patch('sys.stdout', out), mock.patch('sys.stderr', io.StringIO()):
                cli.cmd_torrent(args, base)
        return out.getvalue(), (a, b, c, other), dl

    def test_poll_makes_one_eta_from_recorded_installs(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(torrent, 'STATS', d / 'stats.json'):
            out, (a, *_), _dl = self.poll('--json')
            self.assertFalse(next(x for x in json.loads(out)['pipeline']
                                  if x['hash'] == a)['eta_all'])
            torrent.record_install({'at': 1, 'drive': None, 'written': 10 << 30,
                                    'expected': 10 << 30, 'install_s': 1000.0,
                                    'verify_bytes': None, 'verify_s': None, 'finish_s': 20.0})
            out, (a, *_), _dl = self.poll('--json')
        c = next(x for x in json.loads(out)['pipeline'] if x['hash'] == a)
        self.assertTrue(c['eta_all'])
        self.assertEqual(c['eta'], 120 + 3000 + 20)          # 30 GB at 10 GB per 1000 s

    def test_all_json_has_pipeline_and_drives(self):
        out, (a, b, c, other), dl = self.poll('--json')
        data = json.loads(out)
        self.assertEqual({r['hash'] for r in data['torrents']}, {a, b, c, other})
        self.assertIn('client', data)
        cards = {x['hash']: x for x in data['pipeline']}
        self.assertEqual(set(cards), {a, b, c})                # not the untracked one
        self.assertEqual((cards[a]['stage'], cards[a]['state']), ('download', 'downloading'))
        self.assertEqual((cards[b]['state'], cards[b]['install']['written']),
                         ('installing', 5 << 30))
        self.assertTrue(cards[b]['eta_est'])
        self.assertEqual((cards[c]['state'], cards[c]['reason']),
                         ('failed', 'stopped'))           # its file is stale: killed
        drives = data['drives']
        self.assertEqual(len(drives), 1)                       # one temp folder: one drive
        dr = drives[0]
        self.assertEqual(sorted(dr['roles']), ['download', 'install'])
        self.assertEqual(dr['id'], str(os.stat(dl).st_dev))
        self.assertTrue(dr['about'])
        self.assertEqual(cards[a]['drives'], {'download': dr['id'], 'install': dr['id']})
        # 6 GB still to download + 30 GB to install, 15 GB left to write.
        self.assertEqual(dr['to_write'], (6 << 30) + (30 << 30) + (15 << 30))
        self.assertEqual(dr['free_after'], dr['free'] - dr['to_write'])

    def test_every_drive_a_card_writes_to_is_counted(self):
        # A download in a folder on a third drive is forecast there too; and
        # folders are looked up only for downloads still coming (a dead
        # network folder of a finished or gone one is never touched).
        from collections import namedtuple
        from nexus.cli import downloads as D
        G = 1 << 30
        where = {'/dl': ('1', '/'), '/games': ('2', '/games'), '/other': ('3', '/other')}
        asked = []

        def drive_of(path):
            asked.append(Path(path).as_posix())
            hit = next((v for k, v in where.items() if Path(path).as_posix().startswith(k)), None)
            return {'id': hit[0], 'mount': hit[1]} if hit else None
        usage = namedtuple('usage', 'total used free')
        rows = [{'hash': 'a' * 40, 'tracked': True, 'percent': 50.0, 'state': 'downloading',
                 'size': 10 * G, 'dir': '/other', 'original_bytes': 20 * G},
                {'hash': 'b' * 40, 'tracked': True, 'done': True, 'percent': 100.0,
                 'dir': '/nas/dead', 'installed': False},
                {'hash': 'c' * 40, 'tracked': True, 'gone': True, 'dir': '/nas/gone'},
                {'hash': 'd' * 40, 'tracked': False, 'dir': '/nas/theirs', 'percent': 1.0}]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(torrent, 'PIPELINE_DIR', Path(d, 'pipeline')), \
                mock.patch.object(D, 'drive_of', drive_of), \
                mock.patch.object(D, 'existing_ancestor', lambda p: Path('/games')), \
                mock.patch.object(D.shutil, 'disk_usage', lambda m: usage(100 * G, 0, 50 * G)):
            cards, drives = D._pipeline(rows, {'download_dir': '/dl'}, None, Path('/games'))
        self.assertFalse([a for a in asked if a.startswith('/nas')], asked)
        by_id = {x['id']: x for x in drives}
        self.assertEqual(set(by_id), {'1', '2', '3'})
        self.assertEqual(by_id['3']['roles'], ['download'])
        self.assertEqual(by_id['3']['to_write'], 5 * G)
        self.assertEqual(by_id['2']['to_write'], 20 * G)
        self.assertEqual(by_id['1']['to_write'], 0)

    def test_clear_history_marks_tracked(self):
        # Done recently's Clear (w-94): a mark on each download, nothing else
        # touched; an unknown hash changes nothing and exits 1.
        from nexus.cli import entry
        a, b = 'a' * 40, 'b' * 40
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)

        def run(*argv):
            args = entry.build_parser().parse_args(list(argv))
            out = io.StringIO()
            with mock.patch('sys.stdout', out), mock.patch('sys.stderr', io.StringIO()):
                cli.cmd_torrent(args, d)
            return out.getvalue()
        with mock.patch.object(torrent, 'TRACKED', d / 't.json'):
            torrent.save_tracked({a: {'name': 'A', 'installed': True,
                                      'installed_at': '2026-01-01T00:00:00'},
                                  b: {'name': 'B', 'installed': False}})
            run('--torrent-clear-history', a.upper())
            got = torrent.load_tracked()
            self.assertTrue(got[a]['history_cleared'])
            self.assertEqual(got[a]['installed_at'], '2026-01-01T00:00:00')
            self.assertNotIn('history_cleared', got[b])
            # The known ones are cleared, the unknown ones reported.
            out = run('--torrent-clear-history', b, 'c' * 40, '--json')
            self.assertEqual(json.loads(out), {'cleared': [b], 'unknown': ['c' * 40]})
            self.assertIn('history_cleared', torrent.load_tracked()[b])
            with self.assertRaises(SystemExit) as e:          # none known: exit 1
                run('--torrent-clear-history', 'c' * 40)
            self.assertEqual(e.exception.code, 1)

    def test_text_mode_one_line_per_card(self):
        out, (a, b, c, other), _dl = self.poll()
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        self.assertEqual(len(lines), 4)
        self.assertIn("Test Game · Downloading 40% · 2 min left", lines)
        two = next(x for x in lines if x.startswith("Test Game Two"))
        self.assertTrue(two.startswith("Test Game Two · Installing 5.4 of about 21 GB · est."),
                        two)
        self.assertIn("Test Game Three · The install stopped", lines)
        self.assertIn("Other: 50%", lines)                     # untracked: as before

    def test_other_transmissions_are_found(self):
        from types import SimpleNamespace as P
        with tempfile.TemporaryDirectory() as d:
            daemon, qt, remote, custom = (Path(d, n) for n in ('daemon', 'qt', 'remote', 'cfg'))
            for f in (daemon, qt, remote, custom):
                f.mkdir()
            Path(remote, 'settings.json').write_text('{"remote-session-enabled": true}')
            procs = [P(pid=1, name='transmission-da', cmdline=['/usr/bin/transmission-daemon']),
                     P(pid=2, name='transmission-qt', cmdline=['transmission-qt']),
                     P(pid=3, name='transmission-gt', cmdline=['/usr/bin/transmission-gtk',
                                                              '-g', str(remote)]),
                     P(pid=4, name='transmission-da',
                       cmdline=['transmission-daemon', f'--config-dir={custom}/']),
                     P(pid=5, name='bash', cmdline=['bash', 'transmission-qt']),
                     P(pid=6, name='Transmission-Qt', cmdline=[])]
            dirs = {'daemon': daemon, 'qt': qt, 'gtk': qt}
            with mock.patch.object(torrent.platform, 'processes', lambda: iter(procs)), \
                    mock.patch.object(torrent.platform, 'transmission_config_dir', dirs.get):
                found = torrent.other_transmissions(str(daemon) + '/')
                # qt (its own session) and the -g daemon; not the served one, not
                # the GTK app driving a remote session, not bash's argument.
                self.assertEqual([(o['name'], o['pid']) for o in found],
                                 [('transmission-qt', 2), ('transmission-daemon', 4),
                                  ('transmission-qt', 6)])
                self.assertEqual(found[1]['config_dir'], str(custom))
                self.assertEqual([o['pid'] for o in torrent.other_transmissions(str(qt))],
                                 [1, 4])
                self.assertEqual(torrent.other_transmissions(None), [])
            # A folder this platform can't name is left out, not guessed.
            with mock.patch.object(torrent.platform, 'processes', lambda: iter(procs[:2])), \
                    mock.patch.object(torrent.platform, 'transmission_config_dir',
                                      lambda kind: None):
                self.assertEqual(torrent.other_transmissions(str(daemon)), [])


class RepackPage(unittest.TestCase):
    PAGE = {'url': 'https://fitgirl-repacks.site/test-game/', 'title': 'Test Game',
            'magnets': [{'hash': 'a' * 40, 'uri': Torrents.MAGNET}],
            'repack_size': 'from 12 GB', 'original_size': '38 GB', 'hv': None}

    def page(self, s):
        """`--repack-page --json` with one tracked download in flight (40% of
        10 GB, 30 GB to install) in a temp folder: (answer, that folder)."""
        from nexus import repackindex
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / 'dl').mkdir()
        if s.get('client'):
            s = dict(s, download_dir=str(d / 'dl'))
            FakeTransmission.torrents['b' * 40]['downloadDir'] = str(d / 'dl')
        out = io.StringIO()
        with mock.patch.object(torrent, 'TRACKED', d / 't.json'), \
                mock.patch.object(torrent, 'PIPELINE_DIR', d / 'pipeline'), \
                mock.patch.object(torrent, 'settings', return_value=s), \
                mock.patch.object(repackindex, 'fetch_page', return_value=self.PAGE), \
                mock.patch('sys.stdout', out):
            torrent.save_tracked({'b' * 40: {'name': 'Test Game Two', 'installed': False,
                                             'original_bytes': 30 << 30}})
            cli.cmd_repack_page('test-game', True, d / 'Games')
        return json.loads(out.getvalue()), d

    def test_page_reports_install_drive_and_committed(self):
        s = Torrents.serve(self, {'b' * 40: {
            'hashString': 'b' * 40, 'name': 'Test Game Two', 'percentDone': 0.4, 'eta': 60,
            'rateDownload': 5, 'status': 4, 'error': 0, 'sizeWhenDone': 10 << 30,
            'downloadDir': None, 'addedDate': 1}})
        got, d = self.page(s)
        drive = str(os.stat(d).st_dev)
        self.assertEqual((got['size_bytes'], got['original_bytes']), (12 << 30, 38 << 30))
        self.assertEqual(got['install_dest'], str(d / 'Games'))
        self.assertEqual((got['dest_drive'], got['install_drive']), (drive, drive))
        self.assertIsInstance(got['install_free'], int)
        self.assertTrue(got['install_mount'] and got['dest_mount'])
        self.assertNotIn('magnet:', json.dumps(got))
        # What the download in flight still needs: 6 GB to come, 30 GB to install.
        self.assertEqual(got['committed'], {drive: (6 << 30) + (30 << 30)})

    def test_no_client_means_no_committed(self):
        got, d = self.page({'client': None, 'url': '', 'username': '', 'password': '',
                            'download_dir': None, 'seed_ratio': None})
        self.assertIsNone(got['committed'])
        self.assertIsNone(got['dest'])
        self.assertIsNone(got['dest_drive'])
        self.assertEqual(got['install_drive'], str(os.stat(d).st_dev))


try:
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ.setdefault('NEXUS_NO_GAMEPAD', '1')
    from PyQt6.QtWidgets import QApplication, QWidget
    _app = QApplication.instance() or QApplication([])
    from nexus import gui as nx
except ImportError:
    nx = None


@unittest.skipIf(nx is None, "PyQt6 not available")
class Attract(unittest.TestCase):
    def test_runs_and_a_key_opens_the_game(self):
        host = QWidget()
        host.resize(640, 360)
        a = nx.AttractOverlay(host)
        opened = []
        a.open_game.connect(opened.append)
        with patch_gui('cli_json'):
            a.start([{'slug': 'g', 'display_name': 'G', 'source': 'nexus'},
                     {'slug': 'h', 'display_name': 'H', 'hidden': True}])
            self.assertTrue(a.running)
            self.assertEqual(a.game['slug'], 'g')                # hidden games never shown
            from PyQt6.QtCore import Qt
            from PyQt6.QtGui import QKeyEvent
            from PyQt6.QtCore import QEvent
            a.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                      Qt.KeyboardModifier.NoModifier))
        self.assertFalse(a.running)
        self.assertEqual([g['slug'] for g in opened], ['g'])
        a.start([])
        self.assertFalse(a.running)                               # nothing to show
        host.deleteLater()


if __name__ == '__main__':
    unittest.main()


class QueueMove(unittest.TestCase):
    """Reordering a download in the client's own queue."""
    H = 'a' * 40
    QB = {'client': 'qbittorrent', 'url': 'http://127.0.0.1:1', 'username': '',
          'password': '', 'download_dir': None, 'seed_ratio': None}

    def test_transmission_queue_move(self):
        FakeTransmission.torrents, FakeTransmission.moves = {}, []
        srv = HTTPServer(('127.0.0.1', 0), FakeTransmission)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        s = {'client': 'transmission', 'url': f'http://127.0.0.1:{srv.server_port}',
             'username': '', 'password': '', 'download_dir': None, 'seed_ratio': None}
        for where in ('top', 'up', 'down', 'bottom'):
            torrent.queue_move(self.H.upper(), where, s=s)
        self.assertEqual(FakeTransmission.moves, [(w, [self.H]) for w in
                                                  ('top', 'up', 'down', 'bottom')])
        for bad in ('sideways', '', None):
            with self.assertRaises(torrent.TorrentError):
                torrent.queue_move(self.H, bad, s=s)
        with self.assertRaises(torrent.TorrentError):
            torrent.queue_move('nothex', 'top', s=s)
        self.assertEqual(len(FakeTransmission.moves), 4)

    def test_qbittorrent_queue_move_and_409(self):
        sent = []

        def req(self, path, form=None, **k):
            sent.append((path, form))
            return 200, {}, b''
        with mock.patch.object(torrent.QBittorrent, '_req', req):
            for where, path in (('top', '/torrents/topPrio'), ('up', '/torrents/increasePrio'),
                                ('down', '/torrents/decreasePrio'),
                                ('bottom', '/torrents/bottomPrio')):
                torrent.queue_move(self.H, where, s=self.QB)
                self.assertEqual(sent[-1], (path, {'hashes': self.H}))
        with mock.patch.object(torrent.QBittorrent, '_req', lambda *a, **k: (409, {}, b'')):
            with self.assertRaises(torrent.TorrentError) as cm:
                torrent.queue_move(self.H, 'top', s=self.QB)
            self.assertIn('Torrent Queueing', str(cm.exception))
        calls = []

        def other(self, path, form=None, **k):
            calls.append(path)
            return 500, {}, b''
        with mock.patch.object(torrent.QBittorrent, '_req', other):
            with self.assertRaises(torrent.TorrentError):
                torrent.queue_move(self.H, 'up', s=self.QB)
        self.assertEqual(calls, ['/torrents/increasePrio'])         # never retried


class QueueFields(unittest.TestCase):
    """The rows carry the queue position, the recheck fraction and whether
    the download ever finished."""
    T = {'hashString': 'A' * 40, 'name': 'G', 'percentDone': 1.0, 'status': 2}

    def tr(self, **kw):
        return torrent.Transmission({'url': 'http://127.0.0.1:1', 'username': ''})._row(
            {**self.T, **kw})

    def test_transmission_recheck_and_queue_fields(self):
        self.assertIn('recheckProgress', torrent.Transmission.FIELDS)
        self.assertIn('queuePosition', torrent.Transmission.FIELDS)
        r = self.tr(recheckProgress=0.43, queuePosition=2, doneDate=1700000000)
        self.assertEqual((r['check'], r['queue'], r['completed']), (0.43, 2, True))
        # JSON-RPC 2.0 names (Transmission 4.1) are read as a fallback
        r = self.tr(recheck_progress=0.5, queue_position=0, done_date=5)
        self.assertEqual((r['check'], r['queue'], r['completed']), (0.5, 0, True))
        # only while checking; never finished; no queue position
        r = self.tr(status=4, recheckProgress=0.9, doneDate=0)
        self.assertEqual((r['check'], r['queue'], r['completed']), (None, None, False))
        # checking, but the client gave no figure
        self.assertIsNone(self.tr()['check'])
        self.assertEqual(self.tr(recheckProgress=7)['check'], 1.0)

    def test_qbittorrent_queue_and_checking_fields(self):
        qb = torrent.QBittorrent({'url': 'http://127.0.0.1:1', 'username': ''})
        base = {'hash': 'A' * 40, 'name': 'G', 'progress': 1.0, 'state': 'checkingUP'}
        r = qb._row({**base, 'priority': 3, 'completion_on': 1700000000})
        self.assertEqual((r['queue'], r['check'], r['completed'], r['state']),
                         (2, None, True, 'checking'))
        r = qb._row({**base, 'state': 'downloading', 'priority': 0, 'completion_on': -1})
        self.assertEqual((r['queue'], r['completed']), (None, False))
        r = qb._row({**base, 'priority': -1, 'completion_on': 4294967295})
        self.assertEqual((r['queue'], r['completed']), (None, False))
        r = qb._row(base)
        self.assertEqual((r['queue'], r['completed']), (None, False))


class CliQueue(unittest.TestCase):
    def test_cli_torrent_queue(self):
        from nexus.cli import entry
        h = 'a' * 40
        FakeTransmission.torrents, FakeTransmission.moves = {}, []
        srv = HTTPServer(('127.0.0.1', 0), FakeTransmission)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        s = {'client': 'transmission', 'url': f'http://127.0.0.1:{srv.server_port}',
             'username': '', 'password': '', 'download_dir': None, 'seed_ratio': None}
        with mock.patch.object(torrent, 'settings', return_value=s):
            args = entry.build_parser().parse_args(['--torrent-queue', 'top', h, '--json'])
            self.assertEqual(args.torrent_queue, ['top', h])
            out = io.StringIO()
            with mock.patch('sys.stdout', out):
                cli.cmd_torrent(args, Path('/nowhere'))
            self.assertEqual(json.loads(out.getvalue()), {'ok': True})
            self.assertEqual(FakeTransmission.moves, [('top', [h])])
            bad = entry.build_parser().parse_args(['--torrent-queue', 'sideways', h, '--json'])
            out = io.StringIO()
            with mock.patch('sys.stdout', out), mock.patch('sys.stderr', io.StringIO()), \
                    self.assertRaises(SystemExit):
                cli.cmd_torrent(bad, Path('/nowhere'))
            self.assertIn('error', json.loads(out.getvalue()))
            self.assertEqual(len(FakeTransmission.moves), 1)
