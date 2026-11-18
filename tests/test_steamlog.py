"""nexus/steamlog.py (w-150): Steam's local game log read into play sessions.
The parser (exact-match lines only), the import with its cursor, and the
files it writes beside sessions.jsonl. No real home, Steam or network."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from unittest import mock  # noqa: E402

from nexus import features, history, steamlog  # noqa: E402
from tests.patching import linux_features, patch_cli  # noqa: E402

FIXTURE = REPO / 'tests' / 'fixtures' / 'steam' / 'gameprocess_log.txt'


def lines(*rows):
    return [r + '\n' for r in rows]


class Parse(unittest.TestCase):
    def test_fixture_sessions(self):
        got = steamlog.parse(FIXTURE.read_text(encoding='utf-8').splitlines(True))
        self.assertEqual(got, [
            {'appid': 620, 'start': datetime(2026, 3, 1, 20, 0, 0),
             'end': datetime(2026, 3, 1, 21, 30, 0)},
            {'appid': 2147483649, 'start': datetime(2026, 3, 2, 18, 0, 0),
             'end': datetime(2026, 3, 2, 19, 0, 0)}])

    def test_shortcut_id_pairs(self):
        got = steamlog.parse(lines(
            '[2026-03-02 18:00:00] AppID 9223372041183297536 adding PID 1 as a tracked process',
            '[2026-03-02 19:00:00] Remove -2147483647 from running list'))
        self.assertEqual([s['appid'] for s in got], [2147483649])

    def test_open_session_dropped_at_client_version(self):
        got = steamlog.parse(lines(
            '[2026-03-03 10:00:00] AppID 70 adding PID 1 as a tracked process',
            '[2026-03-03 12:00:00] Client version: 5',
            '[2026-03-03 12:30:00] Remove 70 from running list'))
        self.assertEqual(got, [])

    def test_duplicate_remove_ignored(self):
        got = steamlog.parse(lines(
            '[2026-03-01 20:00:00] AppID 620 adding PID 1 as a tracked process',
            '[2026-03-01 21:00:00] Remove 620 from running list',
            '[2026-03-01 21:00:05] Remove 620 from running list'))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['end'], datetime(2026, 3, 1, 21, 0, 0))

    def test_malformed_lines_skipped(self):
        got = steamlog.parse(lines(
            'garbage',
            '[2026-13-45 99:00:00] AppID 620 adding PID 1 as a tracked process',
            '[2026-03-01 20:00:00] AppID x adding PID 1 as a tracked process',
            '[2026-03-01 20:00:00] AppID 620 adding PID 1 as a tracked process',
            '[2026-03-01 20:30:00] Remove 620 from running list'))
        self.assertEqual(len(got), 1)

    def test_short_sessions_dropped(self):
        got = steamlog.parse(lines(
            '[2026-03-01 20:00:00] AppID 620 adding PID 1 as a tracked process',
            '[2026-03-01 20:00:30] Remove 620 from running list'))
        self.assertEqual(got, [])

    def test_zero_sessions_from_nonempty_log(self):
        """The format drifted: a log with lines but no session is visible."""
        self.assertEqual(steamlog.parse(lines('[2026-03-01 20:00:00] AppID 620 started',
                                              '[2026-03-01 20:30:00] 620 stopped')), [])

    def test_command_is_never_kept(self):
        got = steamlog.parse(FIXTURE.read_text(encoding='utf-8').splitlines(True))
        self.assertNotIn('steam-launch-wrapper', json.dumps(got, default=str))


ADD = '[{}] AppID {} adding PID 1 as a tracked process'
REM = '[{}] Remove {} from running list'


class Import(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.log, self.prev = d / 'gameprocess_log.txt', d / 'gameprocess_log.previous.txt'
        self.out, self.cursor = d / 'steam-log.jsonl', d / 'steam-log.cursor.json'

    def write(self, path, *rows):
        path.write_text(''.join(r + '\n' for r in rows), encoding='utf-8')

    def append(self, *rows):
        with open(self.log, 'a', encoding='utf-8') as f:
            f.write(''.join(r + '\n' for r in rows))

    def run_import(self, games=()):
        return steamlog.import_new(self.log, self.out, self.cursor, list(games))

    def written(self):
        try:
            return [json.loads(x) for x in self.out.read_text(encoding='utf-8').splitlines()]
        except OSError:
            return []

    def test_appends_once(self):
        self.write(self.log, ADD.format('2026-03-01 20:00:00', 620),
                   REM.format('2026-03-01 21:30:00', 620))
        self.assertEqual(self.run_import(), 1)
        self.assertEqual(self.run_import(), 0)
        rows = self.written()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            'appid': 620, 'slug': 'steam-620', 'name': 'Steam app 620',
            'start': '2026-03-01T20:00:00', 'end': '2026-03-01T21:30:00',
            'minutes': 90.0, 'source': 'steam', 'via': 'steam-log'})

    def test_open_session_reread_next_time(self):
        self.write(self.log, ADD.format('2026-03-01 20:00:00', 620),
                   ADD.format('2026-03-01 20:10:00', 70),
                   REM.format('2026-03-01 20:40:00', 70))
        self.assertEqual(self.run_import(), 1)        # 70 is done, 620 still running
        cur = json.loads(self.cursor.read_text(encoding='utf-8'))
        self.assertEqual(cur['offset'], 0)            # back at 620's adding line
        self.append(REM.format('2026-03-01 22:00:00', 620))
        self.assertEqual(self.run_import(), 1)
        self.assertEqual(sorted(r['appid'] for r in self.written()), [70, 620])

    def test_rotation_reads_previous_then_top(self):
        self.write(self.log, ADD.format('2026-03-01 20:00:00', 620),
                   REM.format('2026-03-01 21:00:00', 620),
                   ADD.format('2026-03-01 22:00:00', 1234567),
                   REM.format('2026-03-01 22:00:20', 1234567))     # too short: dropped
        self.run_import()
        # Steam restarts: the old file becomes .previous with more in it, the new one is short.
        self.write(self.prev, ADD.format('2026-03-01 20:00:00', 620),
                   REM.format('2026-03-01 21:00:00', 620),
                   ADD.format('2026-03-01 22:00:00', 1234567),
                   REM.format('2026-03-01 22:00:20', 1234567),
                   ADD.format('2026-03-01 23:00:00', 70), REM.format('2026-03-02 00:00:00', 70))
        self.write(self.log, ADD.format('2026-03-02 10:00:00', 400),
                   REM.format('2026-03-02 10:30:00', 400))
        self.assertEqual(self.run_import(), 2)
        self.assertEqual(sorted(r['appid'] for r in self.written()), [70, 400, 620])

    def test_rotation_detected_when_new_log_is_larger(self):
        old = [ADD.format('2026-03-01 20:00:00', 620), REM.format('2026-03-01 21:00:00', 620)]
        self.write(self.log, *old)
        self.run_import()
        self.write(self.prev, *old, ADD.format('2026-03-01 23:00:00', 70),
                   REM.format('2026-03-02 00:00:00', 70))
        rows = ['[2026-03-02 09:00:00] Client version: 9']
        for i in range(8):
            rows += [ADD.format(f'2026-03-02 1{i}:00:00', 400 + i),
                     REM.format(f'2026-03-02 1{i}:30:00', 400 + i)]
        self.write(self.log, *rows)
        self.assertGreater(self.log.stat().st_size, 2 * len(old[0]))
        self.assertEqual(self.run_import(), 9)
        self.assertEqual(sorted(r['appid'] for r in self.written()),
                         [70, *range(400, 408), 620])

    def test_partial_last_line_read_next_time(self):
        self.write(self.log, ADD.format('2026-03-01 20:00:00', 620))
        with open(self.log, 'a', encoding='utf-8') as f:
            f.write('[2026-03-01 21:00:00] Remove 620 fr')     # Steam is mid-write
        self.assertEqual(self.run_import(), 0)
        with open(self.log, 'a', encoding='utf-8') as f:
            f.write('om running list\n')
        self.assertEqual(self.run_import(), 1)
        with open(self.log, 'a', encoding='utf-8') as f:
            f.write('[2026-03-02 20:30:00] AppID 9 adding PID 1 as a tr')
        self.run_import()
        with open(self.log, 'a', encoding='utf-8') as f:
            f.write('acked process\n' + REM.format('2026-03-02 21:00:00', 9) + '\n')
        self.assertEqual(self.run_import(), 1)     # 9 started on the half-written line

    def test_mapping_steam_and_shortcut(self):
        games = [{'slug': 'my-game', 'display_name': 'My Game', 'steam_appid': 2147483649},
                 {'slug': 'steam-620', 'display_name': 'Portal 2', 'appid': 620, 'source': 'steam'}]
        self.write(self.log, ADD.format('2026-03-01 20:00:00', 620),
                   REM.format('2026-03-01 21:00:00', 620),
                   ADD.format('2026-03-02 18:00:00', 9223372041183297536),
                   REM.format('2026-03-02 19:00:00', -2147483647))
        self.run_import(games)
        by = {r['appid']: r for r in self.written()}
        self.assertEqual((by[620]['slug'], by[620]['name']), ('steam-620', 'Portal 2'))
        self.assertEqual((by[2147483649]['slug'], by[2147483649]['name']), ('my-game', 'My Game'))

    def test_missing_log_is_nothing(self):
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.written(), [])

    def test_load_dedupes_and_skips_torn_lines(self):
        row = {'appid': 620, 'slug': 'steam-620', 'name': 'P', 'start': '2026-03-01T20:00:00',
               'end': '2026-03-01T21:00:00', 'minutes': 60.0, 'source': 'steam', 'via': 'steam-log'}
        self.out.write_text(json.dumps(row) + '\n' + json.dumps(row) + '\n{torn\n',
                            encoding='utf-8')
        got = steamlog.load(self.out)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['_start'], datetime(2026, 3, 1, 20, 0, 0))


class Wiring(unittest.TestCase):
    """The CLI around it: the library scan imports, history and insights merge,
    each only with the feature on and the setting on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.root = d / 'steam'
        (self.root / 'logs').mkdir(parents=True)
        (self.root / 'logs' / steamlog.LOG_NAME).write_text(
            ADD.format('2026-03-01 20:00:00', 620) + '\n'
            + REM.format('2026-03-01 21:30:00', 620) + '\n', encoding='utf-8')
        for mod, name, path in ((history, 'PATH', d / 'sessions.jsonl'),
                                (steamlog, 'PATH', d / 'steam-log.jsonl'),
                                (steamlog, 'CURSOR_PATH', d / 'steam-log.cursor.json')):
            p = mock.patch.object(mod, name, path)
            p.start()
            self.addCleanup(p.stop)
        p = patch_cli('STEAM_ROOT', self.root)
        p.start()
        self.addCleanup(p.stop)
        self.base = d / 'base'

    def library(self, on=True):
        from nexus.cli import manage
        with patch_cli('load_config', return_value={'import_steam_log': on}), \
                contextlib.redirect_stdout(io.StringIO()):
            manage.cmd_library(self.base, True)

    @linux_features
    def test_cmd_library_imports(self):
        self.library()
        self.assertEqual(len(steamlog.load()), 1)

    @linux_features
    def test_cmd_library_skips_when_setting_off(self):
        self.library(on=False)
        self.assertFalse(steamlog.PATH.exists())

    def test_cmd_library_skips_when_feature_off(self):
        with mock.patch.object(features, 'FEATURES', features.ALL - {'steam_log'}):
            self.library()
        self.assertFalse(steamlog.PATH.exists())

    def history_json(self, on=True):
        from nexus.cli import info
        out = io.StringIO()
        with patch_cli('load_config', return_value={'import_steam_log': on}), \
                contextlib.redirect_stdout(out):
            info.cmd_history(True)
        return json.loads(out.getvalue())

    @linux_features
    def test_cmd_history_merges_only_when_on(self):
        self.library()
        self.assertEqual(self.history_json()['since'], '2026-03-01')
        self.assertTrue(self.history_json()['includes']['steam_log'])      # w-152 note
        self.assertIsNone(self.history_json(on=False)['since'])
        self.assertFalse(self.history_json(on=False)['includes']['steam_log'])
        with mock.patch.object(features, 'FEATURES', features.ALL - {'steam_log'}):
            self.assertIsNone(self.history_json()['since'])

    @linux_features
    def test_cmd_insights_merges(self):
        from nexus.cli import info
        self.library()
        out = io.StringIO()
        with patch_cli('load_config', return_value={'import_steam_log': True}), \
                mock.patch('nexus.insights.library_games', return_value=[]), \
                contextlib.redirect_stdout(out):
            info.cmd_insights(True)
        got = json.loads(out.getvalue())
        self.assertIn('2026-03-01', got['days'])
        self.assertEqual(got['includes'], {'steam_log': True, 'estimates': False})


if __name__ == '__main__':
    unittest.main()
