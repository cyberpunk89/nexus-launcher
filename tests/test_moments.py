"""For you now's ranking (nexus/gui/moments.py, w-88): pure, plain inputs,
a fixed `now`. No Qt widgets, no real home, Steam or network. The adapter
moments_inputs is checked with fake hub objects."""

import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus.gui import moments as M  # noqa: E402
from tests.patching import linux_features  # noqa: E402

NOW = datetime(2026, 11, 6, 15, 0)


def iso(days=0, hours=0):
    return (NOW - timedelta(days=days, hours=hours)).isoformat(timespec='seconds')


def game(slug, **kw):
    g = {'slug': slug, 'display_name': slug.title(), 'source': 'nexus', 'installed': True}
    g.update(kw)
    return g


def wish(appid, title=None, atl=False, sale=False, cut=0, **price):
    p = {'currentStr': '9.99', 'onSale': sale or bool(cut), 'cutPct': cut,
         'atHistoricalLow': atl}
    p.update(price)
    return {'appid': appid, 'title': title or f'Wish {appid}', 'price': p}


def friend_entry(slug, title, minutes=10, names=('Ana',)):
    return {'appid': 1, 'title': title, 'slug': slug, 'minutes': minutes,
            'people': [{'name': n} for n in names]}


SALES = {'sale': True, 'sale_min_cut': 10, 'atl': True, 'fitgirl': True}


def rank(inputs, **kw):
    return M.home_moments(inputs, NOW, **kw)


class Fixtures(unittest.TestCase):
    def test_nothing_in_nothing_out(self):
        self.assertEqual(rank({}), [])
        self.assertEqual(rank({'games': [], 'jobs': [], 'disks': [], 'checks': []}), [])

    def test_one_input_gives_one_moment_in_the_documented_shape(self):
        out = rank({'friends_now': [friend_entry('hades', 'Hades', names=('Ana', 'Ben'))],
                    'games': [game('hades')]})
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(set(m), {'id', 'kind', 'sub', 'rank', 'key', 'title', 'why', 'action',
                                  'badge', 'urgency', 'count', 'when', 'snooze_key'})
        self.assertEqual((m['id'], m['kind'], m['rank'], m['key'], m['count']),
                         ('friends:hades', 'friends', 3, 'hades', 1))
        self.assertEqual(m['action']['id'], 'launch')
        self.assertEqual(m['action']['label'], 'Launch')
        self.assertEqual(m['snooze_key'], 'friends:hades')
        self.assertIn('Ana', m['title'] + m['why'])
        glyph, role = m['badge']
        self.assertIn(role, ('danger', 'accent', 'success', 'warning'))
        self.assertTrue(0 <= m['urgency'] <= 100)
        self.assertIsInstance(m['when'], (int, float))

    def twenty(self):
        """Twenty candidates over all eight kinds, three of them per kind
        where the kind allows."""
        games = [game(f'g{i}', last_played=iso(days=10 + i), playtime_minutes=120)
                 for i in range(12)]
        return {
            'checks': [{'id': 'x', 'status': 'fail', 'title': 'Broken thing', 'fix': 'Fix it'},
                       {'id': 'y', 'status': 'warn', 'title': 'Only a warning'}],
            'disks': [{'path': '/full', 'total': 1000, 'used': 980, 'free': 20},
                      {'path': '/fuller', 'total': 1000, 'used': 850, 'free': 150}],
            'jobs': [{'title': 'Installing G0', 'state': 'running', 'kind': 'install',
                      'active': True, 'slug': 'g0', 'progress': 40, 'step': 'Copying'}],
            'friends_now': [friend_entry('g1', 'G1'), friend_entry('g2', 'G2')],
            'deals': [wish(10, atl=True, cut=50), wish(11, cut=30), wish(12, cut=60)],
            'alert_settings': SALES,
            'repacks': {'13': {'title': 'Wish 13 repack', 'date': '2026-11-05T10:00:00+00:00'}},
            'repack_updates': {'g3': '/dl/g3-v2'},
            'proton': {'update': True, 'latest': 'GE-Proton10-30'},
            'counts': {'g4': [9, 10], 'g5': [18, 20]},
            'facts': {f'g{i}': {'length': {'main': 20}} for i in range(12)},
            'games': games,
        }

    def test_twenty_candidates_give_exactly_eight_in_rank_order(self):
        out = rank(self.twenty())
        self.assertEqual(len(out), 8)
        ranks = [m['rank'] for m in out]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(out[0]['kind'], 'broken')
        self.assertEqual([m['kind'] for m in out[:5]],
                         ['broken', 'broken', 'running', 'friends', 'friends'])
        urg = [m['urgency'] for m in out]
        self.assertEqual(urg, sorted(urg, reverse=True))
        self.assertEqual(len(rank(self.twenty(), limit=3)), 3)
        self.assertEqual(len(rank(self.twenty(), limit=50)), M.TOTAL_MAX)

    def test_ties_go_to_the_newest(self):
        games = [game('old', last_played=iso(days=20), playtime_minutes=90),
                 game('new', last_played=iso(days=6), playtime_minutes=90),
                 game('mid', last_played=iso(days=9), playtime_minutes=90)]
        facts = {g['slug']: {'length': {'main': 10}} for g in games}
        out = rank({'games': games, 'facts': facts})
        self.assertEqual([m['key'] for m in out], ['new', 'mid', 'old'])
        fr = rank({'friends_now': [friend_entry('a', 'A', minutes=50),
                                   friend_entry('b', 'B', minutes=5)]})
        self.assertEqual([m['key'] for m in fr], ['b', 'a'])


