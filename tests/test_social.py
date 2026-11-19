"""nexus/social.py (w-160): the Friends ladder (now / together / picks / quiet,
plus setup and hidden) and the picks ranking, from plain dicts: a
`--friends --json` answer, library games and facts. No window, no Steam."""

import ast
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import social  # noqa: E402

NOW = 1_800_000_000.0
ME = '76561190000000001'
A, B, C, D = (f'7656119000000010{i}' for i in range(4))
NAMES = {A: 'Ana', B: 'Ben', C: 'Cy', D: 'Dee'}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='seconds')


def friend(sid, owned=None, recent=None, state=1, playing=None, visible=True, **kw):
    return {'steamid': sid, 'id': sid, 'source': 'steam', 'name': NAMES.get(sid, sid),
            'visible': visible, 'private_reason': None if visible else 'game-details',
            'playing': playing, 'owned': owned or {}, 'recent': recent or {},
            'evidence': {'personastate': state}, **kw}


def answer(*rows, age=30, error=None, enabled=True, **kw):
    return {'enabled': enabled, 'me': ME, 'friends': list(rows), 'error': error,
            'updated': iso(NOW - age), 'gamedb': None, **kw}


def game(appid, title=None, minutes=0, **kw):
    return {'slug': f'steam-{appid}', 'display_name': title or f'Game {appid}',
            'source': 'steam', 'appid': str(appid), 'playtime_minutes': minutes, **kw}


COOP = {'modes': ['single', 'coop']}
SOLO = {'modes': ['single']}


def digest(ans, games=(), facts=None, **kw):
    return social.friends_digest(ans, list(games), facts or {}, now=NOW, **kw)


def playing(appid, name='Ten'):
    return {'appid': appid, 'name': name, 'since': NOW - 600}


