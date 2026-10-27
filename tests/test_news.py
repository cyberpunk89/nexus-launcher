"""Tests for nexus/news.py feed parsing and sanitising."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import news  # noqa: E402

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
<item><title>Proton &amp; you</title><link>https://example.com/a</link>
<pubDate>Tue, 30 Sep 2026 10:00:00 +0000</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;script&gt;x&lt;/script&gt;&lt;/p&gt;</description></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Y</title><entry><title>Atom post</title><link rel="alternate" href="https://example.com/b"/>
<updated>2026-09-29T08:00:00Z</updated><summary>Short   summary</summary></entry></feed>"""


class Parse(unittest.TestCase):
    def test_rss(self):
        items = news.parse_feed(RSS, 'X')
        self.assertEqual(items[0]['title'], 'Proton & you')
        self.assertEqual(items[0]['link'], 'https://example.com/a')
        self.assertNotIn('<', items[0]['summary'])
        self.assertTrue(items[0]['date'].startswith('2026-09-30'))
        self.assertIsNone(items[1]['link'])            # non-http links dropped

    def test_atom(self):
        items = news.parse_feed(ATOM, 'Y')
        self.assertEqual(items[0]['link'], 'https://example.com/b')
        self.assertEqual(items[0]['summary'], 'Short summary')

    def test_bbcode_and_length(self):
        t = news._text('[h1]Patch[/h1] ' + 'word ' * 200, 50)
        self.assertTrue(t.startswith('Patch word'))
        self.assertLessEqual(len(t), 51)


RSS_IMAGES = """<?xml version="1.0"?><rss version="2.0"
 xmlns:media="http://search.yahoo.com/mrss/"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Z</title>
<item><title>Enclosure</title><link>https://e.com/1</link>
<enclosure url="https://e.com/a.jpg" type="image/jpeg" length="1"/></item>
<item><title>Media</title><link>https://e.com/2</link>
<media:content url="https://e.com/v.mp4" type="video/mp4"/>
<media:thumbnail url="https://e.com/t.png"/></item>
<item><title>Inline</title><link>https://e.com/3</link>
<description>&lt;img src=&quot;https://e.com/i.webp&quot; /&gt; Words</description></item>
<item><title>Content</title><link>https://e.com/4</link><description>none</description>
<content:encoded><![CDATA[<p><img alt="x" src='https://e.com/c.jpg'></p>]]></content:encoded></item>
<item><title>Insecure</title><link>https://e.com/5</link>
<description>&lt;img src="http://e.com/plain.jpg"&gt;</description></item>
</channel></rss>"""


class Images(unittest.TestCase):
    def test_feed_images(self):
        got = {i['title']: i['image'] for i in news.parse_feed(RSS_IMAGES, 'Z')}
        self.assertEqual(got, {'Enclosure': 'https://e.com/a.jpg',
                               'Media': 'https://e.com/t.png',      # the video is skipped
                               'Inline': 'https://e.com/i.webp',
                               'Content': 'https://e.com/c.jpg',
                               'Insecure': None})                    # https only
        inline = next(i for i in news.parse_feed(RSS_IMAGES, 'Z') if i['title'] == 'Inline')
        self.assertEqual(inline['summary'], 'Words')

    def test_steam_images(self):
        self.assertEqual(news.steam_image('[img]{STEAM_CLAN_IMAGE}/4004/ab.jpg[/img] hi'),
                         'https://clan.akamai.steamstatic.com/images/4004/ab.jpg')
        self.assertEqual(news.steam_image('see https://cdn.x/y.png now'), 'https://cdn.x/y.png')
        self.assertIsNone(news.steam_image('no pictures'))
        self.assertIsNone(news.steam_image('http://cdn.x/y.png'))


if __name__ == '__main__':
    unittest.main()