class Kinds(unittest.TestCase):
    def test_broken_is_a_health_fail_or_a_drive_under_five_percent(self):
        out = rank({'checks': [{'id': 'w', 'status': 'warn', 'title': 'W'},
                               {'id': 'f', 'status': 'fail', 'title': 'F', 'fix': 'Do X'}],
                    'disks': [{'path': '/a', 'total': 100, 'used': 96, 'free': 4},
                              {'path': '/b', 'total': 100, 'used': 50, 'free': 50}]})
        self.assertEqual([(m['kind'], m['sub']) for m in out],
                         [('broken', 'health'), ('broken', 'disk')] if out[0]['sub'] == 'health'
                         else [('broken', 'disk'), ('broken', 'health')])
        acts = {m['sub']: m['action']['id'] for m in out}
        self.assertEqual(acts, {'health': 'open_health', 'disk': 'open_storage'})
        self.assertTrue(all(m['badge'][1] == 'danger' for m in out))

    def test_drive_almost_full_is_the_last_kind(self):
        used = int(100 * M.DISK_FULL) + 5
        out = rank({'disks': [{'path': '/a', 'total': 100, 'used': used, 'free': 100 - used}]})
        self.assertEqual([(m['kind'], m['rank'], m['sub']) for m in out], [('disk', 8, 'disk')])
        self.assertEqual(rank({'disks': [{'path': '/a', 'total': 100, 'used': 50,
                                          'free': 50}]}), [])
        self.assertEqual(rank({'disks': [{'path': '/a', 'total': 0, 'used': 0, 'free': 0}]}), [])

    def test_running_install_and_ready_to_play(self):
        now = NOW.timestamp()
        jobs = [{'title': 'Installing A', 'state': 'running', 'kind': 'install', 'active': True,
                 'slug': 'a', 'progress': 40, 'step': 'Unpacking'},
                {'title': 'Installing B', 'state': 'done', 'kind': 'install', 'active': False,
                 'slug': 'b', 'ended': now - 600},
                {'title': 'Installing C', 'state': 'failed', 'kind': 'install', 'active': False,
                 'slug': 'c', 'ended': now - 60},
                {'title': 'Installing D', 'state': 'done', 'kind': 'install', 'active': False,
                 'slug': 'd', 'ended': now - (M.READY_HOURS + 1) * 3600},
                {'title': 'Verifying E', 'state': 'running', 'kind': 'task', 'active': True,
                 'slug': 'e'}]
        games = [game('a'), game('b'), game('c'), game('d'), game('e')]
        out = rank({'jobs': jobs, 'games': games})
        self.assertEqual([m['key'] for m in out], ['a', 'b'])     # newest first: A is now
        by = {m['key']: m for m in out}
        self.assertEqual(by['a']['action']['id'], 'open_job')
        self.assertIn('40%', by['a']['why'])
        self.assertEqual((by['b']['action']['id'], by['b']['action']['label']),
                         ('launch', 'Launch'))
        self.assertIn('Ready to play', by['b']['title'] + by['b']['why'])
        # Played since it finished: done with it.
        games[1]['last_played'] = datetime.fromtimestamp(now - 60).isoformat()
        self.assertEqual([m['key'] for m in rank({'jobs': jobs, 'games': games})], ['a'])

    def test_ready_at_downloads_and_store_installs_count_as_running(self):
        now = NOW.timestamp()
        out = rank({'ready_at': {'r': now - 30},
                    'downloads': [{'hash': 'abcdef0123456789', 'name': 'Big Repack',
                                   'tracked': True, 'percent': 12.5, 'state': 'downloading'},
                                  {'hash': 'ff', 'name': 'Done', 'tracked': True, 'done': True},
                                  {'hash': 'ee', 'name': 'Not ours', 'percent': 3}],
                    'games': [game('r'), game('s', source='steam', appid=5,
                                                  installing={'done': 1, 'total': 4})]})
        self.assertEqual({m['kind'] for m in out}, {'running'})
        self.assertEqual(len(out), 3)
        titles = ' '.join(m['title'] for m in out)
        self.assertIn('Big Repack', titles)
        self.assertNotIn('Not ours', titles)

    def test_friends_need_a_game_you_own(self):
        out = rank({'friends_now': [friend_entry(None, 'Not yours'),
                                    friend_entry('mine', 'Mine')]})
        self.assertEqual([m['key'] for m in out], ['mine'])
        self.assertNotEqual(out[0]['action']['label'], 'Play')

    def test_store_order_atl_then_sale_by_cut_then_fitgirl(self):
        inputs = {'deals': [wish(1, cut=20), wish(2, atl=True, cut=10), wish(3, cut=70)],
                  'alert_settings': SALES,
                  'repacks': {'4': {'title': 'Four', 'date': '2026-11-01T00:00:00+00:00'}}}
        out = rank(inputs)
        self.assertEqual([(m['sub'], m['key']) for m in out],
                         [('atl', 'steam-2'), ('sale', 'steam-3')])        # STORE_MAX
        self.assertEqual(M.STORE_MAX, 2)
        self.assertEqual(out[0]['action']['id'], 'open_deals')
        # Sales are off in the alert settings: only the low and FitGirl.
        out = rank(dict(inputs, alert_settings={'sale': False, 'sale_min_cut': 0}))
        self.assertEqual([m['sub'] for m in out], ['atl', 'fitgirl'])

    def test_a_sale_ending_within_48_hours_goes_first(self):
        soon = (NOW + timedelta(hours=20)).isoformat()
        later = (NOW + timedelta(days=5)).isoformat()
        out = rank({'deals': [wish(1, atl=True, cut=40, endsAt=later),
                              wish(2, cut=15, endsAt=soon), wish(3, cut=90)],
                    'alert_settings': SALES})
        self.assertEqual([m['key'] for m in out], ['steam-2', 'steam-1'])

    def test_store_moment_of_a_game_you_own_uses_its_slug(self):
        out = rank({'deals': [wish(77, atl=True, cut=50)], 'alert_settings': SALES,
                    'store_ids': {'77': 'owned-game'}})
        self.assertEqual(out[0]['key'], 'owned-game')

    def test_updates_repack_steam_and_proton(self):
        games = [game('rp'), game('st', source='steam', appid=9, update_pending=True)]
        inputs = {'games': games, 'repack_updates': {'rp': '/dl/rp'},
                  'proton': {'update': True, 'latest': 'GE-Proton10-30'}}
        out = rank(inputs)
        self.assertEqual({(m['kind'], m['sub']) for m in out},
                         {('update', 'repack_updates'), ('update', None),
                          ('update', 'proton')})
        pr = next(m for m in out if m['sub'] == 'proton')
        self.assertEqual(pr['action']['id'], 'install_ge')
        self.assertIn('GE-Proton10-30', pr['snooze_key'])
        self.assertEqual({m['action']['id'] for m in out if m['sub'] != 'proton'}, {'update'})
        # Off where Proton isn't a feature (Windows): no Proton moment.
        self.assertNotIn('proton', {m['sub'] for m in rank(inputs, linux=False)})
        self.assertNotIn('proton', {m['sub'] for m in rank(
            dict(inputs, proton={'update': False, 'latest': 'GE-Proton10-30'}))})

    def test_achievements_close_to_100(self):
        games = [game('near', source='steam', appid=1), game('far', source='steam', appid=2),
                 game('done', source='steam', appid=3)]
        out = rank({'games': games, 'counts': {'near': [19, 20], 'far': [2, 20],
                                               'done': [20, 20]}})
        self.assertEqual([(m['kind'], m['key']) for m in out], [('achievements', 'near')])
        self.assertEqual(out[0]['action']['label'], 'Resume')
        self.assertIn('19 of 20', out[0]['why'])

    def test_pick_up_again_needs_facts_play_time_and_five_idle_days(self):
        self.assertEqual(M.IDLE_MOMENT_DAYS, 5)
        fact = {'length': {'main': 10}}
        games = [game('ok', last_played=iso(days=6), playtime_minutes=120),
                 game('recent', last_played=iso(days=2), playtime_minutes=120),
                 game('short', last_played=iso(days=6), playtime_minutes=30),
                 game('nofacts', last_played=iso(days=6), playtime_minutes=120),
                 game('through', last_played=iso(days=6), playtime_minutes=600),
                 game('gone', last_played=iso(days=6), playtime_minutes=120,
                      installed=False)]
        facts = {g['slug']: fact for g in games if g['slug'] != 'nofacts'}
        out = rank({'games': games, 'facts': facts})
        self.assertEqual([m['key'] for m in out], ['ok'])
        self.assertEqual((out[0]['kind'], out[0]['action']['id']), ('idle', 'resume'))

    def test_hidden_and_duplicate_games_are_left_out(self):
        fact = {'length': {'main': 10}}
        games = [game('h', last_played=iso(days=6), playtime_minutes=120, hidden=True),
                 game('d', last_played=iso(days=6), playtime_minutes=120, dup_of='x')]
        self.assertEqual(rank({'games': games, 'facts': {'h': fact, 'd': fact}}), [])