class Ladder(unittest.TestCase):
    def test_shape(self):
        d = digest(answer(friend(A)))
        self.assertEqual(set(d), {'state', 'now', 'together', 'picks', 'wishlist',
                                  'counts', 'stale', 'updated'})
        self.assertEqual(set(d['counts']), {'public', 'private', 'total'})

    def test_hidden_without_an_answer_or_when_off(self):
        self.assertEqual(digest(None)['state'], 'hidden')
        self.assertEqual(digest({})['state'], 'hidden')
        self.assertEqual(digest(answer(friend(A), enabled=False))['state'], 'hidden')

    def test_setup_when_there_is_no_key_or_account(self):
        for kind in ('no-key', 'no-account'):
            d = digest(answer(friend(A, {'10': 600}), error={'kind': kind, 'message': ''}),
                       [game(10)])
            self.assertEqual(d['state'], 'setup')
            self.assertEqual(d['picks'], [])          # the old cache is not shown

    def test_now_beats_together_and_picks(self):
        ans = answer(friend(A, {'10': 100, '20': 900}, playing=playing(10)),
                     friend(B, {'20': 500}))
        d = digest(ans, [game(10, 'Ten'), game(20, 'Twenty')], {'steam-20': COOP})
        self.assertEqual(d['state'], 'now')
        self.assertEqual(len(d['now']), 1)
        self.assertEqual((d['now'][0]['appid'], d['now'][0]['title'], d['now'][0]['slug']),
                         (10, 'Ten', 'steam-10'))
        self.assertEqual(d['now'][0]['people'], [A])
        self.assertTrue(d['now'][0]['owned'])
        self.assertTrue(d['together'] and d['picks'])    # lower rungs still filled

    def test_now_groups_by_game_most_friends_first_and_unowned_has_no_slug(self):
        ans = answer(friend(A, playing=playing(10)), friend(B, playing=playing(30, 'Thirty')),
                     friend(C, playing=playing(30, 'Thirty')))
        d = digest(ans, [game(10)])
        self.assertEqual([g['appid'] for g in d['now']], [30, 10])
        self.assertEqual(d['now'][0]['people'], [B, C])
        self.assertEqual((d['now'][0]['slug'], d['now'][0]['owned'], d['now'][0]['title']),
                         (None, False, 'Thirty'))

    def test_now_without_a_game_name_says_a_game_not_an_id(self):
        ans = answer(friend(A, playing=playing(1086940, name=None)))
        self.assertEqual(digest(ans)['now'][0]['title'], 'a game')

    def test_hidden_or_duplicate_library_game_counts_as_not_owned_in_now(self):
        ans = answer(friend(A, playing=playing(10)))
        d = digest(ans, [game(10, hidden=True)])
        self.assertFalse(d['now'][0]['owned'])
        d = digest(ans, [game(10, dup_of='other')])
        self.assertFalse(d['now'][0]['owned'])

    def test_together_when_friends_are_online_and_share_coop_games(self):
        ans = answer(friend(A, {'20': 5, '30': 5}), friend(B, {'20': 5}), friend(C, {'20': 5}, state=0))
        d = digest(ans, [game(20, 'Co'), game(30, 'Solo'), game(40)],
                   {'steam-20': COOP, 'steam-30': SOLO, 'steam-40': COOP})
        self.assertEqual(d['state'], 'together')
        t = d['together']
        self.assertEqual(sorted(t['people']), [A, B])        # Cy is offline
        self.assertEqual([g['slug'] for g in t['games']], ['steam-20'])
        self.assertEqual(t['games'][0]['owners'], 2)
        self.assertTrue(t['games'][0]['all'])

    def test_together_ranks_by_online_owners_and_keeps_unknown_modes(self):
        ans = answer(friend(A, {'20': 5, '30': 5}), friend(B, {'30': 5}))
        d = digest(ans, [game(20), game(30)], {})            # no facts: modes unknown, stays in
        self.assertEqual([g['slug'] for g in d['together']['games']], ['steam-30', 'steam-20'])
        self.assertTrue(d['together']['games'][0]['all'])
        self.assertFalse(d['together']['games'][1]['all'])

    def test_online_without_shared_games_falls_to_picks_or_quiet(self):
        ans = answer(friend(A, {'99': 5}))
        d = digest(ans, [game(20, minutes=0)])
        self.assertEqual((d['state'], d['together']), ('quiet', None))

    def test_picks_when_nobody_is_on(self):
        ans = answer(friend(A, {'20': 3840}, state=0), friend(B, {'20': 60}, state=0))
        d = digest(ans, [game(20, 'Hades')])
        self.assertEqual(d['state'], 'picks')
        self.assertIsNone(d['together'])
        p = d['picks'][0]
        self.assertEqual((p['slug'], p['title'], p['appid']), ('steam-20', 'Hades', 20))
        self.assertEqual(p['minutes'], {A: 3840, B: 60})
        self.assertEqual(p['why'], "Ana 64 h · Ben 1 h · you haven't started")

    def test_quiet_when_nothing_to_say(self):
        self.assertEqual(digest(answer(friend(A, state=0)), [game(20)])['state'], 'quiet')
        self.assertEqual(digest(answer())['state'], 'quiet')

    def test_stale_presence_is_not_now_nor_together(self):
        ans = answer(friend(A, {'20': 5}, playing=playing(20)), friend(B, {'20': 900}),
                     age=social.PLAYING_STALE + 1)
        d = digest(ans, [game(20)], {'steam-20': COOP})
        self.assertTrue(d['stale'])
        self.assertEqual((d['now'], d['together']), ([], None))
        self.assertEqual(d['state'], 'picks')               # owned minutes don't go stale
        self.assertFalse(digest(dict(ans, updated=iso(NOW - 5)),
                                [game(20)])['stale'])

    def test_missing_updated_is_stale(self):
        ans = answer(friend(A, playing=playing(20)))
        ans['updated'] = None
        d = digest(ans, [game(20)])
        self.assertTrue(d['stale'])
        self.assertEqual(d['now'], [])

    def test_private_friends_count_but_never_appear(self):
        ans = answer(friend(A, {'20': 900}, state=0),
                     friend(B, playing=playing(20), visible=False),
                     friend(C, {'20': 99999}, visible=False))
        d = digest(ans, [game(20)])
        self.assertEqual(d['counts'], {'public': 1, 'private': 2, 'total': 3})
        self.assertEqual(d['now'], [])
        self.assertEqual(list(d['picks'][0]['minutes']), [A])

    def test_an_all_private_list_is_quiet_not_hidden(self):
        d = digest(answer(friend(A, visible=False)), [game(20)])
        self.assertEqual(d['state'], 'quiet')
        self.assertEqual(d['counts']['private'], 1)

    def test_game_db_friends_still_show_without_a_steam_key(self):
        gdb = {'id': 'gdb:1', 'source': 'gamedb', 'steamid': None, 'name': 'Gus',
               'visible': True, 'owned': {'20': 600}, 'recent': {}, 'evidence': {}}
        ans = answer(friend(A, {'20': 5000}), gdb, error={'kind': 'no-key', 'message': ''},
                     gamedb={'count': 1, 'error': None})
        d = digest(ans, [game(20)])
        self.assertEqual(d['state'], 'picks')
        self.assertEqual(list(d['picks'][0]['minutes']), ['gdb:1'])   # no Steam rows, no presence
        self.assertEqual(d['now'], [])

    def test_a_gamedb_row_for_a_steam_friend_is_one_person(self):
        gdb = {'id': 'gdb:1', 'source': 'gamedb', 'steamid': A, 'name': 'Ana',
               'visible': True, 'owned': {'20': 900}, 'recent': {}, 'evidence': {}}
        ans = answer(friend(A, {'20': 600}, state=0), gdb, gamedb={'count': 1, 'error': None})
        d = digest(ans, [game(20)])
        self.assertEqual(d['counts'], {'public': 1, 'private': 0, 'total': 1})
        self.assertEqual(d['picks'][0]['minutes'], {A: 900})        # the larger, once


