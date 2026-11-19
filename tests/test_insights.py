"""nexus/insights.py (w-138): the pure payload behind `nexus-cli --insights
--json`. Streaks over midnight and DST, the on-this-day window, the daily
pick, the backlog buckets, empty input, speed and no network."""

import json
import socket
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import history, insights, ledger, steamlog  # noqa: E402

TODAY = date(2026, 11, 16)


def sess(slug, when, minutes=60.0, name=None, source=None):
    """A session as history.load() returns it."""
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    elif not isinstance(when, datetime):
        when = datetime.combine(when, datetime.min.time()) + timedelta(hours=10)
    t = when
    out = {'slug': slug, 'name': name or slug.title(), 'start': t.isoformat(timespec='seconds'),
           'minutes': float(minutes), '_start': t}
    if source:
        out['source'] = source
    return out


def game(slug, minutes=0, days_ago=None, size=None, **kw):
    g = {'slug': slug, 'display_name': slug.title(), 'playtime_minutes': minutes,
         'last_played': (datetime.combine(TODAY, datetime.min.time()) - timedelta(days=days_ago)
                         ).isoformat() if days_ago is not None else None,
         'installed': True}
    if size is not None:
        g['disk_bytes'] = size
    g.update(kw)
    return g


def fact(main=None, release=None, modes=('single',)):
    return {'length': {'main': main} if main else None, 'modes': list(modes),
            'release': release}


def build(sessions=(), games=(), facts=None, today=TODAY):
    return insights.build(list(sessions), list(games), facts or {}, today)


class Streaks(unittest.TestCase):
    def test_session_over_midnight_counts_on_its_start_day(self):
        s = [sess('a', '2026-11-14T23:30:00', 120), sess('a', '2026-11-15T22:00:00', 30)]
        out = build(s)
        self.assertEqual(out['days'], {'2026-11-14': 120.0, '2026-11-15': 30.0})
        self.assertEqual(out['streak']['now'], 2)

    def test_streak_runs_through_dst_changes(self):
        for first in (date(2026, 3, 27), date(2026, 10, 23)):   # the EU changes on 03-29 and 10-25
            s = [sess('a', datetime.combine(first + timedelta(days=i), datetime.min.time())
                      + timedelta(hours=2, minutes=30)) for i in range(5)]
            out = build(s, today=first + timedelta(days=4))
            self.assertEqual(out['streak']['now'], 5, first)
            self.assertEqual(out['streak']['best'], 5, first)

    def test_aware_and_naive_timestamps_mix(self):
        aware = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        local = aware.astimezone().replace(tzinfo=None)
        s = [sess('a', aware, 30), sess('a', '2026-11-16T09:00:00', 40)]
        out = build(s)
        self.assertEqual(out['days'][local.date().isoformat()] > 0, True)
        self.assertEqual(sum(out['days'].values()), 70.0)

    def test_best_and_busiest_match_history_summary(self):
        s = [sess('a', '2026-01-05T10:00:00', 30), sess('a', '2026-01-06T10:00:00', 30),
             sess('a', '2026-01-07T10:00:00', 30), sess('b', '2026-03-10T10:00:00', 500),
             sess('a', '2025-12-01T10:00:00', 999)]
        out = build(s)
        year = history.summary(s, TODAY)['year']
        self.assertEqual(out['streak']['best'], year['best_streak'])
        self.assertEqual(out['busiest_day'], year['busiest_day'])
        self.assertEqual(out['year_minutes'], year['minutes'])

    def test_days_window_is_371_days(self):
        s = [sess('a', TODAY - timedelta(days=370), 5), sess('a', TODAY - timedelta(days=371), 5)]
        self.assertEqual(list(build(s)['days']), [(TODAY - timedelta(days=370)).isoformat()])


def at(y, m, d, slug='a', minutes=60):
    return sess(slug, datetime(y, m, d, 20, 0), minutes)