class Rules(unittest.TestCase):
    def test_one_moment_per_game_the_higher_kind_wins(self):
        games = [game('x', source='steam', appid=4, last_played=iso(days=7),
                      playtime_minutes=200)]
        out = rank({'games': games, 'friends_now': [friend_entry('x', 'X')],
                    'counts': {'x': [9, 10]}, 'facts': {'x': {'length': {'main': 50}}}})
        self.assertEqual([m['kind'] for m in out], ['friends'])

    def test_never_the_stage_game(self):
        inputs = {'friends_now': [friend_entry('x', 'X'), friend_entry('y', 'Y')]}
        self.assertEqual([m['key'] for m in rank(inputs, stage_key='x')], ['y'])
        self.assertEqual(len(rank(inputs, stage_key=None)), 2)

    def test_more_than_group_of_one_alert_kind_is_one_summary(self):
        from nexus import alerts
        n = alerts.GROUP + 1
        out = rank({'deals': [wish(i, atl=True, cut=10 + i) for i in range(n)],
                    'alert_settings': SALES})
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual((m['kind'], m['sub'], m['count'], m['key']), ('store', 'atl', n, ''))
        self.assertIn(str(n), m['title'])
        self.assertEqual(m['action']['id'], 'open_deals')
        # Exactly GROUP stay single (then the store cap keeps 2).
        out = rank({'deals': [wish(i, atl=True, cut=10) for i in range(alerts.GROUP)],
                    'alert_settings': SALES})
        self.assertEqual([m['count'] for m in out], [1, 1])

    def test_repack_updates_group_too(self):
        from nexus import alerts
        ups = {f's{i}': f'/dl/{i}' for i in range(alerts.GROUP + 2)}
        out = rank({'repack_updates': ups, 'games': [game(s) for s in ups]})
        self.assertEqual([(m['sub'], m['count']) for m in out],
                         [('repack_updates', alerts.GROUP + 2)])

    def test_kinds_off_hides_whole_kinds_or_alert_kinds(self):
        inputs = {'friends_now': [friend_entry('x', 'X')],
                  'deals': [wish(1, atl=True, cut=40), wish(2, cut=30)],
                  'alert_settings': SALES}
        self.assertEqual({m['kind'] for m in rank(inputs, kinds_off=('store',))}, {'friends'})
        self.assertEqual([m['sub'] for m in rank(inputs, kinds_off=['friends', 'atl'])],
                         ['sale'])

    def test_kinds_off_never_hides_a_problem(self):
        """w-89: Customise's Storage chip is kind 'disk'; a nearly full drive
        is kind 'broken' with sub 'disk' and must still show."""
        inputs = {'disks': [{'path': '/a', 'total': 100, 'free': 2},
                            {'path': '/b', 'total': 100, 'free': 10}],
                  'checks': [{'id': 'c', 'title': 'Bad', 'status': 'fail'}]}
        out = rank(inputs, kinds_off=['disk', 'broken', 'health'])
        self.assertEqual(sorted(m['kind'] for m in out), ['broken', 'broken'])


