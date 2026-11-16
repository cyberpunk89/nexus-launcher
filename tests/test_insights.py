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
from nexus import history, insights  # noqa: E402

TODAY = date(2026, 11, 16)


def sess(slug, when, minutes=60.0, name=None):
    """A session as history.load() returns it."""
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    elif not isinstance(when, datetime):
        when = datetime.combine(when, datetime.min.time()) + timedelta(hours=10)
    t = when
    return {'slug': slug, 'name': name or slug.title(), 'start': t.isoformat(timespec='seconds'),
            'minutes': float(minutes), '_start': t}


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
                                          'idle_year_bytes': 0})
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