class OnThisDay(unittest.TestCase):
    def test_window_three_days_in_earlier_years(self):
        s = [at(2025, 11, 13, 'in3'), at(2025, 11, 19, 'in3b'), at(2025, 11, 12, 'out'),
             at(2025, 11, 20, 'out2'), at(2024, 11, 16, 'exact')]
        got = {e['slug']: e for e in build(s)['on_this_day']}
        self.assertEqual(set(got), {'in3', 'in3b', 'exact'})
        self.assertEqual(got['in3']['years_ago'], 1)
        self.assertEqual(got['exact']['years_ago'], 2)
        self.assertEqual(got['in3']['date'], '2025-11-13')

    def test_top_three_by_minutes(self):
        s = [at(2025, 11, 16, f'g{i}', minutes=10 * (i + 1)) for i in range(5)]
        self.assertEqual([e['slug'] for e in build(s)['on_this_day']], ['g4', 'g3', 'g2'])

    def test_feb_29_on_a_common_year(self):
        s = [at(2025, 2, 28, 'x'), at(2025, 3, 3, 'y'), at(2025, 3, 4, 'z')]
        got = {e['slug'] for e in build(s, today=date(2028, 2, 29))['on_this_day']}
        self.assertEqual(got, {'x', 'y'})                # Feb 28 +- 3 days, not Mar 4

    def test_this_year_never_counts(self):
        s = [at(2026, 11, 15, 'now'), at(2026, 11, 16, 'now2')]
        self.assertEqual(build(s)['on_this_day'], [])

    def test_around_new_year(self):
        s = [at(2024, 12, 30, 'dec'), at(2025, 12, 30, 'last-week')]
        got = build(s, today=date(2026, 1, 1))['on_this_day']
        self.assertEqual([(e['slug'], e['years_ago']) for e in got], [('dec', 1)])


class Daily(unittest.TestCase):
    GAMES = [game(f'game-{i:02d}') for i in range(40)]

    def test_same_pick_all_day(self):
        a = build(games=self.GAMES)
        b = build(games=list(reversed(self.GAMES)))
        self.assertEqual(a['daily'], b['daily'])
        self.assertEqual(a['daily']['reason'], 'pick')
        self.assertIsNone(a['daily']['years'])
        self.assertNotEqual(a['daily']['slug'], a['daily_alt']['slug'])

    def test_next_day_differs(self):
        picks = {build(games=self.GAMES, today=TODAY + timedelta(days=i))['daily']['slug']
                 for i in range(5)}
        self.assertGreater(len(picks), 1)

    def test_release_anniversary_wins(self):
        facts = {'game-07': fact(release='2020-11-16'), 'game-08': fact(release='2020-11')}
        out = build(games=self.GAMES, facts=facts)
        self.assertEqual(out['daily'], {'slug': 'game-07', 'reason': 'released', 'years': 6})

    def test_first_played_anniversary(self):
        out = build([sess('game-03', '2024-11-16T10:00:00')], self.GAMES)
        self.assertEqual(out['daily'], {'slug': 'game-03', 'reason': 'first_played', 'years': 2})

    def test_no_games(self):
        out = build()
        self.assertIsNone(out['daily'])
        self.assertIsNone(out['daily_alt'])


class Backlog(unittest.TestCase):
    def test_buckets_follow_smart_progress(self):
        games = [game('done', 600, 100), game('playing', 60, 3), game('started', 120, 60),
                 game('never'), game('nofact', 90, 40)]
        facts = {'done': fact(10), 'playing': fact(10), 'started': fact(10), 'never': fact(10)}
        b = build(games=games, facts=facts)['backlog']
        self.assertEqual((b['done'], b['playing'], b['started'], b['never']), (1, 1, 2, 1))
        self.assertEqual(b['main_hours_left'], 9.0 + 8.0 + 10.0)

    def test_pace_is_the_last_28_days(self):
        s = [sess('a', TODAY - timedelta(days=2), 120), sess('a', TODAY - timedelta(days=40), 999)]
        self.assertEqual(build(s)['backlog']['pace_hours_week'], 0.5)

    def test_idle_year_bytes(self):
        games = [game('old', 60, 400, size=5000), game('new', 60, 10, size=7000),
                 game('never', size=9000), game('gone', 60, 500, size=3000, installed=False)]
        self.assertEqual(build(games=games)['backlog']['idle_year_bytes'], 5000)

    def test_shortest_unplayed_installed(self):
        games = [game('b', 0), game('a', 0), game('c', 0), game('d', 0), game('played', 60, 5),
                 game('gone', 0, installed=False), game('nofact', 0)]
        facts = {'b': fact(5), 'a': fact(5), 'c': fact(2), 'd': fact(30), 'played': fact(1),
                 'gone': fact(1)}
        sh = build(games=games, facts=facts)['backlog']['shortest']
        self.assertEqual([(r['slug'], r['main_hours']) for r in sh], [('c', 2.0), ('a', 5.0), ('b', 5.0)])
        self.assertEqual(sh[0]['name'], 'C')
        self.assertEqual(build(games=[game('x')], facts={})['backlog']['shortest'], [])
        games = [game('z1', 0, dup_of='a'), game('z2', 0, hidden=True), game('z3', 0, dup_of='a'),
                 game('z4', 0, dup_of='a'), game('ok', 0)]
        facts = {k: fact(1) for k in ('z1', 'z2', 'z3', 'z4')} | {'ok': fact(9)}
        self.assertEqual([r['slug'] for r in build(games=games, facts=facts)['backlog']['shortest']],
                         ['ok'])                      # copies never crowd the row out


