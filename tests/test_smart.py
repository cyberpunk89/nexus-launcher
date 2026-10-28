"""Smart shelves (nexus/smart.py): every rule with a fake play history and
facts index, the 3-game minimum, the 6-shelf cap, hidden shelves, the top
genres, Linux-only rules and the filter keys."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import smart  # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0)


def game(slug, minutes=0, days_ago=None, **kw):
    g = {'slug': slug, 'display_name': slug.title(), 'playtime_minutes': minutes,
         'last_played': (NOW - timedelta(days=days_ago)).isoformat(timespec='seconds')
         if days_ago is not None else None, 'installed': True}
    g.update(kw)
    return g


def fact(main=None, genres=(), modes=('single',), status='released'):
    return {'length': {'main': main} if main else None, 'genres': list(genres),
            'modes': list(modes), 'status': status}


def keys(shelves):
    return [s['key'] for s in shelves]


def slugs(games):
    return [g['slug'] for g in games]


class Rules(unittest.TestCase):
    def match(self, key, games, facts=None, linux=True):
        return slugs(smart.games_for(key, games, facts or {}, NOW, linux))

    def test_continue_playing_needs_no_facts(self):
        games = [game('a', 120, 2), game('b', 60, 20), game('c', 30, 13)]
        self.assertEqual(self.match('smart:continue', games), ['a', 'c'])

    def test_continue_playing_skips_a_game_near_its_end(self):
        games = [game('a', 9 * 60, 1), game('b', 60, 1)]
        facts = {'a': fact(main=10), 'b': fact(main=10)}
        self.assertEqual(self.match('smart:continue', games, facts), ['b'])

    def test_unfinished(self):
        games = [game('a', 120, 40), game('b', 30, 40), game('c', 120, 5),
                 game('d', 9 * 60, 40), game('e', 120, 40)]
        facts = {s: fact(main=10) for s in 'abcd'}           # e: no length known
        self.assertEqual(self.match('smart:unfinished', games, facts), ['a'])

    def test_length_rules_skip_online_only_games(self):
        games = [game('mp', 120, 40)]
        facts = {'mp': fact(main=10, modes=('online',))}
        self.assertEqual(self.match('smart:unfinished', games, facts), [])
        self.assertEqual(self.match('smart:short', games, facts), [])
        self.assertIsNone(smart.main_hours(facts['mp']))

    def test_almost_done_is_ordered_by_progress(self):
        games = [game('a', 8 * 60, 3), game('b', int(9.5 * 60), 50), game('c', 11 * 60, 1)]
        facts = {s: fact(main=10) for s in 'abc'}
        self.assertEqual(self.match('smart:almost', games, facts), ['b', 'a'])

    def test_never_played(self):
        games = [game('a', added_at='2026-10-01'), game('b', 10, 3),
                 game('c', added_at='2026-10-20')]
        self.assertEqual(self.match('smart:never', games), ['c', 'a'])

    def test_short_games_shortest_first(self):
        games = [game('a'), game('b'), game('c')]
        facts = {'a': fact(main=4), 'b': fact(main=2.5), 'c': fact(main=6)}
        self.assertEqual(self.match('smart:short', games, facts), ['b', 'a'])

    def test_coop_and_early_access(self):
        games = [game('a'), game('b'), game('c')]
        facts = {'a': fact(modes=('single', 'coop')), 'b': fact(status='early_access'),
                 'c': fact()}
        self.assertEqual(self.match('smart:coop', games, facts), ['a'])
        self.assertEqual(self.match('smart:early', games, facts), ['b'])

    def test_runs_great_on_linux_only_on_linux(self):
        games = [game('a', protondb='platinum'), game('b', protondb='gold')]
        self.assertEqual(self.match('smart:linux', games), ['a'])
        self.assertEqual(self.match('smart:linux', games, linux=False), [])
        self.assertNotIn('smart:linux', smart.rule_keys(linux=False))

    def test_genre(self):
        games = [game('a'), game('b')]
        facts = {'a': fact(genres=('RPG', 'Action')), 'b': fact(genres=('Action',))}
        self.assertEqual(self.match('genre:RPG', games, facts), ['a'])

    def test_bad_values_dont_raise(self):
        games = [{'slug': 'x', 'last_played': 'garbage', 'playtime_minutes': None}]
        facts = {'x': {'length': {'main': 'n/a'}, 'modes': None}}
        for key in smart.rule_keys():
            smart.games_for(key, games, facts, NOW)

    def test_timezone_aware_last_played(self):
        g = game('a', 60)
        g['last_played'] = (NOW - timedelta(days=1)).astimezone().isoformat()
        self.assertEqual(self.match('smart:continue', [g]), ['a'])


class Shelves(unittest.TestCase):
    def test_three_games_minimum(self):
        two = [game('a', 60, 1), game('b', 60, 2)]
        self.assertEqual(smart.shelves(two, {}, now=NOW), [])
        three = two + [game('c', 60, 3)]
        self.assertEqual(keys(smart.shelves(three, {}, now=NOW)), ['smart:continue'])

    def test_first_run_without_facts_shows_play_history_shelves(self):
        games = [game(f'p{i}', 60, i) for i in range(3)] + [game(f'n{i}') for i in range(3)]
        got = smart.shelves(games, {}, now=NOW)
        self.assertEqual(keys(got), ['smart:continue', 'smart:never'])
        self.assertEqual(got[0]['title'], "Continue playing")
        self.assertTrue(got[0]['tip'])

    def test_at_most_six_in_priority_order(self):
        games, facts = [], {}
        for i in range(3):
            for slug, g, f in (
                    (f'c{i}', game(f'c{i}', 60, 1), fact(main=50, genres=('RPG',))),
                    (f'u{i}', game(f'u{i}', 120, 60), fact(main=50, genres=('RPG',))),
                    (f'a{i}', game(f'a{i}', 9 * 60, 60), fact(main=10, genres=('Action',))),
                    (f'n{i}', game(f'n{i}'), fact(main=3, genres=('Indie',),
                                                  modes=('single', 'coop'),
                                                  status='early_access'))):
                games.append(g)
                facts[slug] = f
        got = keys(smart.shelves(games, facts, now=NOW))
        self.assertEqual(len(got), smart.MAX_SHELVES)
        self.assertEqual(got, ['smart:continue', 'smart:unfinished', 'smart:almost',
                               'smart:never', 'genre:RPG', 'genre:Action'])

    def test_hidden_shelf_makes_room(self):
        games = [game(f'p{i}', 60, i) for i in range(3)] + [game(f'n{i}') for i in range(3)]
        got = smart.shelves(games, {}, hidden={'smart:continue'}, now=NOW)
        self.assertEqual(keys(got), ['smart:never'])

    def test_top_genres(self):
        games = [game(s) for s in 'abcdef']
        facts = {'a': fact(genres=('RPG',)), 'b': fact(genres=('RPG', 'Horror')),
                 'c': fact(genres=('Horror',)), 'd': fact(genres=('Indie',)),
                 'e': fact(genres=('Action',)), 'f': fact(genres=('RPG',))}
        self.assertEqual(smart.top_genres(games, facts),
                         [('RPG', 3), ('Horror', 2), ('Action', 1)])

    def test_no_repack_wording(self):
        for key, title, tip, _f in smart.RULES:
            for text in (title, tip):
                self.assertNotIn('repack', text.lower(), key)

    def test_filter_keys_list_hidden_and_small_matches(self):
        games = [game('a', 60, 1), game('b')]
        facts = {'a': fact(genres=('RPG',))}
        self.assertEqual(smart.filter_keys(games, facts, now=NOW),
                         ['smart:continue', 'smart:never', 'genre:RPG'])

    def test_labels(self):
        self.assertEqual(smart.label('smart:coop'), "Co-op")
        self.assertEqual(smart.label('genre:Racing'), "Racing")
        self.assertTrue(smart.needs_facts('genre:Racing'))
        self.assertFalse(smart.needs_facts('smart:never'))
        self.assertIn('unfinished', smart.WIDGET_RULES)


if __name__ == '__main__':
    unittest.main()
