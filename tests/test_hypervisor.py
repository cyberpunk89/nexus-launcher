"""Hypervisor-bypass repacks: marked in the catalogue, on the repack page
and in the feed, found in a downloaded folder, and held back at install on
Linux (where they can't run). Every request is mocked."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.test_repack_extras import make_repack  # noqa: E402
from tests.test_repackindex import post, SITE  # noqa: E402
from nexus import features, hypervisor, net, repackfeed, repackindex, safety  # noqa: E402
from nexus.cli import install  # noqa: E402

LINUX = mock.patch.object(features, 'FEATURES', features.ALL)
WINDOWS = mock.patch.object(features, 'FEATURES', features.WINDOWS)


class Site:
    """net.get for the posts API: `posts` for the catalogue, `hv` for the
    Hypervisor Bypass category (None: that request fails)."""

    def __init__(self, posts, hv=()):
        self.posts, self.hv = posts, hv

    def __call__(self, url, timeout=20, headers=None, limit=0):
        q = parse_qs(urlsplit(url).query)
        page = int(q.get('page', ['1'])[0])
        if q.get('categories') == [str(hypervisor.CATEGORY)]:
            if self.hv is None:
                raise net.NetError("down")
            rows = list(self.hv)
        else:
            rows = self.posts
        if page > 1:
            raise net.NetError("past the end", 400)
        return json.dumps(rows).encode()


class Kind(unittest.TestCase):
    def test_title_says_whether_a_non_hv_build_ships_too(self):
        self.assertEqual(hypervisor.kind("UNO – v48977 HV / Build 10181864 Non-HV + 15/8 DLCs"),
                         'both')
        self.assertEqual(hypervisor.kind("NfS Payback – v1.0.51 HV / v1.0.51 Non_HV"), 'both')
        self.assertEqual(hypervisor.kind("WILD HEARTS – v1.3.3 HV / v1.0.1.1 (Denuvoless)"),
                         'both')
        self.assertEqual(hypervisor.kind("Madden NFL 21 – v1.0.59.48236"), 'hv')
        self.assertEqual(hypervisor.badge('both'), "HV + non-HV")
        self.assertIsNone(hypervisor.badge(None))

    def test_note_per_edition(self):
        with LINUX:
            self.assertIn("won't run under Proton", hypervisor.note())
            self.assertIn("non-HV build", hypervisor.note('both'))
        with WINDOWS:
            self.assertIn("VBS.cmd", hypervisor.note())
            self.assertNotIn("Proton", hypervisor.note())

    def test_folder(self):
        with tempfile.TemporaryDirectory() as d:
            hv = Path(d, "Madden NFL 21 [FitGirl HV Repack]")
            hv.mkdir()
            self.assertTrue(hypervisor.in_folder(hv))
            plain = Path(d, "Persona 3 Reload [FitGirl Repack]")
            plain.mkdir()
            self.assertFalse(hypervisor.in_folder(plain))
            (plain / 'VBS.cmd').write_text('@echo off', encoding='utf-8')
            self.assertTrue(hypervisor.in_folder(plain))
            self.assertFalse(hypervisor.in_folder(Path(d, 'missing')))


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, attr, new in ((repackindex, 'STORE', Path(self.tmp.name, 'index.json')),
                                  (repackindex.time, 'sleep', lambda _s: None)):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    def test_sync_marks_and_clears(self):
        posts = [post(1, 'uno', 'UNO – v48977 HV / Build 10181864 Non-HV'),
                 post(2, 'madden-nfl-21', 'Madden NFL 21 – v1.0.59.48236'),
                 post(3, 'persona-3-reload', 'Persona 3 Reload – v1.11')]
        with mock.patch.object(net, 'get', Site(posts, hv=posts[:2])):
            data = repackindex.sync()
        self.assertEqual({s: p.get('hv') for s, p in data['posts'].items()},
                         {'uno': 'both', 'madden-nfl-21': 'hv', 'persona-3-reload': None})
        with mock.patch.object(net, 'get', Site([], hv=None)):        # offline: marks stay
            data = repackindex.sync(force=True)
        self.assertEqual(data['posts']['madden-nfl-21'].get('hv'), 'hv')
        with mock.patch.object(net, 'get', Site([], hv=posts[:1])):    # left the category
            data = repackindex.sync(force=True)
        self.assertNotIn('hv', data['posts']['madden-nfl-21'])
        self.assertEqual(data['posts']['uno']['hv'], 'both')

    def test_wishlist_match_carries_it(self):
        posts = [post(2, 'madden-nfl-21', 'Madden NFL 21 – v1.0.59.48236')]
        with mock.patch.object(net, 'get', Site(posts, hv=posts)):
            data = repackindex.sync()
        m = repackindex.wishlist_matches([{'appid': 1, 'title': 'Madden NFL 21'}], data)
        self.assertEqual(m['1']['hv'], 'hv')

    def test_repack_page(self):
        def page(cats):
            return json.dumps([{'link': SITE + 'uno/', 'categories': cats,
                                'title': {'rendered': 'UNO – v48977 HV / Build 1 Non-HV'},
                                'content': {'rendered': ''}}]).encode()
        with mock.patch.object(net, 'get', return_value=page([319, 5])):
            self.assertEqual(repackindex.fetch_page('uno')['hv'], 'both')
        with mock.patch.object(net, 'get', return_value=page([5])):
            self.assertIsNone(repackindex.fetch_page('uno')['hv'])


class Feed(unittest.TestCase):
    RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Madden NFL 21 &#8211; v1.0.59.48236</title>
<link>https://fitgirl-repacks.site/madden-nfl-21/</link>
<pubDate>Wed, 11 Jun 2026 10:00:00 +0000</pubDate>
<category><![CDATA[Hypervisor Bypass]]></category><category><![CDATA[Lossless Repack]]></category>
</item>
<item><title>Atomfall</title><link>https://fitgirl-repacks.site/atomfall/</link>
<pubDate>Wed, 08 Oct 2026 10:00:00 +0000</pubDate>
<category><![CDATA[Lossless Repack]]></category></item>
</channel></rss>"""

    def test_feed_marks_hv_repacks(self):
        got = {p['slug']: p.get('hv') for p in repackfeed.parse_feed(self.RSS)}
        self.assertEqual(got, {'madden-nfl-21': 'hv', 'atomfall': None})


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repack = make_repack(Path(self.tmp.name), {'VBS.cmd': '@echo off'})

    def check(self):
        return safety.check(self.repack, header_reader=lambda _p: b'Inno')

    def gate(self, **kw):
        args = SimpleNamespace(**{'ignore_safety': False, 'yes': True, **kw})
        with mock.patch.object(safety, 'check', return_value={'verdict': 'ok', 'findings': []}):
            install.safety_gate(args, self.repack)

    def test_safety_finding_per_edition(self):
        with LINUX:
            r = self.check()
            self.assertEqual(r['verdict'], 'warn')
        with WINDOWS:
            r2 = self.check()
            self.assertEqual(r2['verdict'], 'ok')
        hv = [f for f in r['findings'] if f.get('hv')]
        self.assertEqual(len(hv), 1)
        self.assertTrue(r['hypervisor'] and r2['hypervisor'])
        self.assertEqual([f['level'] for f in r2['findings'] if f.get('hv')], ['info'])

    def test_linux_stops_unless_told(self):
        with LINUX, mock.patch('sys.stderr'), self.assertRaises(SystemExit) as ex:
            self.gate()
        self.assertEqual(ex.exception.code, 1)
        with LINUX, mock.patch('sys.stderr'), mock.patch('builtins.input', return_value='y'):
            self.gate(yes=False)                                 # asked, and said yes
        with LINUX, mock.patch('sys.stderr'), mock.patch('builtins.input', return_value=''), \
                self.assertRaises(SystemExit):
            self.gate(yes=False)
        with LINUX:
            self.gate(ignore_safety=True)

    def test_windows_only_says_what_it_needs(self):
        with WINDOWS, mock.patch('builtins.print') as out:
            self.gate()
        self.assertIn(hypervisor.GUIDE, ' '.join(str(a) for c in out.call_args_list
                                                   for a in c.args))


if __name__ == '__main__':
    unittest.main()