class Space(unittest.TestCase):
    def test_top_12_by_bytes(self):
        games = [game(f'g{i:02d}', 60, 5 if i % 2 else 400, size=(i + 1) * 100) for i in range(20)]
        games.append(game('zero', size=0))
        sp = build(games=games)['space']
        self.assertEqual(len(sp), 12)
        self.assertEqual([e['bytes'] for e in sp], [(i + 1) * 100 for i in range(19, 7, -1)])
        self.assertEqual(sp[0], {'slug': 'g19', 'bytes': 2000, 'idle_days': 5})
        self.assertEqual(sp[1]['idle_days'], 400)

    def test_never_played_has_no_idle_days(self):
        self.assertIsNone(build(games=[game('n', size=10)])['space'][0]['idle_days'])


class SpaceMore(unittest.TestCase):
    def test_more_counts_the_rest(self):
        games = [game(f'g{i:02d}', 60, 5, size=(i + 1) * 100) for i in range(15)]
        out = build(games=games)
        self.assertEqual(len(out['space']), 12)
        self.assertEqual(out['space_more'], {'n': 3, 'bytes': 100 + 200 + 300})

    def test_none_past_twelve_is_zero(self):
        out = build(games=[game('a', size=10)])
        self.assertEqual(out['space_more'], {'n': 0, 'bytes': 0})


def fact_row(year=None, genres=(), developers=(), main=None):
    f = fact(main)
    f.update({'year': year, 'genres': list(genres), 'developers': list(developers)})
    return f


class Facts(unittest.TestCase):
    def test_each_kind_from_local_data(self):
        games = [game('alpha', 600, 3, size=500), game('beta', 120, 50, size=9000),
                 game('gamma', size=100), game('delta', size=200)]
        facts = {'alpha': fact_row(2010, ['RPG'], ['Studio X'], 40),
                 'beta': fact_row(2001, ['RPG'], ['Studio X'], 12),
                 'gamma': fact_row(2020, ['Action'], ['Studio Y'], 3),
                 'delta': fact_row(2015, [], [], 8)}
        rows = {f['kind']: f for f in build(games=games, facts=facts)['facts']}
        self.assertEqual(list(rows), ['library', 'top_genre', 'most_played', 'oldest',
                                      'shortest_unplayed', 'biggest', 'top_studio'])
        self.assertEqual((rows['library']['n'], rows['library']['hours']), (4, 12.0))
        self.assertEqual((rows['top_genre']['name'], rows['top_genre']['n']), ('RPG', 2))
        self.assertEqual((rows['most_played']['slug'], rows['most_played']['n']), ('alpha', 10.0))
        self.assertEqual((rows['oldest']['slug'], rows['oldest']['n']), ('beta', 2001))
        self.assertEqual((rows['shortest_unplayed']['slug'], rows['shortest_unplayed']['n']),
                         ('gamma', 3.0))
        self.assertEqual((rows['biggest']['slug'], rows['biggest']['n']), ('beta', 9000))
        self.assertEqual((rows['top_studio']['name'], rows['top_studio']['n']), ('Studio X', 2))
        self.assertIsNone(rows['top_studio']['slug'])
        json.dumps(rows)

    def test_a_kind_without_data_is_left_out(self):
        out = build(games=[game('a', 60, 1)], facts={})['facts']
        self.assertEqual([f['kind'] for f in out], ['library', 'most_played'])
        self.assertEqual(build()['facts'], [])


