"""The background agent (nexus-cli --agent) and the alerts it sends: who
announces a finished download, who installs a finished torrent, what's
alerted once, and the systemd unit. Nothing runs for real: the torrent
client, systemctl, notify-send and the window's socket are stand-ins."""

import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.patching import patch_cli  # noqa: E402
from nexus import alerts, instance, torrent, watch  # noqa: E402
from nexus import cli  # noqa: E402
from nexus.cli import agent as A  # noqa: E402


def wish(appid, title, **price):
    return {'appid': appid, 'title': title, 'price': {'currentStr': '€10', **price}}


class Alerts(unittest.TestCase):
    def setUp(self):
        alerts.STATE.unlink(missing_ok=True)
        self.addCleanup(alerts.STATE.unlink, missing_ok=True)
        self.s = {'atl': True, 'sale': True, 'sale_min_cut': 50, 'fitgirl': True,
                  'repack_updates': True, 'proton': True}

    def test_first_check_only_records_then_each_comes_once(self):
        items = alerts.deal_alerts([wish(1, 'One', atHistoricalLow=True, cutPct=40)], self.s)
        self.assertEqual(alerts.fresh('atl', items['atl'], self.s), [])     # primed
        self.assertEqual(alerts.fresh('atl', items['atl'], self.s), [])     # known
        more = alerts.deal_alerts([wish(1, 'One', atHistoricalLow=True, cutPct=40),
                                   wish(2, 'Two', atHistoricalLow=True, onSale=True)], self.s)
        self.assertEqual([i['title'] for i in alerts.fresh('atl', more['atl'], self.s)],
                         ['Two'])

    def test_a_sale_that_ended_alerts_again_next_time(self):
        low = alerts.deal_alerts([wish(1, 'One', atHistoricalLow=True, cutPct=40)], self.s)
        alerts.fresh('atl', [], self.s)                                      # primed, empty
        self.assertEqual(len(alerts.fresh('atl', low['atl'], self.s)), 1)
        alerts.fresh('atl', [], self.s)                                      # sale over
        self.assertEqual(len(alerts.fresh('atl', low['atl'], self.s)), 1)    # back again

    def test_full_price_lows_and_small_sales_are_not_news(self):
        got = alerts.deal_alerts([wish(1, 'New game', atHistoricalLow=True),
                                  wish(2, 'Small', onSale=True, cutPct=20),
                                  wish(3, 'Big', onSale=True, cutPct=60)], self.s)
        self.assertEqual(got['atl'], [])
        self.assertEqual([i['title'] for i in got['sale']], ['Big'])
        self.assertEqual(alerts.deal_alerts([wish(3, 'Big', onSale=True, cutPct=60)],
                                            dict(self.s, sale=False))['sale'], [])

    def test_a_kind_switched_off_records_without_alerting(self):
        alerts.fresh('proton', [], self.s)
        off = dict(self.s, proton=False)
        self.assertEqual(alerts.fresh('proton', alerts.proton_alerts('GE-Proton11-11', True),
                                      off), [])
        # Switched back on: that release was already recorded.
        self.assertEqual(alerts.fresh('proton', alerts.proton_alerts('GE-Proton11-11', True),
                                      self.s), [])
        self.assertEqual(alerts.proton_alerts('GE-Proton11-11', False), [])

    def test_fitgirl_matches_are_already_new(self):
        news = [{'appid': '7', 'title': 'Hades', 'slug': 'hades'}]
        self.assertEqual(len(alerts.fresh('fitgirl', alerts.fitgirl_alerts(news), self.s,
                                          prime=False)), 1)
        self.assertEqual(alerts.fresh('fitgirl', alerts.fitgirl_alerts(news), self.s,
                                      prime=False), [])

    def test_many_of_a_kind_become_one_push(self):
        new = [{'key': f'atl:{n}', 'kind': 'atl', 'title': f'G{n}', 'text': f'G{n} low'}
               for n in range(5)] + [{'key': 'proton:x', 'kind': 'proton', 'title': 'x',
                                      'text': 'x is out'}]
        sent = []
        with mock.patch('nexus.notify.settings', return_value={'ntfy': 'https://n/t'}), \
                mock.patch('nexus.notify.configured', return_value=True), \
                mock.patch('nexus.notify.push',
                           side_effect=lambda t, b, ok, s: sent.append((t, b)) or []), \
                mock.patch('shutil.which', return_value=None):
            self.assertEqual(alerts.deliver(new, desktop=True), [])
        self.assertEqual(sent, [("5 wishlist games are at their all-time low",
                                 "G0, G1, G2, G3, G4"), ("Nexus", "x is out")])

    def test_settings_from_config(self):
        from nexus.paths import CONFIG_PATH
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        old = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else None
        self.addCleanup(lambda: CONFIG_PATH.write_text(old) if old is not None
                        else CONFIG_PATH.unlink(missing_ok=True))
        CONFIG_PATH.write_text('[alerts]\nproton = false\nsale_min_cut = 30\n')
        s = alerts.settings()
        self.assertEqual((s['proton'], s['atl'], s['sale'], s['sale_min_cut']),
                         (False, True, True, 30))


