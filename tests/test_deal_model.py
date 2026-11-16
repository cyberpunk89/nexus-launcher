"""The Deals home model (w-146): shelves, For you picks and catalogue search.
Invented titles only; no Qt, no network."""

import unittest

try:
    from nexus.gui import deal_model as dm
except ImportError:         # pragma: no cover
    from gui import deal_model as dm


def game(n, current=20.0, cut=0, low=False, sale=True, **kw):
    g = {'appid': n, 'title': f"Game {n}",
         'price': {'current': current, 'onSale': sale, 'cutPct': cut, 'atHistoricalLow': low}}
    g.update(kw)
    return g


def keys_of(shelf):
    return [g['appid'] for g in shelf['items']]


class Taste(unittest.TestCase):
    def test_play_time_outweighs_game_count(self):
        games = [{'slug': 'a', 'display_name': 'Moss Lantern', 'playtime_minutes': 3000},
                 {'slug': 'b', 'display_name': 'Tin Harbour', 'playtime_minutes': 10},
                 {'slug': 'c', 'display_name': 'Paper Comet', 'playtime_minutes': 20}]
        facts = {'a': {'genres': ['RPG']}, 'b': {'genres': ['Puzzle']},
                 'c': {'genres': ['Puzzle']}}
        got = dm.taste(games, facts)
        self.assertEqual([(g, t) for g, _w, t in got], [('RPG', 'Moss Lantern'),
                                                        ('Puzzle', 'Paper Comet')])
        self.assertEqual(dm.taste(games, {}), [])

    def test_in_shelf_matches_the_shelves(self):
        g = game(1, 5, 80, low=True, hltb={'main': 4})
        self.assertTrue(all(dm.in_shelf(g, k) for k in ('atl', 'under', 'big', 'short')))
        self.assertFalse(dm.in_shelf(game(2, 5, 80, sale=False), 'under'))


class Shelves(unittest.TestCase):
    def by(self, items, **kw):
        return {s['key']: s for s in dm.shelves(items, **kw)}

    def test_four_shelves_in_order(self):
        self.assertEqual([s['key'] for s in dm.shelves([])], ['atl', 'under', 'big', 'short'])

    def test_each_shelf_picks_its_games(self):
        items = [game(1, 30, 50, low=True), game(2, 5, 20), game(3, 40, 80),
                 game(4, 15, 10, hltb={'main': 6}), game(5, 15, 0, sale=False, hltb={'main': 3})]
        s = self.by(items)
        self.assertEqual(keys_of(s['atl']), [1])
        self.assertEqual(keys_of(s['under']), [2])
        self.assertEqual(keys_of(s['big']), [3])
        self.assertEqual(keys_of(s['short']), [4])
        self.assertEqual(s['under']['count'], 1)

    def test_no_decimal_currency_scales_the_line(self):
        items = [game(1, 900, 30), game(2, 1200, 30)]
        self.assertEqual(keys_of(self.by(items, currency='JPY')['under']), [1])
        self.assertEqual(dm.under_threshold('jpy'), 1000)
        self.assertEqual(dm.under_threshold('EUR'), 10)
        self.assertEqual(keys_of(self.by(items, currency='EUR')['under']), [])

    def test_hide_owned(self):
        items = [game(1, 5, 80, owned=True), game(2, 5, 80)]
        self.assertEqual(keys_of(self.by(items)['big']), [1, 2])
        self.assertEqual(keys_of(self.by(items, hide_owned=True)['big']), [2])

    def test_cap_keeps_the_full_count_best_first(self):
        items = [game(i, 5, 10 + i) for i in range(30)]
        s = self.by(items)['under']
        self.assertEqual(s['count'], 30)
        self.assertEqual(len(s['items']), dm.SHELF_CAP)
        self.assertEqual(s['items'][0]['appid'], 29)

    def test_unpriced_games_are_ignored(self):
        items = [{'appid': 1, 'price': None}, game(2, None, 90)]
        self.assertTrue(all(s['count'] == 0 for s in dm.shelves(items)))


class ForYou(unittest.TestCase):
    TASTE = [('RPG', 9, "Quest Saga"), ('Action', 4, "Blade Run")]

    def test_reason_names_most_played_shared_genre(self):
        items = [game(1, 10, 50, genres=['Action', 'RPG'])]
        picks = dm.for_you(items, self.TASTE)
        self.assertEqual(picks[0]['reason'], "Because you play a lot of RPG (Quest Saga)")

    def test_three_picks_heaviest_genre_first(self):
        items = [game(1, 10, 50, genres=['Action']), game(2, 10, 50, genres=['RPG']),
                 game(3, 10, 60, genres=['RPG']), game(4, 10, 20, genres=['RPG'])]
        picks = dm.for_you(items, self.TASTE)
        self.assertEqual([p['item']['appid'] for p in picks], [3, 2, 4])

    def test_fallback_without_genres(self):
        items = [game(1, 10, 50, low=True, reviews={'steamPercent': 70}),
                 game(2, 10, 50, low=True, reviews={'steamPercent': 95}),
                 game(3, 10, 50, reviews={'steamPercent': 99})]
        picks = dm.for_you(items, [])
        self.assertEqual([p['item']['appid'] for p in picks], [3, 2, 1])
        self.assertEqual(picks[1]['reason'], dm.FALLBACK_REASON)
        self.assertEqual(picks[0]['reason'], dm.FALLBACK_REASON_PLAIN)

    def test_owned_and_unsold_are_skipped(self):
        items = [game(1, owned=True, cut=50), game(2, cut=50, sale=False), game(3, cut=50)]
        self.assertEqual([p['item']['appid'] for p in dm.for_you(items, [])], [3])


class Catalogue(unittest.TestCase):
    ROWS = [
        {'title': "Alpha Quest", 'date': '2026-05-01', 'keys': ['alpha quest'], 'marks': []},
        {'title': "Brave Orbit: Deluxe", 'date': '2026-06-01', 'keys': ['brave orbit'],
         'marks': ['steam'], 'hv': True},
        {'title': "Cinder", 'date': '2026-04-01', 'keys': ['cinder'], 'marks': ['wishlist']},
    ]

    def test_query_uses_keys(self):
        r = dm.catalogue_match(self.ROWS, "ORBIT brave")
        self.assertEqual([x['title'] for x in r['rows']], ["Brave Orbit: Deluxe"])
        self.assertEqual(dm.catalogue_match(self.ROWS, "zzz")['total'], 0)

    def test_newest_first_and_name_sort(self):
        self.assertEqual([x['title'] for x in dm.catalogue_match(self.ROWS)['rows']][0],
                         "Brave Orbit: Deluxe")
        names = [x['title'] for x in dm.catalogue_match(self.ROWS, sort='name')['rows']]
        self.assertEqual(names, ["Alpha Quest", "Brave Orbit: Deluxe", "Cinder"])

    def test_hide_have_and_hv(self):
        self.assertEqual(dm.catalogue_match(self.ROWS, hide_have=True)['total'], 2)
        self.assertEqual(dm.catalogue_match(self.ROWS, hide_hv=True)['total'], 2)
        self.assertEqual(dm.catalogue_match(self.ROWS, hide_have=True, hide_hv=True)['total'], 2)

    def test_cap_100_with_total(self):
        rows = [{'title': f"T{i}", 'date': f"2026-01-{i % 28 + 1:02d}", 'keys': [f"t{i}"]}
                for i in range(250)]
        r = dm.catalogue_match(rows)
        self.assertEqual((len(r['rows']), r['total']), (100, 250))


if __name__ == '__main__':
    unittest.main()
