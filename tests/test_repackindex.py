"""The FitGirl catalogue (wp-json) and how wishlist titles are matched
against it. Every request is mocked; the store lives in a temp dir."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from nexus import net, repackindex  # noqa: E402

SITE = "https://fitgirl-repacks.site/"


def post(pid, slug, title, modified='2026-10-01T10:00:00', link=None):
    return {'id': pid, 'slug': slug, 'link': link or f"{SITE}{slug}/",
            'title': {'rendered': title}, 'date_gmt': '2026-01-01T00:00:00',
            'modified': modified}


def catalogue(*titles):
    posts = [post(i, f"s{i}", t) for i, t in enumerate(titles)]
    return {p['slug']: p for p in repackindex.parse_posts(posts)}


class Fake:
    """net.get for the posts API: `pages` is a list of pages of rows."""

    def __init__(self, pages, count=None, fail_at=None):
        self.pages, self.count, self.fail_at, self.urls = pages, count, fail_at, []

    def __call__(self, url, timeout=20, headers=None, limit=0):
        self.urls.append(url)
        if '/categories/' in url:
            return json.dumps({'count': self.count or 0}).encode()
        q = parse_qs(urlsplit(url).query)
        page = int(q['page'][0])
        if self.fail_at == page:
            raise net.NetError("down")
        if page > len(self.pages):
            raise net.NetError("past the end", 400)
        return json.dumps(self.pages[page - 1]).encode()


class Keys(unittest.TestCase):
    def test_keys(self):
        for title, want in (
                ("Frosthaven – v1.0.34024", ['frosthaven']),
                ("Dangerous Golf v1.02", ['dangerous golf']),
                ("Star Trek: Infinite – Deluxe Edition, v1.0.0 (8788) + 4 DLCs",
                 ['star trek infinite']),
                ("Baldur’s Gate 3 – Digital Deluxe Edition, v4.1",
                 ['baldurs gate 3 digital deluxe edition', 'baldurs gate 3']),
                ("Savant: Ascent REMIX – Bundle, v1.04 + Bonus Soundtrack",
                 ['savant ascent remix bundle', 'savant ascent remix']),
                ("ELDEN RING: Shadow of the Erdtree Deluxe Edition, v1.12 + 9 DLCs",
                 ['elden ring shadow of the erdtree deluxe edition', '~elden ring']),
                ("Hollow Knight: Silksong – v1.0", ['hollow knight silksong']),
                ("Death Howl: Deluxe Edition – v1.0.0.1 + 2 Bonus DLCs",
                 ['death howl deluxe edition', 'death howl']),
                ("Persona 3 Reload: Digital Premium Edition – v1.11",
                 ['persona 3 reload digital premium edition', 'persona 3 reload']),
                ("Ratchet & Clank: Rift Apart", ['ratchet and clank rift apart'])):
            self.assertEqual(repackindex.keys(title), want, title)


class Match(unittest.TestCase):
    def setUp(self):
        self.m = repackindex.Matcher(catalogue(
            "ELDEN RING: Shadow of the Erdtree Deluxe Edition, v1.12 + 9 DLCs",
            "Hollow Knight: Silksong – v1.0",
            "Dead Space 2 – v1.1",
            "Baldur’s Gate 3 – Digital Deluxe Edition, v4.1",
            "The Witcher 3: Wild Hunt – Complete Edition, v4.04",
            "Kit’s Quest + Bonus Soundtrack"))

    def find(self, title):
        hit = self.m.find(title)
        return (hit[0]['title'].split(' ')[0], hit[1]) if hit else None

    def test_exact_and_editions(self):
        self.assertEqual(self.find("Baldur's Gate 3"), ('Baldur’s', 'exact'))
        self.assertEqual(self.find("Hollow Knight: Silksong"), ('Hollow', 'exact'))
        self.assertEqual(self.find("Kit's Quest™"), ('Kit’s', 'exact'))
        self.assertEqual(self.find("The Witcher® 3: Wild Hunt"), ('The', 'exact'))

    def test_before_the_colon_is_only_likely(self):
        self.assertEqual(self.find("ELDEN RING"), ('ELDEN', 'close'))

    def test_no_false_friends(self):
        self.assertIsNone(self.find("Hollow Knight"))     # a sequel isn't the game
        self.assertIsNone(self.find("Dead Space"))        # numbers must agree
        self.assertIsNone(self.find("Dead Space 3"))
        self.assertIsNone(repackindex.Matcher(catalogue("Corpse Keeper – v1.0")).find(
            "Core Keeper"))

    def test_close_spelling(self):
        self.assertEqual(self.find("Hollow Knight Silksonng"), ('Hollow', 'close'))
        self.assertEqual(self.find("Baldurs Gate 3"), ('Baldur’s', 'exact'))

    def test_wishlist_matches_and_rejection(self):
        data = {'posts': catalogue("Hollow Knight: Silksong – v1.0", "Frosthaven – v1"),
                'rejected': {}}
        wl = [{'appid': 1030300, 'title': 'Hollow Knight: Silksong'},
              {'appid': 5, 'title': 'Nothing Like It'}, {'title': 'no appid'}]
        got = repackindex.wishlist_matches(wl, data)
        self.assertEqual(list(got), ['1030300'])
        self.assertEqual(got['1030300']['url'], SITE + 's0/')
        data['rejected']['1030300'] = 's0'
        self.assertEqual(repackindex.wishlist_matches(wl, data), {})

    def test_new_matches_first_time_is_a_summary(self):
        data = {}
        fresh, first = repackindex.new_matches({'1': {}, '2': {}}, data)
        self.assertTrue(first)
        self.assertEqual(sorted(fresh), ['1', '2'])
        fresh, first = repackindex.new_matches({'1': {}, '2': {}, '3': {}}, data)
        self.assertEqual((fresh, first), (['3'], False))


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(repackindex, 'STORE', Path(self.tmp.name, 'index.json'))
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(repackindex.time, 'sleep')
        s.start()
        self.addCleanup(s.stop)

    def test_full_build_pages_until_the_end_and_drops_clones(self):
        page1 = [post(i, f"g{i}", f"Game {i}") for i in range(100)]
        page2 = [post(200, 'last', 'Last Game – v2', modified='2026-10-05T00:00:00'),
                 post(201, 'evil', 'Evil', link='https://fitgirl-repacks.to/evil/')]
        fake = Fake([page1, page2], count=102)
        seen = []
        with mock.patch.object(net, 'get', fake):
            data = repackindex.sync(progress=lambda p, n: seen.append((p, n)))
        self.assertEqual(len(data['posts']), 101)
        self.assertNotIn('evil', data['posts'])
        self.assertEqual(seen, [(1, 2), (2, 2)])
        self.assertEqual(data['max_modified'], '2026-10-05T00:00:00')
        self.assertTrue(data['built_at'] and data['synced_at'])
        self.assertTrue(repackindex.STORE.exists())

    def test_incremental_asks_only_for_changes(self):
        with mock.patch.object(net, 'get', Fake([[post(1, 'a', 'A')]])):
            repackindex.sync()
        fake = Fake([[post(2, 'b', 'B', modified='2026-10-09T00:00:00')]])
        with mock.patch.object(net, 'get', fake):
            self.assertEqual(repackindex.sync(), repackindex.load())   # too soon: no request
            self.assertEqual(fake.urls, [])
            data = repackindex.sync(force=True)
        self.assertEqual(set(data['posts']), {'a', 'b'})
        q = parse_qs(urlsplit(fake.urls[0]).query)
        self.assertEqual(q['modified_after'], ['2026-10-01T10:00:00'])

    def test_failure_keeps_the_store(self):
        with mock.patch.object(net, 'get', Fake([[post(1, 'a', 'A')]])):
            repackindex.sync()
        with mock.patch.object(net, 'get', Fake([], fail_at=1)):
            with self.assertRaises(net.NetError):
                repackindex.sync(force=True)
        self.assertEqual(set(repackindex.load()['posts']), {'a'})

    def test_reject_round_trip(self):
        repackindex.reject('10', 'x')
        self.assertEqual(repackindex.load()['rejected'], {'10': 'x'})
        repackindex.reject('10', None)
        self.assertEqual(repackindex.load()['rejected'], {})


class Page(unittest.TestCase):
    CONTENT = ('<p>Repack Size: <strong>from 47.4 GB</strong><br>Original Size: '
               '<strong>66.4 GB</strong></p><a href="magnet:?xt=urn:btih:'
               '4CC8E331E699748269F6AC1B98B802684F5AC97A&#038;dn=Elden&#038;tr=udp">1337x</a>'
               '<a href="magnet:?xt=urn:btih:4cc8e331e699748269f6ac1b98b802684f5ac97a'
               '&#038;dn=rutor">RuTor</a><a href="magnet:?xt=urn:btih:nothash">bad</a>')

    def test_magnets_and_sizes(self):
        body = json.dumps([{'link': SITE + 'elden-ring/', 'title': {'rendered': 'ELDEN RING'},
                            'content': {'rendered': self.CONTENT}}]).encode()
        with mock.patch.object(net, 'get', return_value=body):
            p = repackindex.fetch_page('elden-ring')
        self.assertEqual(p['repack_size'], 'from 47.4 GB')
        self.assertEqual(p['original_size'], '66.4 GB')
        self.assertEqual(len(p['magnets']), 1)                     # same hash once
        self.assertEqual(p['magnets'][0]['hash'], '4cc8e331e699748269f6ac1b98b802684f5ac97a')
        self.assertIn('&dn=Elden&tr=udp', p['magnets'][0]['uri'])   # unescaped

    def test_only_the_real_site(self):
        body = json.dumps([{'link': 'https://fitgirl-repack.store/elden-ring/',
                            'content': {'rendered': self.CONTENT}}]).encode()
        with mock.patch.object(net, 'get', return_value=body):
            with self.assertRaises(net.NetError):
                repackindex.fetch_page('elden-ring')
        with self.assertRaises(net.NetError):
            repackindex.fetch_page('../wp-admin')


if __name__ == '__main__':
    unittest.main()
