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


def shelf(*rules, match='all', **kw):
    return {'id': 'abc123', 'name': 'Mine', 'match': match,
            'rules': [{'field': f, 'op': o, 'value': v} for f, o, v in rules], **kw}


class UserShelves(unittest.TestCase):
    """Shelves you build from rules (w-51)."""

    def on(self, s, games, facts=None):
        return slugs(smart.user_games(s, games, facts or {}, NOW))

    def test_play_time(self):
        games = [game('none'), game('short', 30, 2), game('long', 600, 2)]
        self.assertEqual(self.on(shelf(('playtime', 'none', None)), games), ['none'])
        self.assertEqual(sorted(self.on(shelf(('playtime', 'some', None)), games)),
                         ['long', 'short'])
        self.assertEqual(self.on(shelf(('playtime', 'atleast', 5)), games), ['long'])
        self.assertEqual(sorted(self.on(shelf(('playtime', 'under', 5)), games)),
                         ['none', 'short'])

    def test_last_played_and_added(self):
        games = [game('a', 60, 3, added_at=(NOW - timedelta(days=100)).isoformat()),
                 game('b', 60, 40, added_at=(NOW - timedelta(days=5)).isoformat()),
                 game('c')]
        self.assertEqual(self.on(shelf(('last', 'within', 7)), games), ['a'])
        self.assertEqual(self.on(shelf(('last', 'over', 30)), games), ['b'])   # never: no
        self.assertEqual(self.on(shelf(('added', 'within', 30)), games), ['b'])
        self.assertEqual(self.on(shelf(('added', 'over', 90)), games), ['a'])

    def test_store_size_favourite(self):
        games = [game('a', source='steam', disk_bytes=60e9, favorite=True),
                 game('b', source='nexus', disk_bytes=2e9), game('c', source='heroic')]
        self.assertEqual(self.on(shelf(('store', 'is', 'steam')), games), ['a'])
        self.assertEqual(sorted(self.on(shelf(('store', 'not', 'steam')), games)), ['b', 'c'])
        self.assertEqual(self.on(shelf(('size', 'over', 50)), games), ['a'])
        self.assertEqual(self.on(shelf(('size', 'under', 5)), games), ['b'])   # c: unknown
        self.assertEqual(self.on(shelf(('favorite', 'yes', None)), games), ['a'])
        self.assertEqual(sorted(self.on(shelf(('favorite', 'no', None)), games)), ['b', 'c'])

    def test_fact_rules_never_match_unknown_games(self):
        games = [game('rpg'), game('act'), game('nofacts')]
        facts = {'rpg': fact(main=8, genres=('RPG',), modes=('single', 'coop')),
                 'act': fact(main=30, genres=('Action',))}
        self.assertEqual(self.on(shelf(('genre', 'is', 'RPG')), games, facts), ['rpg'])
        self.assertEqual(self.on(shelf(('genre', 'not', 'RPG')), games, facts), ['act'])
        self.assertEqual(self.on(shelf(('mode', 'has', 'coop')), games, facts), ['rpg'])
        self.assertEqual(self.on(shelf(('mode', 'lacks', 'coop')), games, facts), ['act'])
        self.assertEqual(self.on(shelf(('length', 'under', 10)), games, facts), ['rpg'])
        self.assertEqual(self.on(shelf(('length', 'over', 20)), games, facts), ['act'])

    def test_all_and_any(self):
        games = [game('a', 600, 1, source='steam'), game('b', source='steam'),
                 game('c', 600, 1, source='nexus')]
        rules = (('store', 'is', 'steam'), ('playtime', 'atleast', 5))
        self.assertEqual(self.on(shelf(*rules), games), ['a'])
        self.assertEqual(sorted(self.on(shelf(*rules, match='any'), games)), ['a', 'b', 'c'])

    def test_rules_without_a_value_are_skipped_and_no_rule_is_empty(self):
        games = [game('a', source='steam'), game('b', source='nexus')]
        self.assertEqual(self.on(shelf(('store', 'is', 'steam'), ('genre', 'is', None)), games),
                         ['a'])
        self.assertEqual(self.on(shelf(('genre', 'is', None)), games), [])
        self.assertEqual(self.on(shelf(), games), [])

    def test_ordered_by_last_played(self):
        games = [game('old', 60, 20), game('new', 60, 1), game('never')]
        self.assertEqual(self.on(shelf(('store', 'is', 'nexus')), games), ['new', 'old', 'never'])

    def test_clean_shelves(self):
        raw = [{'id': 'a1', 'name': '  Quick   RPGs ', 'match': 'any',
                'rules': [{'field': 'genre', 'op': 'is', 'value': 'RPG'},
                          {'field': 'bogus', 'op': 'is'},
                          {'field': 'playtime', 'op': 'atleast', 'value': 'lots'},
                          {'field': 'favorite', 'op': 'yes', 'value': 3}]},
               {'id': 'a1', 'name': 'dupe'}, {'name': 'no id'}, 'junk',
               {'id': '../evil'}, {'id': 'b2', 'removed': True, 'rules': 'x', 'match': 'odd'}]
        got = smart.clean_shelves(raw)
        self.assertEqual([s['id'] for s in got], ['a1', 'b2'])
        a = got[0]
        self.assertEqual(a['name'], 'Quick RPGs')
        self.assertEqual(a['match'], 'any')
        self.assertEqual(a['rules'], [{'field': 'genre', 'op': 'is', 'value': 'RPG'},
                                      {'field': 'playtime', 'op': 'atleast', 'value': None},
                                      {'field': 'favorite', 'op': 'yes', 'value': None}])
        self.assertFalse(a['removed'])
        self.assertEqual(got[1], {'id': 'b2', 'name': '', 'match': 'all', 'rules': [],
                                  'removed': True})
        self.assertEqual(smart.clean_shelves(None), [])
        many = [{'id': f'x{i}'} for i in range(smart.MAX_USER_SHELVES + 5)]
        self.assertEqual(len(smart.clean_shelves(many)), smart.MAX_USER_SHELVES)
        rules = [{'field': 'favorite', 'op': 'yes'}] * (smart.MAX_RULES + 3)
        self.assertEqual(len(smart.clean_shelf({'id': 'r', 'rules': rules})['rules']),
                         smart.MAX_RULES)

    def test_fields_offered_follow_the_facts_you_have(self):
        games = [game('a')]
        self.assertEqual(smart.fields_offered(games, {}),
                         ['playtime', 'last', 'store', 'favorite'])
        games = [game('a', added_at='2026-10-01', disk_bytes=1e9)]
        facts = {'a': fact(main=5, genres=('RPG',))}
        self.assertEqual(smart.fields_offered(games, facts),
                         ['playtime', 'last', 'added', 'store', 'size', 'favorite', 'genre',
                          'mode', 'length'])
        facts = {'a': {'genres': [], 'modes': None, 'length': None}}
        self.assertNotIn('genre', smart.fields_offered(games, facts))
        self.assertNotIn('mode', smart.fields_offered(games, facts))

    def test_values_offered(self):
        games = [game('a', source='steam'), game('b', source='nexus')]
        facts = {'a': fact(genres=('RPG', 'Horror'), modes=('single', 'coop'))}
        self.assertEqual(smart.values_for('store', games, facts),
                         [('nexus', "Repacks"), ('steam', "Steam")])
        self.assertEqual(smart.values_for('genre', games, facts),
                         [('RPG', 'RPG'), ('Horror', 'Horror')])
        self.assertEqual([v for v, _t in smart.values_for('mode', games, facts)],
                         ['single', 'coop'])
        self.assertEqual(smart.values_for('hours', games, facts)[0], (1, "1 h"))
        # a saved value nobody has any more is still offered, so it shows
        self.assertIn(('epic', "Epic"), smart.values_for('store', games, facts, 'epic'))
        self.assertIn(('Racing', 'Racing'), smart.values_for('genre', games, facts, 'Racing'))

    def test_describe_and_keys(self):
        s = shelf(('genre', 'is', 'RPG'), ('length', 'under', 10), ('playtime', 'none', None))
        self.assertEqual(smart.describe(s),
                         "Genre is RPG and Main story under 10 h and Play time none yet")
        s['match'] = 'any'
        self.assertIn(" or ", smart.describe(s))
        self.assertEqual(smart.describe(shelf()), "No rules yet")
        self.assertEqual(smart.title_of({'name': ''}), smart.UNTITLED)
        self.assertTrue(smart.is_user('user:abc123'))
        self.assertFalse(smart.is_user('user:'))
        self.assertFalse(smart.is_user('smart:never'))
        self.assertIs(smart.find_user([s], 'user:abc123'), s)
        self.assertIsNone(smart.find_user([s], 'smart:never'))

    def test_new_shelf_and_default_rules(self):
        s = smart.new_shelf('z9', ['genre', 'playtime'])
        self.assertEqual(s['rules'], [{'field': 'genre', 'op': 'is', 'value': None}])
        self.assertEqual(smart.default_rule('playtime'),
                         {'field': 'playtime', 'op': 'none', 'value': None})
        self.assertEqual(smart.default_rule('size')['value'], 25)
        self.assertEqual(smart.clean_shelf(s), s)

    def test_no_repack_wording_in_field_names(self):
        for field, (title, ops, _f) in smart.FIELDS.items():
            for text in (title, *(o[1] for o in ops)):
                self.assertNotIn('repack', text.lower(), field)

    def test_bad_values_dont_raise(self):
        games = [{'slug': 'x', 'last_played': 'garbage', 'added_at': 7, 'disk_bytes': 'big',
                  'playtime_minutes': None}]
        facts = {'x': {'length': {'main': 'n/a'}, 'modes': None, 'genres': None}}
        for field, (_t, ops, _f) in smart.FIELDS.items():
            for op, _l, kind in ops:
                value = (smart.values_for(kind, games, facts) or [(None, '')])[0][0] \
                    if kind else None
                smart.user_games(shelf((field, op, value or 'RPG')), games, facts, NOW)


if __name__ == '__main__':
    unittest.main()
