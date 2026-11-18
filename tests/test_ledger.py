"""nexus/ledger.py (w-151): a daily note of each game's play-time total, and
the estimates that fill days Nexus missed. No real home, Steam or network."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)

from unittest import mock  # noqa: E402

from nexus import history, ledger, steamlib  # noqa: E402
from tests.patching import patch_cli  # noqa: E402

T0 = datetime(2026, 11, 10, 9, 0, 0)


def game(slug, minutes, source='heroic', last_played=None, name=None):
    return {'slug': slug, 'display_name': name or slug.title(), 'source': source,
            'playtime_minutes': minutes, 'last_played': last_played}


def line(slug, at, total, last_played=None, source='heroic'):
    return {'slug': slug, 'name': slug.title(), 'source': source, 'total': total,
            'at': at.isoformat(timespec='seconds'), 'date': at.date().isoformat(),
            'last_played': last_played, '_at': at}


def real(slug, start, minutes):
    return {'slug': slug, 'start': start.isoformat(), '_start': start, 'minutes': float(minutes)}


class Snapshot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'playtime-ledger.jsonl'

    def lines(self):
        return [json.loads(x) for x in self.path.read_text(encoding='utf-8').splitlines()]

    def test_first_scan_is_baseline_only(self):
        n = ledger.snapshot([game('hades', 600), game('tunic', 90)], self.path, T0)
        self.assertEqual(n, 2)
        got = self.lines()
        self.assertEqual({(x['slug'], x['total'], x['date']) for x in got},
                         {('hades', 600, '2026-11-10'), ('tunic', 90, '2026-11-10')})
        self.assertEqual(ledger.estimates(ledger.load(self.path), []), [])

    def test_appends_only_changed_totals(self):
        ledger.snapshot([game('hades', 600), game('tunic', 90)], self.path, T0)
        n = ledger.snapshot([game('hades', 600), game('tunic', 130, last_played='2026-11-11T20:00:00')],
                            self.path, T0 + timedelta(days=1))
        self.assertEqual(n, 1)
        last = self.lines()[-1]
        self.assertEqual((last['slug'], last['total'], last['source'], last['last_played']),
                         ('tunic', 130, 'heroic', '2026-11-11T20:00:00'))
        self.assertEqual(len(self.lines()), 3)

    def test_unusable_totals_are_skipped(self):
        n = ledger.snapshot([game('epic-one', None, source='epic'), game('b', True),
                             game('c', '12'), {'playtime_minutes': 5}], self.path, T0)
        self.assertEqual(n, 0)
        self.assertFalse(self.path.exists())


class Estimates(unittest.TestCase):
    def est(self, lines, sessions=()):
        return ledger.estimates(lines, list(sessions))

    def test_30_min_change_no_session_on_last_played_day(self):
        got = self.est([line('hades', T0, 600),
                        line('hades', T0 + timedelta(days=3), 630,
                             last_played='2026-11-11T20:00:00')])
        self.assertEqual(len(got), 1)
        e = got[0]
        self.assertEqual((e['slug'], e['minutes'], e['kind'], e['source'], e['name']),
                         ('hades', 30.0, 'estimate', 'heroic', 'Hades'))
        self.assertEqual(e['start'], '2026-11-11T00:00:00')
        self.assertEqual(e['_start'], datetime(2026, 11, 11))
        self.assertFalse(history.is_real(e))

    def test_change_explained_by_sessions_adds_nothing(self):
        lines = [line('hades', T0, 600), line('hades', T0 + timedelta(days=1), 660)]
        self.assertEqual(self.est(lines, [real('hades', T0 + timedelta(hours=5), 60)]), [])
        # a session of another game, or one ending outside the window, explains nothing
        got = self.est(lines, [real('tunic', T0 + timedelta(hours=5), 60),
                               real('hades', T0 - timedelta(hours=5), 60)])
        self.assertEqual([e['minutes'] for e in got], [60.0])

    def test_partly_explained_keeps_the_rest(self):
        lines = [line('hades', T0, 600), line('hades', T0 + timedelta(days=1), 660)]
        got = self.est(lines, [real('hades', T0 + timedelta(hours=5), 40)])
        self.assertEqual([e['minutes'] for e in got], [20.0])

    def test_remainder_under_five_minutes_is_dropped(self):
        lines = [line('hades', T0, 600), line('hades', T0 + timedelta(days=1), 660)]
        self.assertEqual(self.est(lines, [real('hades', T0 + timedelta(hours=5), 57.5)]), [])

    def test_falls_back_to_snapshot_date(self):
        got = self.est([line('hades', T0, 600),
                        line('hades', T0 + timedelta(days=3), 630, last_played='2026-11-01T20:00:00')])
        self.assertEqual(got[0]['start'], '2026-11-13T00:00:00')
        got = self.est([line('hades', T0, 600), line('hades', T0 + timedelta(days=3), 630)])
        self.assertEqual(got[0]['start'], '2026-11-13T00:00:00')

    def test_decrease_resets_baseline(self):
        got = self.est([line('hades', T0, 600),
                        line('hades', T0 + timedelta(days=1), 100),         # reinstall: a new baseline
                        line('hades', T0 + timedelta(days=2), 130)])
        self.assertEqual([(e['minutes'], e['start']) for e in got], [(30.0, '2026-11-12T00:00:00')])

    def test_delta_larger_than_elapsed_dropped(self):
        got = self.est([line('hades', T0, 600),
                        line('hades', T0 + timedelta(minutes=60), 900)])     # 300 min in an hour
        self.assertEqual(got, [])

    def test_heroic_covered(self):
        got = self.est([line('a', T0, 10, source='steam'), line('a', T0 + timedelta(days=1), 40, source='steam'),
                        line('b', T0, 0, source='heroic'), line('b', T0 + timedelta(days=1), 25, source='heroic')])
        self.assertEqual(sorted((e['slug'], e['source'], e['minutes']) for e in got),
                         [('a', 'steam', 30.0), ('b', 'heroic', 25.0)])

    def test_tz_aware_and_bad_lines_dont_break_estimates(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / 'l.jsonl'
        rows = [{'slug': 'a', 'name': 'A', 'source': 'heroic', 'total': 10,
                 'at': '2026-11-10T09:00:00', 'last_played': None},
                {'slug': 'a', 'name': 'A', 'source': 'heroic', 'total': 50,
                 'at': '2026-11-12T09:00:00+02:00', 'last_played': '2026-11-11T20:00:00Z'},
                {'slug': 'a', 'total': 60, 'at': 'garbage'},
                {'slug': 'a', 'total': 70, 'at': None}]
        p.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
        loaded = ledger.load(p)
        self.assertEqual(len(loaded), 2)
        self.assertTrue(all(x['_at'].tzinfo is None for x in loaded))
        got = ledger.estimates(loaded, [])
        self.assertEqual([e['minutes'] for e in got], [40.0])
        history_sessions = got + [real('a', T0, 5)]
        self.assertTrue(history_sessions)

    def test_launched_by_nexus_notes_only_the_external_part(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / 'l.jsonl'
        g = dict(game('nx', 150, source='nexus'), external_minutes=30)
        ledger.snapshot([g], p, T0)
        self.assertEqual(ledger.load(p)[0]['total'], 30)

    def test_unordered_lines_and_load_skip_torn(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / 'l.jsonl'
        p.write_text('{"torn\n' + json.dumps({k: v for k, v in line('a', T0, 5).items() if k != '_at'})
                     + '\nnot json\n', encoding='utf-8')
        self.assertEqual(len(ledger.load(p)), 1)
        self.assertEqual(ledger.load(Path(tmp.name) / 'missing'), [])


class Wiring(unittest.TestCase):
    """The library scan snapshots (both editions, behind [history] ledger),
    and history/insights read the estimates with the same switch."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.base = d / 'base'
        stack = [mock.patch.object(ledger, 'PATH', d / 'playtime-ledger.jsonl'),
                 mock.patch.object(history, 'PATH', d / 'sessions.jsonl'),
                 mock.patch.object(steamlib, 'scan', lambda *a, **k: [
                     dict(game('portal', 600, source='steam', last_played='2026-11-01T20:00:00'),
                          appid=620, installed=True)])]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def library(self, on):
        from nexus.cli import manage
        with patch_cli('load_config', return_value={'ledger': on, 'import_steam_log': False}), \
                contextlib.redirect_stdout(io.StringIO()):
            manage.cmd_library(self.base, True)

    def test_cmd_library_snapshots_when_on(self):
        self.library(True)
        got = ledger.load()
        self.assertEqual([(x['slug'], x['total'], x['source']) for x in got],
                         [('portal', 600, 'steam')])
        self.library(True)                                   # nothing changed: nothing appended
        self.assertEqual(len(ledger.load()), 1)

    def test_cmd_library_skips_when_setting_off(self):
        self.library(False)
        self.assertFalse(ledger.PATH.exists())

    def test_cmd_library_survives_an_unwritable_ledger(self):
        with mock.patch.object(ledger, 'snapshot', side_effect=OSError('read-only')):
            self.library(True)

    def test_history_and_insights_pass_the_switch(self):
        from nexus.cli import info
        old, new = datetime(2026, 11, 1, 9), datetime(2026, 11, 3, 9)
        ledger.snapshot([game('portal', 100)], ledger.PATH, old)
        ledger.snapshot([game('portal', 160, last_played='2026-11-02T21:00:00')], ledger.PATH, new)

        def hist(on):
            out = io.StringIO()
            with patch_cli('load_config', return_value={'ledger': on, 'import_steam_log': False}), \
                    contextlib.redirect_stdout(out):
                info.cmd_history(True)
            return json.loads(out.getvalue())['year']['minutes']
        with mock.patch('nexus.history.date') as d:
            d.today.return_value = new.date()
            d.side_effect = lambda *a, **k: __import__('datetime').date(*a, **k)
            self.assertEqual((hist(True), hist(False)), (60.0, 0.0))


if __name__ == '__main__':
    unittest.main()