class UserFeeds(unittest.TestCase):
    def test_editing_feeds_keeps_disabled_ones(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            cfg = tests.sandbox.config_dir(home) / 'news.toml'
            cfg.parent.mkdir(parents=True)
            cfg.write_text('disabled = ["gol", "user:https://b.example/feed"]\n'
                           '[[feed]]\nname = "A"\nurl = "https://a.example/feed"\n'
                           '[[feed]]\nname = "B"\nurl = "https://b.example/feed"\n')
            env = {**os.environ, **tests.sandbox.home_env(home)}
            cli = Path(__file__).resolve().parent.parent / 'bin' / 'nexus-cli'
            subprocess.run([sys.executable, str(cli), '--news-add-feed', 'https://c.example/rss'],
                           env=env, check=True, capture_output=True, timeout=60)
            with mock.patch.object(news, 'NEWS_CONFIG', cfg):
                self.assertEqual([f['url'] for f in news.user_feeds()],
                                 ['https://a.example/feed', 'https://b.example/feed',
                                  'https://c.example/rss'])
                self.assertNotIn('gol', [f['id'] for f in news.feeds()])
                self.assertNotIn('https://b.example/feed', [f['url'] for f in news.feeds()])

    def test_cache_names_differ_for_long_urls_on_one_site(self):
        seen = []
        with mock.patch.object(news.net, 'cached',
                               lambda url, name, *a, **k: seen.append(name) or (RSS, 0, False)):
            base = 'https://example.com/' + 'a' * 80
            for u in (base + '/one', base + '/two'):
                news._fetch_feed({'id': f'user:{u}', 'url': u, 'name': 'x'}, False)
        self.assertEqual(len(set(seen)), 2)


class Sources(unittest.TestCase):
    """The catalogue, per-platform defaults and the on/off switches."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / 'news.toml'
        self.addCleanup(self._tmp.cleanup)
        patch = mock.patch.object(news, 'NEWS_CONFIG', self.cfg)
        patch.start()
        self.addCleanup(patch.stop)

    def on(self):
        return [f['id'] for f in news.feeds()]

    def test_catalogue_is_sane(self):
        ids = [c['id'] for c in news.CATALOGUE]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(c['url'].startswith('https://') for c in news.CATALOGUE))
        self.assertGreaterEqual(len(ids), 8)

    def test_defaults_follow_the_platform(self):
        with mock.patch.object(news, 'platform', lambda: 'linux'):
            self.assertEqual(self.on(), ['gol', 'boilingsteam'])
            self.assertTrue(news.options()['proton'])
        with mock.patch.object(news, 'platform', lambda: 'windows'):
            self.assertEqual(self.on(), ['pcgamer', 'rps'])
            self.assertEqual(news.options(), {'steam': True, 'proton': False})

    def test_switches_store_only_the_difference(self):
        with mock.patch.object(news, 'platform', lambda: 'linux'):
            self.assertTrue(news.set_source('ign', True))
            self.assertTrue(news.set_source('gol', False))
            self.assertEqual(self.on(), ['boilingsteam', 'ign'])
            text = self.cfg.read_text()
            self.assertIn('enabled = ["ign"]', text)
            self.assertIn('disabled = ["gol"]', text)
            news.set_source('gol', True)           # back to its default: nothing stored
            self.assertNotIn('gol', self.cfg.read_text())
            self.assertFalse(news.set_source('nope', True))

    def test_switches_survive_adding_a_feed(self):
        with mock.patch.object(news, 'platform', lambda: 'linux'):
            news.set_source('kotaku', True)
            news.set_option('steam', False)
            news.save_user_feeds([{'name': 'Mine', 'url': 'https://m.example/rss'}])
            self.assertIn('kotaku', self.on())
            self.assertIn('user:https://m.example/rss', self.on())
            self.assertEqual(news.options(), {'steam': False, 'proton': True})
            news.set_source('user:https://m.example/rss', False)
            self.assertNotIn('user:https://m.example/rss', self.on())
            self.assertIn('user:https://m.example/rss', [f['id'] for f in news.all_feeds()])

    def test_options_round_trip_against_platform_default(self):
        with mock.patch.object(news, 'platform', lambda: 'windows'):
            news.set_option('proton', True)
            self.assertIn('proton = true', self.cfg.read_text())
            self.assertTrue(news.options()['proton'])
            news.set_option('proton', False)
            self.assertNotIn('proton', self.cfg.read_text())
            self.assertFalse(news.set_option('bogus', True))