class Dismiss(unittest.TestCase):
    def test_snooze_key_and_is_snoozed(self):
        self.assertEqual(M.snooze_key('friends', 'steam-3164500'), 'friends:steam-3164500')
        until = (NOW.date() + timedelta(days=M.SNOOZE_DAYS)).isoformat()
        sn = {'friends:x': until}
        self.assertTrue(M.is_snoozed(sn, 'friends:x', NOW))
        self.assertTrue(M.is_snoozed(sn, 'friends:x', NOW + timedelta(days=6, hours=8)))
        self.assertFalse(M.is_snoozed(sn, 'friends:x', NOW + timedelta(days=7)))
        self.assertFalse(M.is_snoozed(sn, 'friends:y', NOW))
        self.assertFalse(M.is_snoozed(None, 'friends:x', NOW))
        self.assertFalse(M.is_snoozed({'friends:x': 'soon'}, 'friends:x', NOW))
        self.assertFalse(M.is_snoozed({'friends:x': 5}, 'friends:x', NOW))
        self.assertFalse(M.is_snoozed(['friends:x'], 'friends:x', NOW))

    def test_not_now_hides_that_kind_for_that_game_for_seven_days(self):
        games = [game('x', source='steam', appid=4)]
        inputs = {'games': games, 'friends_now': [friend_entry('x', 'X'),
                                                  friend_entry('y', 'Y')],
                  'counts': {'x': [9, 10]}}
        until = (NOW.date() + timedelta(days=M.SNOOZE_DAYS)).isoformat()
        sn = {M.snooze_key('friends', 'x'): until}
        out = rank(inputs, snoozes=sn)
        # x's friends moment is snoozed: its achievements moment shows; y stays.
        self.assertEqual([(m['kind'], m['key']) for m in out],
                         [('friends', 'y'), ('achievements', 'x')])
        later = M.home_moments(inputs, NOW + timedelta(days=M.SNOOZE_DAYS), snoozes=sn)
        self.assertEqual([(m['kind'], m['key']) for m in later],
                         [('friends', 'x'), ('friends', 'y')])

    def test_snoozing_a_summary_or_an_alert_kind_uses_its_snooze_key(self):
        out = rank({'deals': [wish(5, atl=True, cut=40)], 'alert_settings': SALES})
        key = out[0]['snooze_key']
        self.assertEqual(key, 'atl:steam-5')
        until = (NOW.date() + timedelta(days=7)).isoformat()
        self.assertEqual(rank({'deals': [wish(5, atl=True, cut=40)], 'alert_settings': SALES},
                              snoozes={key: until}), [])

    def test_quiet_until_next_is_four_in_the_morning(self):
        self.assertEqual(M.QUIET_HOUR, 4)
        self.assertEqual(M.quiet_until_next(datetime(2026, 11, 6, 15, 0)),
                         datetime(2026, 11, 7, 4, 0))
        self.assertEqual(M.quiet_until_next(datetime(2026, 11, 6, 2, 30)),
                         datetime(2026, 11, 6, 4, 0))
        self.assertEqual(M.quiet_until_next(datetime(2026, 11, 6, 4, 0)),
                         datetime(2026, 11, 7, 4, 0))
        self.assertEqual(M.quiet_until_next(datetime(2026, 12, 31, 23, 59)),
                         datetime(2027, 1, 1, 4, 0))

    def test_quiet_until_next_across_dst(self):
        """The night clocks change, 04:00 is still 04:00 on the wall
        (a timedelta from midnight would give 03:00 or 05:00)."""
        for night in (datetime(2026, 3, 29, 1, 0), datetime(2026, 10, 25, 1, 0),
                      datetime(2026, 3, 28, 22, 0), datetime(2026, 10, 24, 22, 0)):
            q = M.quiet_until_next(night)
            self.assertEqual((q.hour, q.minute), (4, 0), night)
            self.assertGreater(q, night)
            self.assertLessEqual(q.date() - night.date(), timedelta(days=1))
        aware = datetime(2026, 10, 25, 1, 0, tzinfo=timezone(timedelta(hours=2)))
        q = M.quiet_until_next(aware)
        self.assertEqual((q.hour, q.minute, q.tzinfo), (4, 0, None))

    def test_quiet_hides_everything_but_broken_until_it_ends(self):
        inputs = {'checks': [{'id': 'f', 'status': 'fail', 'title': 'F'}],
                  'friends_now': [friend_entry('x', 'X')]}
        q = M.quiet_until_next(NOW)
        self.assertEqual([m['kind'] for m in rank(inputs, quiet_until=q)], ['broken'])
        self.assertEqual([m['kind'] for m in M.home_moments(inputs, q, quiet_until=q)],
                         ['broken', 'friends'])
        self.assertEqual(len(rank(inputs, quiet_until=NOW - timedelta(minutes=1))), 2)
        self.assertEqual(len(rank(inputs, quiet_until='not a date')), 2)