class Picks(unittest.TestCase):
    def test_score_is_summed_minutes_and_two_week_play_counts_double(self):
        ans = answer(friend(A, {'20': 600, '30': 500}, {'30': 200}, state=0),
                     friend(B, {'20': 10}, state=0))
        d = digest(ans, [game(20), game(30)])
        self.assertEqual([p['slug'] for p in d['picks']], ['steam-30', 'steam-20'])  # 700 > 610
        self.assertEqual(d['picks'][0]['recent'], {A: 200})
        self.assertGreater(d['picks'][0]['score'], d['picks'][1]['score'])

    def test_hidden_and_duplicate_games_are_skipped_and_dups_keep_the_kept_one(self):
        ans = answer(friend(A, {'20': 900, '30': 800, '40': 700}, state=0))
        games = [game(20, hidden=True), game(30, dup_of='steam-40'), game(40)]
        self.assertEqual([p['slug'] for p in digest(ans, games)['picks']], ['steam-40'])

    def test_two_library_rows_for_one_app_give_one_pick(self):
        ans = answer(friend(A, {'20': 900}, state=0))
        g2 = {**game(20), 'slug': 'other-20'}
        self.assertEqual(len(digest(ans, [game(20), g2])['picks']), 1)

    def test_a_game_you_played_past_a_fifth_of_its_length_is_dropped(self):
        ans = answer(friend(A, {'20': 900, '30': 900}, state=0))
        facts = {'steam-20': {'length': {'main': 10}}, 'steam-30': {'length': {'main': 10}}}
        d = digest(ans, [game(20, minutes=121), game(30, minutes=119)], facts)
        self.assertEqual([p['slug'] for p in d['picks']], ['steam-30'])
        self.assertIn('you 1 h', d['picks'][0]['why'])

    def test_unknown_length_never_drops_a_game(self):
        ans = answer(friend(A, {'20': 900}, state=0))
        d = digest(ans, [game(20, minutes=6000)])
        self.assertEqual(len(d['picks']), 1)

    def test_zero_minutes_friend_owning_is_not_a_pick(self):
        ans = answer(friend(A, {'20': 0}, state=0))
        self.assertEqual(digest(ans, [game(20)])['picks'], [])

    def test_limit_and_games_not_in_your_library_are_ignored(self):
        ans = answer(friend(A, {str(i): 100 * i for i in range(1, 9)}, state=0))
        d = digest(ans, [game(i) for i in range(1, 8)], limit=3)
        self.assertEqual(len(d['picks']), 3)
        self.assertNotIn('steam-8', [p['slug'] for p in d['picks']])

    def test_picks_rotate_with_the_day_but_are_stable_within_one(self):
        ans = answer(friend(A, {str(i): 6000 for i in range(1, 9)}, state=0))
        games = [game(i) for i in range(1, 9)]
        today = [p['slug'] for p in digest(ans, games)['picks']]
        self.assertEqual(today, [p['slug'] for p in digest(ans, games)['picks']])
        seen = {tuple(p['slug'] for p in social.friends_digest(
            ans, games, {}, now=NOW + n * 86400)['picks']) for n in range(6)}
        self.assertGreater(len(seen), 1)

    def test_why_names_the_top_two_friends_and_your_own_hours(self):
        ans = answer(friend(A, {'20': 300}, state=0), friend(B, {'20': 600}, state=0),
                     friend(C, {'20': 60}, state=0))
        d = digest(ans, [game(20, minutes=180)])
        self.assertEqual(d['picks'][0]['why'], 'Ben 10 h · Ana 5 h · you 3 h')


class Wishlist(unittest.TestCase):
    def test_wishlist_join_lists_public_owners(self):
        ans = answer(friend(A, {'77': 5}, state=0), friend(B, {'77': 5, '78': 1}, state=0),
                     friend(C, {'77': 5}, visible=False))
        d = digest(ans, wishlist=[{'appid': 77, 'title': 'Silksong'},
                                  {'appid': 78, 'title': 'Other'}, {'appid': 79, 'title': 'None'}])
        self.assertEqual([(w['appid'], sorted(w['owners'])) for w in d['wishlist']],
                         [(77, [A, B]), (78, [B])])

    def test_no_wishlist_is_empty(self):
        self.assertEqual(digest(answer(friend(A)))['wishlist'], [])


class Pure(unittest.TestCase):
    def test_social_imports_no_qt(self):
        tree = ast.parse((REPO / 'nexus' / 'social.py').read_text(encoding='utf-8'))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(('.' * n.level) + (n.module or ''))
        self.assertFalse([m for m in mods if 'Qt' in m or '.gui' in m or m.startswith('nexus.gui')])

    def test_input_is_not_changed(self):
        ans = answer(friend(A, {'20': 900}, playing=playing(20)))
        games = [game(20)]
        import copy
        a0, g0 = copy.deepcopy(ans), copy.deepcopy(games)
        digest(ans, games)
        self.assertEqual((ans, games), (a0, g0))

    def test_junk_rows_never_raise(self):
        ans = answer(friend(A, {'20': 'x'}), 'junk', None, {'steamid': None})
        ans['friends'].append({'steamid': B, 'owned': None, 'recent': 5, 'evidence': None})
        d = digest(ans, [game(20), 'junk', {}])
        self.assertIn(d['state'], ('quiet', 'picks', 'together', 'now'))


if __name__ == '__main__':
    unittest.main()