class Claims(unittest.TestCase):
    def setUp(self):
        torrent.TRACKED.unlink(missing_ok=True)
        self.addCleanup(torrent.TRACKED.unlink, missing_ok=True)

    def test_only_the_first_caller_installs(self):
        torrent.save_tracked({'a' * 40: {'name': 'G', 'auto_install': True, 'installed': False}})
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            torrent.claim_install('a' * 40))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [False] * 5 + [True])
        self.assertTrue(torrent.load_tracked()['a' * 40]['install_started'])
        self.assertFalse(torrent.claim_install('b' * 40))           # not ours

    def test_watch_announces_a_folder_once_across_watchers(self):
        watch.STATE.unlink(missing_ok=True)
        self.addCleanup(watch.STATE.unlink, missing_ok=True)
        c = {'path': '/r/G', 'name': 'G', 'fingerprint': 'f', 'size_bytes': 5,
             'complete': True, 'installed': False, 'update_for': None}
        with watch.edit_seen() as box:
            box['seen'] = []
        got = []
        for sizes in ({'/r/G': 5}, {'/r/G': 5}):              # two watchers, both stable
            with watch.edit_seen() as box:
                ready, box['seen'], waiting = watch.triage([c], box['seen'], sizes, set())
            got += ready
        self.assertEqual(len(got), 1)


class Lock(unittest.TestCase):
    def test_one_agent_at_a_time_and_the_probe_sees_it(self):
        self.assertFalse(instance.agent_running())
        fh = A._take_lock()
        self.assertIsNotNone(fh)
        try:
            self.assertTrue(instance.agent_running())
            r = subprocess.run([sys.executable, '-c',
                                'import sys; sys.path.insert(0, %r);'
                                'from nexus.cli import agent;'
                                'print(agent._take_lock() is None)' % str(REPO)],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout.strip(), 'True', r.stderr)
        finally:
            fh.close()
        self.assertFalse(instance.agent_running())


class Forward(unittest.TestCase):
    def test_forward_reaches_a_listening_window(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d, 's.sock'))
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            srv.listen(1)
            got = []

            def accept():
                conn, _ = srv.accept()
                with conn:
                    got.append(conn.recv(65536))
            t = threading.Thread(target=accept)
            t.start()
            with mock.patch.object(instance, 'INSTANCE', path):
                self.assertTrue(instance.forward(['--agent-ready', '{}']))
            t.join(5)
            srv.close()
            self.assertEqual(json.loads(got[0]), {'argv': ['--agent-ready', '{}']})
            with mock.patch.object(instance, 'INSTANCE', path):
                self.assertFalse(instance.forward(['x']))            # nobody listening
                self.assertFalse(instance.window_running())


