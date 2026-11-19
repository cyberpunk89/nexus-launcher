"""The Wishlist card (w-163): Deals and Upcoming in one ranked list. The rank
(gamedb.wishlist_rows), the merge of the two saved widgets (dash_normalize),
the card's states and sizes, and the gallery's "Needs Game-db" tile."""

import itertools
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
from tests.patching import set_gui  # noqa: F401  (imported for the sandbox's order)
from nexus import gamedb

if nx is not None:
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton
    from nexus.gui import dashboard as D
    from nexus.gui.widget_spec import SPEC

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def day(n):
    return (NOW + timedelta(days=n)).strftime('%Y-%m-%dT%H:%M:%SZ')


_ids = itertools.count(1000)


def sale(title, cut, price='€10', appid=None):
    return {'appid': appid or next(_ids), 'title': title,
            'price': {'onSale': True, 'cutPct': cut, 'currentStr': price}}


def soon(title, days, text=None, price=None, appid=None, **extra):
    when = (NOW + timedelta(days=days)).strftime('%d %b %Y')
    return {'appid': appid or next(_ids), 'title': title, 'comingSoon': True,
            'releaseDate': day(days), 'releaseDateText': text or when,
            'price': price, **extra}


class Rank(unittest.TestCase):
    def titles(self, summary):
        return [r['g']['title'] for r in gamedb.wishlist_rows(summary, NOW)]

    def test_sales_and_releases_blend_by_one_score(self):
        """A release counts as a discount of 40% less half a point a day."""
        wl = [sale("A", 40), sale("B", 30), sale("C", 25), sale("D", 20),
              soon("E", 12), soon("F", 34), soon("G", 61)]
        self.assertEqual(self.titles({'wishlist': wl}), ["A", "E", "B", "C", "F", "D", "G"])

    def test_ties_put_the_sale_first_then_the_input_order(self):
        wl = [soon("Soon", 20), sale("Sale", 30), sale("Other", 30)]      # 40 - 20/2 = 30
        self.assertEqual(self.titles({'wishlist': wl}), ["Sale", "Other", "Soon"])

    def test_vague_and_tba_dates_come_last_in_order(self):
        wl = [soon("Vague", 400, text="March"), {'appid': 9, 'title': "TBA", 'comingSoon': True},
              sale("Sale", 5), soon("Far", 90)]
        self.assertEqual(self.titles({'wishlist': wl}), ["Sale", "Far", "Vague", "TBA"])
        wl.append(soon("Years", 9000))            # a real date, however far, beats a vague one
        self.assertEqual(self.titles({'wishlist': wl})[-3:], ["Years", "Vague", "TBA"])

    def test_cancelled_and_not_on_sale_are_left_out(self):
        full = {'appid': 3, 'title': "Full price", 'price': {'onSale': False, 'currentStr': '€9'}}
        wl = [soon("Dead", 5, status='cancelled'), full,
              sale("Sale", 10)]
        self.assertEqual(self.titles({'wishlist': wl}), ["Sale"])

    def test_upcoming_list_and_wishlist_merge_by_appid(self):
        summary = {'wishlist': [soon("Game", 12, appid=7, price={'currentStr': '€20'})],
                   'upcoming': [{'appid': 7, 'title': "Game", 'releaseDate': day(12),
                                 'releaseDateText': '12 Dec 2026'},
                                {'appid': 8, 'title': "Only upcoming", 'releaseDate': day(30),
                                 'releaseDateText': '30 Dec 2026'}]}
        rows = gamedb.wishlist_rows(summary, NOW)
        self.assertEqual([r['g']['title'] for r in rows], ["Game", "Only upcoming"])
        self.assertEqual(rows[0]['g']['price']['currentStr'], '€20')

    def test_a_game_on_sale_and_unreleased_is_a_release(self):
        g = soon("Both", 10, price={'onSale': True, 'cutPct': 50, 'currentStr': '€5'}, appid=4)
        rows = gamedb.wishlist_rows({'wishlist': [g]}, NOW)
        self.assertEqual([r['kind'] for r in rows], ['soon'])

    def test_nothing_is_an_empty_list(self):
        self.assertEqual(gamedb.wishlist_rows(None, NOW), [])
        self.assertEqual(gamedb.wishlist_rows({'wishlist': []}, NOW), [])

    def test_why_texts(self):
        wl = [sale("Sale", 40, '€14.99'),
              soon("Pre", 12, price={'currentStr': '€19.50', 'preorder': True,
                                     'best': {'priceStr': '€19.50', 'category': 'keyshop'}}),
              soon("EA", 34, status='early_access', statusKnown=True),
              soon("Later", 61, status='upcoming', previousDate=day(30), statusKnown=True),
              soon("Tomorrow", 1), soon("Today", 0),
              soon("Vague", 400, text="March"), {'appid': 9, 'title': "TBA", 'comingSoon': True}]
        by = {r['g']['title']: r for r in gamedb.wishlist_rows({'wishlist': wl}, NOW)}
        self.assertEqual(by["Sale"]['why'], "-40% today")
        self.assertEqual(by["Sale"]['pill'], "-40%")
        self.assertEqual(by["Sale"]['price'], "€14.99")
        self.assertEqual(by["Pre"]['why'], "Out in 12 days · pre-order")
        self.assertEqual((by["Pre"]['pill'], by["Pre"]['price'], by["Pre"]['line']),
                         ("in 12 days", "€19.50", "€19.50 · keyshop"))
        self.assertEqual(by["EA"]['why'], "Out in 34 days · early access")
        self.assertEqual(by["EA"]['price'], "No price yet")
        self.assertEqual(by["Later"]['why'], "Out in 61 days · delayed")
        self.assertEqual(by["Tomorrow"]['why'], "Out tomorrow")
        self.assertEqual(by["Today"]['why'], "Out today")
        self.assertEqual(by["Vague"]['why'], "Out March")
        self.assertEqual(by["TBA"]['why'], "Release date TBA")
        self.assertEqual(by["TBA"]['pill'], "TBA")

    def test_text_labels_are_not_dates(self):
        """Steam's own words ("Coming soon", "To be announced") never get "Out"."""
        wl = [{'appid': 1, 'title': "A", 'comingSoon': True, 'releaseDateText': "Coming soon"},
              {'appid': 2, 'title': "B", 'comingSoon': True, 'releaseDateText': "To be announced"},
              {'appid': 3, 'title': "C", 'comingSoon': True, 'releaseDateText': "Q2 2027"},
              {'appid': 4, 'title': "D", 'comingSoon': True, 'releaseDateText': "2027"}]
        by = {r['g']['title']: r for r in gamedb.wishlist_rows({'wishlist': wl}, NOW)}
        self.assertEqual([by[k]['why'] for k in "ABCD"],
                         ["Coming soon", "Release date TBA", "Out Q2 2027", "Out 2027"])
        self.assertEqual([by[k]['pill'] for k in "ABCD"], ["Coming soon", "TBA", "Q2 2027", "2027"])

    def test_released_games(self):
        """A released game on sale is a sale; one not on sale reads Out now for
        14 days after its day, then leaves the list."""
        on = {'currentStr': '€5', 'onSale': True, 'cutPct': 50}
        wl = [soon("Sale", -3, price=on, appid=1), soon("Fresh", -3, appid=2),
              soon("Edge", -14, appid=3), soon("Old", -15, appid=4),
              sale("Big", 39), soon("Unreleased", 4)]
        rows = gamedb.wishlist_rows({'wishlist': wl}, NOW)
        by = {r['g']['title']: r for r in rows}
        self.assertEqual((by["Sale"]['kind'], by["Sale"]['why'], by["Sale"]['score']),
                         ('sale', "-50% today", 50.0))
        self.assertEqual((by["Fresh"]['why'], by["Fresh"]['pill']), ("Out now", "Out now"))
        self.assertIn("Edge", by)
        self.assertNotIn("Old", by)
        self.assertLess(by["Big"]['score'], by["Fresh"]['score'])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Merge(unittest.TestCase):
    def items(self, *spec):
        return [{'id': i, 'size': s, 'hidden': h} for i, s, *r in spec for h in [bool(r and r[0])]]

    def norm(self, *spec):
        out = nx.dash_normalize(self.items(*spec))
        return out, [i['id'] for i in out]

    def test_the_kind_stays_deals_and_upcoming_is_gone(self):
        self.assertNotIn('upcoming', nx.DASH_SIZES)
        self.assertNotIn('upcoming', SPEC)
        self.assertEqual(SPEC['deals'].title, "Wishlist")
        self.assertEqual(SPEC['deals'].note, "Deals and Upcoming, now one card")
        self.assertEqual(nx.DASH_PLACE_AFTER['week'], 'deals')

    def test_deals_only_is_unchanged(self):
        out, ids = self.norm(('stage', 'cinematic'), ('deals', 'tall'), ('news', 'small'))
        self.assertEqual(out[ids.index('deals')], {'id': 'deals', 'size': 'tall', 'hidden': False})

    def test_upcoming_only_becomes_deals_in_place_with_its_size(self):
        out, ids = self.norm(('stage', 'cinematic'), ('upcoming', 'full'), ('news', 'small'))
        self.assertNotIn('upcoming', ids)
        rest = [i for i in ids if i not in ('save', 'foryou', 'friends', 'pick', 'week')]
        self.assertEqual(rest[:3], ['stage', 'deals', 'news'])
        self.assertEqual(out[ids.index('deals')], {'id': 'deals', 'size': 'full', 'hidden': False})

    def test_both_the_first_ones_place_and_size_win(self):
        out, ids = self.norm(('stage', 'cinematic'), ('deals', 'wide'), ('news', 'small'),
                             ('upcoming', 'small'))
        self.assertEqual(ids.count('deals'), 1)
        self.assertEqual(out[ids.index('deals')]['size'], 'wide')
        self.assertLess(ids.index('deals'), ids.index('news'))
        out, ids = self.norm(('stage', 'cinematic'), ('upcoming', 'tall'), ('news', 'small'),
                             ('deals', 'wide'))
        self.assertEqual(ids.count('deals'), 1)
        self.assertEqual(out[ids.index('deals')]['size'], 'tall')
        self.assertLess(ids.index('deals'), ids.index('news'))

    def test_hidden_only_when_both_were_hidden(self):
        hid = lambda *s: nx.dash_normalize(self.items(('stage', 'cinematic'), *s))   # noqa: E731
        got = lambda out: next(i for i in out if i['id'] == 'deals')['hidden']       # noqa: E731
        self.assertTrue(got(hid(('deals', 'wide', True), ('upcoming', 'small', True))))
        self.assertFalse(got(hid(('deals', 'wide', True), ('upcoming', 'small'))))
        self.assertFalse(got(hid(('deals', 'wide'), ('upcoming', 'small', True))))
        self.assertFalse(got(hid(('upcoming', 'small', True), ('deals', 'wide'))))
        self.assertTrue(got(hid(('upcoming', 'small', True))))
        self.assertFalse(got(hid(('upcoming', 'small'))))

    def test_a_bad_size_falls_back_and_it_is_idempotent(self):
        out, _ids = self.norm(('stage', 'cinematic'), ('upcoming', 'cinematic'),
                              ('deals', 'wide'))
        self.assertEqual(next(i for i in out if i['id'] == 'deals')['size'], 'wide')
        self.assertEqual(nx.dash_normalize(out), out)

    def test_it_writes_nothing_on_load_and_saved_layouts_fold_too(self):
        from nexus.gui.layouts import clean_layouts
        raw = [{'name': 'Evening', 'items': self.items(('stage', 'cinematic'),
                                                       ('upcoming', 'wide'), ('deals', 'small'))}]
        got = clean_layouts(raw)
        ids = [i['id'] for i in got[0]['items']]
        self.assertNotIn('upcoming', ids)
        self.assertEqual(next(i for i in got[0]['items'] if i['id'] == 'deals')['size'], 'wide')
        self.assertEqual(clean_layouts(got), got)
        self.assertEqual(raw[0]['items'][1]['id'], 'upcoming')     # the input is not touched


