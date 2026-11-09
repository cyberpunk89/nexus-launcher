"""The opt-in anonymous install count (w-102, nexus/stats.py): who may send,
which periods are due, what the ping holds, and that two processes count a
day once. Nothing here touches the network: relay.count is replaced."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import platform, stats  # noqa: E402
from nexus.cli import config as CC  # noqa: E402
from nexus.report import relay as RL  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ON = {'stats_count': True, 'report_relay': True, 'report_url': None}
NO_ENV = {'NEXUS_NO_STATS': '', 'DO_NOT_TRACK': ''}


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


class Dirs(unittest.TestCase):
    """Each test gets its own DATA_DIR and LOG_DIR."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        for name, sub in (('DATA_DIR', 'data'), ('LOG_DIR', 'logs')):
            p = mock.patch.object(stats, name, self.dir / sub)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, NO_ENV)
        p.start()
        self.addCleanup(p.stop)


class Config(unittest.TestCase):
    def cfg(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.toml'
            p.write_text(text, encoding='utf-8')
            with mock.patch.object(CC, 'CONFIG_PATH', p):
                return CC.load_config()

    def test_missing_key_is_unasked(self):
        self.assertIsNone(self.cfg('')['stats_count'])
        self.assertIsNone(self.cfg('[stats]\n')['stats_count'])
        self.assertIsNone(self.cfg('[stats]\ncount = "yes"\n')['stats_count'])
        self.assertTrue(self.cfg('[stats]\ncount = true\n')['stats_count'])
        self.assertIs(self.cfg('[stats]\ncount = false\n')['stats_count'], False)
        with mock.patch.dict(os.environ, NO_ENV):
            self.assertEqual(stats.state_of({'stats_count': None}), 'unasked')
            self.assertEqual(stats.state_of({'stats_count': False}), 'off')
            self.assertEqual(stats.state_of(ON), 'on')
            with mock.patch.object(RL, 'count', side_effect=AssertionError('network')):
                self.assertEqual(stats.ping({'stats_count': None}), {'skipped': 'unasked'})
                self.assertEqual(stats.ping({'stats_count': False}), {'skipped': 'off'})

    def test_gui_config_reads_it_too(self):
        from nexus.gui import config as GC
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.toml'
            p.write_text('[report]\nrelay = false\n', encoding='utf-8')
            with mock.patch.object(GC, 'CONFIG_PATH', p):
                c = GC.load_config()
            self.assertIsNone(c['stats_count'])
            self.assertFalse(c['report_relay'])
            p.write_text('[stats]\ncount = true\n', encoding='utf-8')
            with mock.patch.object(GC, 'CONFIG_PATH', p):
                c = GC.load_config()
            self.assertTrue(c['stats_count'])
            self.assertTrue(c['report_relay'])

    def test_do_not_track(self):
        for env in ({'DO_NOT_TRACK': '1'}, {'NEXUS_NO_STATS': '1'}, {'DO_NOT_TRACK': 'true'}):
            with mock.patch.dict(os.environ, {**NO_ENV, **env}), \
                    mock.patch.object(RL, 'count', side_effect=AssertionError('network')):
                self.assertEqual(stats.state_of(ON), 'blocked')
                self.assertIn(stats.blocked(ON), ('DO_NOT_TRACK', 'NEXUS_NO_STATS'))
                self.assertEqual(stats.ping(ON)['skipped'], 'blocked')
        with mock.patch.dict(os.environ, {**NO_ENV, 'DO_NOT_TRACK': '0'}):
            self.assertEqual(stats.state_of(ON), 'on')

    def test_report_relay_false_blocks(self):
        cfg = dict(ON, report_relay=False)
        with mock.patch.dict(os.environ, NO_ENV), \
                mock.patch.object(RL, 'count', side_effect=AssertionError('network')):
            self.assertEqual(stats.blocked(cfg), 'relay')
            self.assertEqual(stats.ping(cfg)['skipped'], 'blocked')
        self.assertIsNone(RL.relay_url(cfg))


class Due(unittest.TestCase):
    def test_day_week_month_boundaries_utc(self):
        due = stats.due
        self.assertEqual(due({}, utc(2026, 11, 8, 12)), ['d', 'w', 'm'])
        # Sunday 8 Nov counted; later that Sunday: nothing.
        st = stats.stamped({}, ['d', 'w', 'm'], utc(2026, 11, 8, 12))
        self.assertEqual(st['first'], '2026-W45')
        self.assertEqual(due(st, utc(2026, 11, 8, 23, 59)), [])
        # Monday 9 Nov: a new day and a new ISO week, the same month.
        self.assertEqual(due(st, utc(2026, 11, 9, 0, 0)), ['d', 'w'])
        st = stats.stamped(st, ['d', 'w'], utc(2026, 11, 9, 1))
        self.assertEqual(due(st, utc(2026, 11, 10, 9)), ['d'])
        # 1 Dec: new month, same week (Monday 30 Nov starts W49).
        st = stats.stamped(st, ['d', 'w'], utc(2026, 11, 30, 9))
        self.assertEqual(due(st, utc(2026, 12, 1, 9)), ['d', 'm'])
        # Year end: Thursday 31 Dec 2026 is 2026-W53, Friday 1 Jan 2027 too.
        st = stats.stamped(st, ['d', 'w', 'm'], utc(2026, 12, 31, 9))
        self.assertEqual(st['last_w'], '2026-W53')
        self.assertEqual(due(st, utc(2027, 1, 1, 9)), ['d', 'm'])
        self.assertEqual(due(st, utc(2027, 1, 4, 9)), ['d', 'w', 'm'])
        # Days are UTC, not local: 23:30 at UTC-5 on the 1st is the 2nd in UTC.
        st = stats.stamped({}, ['d', 'w', 'm'], utc(2027, 1, 2, 3))
        local = datetime(2027, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(due(st, local), [])
        # The first count never moves.
        self.assertEqual(stats.stamped(st, ['d'], utc(2027, 3, 1))['first'], '2026-W53')

    def test_age_buckets(self):
        now = utc(2026, 11, 9, 12)
        self.assertEqual(stats.age(None, now), '<2w')
        self.assertEqual(stats.age('2026-W45', now), '<2w')
        self.assertEqual(stats.age('2026-W44', now), '2-5w')
        self.assertEqual(stats.age('2026-W42', now), '2-5w')
        self.assertEqual(stats.age('2026-W41', now), '5-25w')
        self.assertEqual(stats.age('2026-W22', now), '5-25w')
        self.assertEqual(stats.age('2026-W21', now), '25w+')
        self.assertEqual(stats.age('garbage', now), '<2w')


class Sending(Dirs):
    def send(self, answer):
        calls = []

        def count(url, body):
            calls.append((url, body))
            return answer
        p = mock.patch.object(RL, 'count', count)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_failed_send_stamps_nothing(self):
        calls = self.send({'error': 'offline', 'reason': "Couldn't reach the relay."})
        now = utc(2026, 11, 9, 12)
        out = stats.ping(ON, now)
        self.assertFalse(out['ok'])
        self.assertEqual(len(calls), 1)
        st = stats.load_state()
        for k in ('first', 'last_d', 'last_w', 'last_m'):
            self.assertNotIn(k, st)
        self.assertEqual(stats.due(st, now + timedelta(hours=4)), ['d', 'w', 'm'])
        self.assertEqual(calls[0][0], RL.DEFAULT_URL)
        # A success later stamps every period it carried.
        calls = self.send({'status': 'counted'})
        out = stats.ping(ON, now + timedelta(hours=4))
        self.assertTrue(out['ok'])
        self.assertEqual(calls[0][1]['periods'], ['d', 'w', 'm'])
        st = stats.load_state()
        self.assertEqual((st['last_d'], st['last_w'], st['last_m'], st['first']),
                         ('2026-11-09', '2026-W46', '2026-11', '2026-W46'))
        self.assertEqual(stats.ping(ON, now + timedelta(hours=5)), {'skipped': 'not-due'})

    def test_retry_backoff_3h(self):
        now = utc(2026, 11, 9, 12)
        for answer in ({'error': 'off'}, {'error': 'limit'}, {'error': 'offline'}):
            stats.save_state({})
            calls = self.send(answer)
            stats.ping(ON, now)
            self.assertEqual(stats.ping(ON, now + timedelta(hours=2, minutes=59)),
                             {'skipped': 'not-due'})
            self.assertEqual(len(calls), 1, answer)
            stats.ping(ON, now + timedelta(hours=3, minutes=1))
            self.assertEqual(len(calls), 2, answer)

    def test_sent_log_keeps_100(self):
        self.send({'error': 'offline'})
        t = utc(2026, 11, 9, 0)
        for i in range(105):
            stats.save_state({})
            stats.ping(ON, t + timedelta(minutes=i))
        rows = stats.load_sent()
        self.assertEqual(len(rows), stats.KEEP_SENT)
        self.assertEqual(rows[0]['at'], '2026-11-09T01:44:00Z')          # newest first
        self.assertEqual(rows[-1]['at'], '2026-11-09T00:05:00Z')
        self.assertEqual(set(rows[0]), {'at', 'payload', 'answer'})
        self.assertEqual(rows[0]['answer'], {'error': 'offline'})
        self.assertEqual(rows[0]['payload']['schema'], 1)

    def test_custom_relay_url(self):
        calls = self.send({'status': 'counted'})
        stats.ping(dict(ON, report_url='https://relay.example.org/'), utc(2026, 11, 9))
        self.assertEqual(calls[0][0], 'https://relay.example.org')


class Payload(unittest.TestCase):
    LINUX = {'id': 'cachyos', 'version': 'rolling', 'name': 'CachyOS', 'kernel': '7.0',
             'arch': 'x86_64'}

    def payload(self, env=None, rel=None, lang='de', kind='git', periods=('d',)):
        env = {'XDG_CURRENT_DESKTOP': 'niri', 'XDG_SESSION_TYPE': 'wayland', **(env or {})}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(platform, 'os_release', lambda: rel or self.LINUX), \
                mock.patch.object(platform, 'ui_language', lambda: lang), \
                mock.patch('nexus.report.sysinfo.install_kind', lambda: kind):
            return stats.payload(list(periods), {'first': '2026-W40'}, utc(2026, 11, 9))

    def test_key_snapshot(self):
        p = self.payload()
        # A new field needs a review (and the relay's KEYS): change both on purpose.
        self.assertEqual(sorted(p), ['age', 'client', 'desktop', 'edition', 'install', 'lang',
                                     'os', 'os_ver', 'periods', 'py', 'schema', 'session'])
        from nexus import __version__
        self.assertEqual(p, {'schema': 1, 'client': __version__, 'periods': ['d'],
                             'edition': 'linux', 'install': 'git', 'os': 'cachyos',
                             'os_ver': None, 'desktop': 'niri', 'session': 'wayland',
                             'lang': 'de', 'py': f"{sys.version_info[0]}.{sys.version_info[1]}",
                             'age': '5-25w'})
        self.assertLess(len(json.dumps(p)), 2000)

    def test_os_versions(self):
        def ver(i, v):
            return self.payload(rel={**self.LINUX, 'id': i, 'version': v})['os_ver']
        self.assertEqual(ver('fedora', '43'), '43')
        self.assertEqual(ver('ubuntu', '24.04'), '24.04')
        self.assertEqual(ver('linuxmint', '22.1'), '22')
        self.assertEqual(ver('debian', '13'), '13')
        self.assertIsNone(ver('arch', 'rolling'))
        self.assertIsNone(ver('opensuse-tumbleweed', '20261101'))
        self.assertIsNone(ver('gentoo', '2.17'))
        self.assertIsNone(ver('fedora', 'unknown'))
        self.assertIsNone(ver('someos', '7'))           # an unknown OS sends no version

    def test_unknown_os_desktop_other(self):
        p = self.payload(env={'XDG_CURRENT_DESKTOP': 'MyShell', 'XDG_SESSION_TYPE': 'mir'},
                         rel={**self.LINUX, 'id': 'secretos'}, lang=None, kind='weird')
        self.assertEqual((p['os'], p['desktop'], p['session'], p['lang'], p['install']),
                         ('other', 'other', 'other', None, 'other'))
        # Ubuntu's "ubuntu:GNOME" and Mint's "X-Cinnamon" are known desktops.
        self.assertEqual(self.payload(env={'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'})['desktop'],
                         'gnome')
        self.assertEqual(self.payload(env={'XDG_CURRENT_DESKTOP': 'X-Cinnamon'})['desktop'],
                         'cinnamon')
        self.assertEqual(self.payload(env={'XDG_CURRENT_DESKTOP': 'KDE'})['desktop'], 'kde')
        p = self.payload(env={'XDG_CURRENT_DESKTOP': '', 'XDG_SESSION_TYPE': ''})
        self.assertEqual((p['desktop'], p['session']), (None, None))

    def test_windows_edition(self):
        from nexus import features
        win = {'id': 'windows', 'version': '11', 'name': 'Windows 11 24H2',
               'kernel': '26100.1', 'build': '26100.1', 'arch': 'amd64'}
        with mock.patch.object(features, 'FEATURES', frozenset()), \
                mock.patch.object(platform, 'OS_NAME', 'windows'):
            p = self.payload(rel=win, lang='en', kind='win-installer',
                             env={'XDG_CURRENT_DESKTOP': 'KDE'})
        self.assertEqual({k: p[k] for k in ('edition', 'os', 'os_ver', 'desktop', 'session',
                                             'install', 'lang')},
                         {'edition': 'windows', 'os': 'windows', 'os_ver': '11',
                          'desktop': 'windows', 'session': None, 'install': 'win-installer',
                          'lang': 'en'})


class RelayCount(unittest.TestCase):
    def test_posts_to_v1_count_with_a_short_timeout(self):
        import io
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout):
            seen.append((req.full_url, req.get_method(), json.loads(req.data), timeout,
                         req.get_header('User-agent')))
            return Resp(b'{"status": "counted"}')
        with mock.patch('urllib.request.urlopen', urlopen):
            self.assertEqual(RL.count('https://r.example', {'schema': 1}), {'status': 'counted'})
        url, method, body, timeout, ua = seen[0]
        self.assertEqual((url, method, body, timeout), ('https://r.example/v1/count', 'POST',
                                                         {'schema': 1}, 10))
        self.assertTrue(ua.startswith('Nexus/'))


class Ping(Dirs):
    def test_lock_counts_once(self):
        """Two processes at once (the agent and the window): one sends, the
        other finds the lock held or the day already counted."""
        script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(REPO)!r})
            import tests.sandbox
            from pathlib import Path
            from nexus import stats
            from nexus.report import relay
            stats.DATA_DIR = Path({str(self.dir / 'data')!r})
            stats.LOG_DIR = Path({str(self.dir / 'logs')!r})
            def count(url, body):
                time.sleep(1.0)
                return {{'status': 'counted'}}
            relay.count = count
            print(stats.ping({{'stats_count': True, 'report_relay': True}}))
        """)
        env = {**os.environ, **NO_ENV}
        procs = [subprocess.Popen([sys.executable, '-c', script], env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (_o, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(len(stats.load_sent()), 1, outs)
        self.assertEqual(sum("'ok': True" in o for o, _e in outs), 1, outs)

    def test_busy_lock_skips(self):
        stats.DATA_DIR.mkdir(parents=True)
        with mock.patch.object(RL, 'count', side_effect=AssertionError('network')), \
                platform.file_lock(stats.DATA_DIR / stats.LOCK):
            self.assertEqual(stats.ping(ON), {'skipped': 'busy'})


if __name__ == '__main__':
    unittest.main()