class Hidden(unittest.TestCase):
    def test_hidden_games_leave_backlog_space_and_facts(self):
        games = [game('keep', 60, 400, size=100), game('gone', 600, 400, size=9000)]
        facts = {'keep': fact_row(2010, main=10), 'gone': fact_row(1999, main=10)}
        out = insights.build([], games, facts, TODAY, hidden={'gone'})
        self.assertEqual([e['slug'] for e in out['space']], ['keep'])
        self.assertEqual(out['backlog']['idle_year_bytes'], 100)
        self.assertEqual(out['backlog']['started'], 1)
        by = {f['kind']: f for f in out['facts']}
        self.assertEqual(by['library']['n'], 1)
        self.assertEqual(by['oldest']['slug'], 'keep')
        self.assertEqual(by['biggest']['slug'], 'keep')

    def test_reads_prefs_file_and_survives_bad_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'library.json'
            self.assertEqual(insights.hidden_slugs(p), frozenset())
            p.write_text('{nope', encoding='utf-8')
            self.assertEqual(insights.hidden_slugs(p), frozenset())
            p.write_text('{"hidden": ["a", "b", 3]}', encoding='utf-8')
            self.assertEqual(insights.hidden_slugs(p), frozenset({'a', 'b'}))
            p.write_text('[1]', encoding='utf-8')
            self.assertEqual(insights.hidden_slugs(p), frozenset())