class Adapter(unittest.TestCase):
    """moments_inputs(hub): the only place that reads the window."""

    def hub(self, **kw):
        mono = time.monotonic()
        job = SimpleNamespace(title='Installing A', state='done', step='Done', progress=None,
                              active=False, kind='install', slug='a', name='A',
                              ended=mono - 120)
        run = SimpleNamespace(title='Installing B', state='running', step='Copying',
                              progress=(1, 4), active=True, kind='install', slug='b',
                              name='B', ended=None)
        games = [game('a'), game('b'), game('st', source='steam', appid=440,
                                            achievements=[9, 10])]
        stage = SimpleNamespace(art=SimpleNamespace(game={'slug': 'st'}))
        hub = SimpleNamespace(
            _games=games, jobs=SimpleNamespace(jobs=[job, run]),
            _disks=[{'path': '/', 'total': 10, 'used': 5, 'free': 5}],
            health=SimpleNamespace(checks=[{'status': 'fail', 'title': 'F'}]),
            dashboard=SimpleNamespace(stage=stage),
            dealspage=SimpleNamespace(
                _data=lambda: {'wishlist': [wish(1, atl=True, cut=30)]},
                repacks=lambda: {'2': {'title': 'Two'}}),
            _repack_updates={'a': '/dl/a'},
            protonpage=SimpleNamespace(info={'families': {'ge': {'update': True,
                                                                 'latest': 'GE-9'}}}),
            _ready_at={'a': 123.0}, _mine_torrents=[{'hash': 'h'}],
            _smart_facts=lambda: {'a': {'length': {'main': 3}}})
        for k, v in kw.items():
            setattr(hub, k, v)
        return hub

    @linux_features
    def test_reads_every_source_into_plain_data(self):
        hub = self.hub()
        with mock.patch.object(M, '_playing_now', return_value=[friend_entry('st', 'St')]):
            inp = M.moments_inputs(hub)
        self.assertEqual({j['slug'] for j in inp['jobs']}, {'a', 'b'})
        done = next(j for j in inp['jobs'] if j['slug'] == 'a')
        # Job.ended is monotonic; the adapter gives a wall-clock epoch.
        self.assertAlmostEqual(done['ended'], time.time() - 120, delta=5)
        run = next(j for j in inp['jobs'] if j['slug'] == 'b')
        self.assertEqual(run['progress'], 25)
        self.assertNotIn('job', run)
        self.assertEqual(inp['disks'], hub._disks)
        self.assertEqual(inp['checks'], hub.health.checks)
        self.assertEqual(inp['deals'][0]['appid'], 1)
        self.assertEqual(inp['repacks'], {'2': {'title': 'Two'}})
        self.assertEqual(inp['repack_updates'], {'a': '/dl/a'})
        self.assertEqual(inp['proton'], {'update': True, 'latest': 'GE-9'})
        self.assertEqual(inp['counts'], {'st': [9, 10]})
        self.assertEqual(inp['facts'], {'a': {'length': {'main': 3}}})
        self.assertEqual(inp['store_ids'], {'440': 'st'})
        self.assertEqual(inp['ready_at'], {'a': 123.0})
        self.assertEqual(inp['downloads'], [{'hash': 'h'}])
        self.assertEqual(inp['friends_now'][0]['slug'], 'st')
        self.assertIsInstance(inp['alert_settings'], dict)
        self.assertEqual(M.stage_key(hub), 'st')
        out = M.home_moments(inp, datetime.now(), stage_key=M.stage_key(hub))
        self.assertNotIn('st', {m['key'] for m in out})

    def test_proton_feature_off_gives_no_proton_input(self):
        from nexus import features
        with mock.patch.object(features, 'FEATURES', features.WINDOWS), \
                mock.patch.object(M, '_playing_now', return_value=[]):
            inp = M.moments_inputs(self.hub())
        self.assertIsNone(inp['proton'])
        out = M.home_moments(inp, datetime.now())
        self.assertNotIn('proton', {m['sub'] for m in out})

    def test_missing_parts_of_the_hub_are_empty_not_errors(self):
        hub = SimpleNamespace(_games=[], jobs=SimpleNamespace(jobs=[]))
        with mock.patch.object(M, '_playing_now', return_value=[]):
            inp = M.moments_inputs(hub)
        self.assertEqual(M.home_moments(inp, datetime.now()), [])
        self.assertIsNone(M.stage_key(hub))
        hub.dashboard = SimpleNamespace(stage=None)
        self.assertIsNone(M.stage_key(hub))
        hub.dealspage = SimpleNamespace(_data=lambda: {}, repacks=lambda: {})
        hub.protonpage = SimpleNamespace(info=None)
        with mock.patch.object(M, '_playing_now', return_value=[]):
            self.assertEqual(M.home_moments(M.moments_inputs(hub), datetime.now()), [])

    def test_never_refreshes_or_asks_the_cli(self):
        hub = self.hub()
        hub.dealspage.refresh = mock.Mock()
        hub.health.refresh = mock.Mock()
        with mock.patch.object(M, '_playing_now', return_value=[]):
            M.moments_inputs(hub)
        hub.dealspage.refresh.assert_not_called()
        hub.health.refresh.assert_not_called()
        src = (REPO / 'nexus' / 'gui' / 'moments.py').read_text()
        self.assertNotIn('cli_json', src)
        self.assertNotIn('PyQt6', src)


if __name__ == '__main__':
    unittest.main()
