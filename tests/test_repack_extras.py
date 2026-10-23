"""Launching through Steam, Theatre's screensaver, the repack safety look,
achievements from Steam-emulator files, and the torrent-client hand-off.
Files live in temp dirs; the torrent client is a fake local HTTP server."""

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

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
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
        elif req['method'] == 'session-get':
            out = {'version': '4.1.3', 'download-dir': '/dl'}
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