class Merged(unittest.TestCase):
    """w-150: sessions.jsonl plus Steam's game log, merged when read."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for mod, name, path in ((history, 'PATH', d / 'sessions.jsonl'),
                                (steamlog, 'PATH', d / 'steam-log.jsonl'),
                                (ledger, 'PATH', d / 'playtime-ledger.jsonl')):
            p = mock.patch.object(mod, name, path)
            p.start()
            self.addCleanup(p.stop)

    def nexus(self, slug, start, minutes, source='steam'):
        t = datetime.fromisoformat(start)
        with open(history.PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'slug': slug, 'source': source, 'name': slug,
                                'start': start, 'minutes': minutes,
                                'end': (t + timedelta(minutes=minutes)).isoformat()}) + '\n')

    def log(self, slug, start, minutes, appid=620):
        t = datetime.fromisoformat(start)
        with open(steamlog.PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'appid': appid, 'slug': slug, 'name': slug, 'start': start,
                                'end': (t + timedelta(minutes=minutes)).isoformat(),
                                'minutes': float(minutes), 'source': 'steam',
                                'via': 'steam-log'}) + '\n')

    def test_log_only_session_is_added(self):
        self.log('steam-620', '2026-11-10T20:00:00', 90)
        got = history.all_sessions([])
        self.assertEqual([(s['slug'], s['minutes']) for s in got], [('steam-620', 90.0)])

    def test_overlap_nexus_record_wins(self):
        self.nexus('steam-620', '2026-11-10T20:00:30', 80)
        self.log('steam-620', '2026-11-10T20:05:00', 90)          # starts after 120 s but overlaps
        self.log('steam-620', '2026-11-10T22:00:00', 30)          # a different session: kept
        got = history.all_sessions([])
        self.assertEqual(sorted((s['start'], s['minutes']) for s in got),
                         [('2026-11-10T20:00:30', 80), ('2026-11-10T22:00:00', 30.0)])

    def test_start_within_120s_dropped(self):
        self.nexus('steam-620', '2026-11-10T20:00:00', 30)
        self.log('steam-620', '2026-11-10T19:58:30', 1.5)         # ends as it starts, 90 s before
        self.assertEqual(len(history.all_sessions([])), 1)

    def test_other_game_overlapping_is_kept(self):
        self.nexus('steam-620', '2026-11-10T20:00:00', 60)
        self.log('steam-70', '2026-11-10T20:10:00', 30, appid=70)
        self.assertEqual(len(history.all_sessions([])), 2)

    def test_steam_log_off_hides_and_keeps_file(self):
        self.nexus('a', '2026-11-10T20:00:00', 10)
        self.log('steam-620', '2026-11-11T20:00:00', 90)
        before = steamlog.PATH.read_text(encoding='utf-8')
        got = history.all_sessions([], steam_log=False)
        self.assertEqual([s['slug'] for s in got], ['a'])
        self.assertEqual(steamlog.PATH.read_text(encoding='utf-8'), before)

    def test_dedupe_same_appid_start(self):
        self.log('steam-620', '2026-11-10T20:00:00', 60)
        self.log('steam-620', '2026-11-10T20:00:00', 60)
        self.assertEqual(len(history.all_sessions([])), 1)

    def test_is_real_and_no_ledger_file_adds_nothing(self):
        self.nexus('a', '2026-11-10T20:00:00', 10)
        self.assertEqual(len(history.all_sessions([], ledger=True)),
                         len(history.all_sessions([], ledger=False)))
        self.assertTrue(history.is_real({'slug': 'a'}))
        self.assertFalse(history.is_real({'slug': 'a', 'kind': 'estimate'}))

    def test_merged_numbers_reach_history_and_insights(self):
        d = TODAY - timedelta(days=2)
        self.log('steam-620', f'{d.isoformat()}T20:00:00', 90)
        ss = history.all_sessions([])
        self.assertEqual(history.summary(ss, TODAY)['year']['minutes'], 90.0)
        det = build(sessions=ss)['day_games'][d.isoformat()]
        self.assertEqual(det['games'][0]['via'], 'steam-log')

    def test_via_only_when_most_minutes_come_from_the_log(self):
        d = TODAY - timedelta(days=2)
        ss = [dict(sess('a', d, 10), via='steam-log'), sess('a', d, 60), sess('b', d, 30)]
        by = {g['slug']: g for g in build(sessions=ss)['day_games'][d.isoformat()]['games']}
        self.assertNotIn('via', by['a'])
        self.assertNotIn('via', by['b'])

    def ledger_lines(self, slug, *rows, source='heroic'):
        """rows: (iso time, total, last_played)"""
        with open(ledger.PATH, 'a', encoding='utf-8') as f:
            for at, total, lp in rows:
                f.write(json.dumps({'date': at[:10], 'at': at, 'slug': slug, 'name': slug.title(),
                                    'source': source, 'total': total, 'last_played': lp}) + '\n')

    def estimate(self, d, minutes, slug='hades'):
        return {'slug': slug, 'name': slug.title(), 'kind': 'estimate', 'source': 'heroic',
                'start': f'{d.isoformat()}T00:00:00', 'minutes': float(minutes),
                '_start': datetime.combine(d, datetime.min.time())}

    def test_ledger_estimates_are_read_and_switchable(self):
        d = TODAY - timedelta(days=3)
        self.ledger_lines('hades', (f'{(d - timedelta(days=2)).isoformat()}T09:00:00', 100, None),
                          (f'{TODAY.isoformat()}T09:00:00', 160, f'{d.isoformat()}T21:00:00'))
        got = history.all_sessions([])
        self.assertEqual([(s['slug'], s['minutes'], s['kind']) for s in got],
                         [('hades', 60.0, 'estimate')])
        self.assertEqual(history.all_sessions([], ledger=False), [])
        self.assertEqual(history.all_sessions([], steam_log=False)[0]['start'], f'{d.isoformat()}T00:00:00')

    def test_estimates_count_minutes_and_streaks_only(self):
        d1, d2, d3 = (TODAY - timedelta(days=n) for n in (4, 3, 2))
        ss = [sess('tunic', d1, 30), self.estimate(d2, 90), self.estimate(d3, 20)]
        sm = history.summary(ss, TODAY)
        y = sm['year']
        self.assertEqual((y['minutes'], y['sessions'], y['games'], y['days'], y['best_streak']),
                         (140.0, 1, 2, 3, 3))
        self.assertEqual(y['longest']['slug'], 'tunic')           # not the 90-minute estimate
        self.assertEqual([r['slug'] for r in sm['recent']], ['tunic'])
        self.assertEqual(sm['by_game'], {'tunic': {'sessions': 1}})
        self.assertEqual(sm['since'], d1.isoformat())
        self.assertEqual(sum(w['minutes'] for w in sm['weeks']), 140.0)
        p = insights.build(ss, [], {}, TODAY)
        self.assertEqual(p['streak']['now'], 0)    # nothing today or yesterday
        self.assertEqual(p['days'][d2.isoformat()], 90.0)
        self.assertEqual(p['year_minutes'], 140.0)
        self.assertEqual(p['day_games'][d1.isoformat()]['n'], 1)
        self.assertEqual(p['day_games'][d2.isoformat()]['n'], 0)

    def test_three_views_agree_on_a_day(self):
        """w-152: Your year (insights days), Your week and Play history's
        month (history periods) read one merged list, so a day's total is the
        same in all three; session counts leave the estimate out."""
        d = TODAY.isoformat()                         # a Monday: in this week and month
        self.nexus('disco', f'{d}T10:00:00', 40)
        self.log('steam-620', f'{d}T14:00:00', 30)
        merged = history.all_sessions([], ledger=False) + [self.estimate(TODAY, 20)]
        year = insights.build(merged, [], {}, TODAY)['days'][d]
        per = history.summary(merged, TODAY)['periods']
        week = {x['date']: x['minutes'] for x in per['week']['days']}[d]
        month = {x['date']: x['minutes'] for x in per['month']['days']}[d]
        self.assertEqual((year, week, month), (90.0, 90.0, 90.0))
        self.assertEqual(per['week']['sessions'], 2)
        self.assertEqual(history.summary(merged, TODAY)['year']['sessions'], 2)

    def test_includes_says_which_sources_are_in(self):
        self.nexus('disco', '2026-11-10T10:00:00', 40)
        self.assertEqual(history.includes(history.all_sessions([])),
                         {'steam_log': False, 'estimates': False})
        self.log('steam-620', '2026-11-12T14:00:00', 30)
        merged = history.all_sessions([]) + [self.estimate(TODAY, 20)]
        self.assertEqual(history.includes(merged), {'steam_log': True, 'estimates': True})

    def test_estimates_extend_the_current_streak(self):
        ss = [sess('a', TODAY - timedelta(days=2), 10), self.estimate(TODAY - timedelta(days=1), 30)]
        self.assertEqual(insights.build(ss, [], {}, TODAY)['streak']['now'], 2)
        self.assertEqual(history.current_streak(ss, TODAY), 2)

    def test_periods_count_only_real_sessions(self):
        per = history.periods([sess('a', TODAY, 10), self.estimate(TODAY, 50)], TODAY)
        self.assertEqual((per['week']['minutes'], per['week']['sessions']), (60.0, 1))
        self.assertEqual((per['month']['minutes'], per['month']['sessions']), (60.0, 1))
        self.assertEqual(per['current_streak'], 1)

    def test_estimate_only_day_has_no_times(self):
        d = TODAY - timedelta(days=2)
        det = insights.build([self.estimate(d, 45)], [], {}, TODAY)['day_games'][d.isoformat()]
        self.assertEqual((det['n'], det['first'], det['last']), (0, None, None))
        g = det['games'][0]
        self.assertEqual((g['source'], g['kind'], g['minutes']), ('estimated', 'estimate', 45.0))

    def test_mixed_day_keeps_times_from_real_sessions_and_marks_only_pure_estimates(self):
        d = TODAY - timedelta(days=2)
        det = insights.build([sess('tunic', d, 30), self.estimate(d, 45), sess('hades', d, 10)],
                             [], {}, TODAY)['day_games'][d.isoformat()]
        self.assertEqual((det['n'], det['first'], det['last']), (2, '10:00', '10:00'))
        by = {g['slug']: g for g in det['games']}
        self.assertEqual(by['hades']['source'], 'estimated')       # 45 of 55 minutes
        self.assertNotIn('kind', by['hades'])                      # not all of it
        self.assertNotIn('kind', by['tunic'])


class DayDetail(unittest.TestCase):
    """The heatmap's hover: what was played on each day."""

    def test_top_games_per_day_with_session_count_and_rest(self):
        d = TODAY - timedelta(days=2)
        ss = [sess('a', d, 90), sess('b', d, 30), sess('a', d, 20),
              sess('c', d, 10), sess('d', d, 5)]
        det = build(sessions=ss)['day_games'][d.isoformat()]
        self.assertEqual(det['n'], 5)
        self.assertEqual([(g['slug'], g['name'], g['minutes']) for g in det['games']],
                         [('a', 'A', 110.0), ('b', 'B', 30.0), ('c', 'C', 10.0), ('d', 'D', 5.0)])
        self.assertEqual(det['more'], 0)
        ss.append(sess('e', d, 1))
        det = build(sessions=ss)['day_games'][d.isoformat()]
        self.assertEqual(len(det['games']), 4)
        self.assertEqual(det['more'], 1)

    def test_only_played_days_inside_the_window(self):
        old = TODAY - timedelta(days=insights.DAYS + 5)
        got = build(sessions=[sess('a', TODAY, 10), sess('a', old, 10)])['day_games']
        self.assertEqual(list(got), [TODAY.isoformat()])
        self.assertEqual(build()['day_games'], {})