def hub_for(summary, *, enabled=True, loaded=True, error='', friends=False):
    page = mock.Mock(_loaded=loaded, error=error)
    page._data.return_value = summary
    page._base.return_value = None
    return mock.Mock(config={'gamedb_enabled': enabled, 'friends_enabled': friends},
                     dealspage=page)


def texts(w):
    return [lab.text() for lab in w.findChildren(QLabel)]


@unittest.skipIf(nx is None, "PyQt6 not available")
class Card(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if nx is None:
            raise unittest.SkipTest("PyQt6 not available")

    def card(self, hub, size='small'):
        w = D.DealsWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        w.fill(hub)
        return w

    def summary(self):
        return {'currency': 'EUR', 'radar': {'onSaleCount': 2, 'totalSavings': 9},
                'wishlist': [sale("Blue Prince", 40, '€14.99'), sale("Hades II", 30, '€17.49'),
                             soon("Slay the Spire 2", 12, price={'currentStr': '€19.50',
                                                                 'preorder': True})]}

    def test_title_and_kind(self):
        w = D.DealsWidget()
        self.addCleanup(w.deleteLater)
        self.assertEqual((w.KIND, w.title_label.text()), ('deals', "Wishlist"))
        self.assertFalse(hasattr(D, 'UpcomingWidget'))

    def test_small_is_one_ranked_row_list_that_says_why(self):
        w = self.card(hub_for(self.summary()))
        t = texts(w)
        self.assertEqual([x for x in t if 'today' in x or x.startswith('Out')],
                         ["-40% today", "Out in 12 days · pre-order", "-30% today"])
        self.assertEqual([x for x in t if x in ("Blue Prince", "Slay the Spire 2", "Hades II")],
                         ["Blue Prince", "Slay the Spire 2", "Hades II"])
        self.assertIn("2 on sale · 1 coming", t)
        self.assertNotIn("On sale", t)               # no section headings

    def test_rows_open_the_deals_page_or_the_game(self):
        s = self.summary()
        s['wishlist'][2]['steamUrl'] = 'https://s/1'
        hub = hub_for(s)
        w = self.card(hub)
        rows = w.findChildren(nx.ClickRow)
        rows[0].clicked.emit()
        hub.go.assert_called_with(hub.DEALS)
        rows[1].clicked.emit()
        hub.dealspage._open.assert_called_with('https://s/1')

    def test_wide_and_full_are_store_art_tiles_with_a_pill(self):
        for size in ('wide', 'full'):
            w = self.card(hub_for(self.summary()), size)
            self.assertIsNotNone(w.tiles, size)
            tiles = w.findChildren(D.WishTile)
            self.assertEqual([t.row['g']['title'] for t in tiles],
                             ["Blue Prince", "Slay the Spire 2", "Hades II"], size)
            self.assertEqual([t.art.badges() for t in tiles],
                             [["-40%"], ["in 12 days"], ["-30%"]], size)

    def test_tiles_per_row_follow_the_size_and_the_width(self):
        wl = [sale(f"G{i}", 50 - i) for i in range(14)]
        for size, across in (('wide', 4), ('full', 6)):
            w = self.card(hub_for({'wishlist': wl}), size)
            self.assertEqual(w.tiles.max_cols, across)
            self.assertEqual(w.tiles.columns(900), across)
            self.assertEqual(w.tiles.columns(260), 2)
            self.assertEqual(len(w.findChildren(D.WishTile)), {'wide': 8, 'full': 12}[size])

    def test_the_friend_line_only_when_friends_is_on(self):
        s = {'wishlist': [sale("Hades II", 30, '€17.49', appid=11), soon("Soon", 12, appid=12)]}
        fr = SimpleNamespace(usable=lambda: True,
                             owners=lambda a: [{'name': "Cara"}] if a == 11 else
                             [{'name': "Ana"}, {'name': "Ben"}] if a == 12 else [])
        with mock.patch.object(D, 'FRIENDS', fr):
            on = texts(self.card(hub_for(s, friends=True)))
            off = texts(self.card(hub_for(s, friends=False)))
        self.assertIn("-30% today · Cara owns it", on)
        self.assertIn("Out in 12 days · Ana and Ben own it", on)
        self.assertFalse([x for x in off if 'own' in x])
        with mock.patch.object(D, 'FRIENDS', fr):
            wide = self.card(hub_for(s, friends=True), 'wide')
            tile = next(t for t in wide.findChildren(D.WishTile)
                        if t.row['g']['title'] == "Hades II")
        self.assertIn("€17.49 · Cara owns it", texts(tile))

    def test_loading_and_error_and_retry(self):
        w = self.card(hub_for({}, loaded=False))
        self.assertIn("Loading your wishlist…", texts(w))
        self.assertTrue(w.has_content(hub_for({}, loaded=False)))
        hub = hub_for({}, loaded=False, error="Game-db is not answering")
        w = self.card(hub)
        self.assertIn("Couldn't load — Game-db is not answering", texts(w))
        retry = [b for b in w.findChildren(QPushButton) if b.text() == "Retry"]
        self.assertEqual(len(retry), 1)
        retry[0].click()
        hub.dealspage.retry.assert_called()

    def test_not_connected_or_nothing_at_all_leaves_home_without_a_hint(self):
        w = D.DealsWidget()
        self.addCleanup(w.deleteLater)
        self.assertFalse(w.has_content(hub_for(self.summary(), enabled=False)))
        self.assertFalse(w.has_content(hub_for({'wishlist': [], 'upcoming': []})))
        dead = soon("Dead", 3, status='cancelled')
        self.assertFalse(w.has_content(hub_for({'wishlist': [dead]})))
        self.assertTrue(w.has_content(hub_for(self.summary())))
        w.fill(hub_for(self.summary(), enabled=False))
        self.assertEqual([x for x in texts(w) if 'Game-db' in x or 'Settings' in x],
                         ["Needs Game-db · Open Settings"])    # Customise shows it in the body
        self.assertFalse(w.findChildren(QPushButton, 'Primary'))

    def test_needs_note_for_the_gallery(self):
        w = D.DealsWidget()
        self.addCleanup(w.deleteLater)
        self.assertEqual(w.needs_note(hub_for({}, enabled=False)), "Needs Game-db · Open Settings")
        self.assertEqual(w.needs_note(hub_for(self.summary())), "")
        self.assertEqual(D.DashWidget.needs_note(w, hub_for({})), "")

    def test_tall_is_small_with_more_rows_and_the_rest_is_counted(self):
        wl = [sale(f"G{i}", 50 - i) for i in range(14)]
        small = self.card(hub_for({'wishlist': wl}), 'small')
        tall = self.card(hub_for({'wishlist': wl}), 'tall')
        n = lambda w: len(w.findChildren(nx.ClickRow))          # noqa: E731
        self.assertEqual((n(small), n(tall)), (5, 9))
        self.assertIn("+9 more on the Deals page", texts(small))

    def test_the_spec_and_the_widget_agree_on_growing(self):
        self.assertEqual(SPEC['deals'].grow_sizes, ('small', 'tall'))
        self.assertIn('deals', D.FED_BY['friends'])

    def test_only_the_row_sizes_grow(self):
        for size, grows in (('small', True), ('tall', True), ('wide', False), ('full', False)):
            self.assertEqual(self.card(hub_for(self.summary()), size).grows, grows, size)

    def test_savings_said_once_at_full(self):
        w = self.card(hub_for(self.summary()), 'full')
        self.assertIn("2 on sale · 1 coming · save ~9 EUR", texts(w))
        self.assertEqual(sum('save' in x for x in texts(w)), 1)


if __name__ == '__main__':
    unittest.main()
