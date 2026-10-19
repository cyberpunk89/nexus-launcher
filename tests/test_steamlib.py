"""Tests for nexus/steamlib.py against a fake Steam/Heroic layout."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import steamlib  # noqa: E402

try:
    import vdf
except ImportError:
    vdf = None


def acf(appid, name, flags=4, size=1000, last=0, installdir=None):
    return vdf.dumps({'AppState': {
        'appid': str(appid), 'name': name, 'StateFlags': str(flags),
        'installdir': installdir or name, 'SizeOnDisk': str(size),
        'LastPlayed': str(last), 'LastUpdated': '1700000000'}}, pretty=True)


@unittest.skipIf(vdf is None, "python-vdf not installed")
class PlayStatsCache(unittest.TestCase):
    def test_parsed_once_until_the_file_changes(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'Steam'
            cfg = root / 'userdata' / '1' / 'config'
            cfg.mkdir(parents=True)
            lc = cfg / 'localconfig.vdf'

            def write(minutes):
                lc.write_text(vdf.dumps({'UserLocalConfigStore': {'Software': {'Valve': {
                    'Steam': {'apps': {'10': {'Playtime': str(minutes)}}}}}}}))
            write(5)
            cache = Path(d) / 'cache.json'
            calls = []
            real = steamlib._parse_play_stats
            with mock.patch.object(steamlib, '_parse_play_stats',
                                   lambda files: calls.append(1) or real(files)):
                self.assertEqual(steamlib.play_stats(root, cache)[10]['minutes'], 5)
                self.assertEqual(steamlib.play_stats(root, cache)[10]['minutes'], 5)
                self.assertEqual(len(calls), 1)                 # the second came from disk
                write(12345)                                    # Steam rewrote it
                self.assertEqual(steamlib.play_stats(root, cache)[10]['minutes'], 12345)
                self.assertEqual(len(calls), 2)


@unittest.skipIf(vdf is None, "python-vdf not installed")
class SteamScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from unittest import mock                 # never the real play-stats cache
        p = mock.patch.object(steamlib, '_play_cache',
                              lambda: Path(self.tmp.name) / 'playstats.json')
        p.start()
        self.addCleanup(p.stop)
        root = self.root = Path(self.tmp.name) / 'Steam'
        lib2 = self.lib2 = Path(self.tmp.name) / 'SteamLibrary'
        for d in (root / 'steamapps', lib2 / 'steamapps'):
            d.mkdir(parents=True)
        (root / 'steamapps' / 'libraryfolders.vdf').write_text(vdf.dumps({'libraryfolders': {
            '0': {'path': str(root), 'apps': {}}, '1': {'path': str(lib2), 'apps': {}},
            '2': {'path': '/does/not/exist'}}}, pretty=True))
        (root / 'steamapps' / 'appmanifest_1493710.acf').write_text(acf(1493710, 'Proton Experimental'))
        (root / 'steamapps' / 'appmanifest_1628350.acf').write_text(
            acf(1628350, 'Steam Linux Runtime 3.0 (sniper)'))
        (lib2 / 'steamapps' / 'appmanifest_100.acf').write_text(
            acf(100, 'Game A', size=5_000, last=1_700_000_100))
        (lib2 / 'steamapps' / 'appmanifest_200.acf').write_text(acf(200, 'Game B', flags=6))
        (lib2 / 'steamapps' / 'appmanifest_300.acf').write_text(acf(300, 'Downloading', flags=1026))
        cfg = root / 'userdata' / '42' / 'config'
        cfg.mkdir(parents=True)
        (cfg / 'localconfig.vdf').write_text(vdf.dumps({'UserLocalConfigStore': {'Software': {
            'Valve': {'Steam': {'apps': {
                '100': {'LastPlayed': '1700000000', 'Playtime': '90'},
                '200': {'LastPlayed': '1700000500'}}}}}}}, pretty=True))
        art = root / 'appcache' / 'librarycache' / '100' / 'abc123'
        art.mkdir(parents=True)
        for f in ('library_capsule.jpg', 'library_hero.jpg', 'logo.png'):
            (art / f).write_bytes(b'x')

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_filters_tools_and_merges_stats(self):
        games = {g['appid']: g for g in steamlib.scan(self.root, {'100': 'GE-Proton10-1'})}
        self.assertEqual(set(games), {100, 200})          # tools + not-installed dropped
        a, b = games[100], games[200]
        self.assertEqual(a['playtime_minutes'], 90)
        self.assertTrue(a['last_played'].startswith('2023-11-14'))   # acf wins (later)
        self.assertEqual(a['proton'], 'GE-Proton10-1')
        self.assertTrue(a['cover'].endswith('library_capsule.jpg'))
        self.assertTrue(a['hero'].endswith('library_hero.jpg'))
        self.assertIsNone(a['cover_url'])
        self.assertTrue(b['update_pending'])
        self.assertIn('library_600x900', b['cover_url'])  # no local art -> CDN fallback
        self.assertEqual(a['launcher'], 'steam://rungameid/100')
        # Wide/logo art falls back to the CDN, and every game has an install date.
        self.assertIn('header.jpg', b['wide_url'])
        self.assertIn('logo.png', b['logo_url'])
        self.assertTrue(a['installed_at'] and b['installed_at'])

    def test_downloads_reads_only_manifests(self):
        heroic = Path(self.tmp.name) / 'heroic'
        (heroic / 'store').mkdir(parents=True)
        (heroic / 'store' / 'download-manager.json').write_text(json.dumps(
            {'queue': [{'params': {'appName': 'Epic1'}}]}))
        d = steamlib.downloads(self.root, heroic)
        self.assertEqual(d['steam'], {300: {'done': 0, 'total': 0, 'paused': False}})
        self.assertIn(100, d['steam_installed'])
        self.assertIn(200, d['steam_installed'])
        self.assertEqual(d['heroic'], ['Epic1'])
        self.assertEqual(steamlib.downloads(self.root)['heroic'], [])

    def test_birth_time(self):
        t = steamlib.birth_time(self.root)
        self.assertTrue(t is None or t > 0)              # fs-dependent, never raises
        self.assertIsNone(steamlib.birth_time(self.root / 'nope'))

    def test_library_folders_skips_missing(self):
        self.assertEqual(steamlib.library_folders(self.root), [self.root, self.lib2])


class HeroicStore(unittest.TestCase):
    def test_installed_store_games(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            (cfg / 'store_cache').mkdir()
            (cfg / 'store').mkdir()
            (cfg / 'store_cache' / 'legendary_library.json').write_text(json.dumps({'library': [
                {'app_name': 'Snowdrop', 'title': 'Jackbox 4', 'is_installed': True,
                 'art_square': 'https://x/tall.jpg',
                 'install': {'install_path': '/g/j', 'install_size': '1003.83 MiB'}},
                {'app_name': 'Other', 'title': 'Not installed', 'is_installed': False}]}))
            (cfg / 'store_cache' / 'gog_library.json').write_text(json.dumps({'games': [
                {'app_name': 'gog-redist_x', 'is_installed': True, 'install': {}}]}))
            (cfg / 'store' / 'timestamp.json').write_text(json.dumps(
                {'Snowdrop': {'lastPlayed': '2026-08-19T19:17:58.804Z', 'totalPlayed': 73}}))
            games = steamlib.heroic_store_games(cfg)
            self.assertEqual([g['app_name'] for g in games], ['Snowdrop'])
            g = games[0]
            self.assertEqual(g['playtime_minutes'], 73)
            self.assertEqual(g['launcher'], 'heroic://launch/legendary/Snowdrop')
            self.assertEqual(g['cover_url'], 'https://x/tall.jpg')
            self.assertIsNotNone(g['last_played'])


if __name__ == '__main__':
    unittest.main()