class DayDetailRich(unittest.TestCase):
    """w-147: first/last start, source per game, average day, streak position."""

    def test_first_and_last_session_start_local_hhmm(self):
        d = TODAY - timedelta(days=1)
        ss = [sess('a', datetime.combine(d, datetime.min.time()) + timedelta(hours=19, minutes=5), 30),
              sess('b', datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=2), 30),
              sess('a', datetime.combine(d, datetime.min.time()) + timedelta(hours=23, minutes=10), 30)]
        det = build(sessions=ss)['day_games'][d.isoformat()]
        self.assertEqual((det['first'], det['last']), ('09:02', '23:10'))

    def test_day_across_midnight_counts_on_its_start_date(self):
        d = TODAY - timedelta(days=3)
        ss = [sess('a', datetime.combine(d, datetime.min.time()) + timedelta(hours=23, minutes=40), 120)]
        got = build(sessions=ss)['day_games']
        self.assertEqual(list(got), [d.isoformat()])
        self.assertEqual((got[d.isoformat()]['first'], got[d.isoformat()]['last']), ('23:40', '23:40'))

    def test_aware_start_shown_in_local_time(self):
        d = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
        s = {'slug': 'a', 'name': 'A', 'start': d.isoformat(), 'minutes': 10.0}
        det = build(sessions=[s])['day_games']
        local = d.astimezone().replace(tzinfo=None)
        self.assertEqual(det[local.date().isoformat()]['first'], local.strftime('%H:%M'))

    def test_source_per_game_is_the_one_with_most_minutes(self):
        d = TODAY - timedelta(days=1)
        ss = [sess('a', d, 10, source='nexus'), sess('a', d, 50, source='steam'),
              sess('b', d, 5), sess('c', d, 7, source='heroic')]
        det = build(sessions=ss)['day_games'][d.isoformat()]
        self.assertEqual({g['slug']: g['source'] for g in det['games']},
                         {'a': 'steam', 'b': 'nexus', 'c': 'heroic'})

    def test_average_is_over_played_days_of_the_last_90(self):
        ss = [sess('a', TODAY, 60), sess('a', TODAY - timedelta(days=10), 120),
              sess('a', TODAY - timedelta(days=89), 90),
              sess('a', TODAY - timedelta(days=90), 9999)]        # outside
        self.assertEqual(build(ss)['avg_minutes'], 90.0)
        self.assertEqual(build()['avg_minutes'], 0.0)

    def test_streak_pos_inside_a_run_else_null(self):
        run = [TODAY - timedelta(days=i) for i in (5, 4, 3)]
        ss = [sess('a', d, 30) for d in run] + [sess('a', TODAY - timedelta(days=1), 30)]
        got = build(ss)['day_games']
        self.assertEqual([got[d.isoformat()]['streak_pos'] for d in run],
                         [{'day': 1, 'of': 3}, {'day': 2, 'of': 3}, {'day': 3, 'of': 3}])
        self.assertIsNone(got[(TODAY - timedelta(days=1)).isoformat()]['streak_pos'])

    def test_streak_counts_days_before_the_window(self):
        first = TODAY - timedelta(days=insights.DAYS - 1)
        ss = [sess('a', first - timedelta(days=1), 30), sess('a', first, 30)]
        got = build(ss)['day_games']
        self.assertEqual(got[first.isoformat()]['streak_pos'], {'day': 2, 'of': 2})

    def test_old_keys_and_version_unchanged(self):
        out = build([sess('a', TODAY, 10)])
        self.assertEqual(out['v'], 1)
        for k in ('days', 'day_games', 'streak', 'busiest_day', 'year_minutes'):
            self.assertIn(k, out)
        det = out['day_games'][TODAY.isoformat()]
        for k in ('n', 'games', 'more'):
            self.assertIn(k, det)
        json.dumps(out)