class AgentTasks(unittest.TestCase):
    def setUp(self):
        torrent.TRACKED.unlink(missing_ok=True)
        self.addCleanup(torrent.TRACKED.unlink, missing_ok=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name, 'Game [FitGirl Repack]')
        self.folder.mkdir()
        torrent.save_tracked({'c' * 40: {'name': 'Game', 'auto_install': True,
                                         'installed': False}})
        self.row = {'hash': 'c' * 40, 'name': 'Game', 'path': str(self.folder), 'done': True,
                    'tracked': True, 'auto_install': True, 'installed': False}
        self.agent = A.Agent(Path(self.tmp.name))

    def _torrents(self, forwarded: bool):
        spawned, sent = [], []
        with mock.patch.object(torrent, 'settings', return_value={'client': 'transmission'}), \
                mock.patch.object(torrent, 'status', return_value=[self.row]), \
                mock.patch.object(instance, 'forward',
                                  side_effect=lambda argv: sent.append(argv) or forwarded), \
                mock.patch.object(A.subprocess, 'Popen',
                                  side_effect=lambda cmd, **k: spawned.append(cmd) or
                                  mock.Mock(pid=1, poll=lambda: None)), \
                mock.patch.object(A, '_desktop'), mock.patch.object(A, '_say'), \
                patch_cli('load_config', return_value={'unattended': True}), \
                patch_cli('protongen_default_config', return_value=None):
            delay = self.agent.task_torrents()
            self.agent.task_torrents()                           # never twice
        return delay, spawned, sent

    def test_hands_the_install_to_an_open_window(self):
        delay, spawned, sent = self._torrents(forwarded=True)
        self.assertEqual(sent, [['--agent-install', str(self.folder), 'c' * 40]])
        self.assertEqual(spawned, [])
        self.assertEqual(delay, 60)
        self.assertIn(str(self.folder), self.agent.skip)       # not offered as a download too

    def test_installs_by_itself_with_no_window(self):
        _d, spawned, sent = self._torrents(forwarded=False)
        self.assertEqual(len(spawned), 1)
        cmd = spawned[0]
        self.assertEqual(cmd[1:], [str(self.folder), '--yes', '--log', '--defer-steam',
                                   '--from-torrent', 'c' * 40])
        self.assertEqual([n for _p, n in self.agent.children], ['Game'])

    def test_a_task_that_raises_doesnt_stop_the_loop(self):
        ag = A.Agent(Path(self.tmp.name))
        calls = []

        def boom():
            calls.append(1)
            ag.stop = True
            raise RuntimeError("nope")
        ag.task_torrents = boom
        for t in ('scan', 'feed', 'index', 'deals', 'proton', 'art'):
            setattr(ag, 'task_' + t, lambda: 60)
        with mock.patch.object(A, 'TICK', 0), mock.patch.object(A.time, 'monotonic',
                                                               side_effect=iter(range(0, 10**6, 100)).__next__), \
                mock.patch.object(A, '_say'):
            ag.run()
        self.assertEqual(calls, [1])

    def test_scan_announces_through_the_window_or_the_desktop(self):
        watch.STATE.unlink(missing_ok=True)
        self.addCleanup(watch.STATE.unlink, missing_ok=True)
        with watch.edit_seen() as box:
            box['seen'] = []
        cand = {'path': str(self.folder), 'name': 'Game', 'fingerprint': 'f', 'size_bytes': 9,
                'complete': True, 'installed': False, 'update_for': None}
        desk = []
        with patch_cli('resolve_scan_dirs', return_value=[Path(self.tmp.name)]), \
                patch_cli('scan_repacks', return_value=[cand]), \
                mock.patch.object(instance, 'forward', return_value=False), \
                mock.patch.object(A, '_desktop', side_effect=lambda t, b: desk.append(t)):
            self.assertEqual(self.agent.task_scan(), A.EVERY['scan_waiting'])   # size new
            self.agent.scan_waiting = True
            self.agent.task_scan()
            self.agent.task_scan()
        self.assertEqual(desk, ["Game is ready to install"])


class Service(unittest.TestCase):
    def test_enable_writes_the_unit_then_asks_systemd(self):
        unit = A.UNIT_DIR / A.UNIT_NAME
        self.addCleanup(unit.unlink, missing_ok=True)
        calls = []

        def systemctl(*args):
            calls.append(args)
            return mock.Mock(returncode=0, stdout='enabled\n' if 'is-enabled' in args else
                             'active\n', stderr='')
        with mock.patch.object(A, '_systemctl', side_effect=systemctl), \
                mock.patch('sys.stdout'):
            self.assertEqual(A.cmd_agent_enable(True, True), 0)
        text = unit.read_text()
        self.assertIn(f'ExecStart="{cli.CLI_BIN}" --agent', text)
        self.assertIn('WantedBy=default.target', text)
        self.assertEqual(calls[:2], [('daemon-reload',), ('enable', '--now', A.UNIT_NAME)])

    def test_enable_reports_a_failing_systemctl(self):
        self.addCleanup((A.UNIT_DIR / A.UNIT_NAME).unlink, missing_ok=True)
        out = []
        with mock.patch.object(A, '_systemctl', return_value=None), \
                mock.patch('builtins.print', side_effect=lambda *a, **k: out.append(a[0])):
            self.assertEqual(A.cmd_agent_enable(True, True), 1)
        self.assertIn('systemctl not found', json.loads(out[-1])['errors'][0])


if __name__ == '__main__':
    unittest.main()
