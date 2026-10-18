"""Tests for nexus/news.py feed parsing and sanitising."""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
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