class Milestones(unittest.TestCase):
    """w-140: the closest round numbers, each with the game it is about."""

    def test_hours_in_a_game_close_to_a_round_number(self):
        games = [game('near', 97 * 60), game('far', 30 * 60), game('over', 101 * 60)]
        ms = build(games=games)['milestones']
        near = next(m for m in ms if m.get('slug') == 'near')
        self.assertEqual((near['kind'], near['have'], near['goal']), ('hours', 97.0, 100))
        self.assertEqual(near['name'], 'Near')
        slugs = [m.get('slug') for m in ms]
        self.assertNotIn('far', slugs)                      # 30 of 50 h is not close
        self.assertNotIn('over', slugs)                     # 101 of 200 h neither

    def test_closest_first_and_one_per_game(self):
        games = [game('a', 85 * 60), game('b', 99 * 60), game('c', 48 * 60)]
        ms = [m for m in build(games=games)['milestones'] if m['kind'] == 'hours']
        self.assertEqual([m['slug'] for m in ms], ['b', 'c', 'a'])
        self.assertEqual(len({m['slug'] for m in ms}), len(ms))

    def test_games_finished(self):
        games = [game(f'd{i}', 600, 100) for i in range(9)]
        facts = {g['slug']: fact(10) for g in games}
        ms = build(games=games, facts=facts)['milestones']
        fin = next(m for m in ms if m['kind'] == 'finished')
        self.assertEqual((fin['have'], fin['goal']), (9, 10))
        self.assertIsNone(fin['slug'])

    def test_year_hours(self):
        s = [sess('a', TODAY - timedelta(days=i), 60) for i in range(1, 9)]
        ms = build(s)['milestones']
        yr = next(m for m in ms if m['kind'] == 'year')
        self.assertEqual((yr['have'], yr['goal']), (8.0, 10))

    def test_nothing_close_is_an_empty_list(self):
        self.assertEqual(build()['milestones'], [])
        self.assertEqual(build(games=[game('x', 5 * 60)])['milestones'], [])

    def test_at_most_four(self):
        games = [game(f'g{i}', (90 + i) * 60) for i in range(9)]
        self.assertEqual(len(build(games=games)['milestones']), insights.MILESTONES_TOP)


class Empty(unittest.TestCase):
    def test_no_sessions_no_library(self):
        out = build()
        self.assertEqual(out['v'], 1)
        self.assertEqual(out['today'], '2026-11-16')
        self.assertEqual(out['days'], {})
        self.assertEqual(out['streak'], {'now': 0, 'best': 0})
        self.assertIsNone(out['busiest_day'])
        self.assertEqual((out['year_minutes'], out['on_this_day'], out['space']), (0, [], []))
        self.assertEqual(out['backlog'], {'done': 0, 'playing': 0, 'started': 0, 'never': 0,
                                          'main_hours_left': 0, 'pace_hours_week': 0,
                                          'idle_year_bytes': 0, 'shortest': []})
        json.dumps(out)


class Speed(unittest.TestCase):
    def test_20k_sessions_linear(self):
        base = datetime(2022, 1, 1, 10)
        s = [sess(f'g{i % 300}', base + timedelta(hours=i * 1.5), 45) for i in range(20000)]
        games = [game(f'g{i}', 60, i % 400, size=i) for i in range(3000)]
        t = time.perf_counter()
        build(s, games, {}, date(2026, 11, 16))
        self.assertLess(time.perf_counter() - t, 1.0)


class NoNetwork(unittest.TestCase):
    def test_build_opens_no_socket(self):
        with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network')):
            build([at(2025, 11, 16)], [game('a', 5, 3, size=1)], {'a': fact(5)})


if __name__ == '__main__':
    unittest.main()
