"""nexus/pipeline.py: one card per tracked download, from the torrent client
through the install to Ready (Download, Verify, Install, Finish), the disk
forecast and the Grab sheet's space check. Pure: fake rows and times only."""

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import pipeline as P  # noqa: E402

NOW = 1_800_000_000.0
H = 'a' * 40
GB = 1 << 30


def iso(epoch):
    return datetime.fromtimestamp(epoch).isoformat(timespec='seconds')


def row(**kw):
    """A tracked torrent as `--torrents --all` lists it."""
    r = {'hash': H, 'name': 'Test Game [x]', 'title': 'Test Game', 'slug': 'test-game',
         'appid': '7', 'tracked': True, 'auto_install': True, 'installed': False,
         'percent': 40.0, 'done': False, 'state': 'downloading', 'rate': 5 << 20, 'eta': 600,
         'size': 10 * GB, 'dir': '/dl', 'path': '/dl/Test Game [x]', 'error': None,
         'repack_bytes': 10 * GB, 'original_bytes': 30 * GB}
    r.update(kw)
    return r


def prog(**kw):
    """A progress file (DATA_DIR/pipeline/<hash>.json)."""
    p = {'v': 1, 'hash': H, 'slug': 'test-game', 'stage': 'install', 'step': 'Installing',
         'written': 12 * GB, 'expected': 30 * GB, 'rate': 100 << 20, 'done': None,
         'total': None, 'update': False, 'dest': '/games/test-game',
         'started_at': NOW - 300, 'updated_at': NOW - 2, 'pid': 4242}
    p.update(kw)
    return p


def card(r, p=None, alive=(4242,), **kw):
    got = P.cards([r], {H: p} if p else {}, NOW, set(alive), **kw)
    return got[0] if got else None


def track(c):
    return {t['stage']: t['status'] for t in c['track']}


class Stages(unittest.TestCase):
    def test_every_download_state(self):
        cases = [({'gone': True}, 'gone'), ({'error': 'tracker down'}, 'error'),
                 ({'done': True, 'percent': 100.0}, 'done'), ({'state': 'paused'}, 'paused'),
                 ({'state': None, 'paused': True}, 'paused'), ({'state': 'queued'}, 'queued'),
                 ({'state': 'checking'}, 'checking'), ({'state': 'downloading'}, 'downloading'),
                 ({'state': 'unknown'}, 'downloading')]
        for extra, want in cases:
            self.assertEqual(P.download_state(row(**extra)), want, extra)
        for state in ('queued', 'checking', 'downloading', 'paused'):
            c = card(row(state=state))
            self.assertEqual((c['stage'], c['state']), ('download', state))
            self.assertEqual(c['key'], f'torrent:{H}')
            self.assertEqual(c['download'], {'done': 4 * GB, 'total': 10 * GB,
                                             'rate': 5 << 20, 'eta': 600})
            self.assertIsNone(c['install'])
        c = card(row(error='tracker down'))
        self.assertEqual((c['stage'], c['state'], c['error']), ('download', 'error', 'tracker down'))
        self.assertEqual(track(c)['download'], 'failed')
        c = card(row())
        self.assertEqual((c['eta'], c['eta_est']), (600, False))
        self.assertEqual(c['need'], {'download': 6 * GB, 'install': 30 * GB})
        # A finished download nobody started installing: "downloaded", and
        # its install space isn't counted (it may never be installed).
        c = card(row(done=True, percent=100.0, state='seeding', auto_install=False))
        self.assertEqual((c['stage'], c['state'], c['step']), ('install', 'downloaded',
                                                               "Downloaded"))
        self.assertEqual((track(c)['download'], track(c)['install']), ('done', 'todo'))
        self.assertEqual(c['need'], {'download': 0, 'install': 0})
        self.assertIsNone(c['reason'])
        # Untracked and gone ones get no card.
        self.assertIsNone(card(row(tracked=False)))
        self.assertIsNone(card(row(gone=True)))

    def test_progress_file_fresh_stale_dead_pid(self):
        p = prog()
        self.assertTrue(P.fresh(p, NOW, {4242}))
        self.assertFalse(P.fresh(p, NOW, {1}))                      # its CLI is gone
        self.assertFalse(P.fresh(prog(updated_at=NOW - 61), NOW, {4242}))
        self.assertFalse(P.fresh({'junk': 1}, NOW, {4242}))
        r = row(done=True, percent=100.0, install_started=iso(NOW - 300))
        c = card(r, p)
        self.assertEqual((c['stage'], c['state'], c['step']), ('install', 'installing',
                                                               'Installing'))
        self.assertEqual(c['install'], {'written': 12 * GB, 'expected': 30 * GB,
                                        'rate': 100 << 20, 'update': False})
        self.assertAlmostEqual(c['track'][2]['frac'], 0.4)
        self.assertEqual((c['eta'], c['eta_est']), ((18 * GB) // (100 << 20), True))
        self.assertEqual(c['need']['install'], 18 * GB)
        self.assertEqual(c['need']['download'], 0)
        # A file left behind (killed: no heartbeat, or its process is gone):
        # the install stopped.
        for stale in (prog(updated_at=NOW - 120), prog(pid=99)):
            c = card(r, stale)
            self.assertEqual((c['state'], c['reason']), ('failed', 'stopped'))
            self.assertIsNone(c['install'])
        c = card(r, prog(stage='finish', step='Adding it to your library'))
        self.assertEqual((c['stage'], c['state']), ('finish', 'finishing'))
        self.assertEqual(track(c)['install'], 'done')

    def test_installed_is_ready_24h(self):
        r = row(done=True, percent=100.0, installed=True, install_started=iso(NOW - 4000),
                installed_at=iso(NOW - 3600))
        c = card(r)
        self.assertEqual((c['stage'], c['state'], c['overall']), ('ready', 'ready', 1.0))
        self.assertEqual(c['installed_at'], iso(NOW - 3600))
        self.assertEqual(set(track(c).values()) - {'skipped'}, {'done'})
        self.assertEqual(c['need'], {'download': 0, 'install': 0})
        self.assertIsNone(c['eta'])
        self.assertIsNone(card(row(installed=True, installed_at=iso(NOW - 25 * 3600))))
        self.assertIsNone(card(row(installed=True)))         # before installed_at existed
        # "installed" wins over a progress file the CLI left behind.
        self.assertEqual(card(r, prog())['state'], 'ready')
        # Gone from the client after the install: still Ready for the day.
        self.assertEqual(card({**r, 'gone': True})['state'], 'ready')

    def test_waiting_and_failed(self):
        r = row(done=True, percent=100.0, install_started=iso(NOW - 100))
        c = card(r)
        self.assertEqual((c['stage'], c['state']), ('install', 'waiting'))
        self.assertEqual(track(c)['install'], 'active')
        c = card({**r, 'install_failed': iso(NOW - 50)})
        self.assertEqual((c['stage'], c['state'], c['reason']), ('install', 'failed', 'error'))
        self.assertEqual(track(c)['install'], 'failed')
        self.assertIsNone(c['eta'])
        for why, step in (('uac', "The admin prompt was declined"),
                          ('cancelled', "Install cancelled")):
            c = card({**r, 'install_failed': iso(NOW - 50), 'install_failed_reason': why})
            self.assertEqual((c['state'], c['reason'], c['step']), ('failed', why, step))
        # Started a day ago and never wrote a word (killed at once): stopped.
        c = card({**r, 'install_started': iso(NOW - 2 * 86400)})
        self.assertEqual((c['state'], c['reason']), ('failed', 'stopped'))
        # A queued install (claimed, no file yet) is still just waiting.
        self.assertEqual(card({**r, 'install_started': iso(NOW - 3600)})['state'], 'waiting')
        # Started again after the failure: waiting, not failed.
        c = card({**r, 'install_failed': iso(NOW - 200)})
        self.assertEqual(c['state'], 'waiting')
        # A live install beats an older failure mark.
        c = card({**r, 'install_failed': iso(NOW - 200)}, prog())
        self.assertEqual(c['state'], 'installing')

    def test_update_has_no_percent(self):
        r = row(done=True, percent=100.0, install_started=iso(NOW - 100))
        c = card(r, prog(update=True, expected=None))
        self.assertEqual((c['stage'], c['state']), ('install', 'updating'))
        self.assertIsNone(c['overall'])
        self.assertIsNone(c['track'][2]['frac'])
        self.assertIsNone(c['eta'])
        self.assertEqual(c['install']['update'], True)
        self.assertEqual(c['need']['install'], 0)
        # No Original Size known (a torrent added by hand): no install percent.
        c = card({**r, 'original_bytes': None}, prog(expected=None))
        self.assertEqual(c['state'], 'installing')
        self.assertIsNone(c['track'][2]['frac'])
        self.assertIsNone(c['eta'])

    def test_skipped_verify_folds_into_download(self):
        c = card(row())
        weights = {t['stage']: t['weight'] for t in c['track']}
        self.assertEqual([t['stage'] for t in c['track']], ['download', 'verify', 'install',
                                                            'finish'])
        self.assertAlmostEqual(weights['download'], 0.62)
        self.assertEqual(weights['verify'], 0)
        self.assertEqual(track(c)['verify'], 'skipped')
        self.assertAlmostEqual(c['overall'], 0.62 * 0.4)
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        # Nexus's own MD5 check runs: Verify has its share and its bytes.
        r = row(done=True, percent=100.0, install_started=iso(NOW - 100))
        c = card(r, prog(stage='verify', step='Checking files', done=5 * GB, total=10 * GB,
                         written=None, expected=None, rate=None))
        self.assertEqual((c['stage'], c['state']), ('verify', 'verifying'))
        weights = {t['stage']: t['weight'] for t in c['track']}
        self.assertEqual((weights['download'], weights['verify']), (0.52, 0.10))
        self.assertAlmostEqual(c['overall'], 0.52 + 0.05)
        # ... and stays "done" once the install runs (total kept in the file).
        c = card(r, prog(done=10 * GB, total=10 * GB))
        self.assertEqual(track(c)['verify'], 'done')
        self.assertAlmostEqual(c['overall'], 0.62 + 0.33 * 0.4)

    def test_client_recheck_is_verify(self):
        # A finished download the client is re-checking is the Verify stage.
        c = card(row(state='checking', completed=True, done=True, percent=100.0, check=0.43))
        self.assertEqual((c['stage'], c['state']), ('verify', 'verifying'))
        self.assertEqual(track(c), {'download': 'done', 'verify': 'active',
                                    'install': 'todo', 'finish': 'todo'})
        self.assertEqual(c['track'][1]['frac'], 0.43)
        self.assertEqual(c['step'], "Checking files")
        self.assertIsNone(c['eta'])
        # No figure from the client (qBittorrent): indeterminate.
        c = card(row(state='checking', completed=True, done=True, percent=100.0, check=None))
        self.assertEqual(c['state'], 'verifying')
        self.assertIsNone(c['track'][1]['frac'])
        # Never finished: still a Download, with the figure.
        c = card(row(state='checking', completed=False, percent=60.0, check=0.43))
        self.assertEqual((c['stage'], c['state'], c['check']), ('download', 'checking', 0.43))
        # Gone / error / an install in progress keep their own states.
        self.assertIsNone(card(row(state='checking', completed=True, gone=True)))
        c = card(row(state='checking', completed=True, done=True, percent=100.0,
                     error='disk full'))
        self.assertEqual(c['state'], 'error')
        c = card(row(state='checking', completed=True, done=True), prog())
        self.assertEqual(c['state'], 'installing')

    def test_overall_capped_99(self):
        r = row(done=True, percent=100.0, install_started=iso(NOW - 100))
        c = card(r, prog(written=40 * GB))                       # more than the label said
        self.assertAlmostEqual(c['track'][2]['frac'], 0.99)
        c = card(r, prog(stage='finish', step='Adding it to your library'))
        self.assertLessEqual(c['overall'], 0.99)
        self.assertEqual(P.overall({'state': 'finishing', 'track': [
            {'stage': s, 'weight': w, 'frac': 1.0, 'status': 'done'} for s, w in P.STAGES]}),
            0.99)


def drive(id_, mount, free, roles):
    return {'id': id_, 'mount': mount, 'free': free, 'total': 1000 * GB, 'roles': roles}


def sample(drive='1', **kw):
    """One recorded install (pipeline-stats.json): 10 GB in 1000 s, 20 s to finish."""
    x = {'at': NOW - 86400, 'drive': drive, 'written': 10 * GB, 'expected': 10 * GB,
         'install_s': 1000.0, 'verify_bytes': None, 'verify_s': None, 'finish_s': 20.0}
    x.update(kw)
    return x


class Eta(unittest.TestCase):
    """One ETA across the stages that remain, once an install was recorded."""
    INSTALL = 30 * GB / (10 * GB / 1000)            # 3000 s: 30 GB at 10 GB per 1000 s

    def test_no_history_keeps_stage_eta(self):
        for stats in (None, [], [{'junk': 1}]):
            c = card(row(), stats=stats)
            self.assertEqual((c['eta'], c['eta_est'], c['eta_all']), (600, False, False))
        c = card(row())
        self.assertFalse(c['eta_all'])

    def test_one_install_covers_all_stages(self):
        s = [sample()]
        c = card(row(), stats=s)
        self.assertEqual(c['eta'], 600 + self.INSTALL + 20)
        self.assertTrue(c['eta_all'] and c['eta_est'])
        self.assertEqual(c['eta_stage'], 600)
        # Downloaded, waiting: the install and the finish.
        c = card(row(done=True, percent=100.0, state='seeding', eta=None), stats=s)
        self.assertEqual((c['state'], c['eta'], c['eta_all']), ('downloaded',
                                                                self.INSTALL + 20, True))
        c = card(row(done=True, percent=100.0, install_started=iso(NOW - 5)), stats=s)
        self.assertEqual((c['state'], c['eta']), ('waiting', self.INSTALL + 20))
        # Installing: what is left at the live rate, then the finish.
        c = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)), prog(), stats=s)
        self.assertEqual(c['state'], 'installing')
        self.assertEqual(c['eta'], int(18 * GB / (100 << 20)) + 20)
        self.assertTrue(c['eta_all'])
        # Installing with no rate yet: the learned one.
        c = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)),
                 prog(rate=None, written=10 * GB), stats=s)
        self.assertEqual(c['eta'], 20 * GB / (10 * GB / 1000) + 20)
        # Verifying: the rest of it at the learned check rate, install, finish.
        v = [sample(verify_bytes=10 * GB, verify_s=100.0)]
        c = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)),
                 prog(stage='verify', step='Checking files', done=4 * GB, total=10 * GB,
                      written=None, expected=None, rate=None), stats=v)
        self.assertEqual(c['state'], 'verifying')
        self.assertEqual(c['eta'], 60 + self.INSTALL + 20)
        # ... and with no check history, the stage's own (none).
        c = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)),
                 prog(stage='verify', step='Checking files', done=4 * GB, total=10 * GB,
                      written=None, expected=None, rate=None), stats=s)
        self.assertEqual((c['eta'], c['eta_all']), (None, False))

    def test_other_states_keep_the_stage_eta(self):
        s = [sample()]
        for kw in ({'state': 'paused'}, {'state': 'queued'},
                   {'state': 'checking', 'completed': True, 'done': True, 'percent': 100.0},
                   {'state': 'checking'}):
            c = card(row(**kw), stats=s)
            self.assertFalse(c['eta_all'], kw)
            self.assertIsNone(c['eta'], kw)
        c = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)),
                 prog(update=True), stats=s)
        self.assertEqual(c['state'], 'updating')
        self.assertFalse(c['eta_all'])
        c = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)),
                 prog(stage='finish', step='Finishing', written=1, expected=2), stats=s)
        self.assertFalse(c['eta_all'])

    def test_junk_stats_are_no_history(self):
        junk = [sample(written=0), sample(expected='big'), sample(install_s=0),
                sample(verify_bytes='x'), sample(verify_s=True), sample(written=True)]
        for j in junk:
            c = card(row(), stats=[j])
            self.assertEqual((c['eta'], c['eta_all']), (600, False), j)
        self.assertIsNone(P.learned([sample(written=0)], '1'))
        self.assertIsNone(P.eta({'state': 'downloading', 'original_bytes': GB, 'eta_stage': 5,
                                 'drives': {}}, [{'written': 0, 'install_s': 5}]))
        import tempfile
        from unittest import mock
        from nexus import torrent
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, 's.json')
            import json
            f.write_text(json.dumps({'v': 1, 'installs': junk}))
            with mock.patch.object(torrent, 'STATS', f):
                self.assertEqual(torrent.load_stats(), [])
                c = card(row(), stats=torrent.load_stats())
        self.assertEqual((c['eta'], c['eta_all']), (600, False))

    def test_unknown_original_size_keeps_stage_eta(self):
        c = card(row(original_bytes=None), stats=[sample()])
        self.assertEqual((c['eta'], c['eta_all']), (600, False))
        c = card(row(), stats=[sample(expected=None)])           # ratio 1: still fine
        self.assertTrue(c['eta_all'])

    def test_drive_median_and_fallback(self):
        # Twelve installs on drive A; only the last ten count. Their medians.
        a = [sample('A', at=i, written=GB, expected=GB, install_s=1.0 + 99 * (i < 2))
             for i in range(12)]
        b = [sample('B', at=100, written=10 * GB, expected=10 * GB, install_s=10000.0,
                    finish_s=500.0)]
        learned = P.learned(a + b, 'A')
        self.assertEqual(learned['n'], 10)
        self.assertEqual(learned['rate'], GB / 1.0)
        self.assertEqual(learned['finish_s'], 20.0)
        self.assertEqual(P.learned(a + b, 'B')['rate'], GB / 1000)
        self.assertEqual(P.learned(a + b, 'B')['finish_s'], 500.0)
        # A drive with no installs of its own: every drive's last ten.
        fb = P.learned(a + b, 'Z')
        self.assertEqual(fb['n'], 10)
        self.assertEqual(fb['rate'], GB / 1.0)
        self.assertIsNone(P.learned([], 'A'))
        # ratio: written against the label
        r = P.learned([sample(written=12 * GB, expected=10 * GB)], '1')
        self.assertAlmostEqual(r['ratio'], 1.2)
        # The card picks its install drive's history.
        ids = {'install': 'A', 'download': {}}
        c = card(row(done=True, percent=100.0, state='seeding', eta=None), stats=a + b,
                 drive_ids=ids)
        self.assertEqual(c['eta'], 30 * GB / (GB / 1.0) + 20)
        c = card(row(done=True, percent=100.0, state='seeding', eta=None), stats=a + b,
                 drive_ids={'install': 'B', 'download': {}})
        self.assertEqual(c['eta'], 30 * GB / (GB / 1000) + 500)


class Spark(unittest.TestCase):
    """The sparkline's samples: the download rate while downloading, the
    disk rate while installing; the last 120 s."""
    EMPTY = {'kind': None, 'pts': []}

    def test_spark_push_window(self):
        dl = card(row(rate=5 << 20))
        sp = P.spark_push(self.EMPTY, dl, 1000.0)
        self.assertEqual((sp['kind'], sp['pts']), ('download', [(1000.0, 5 << 20)]))
        for t in (1002.0, 1004.0):
            sp = P.spark_push(sp, dl, t)
        self.assertEqual([p[0] for p in sp['pts']], [1000.0, 1002.0, 1004.0])
        # Only the last 120 s stay; the input is never changed.
        before = sp
        sp = P.spark_push(sp, dl, 1121.0)
        self.assertEqual([p[0] for p in sp['pts']], [1002.0, 1004.0, 1121.0])
        self.assertEqual(len(before['pts']), 3)
        # The same moment twice is one sample.
        self.assertEqual(P.spark_push(sp, dl, 1121.2), sp)
        # Installing: the disk rate, a new series (the download's is dropped).
        inst = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)),
                    prog(rate=90 << 20))
        sp2 = P.spark_push(sp, inst, 1123.0)
        self.assertEqual((sp2['kind'], sp2['pts']), ('disk', [(1123.0, 90 << 20)]))
        # No rate yet: nothing to plot. Paused, queued, ready: no series.
        none = card(row(done=True, percent=100.0, install_started=iso(NOW - 300)),
                    prog(rate=None))
        self.assertEqual(P.spark_push(self.EMPTY, none, 1.0), self.EMPTY)
        for kw in ({'state': 'paused'}, {'state': 'queued'}):
            self.assertEqual(P.spark_push(sp, card(row(**kw)), 1130.0), self.EMPTY)
        # A client that reports no rate counts as 0 B/s while downloading.
        self.assertEqual(P.spark_push(self.EMPTY, card(row(rate=0)), 5.0)['pts'], [(5.0, 0)])


class Forecast(unittest.TestCase):
    def test_one_drive(self):
        ids = {'install': '1', 'download': {'/dl': '1'}}
        cs = P.cards([row()], {}, NOW, set(), drive_ids=ids)
        self.assertEqual(cs[0]['drives'], {'download': '1', 'install': '1'})
        out = P.forecast(cs, [drive('1', '/home', 100 * GB, ['download', 'install'])])
        self.assertEqual(len(out), 1)
        d = out[0]
        self.assertEqual((d['to_write'], d['free_after'], d['short_by'], d['about']),
                         (36 * GB, 64 * GB, 0, True))
        self.assertEqual(d['mount'], '/home')

    def test_two_drives_short_by(self):
        ids = {'install': '2', 'download': {'/dl': '1'}}
        rows = [row(), row(hash='b' * 40, percent=0.0, original_bytes=50 * GB)]
        cs = P.cards(rows, {}, NOW, set(), drive_ids=ids)
        out = {d['id']: d for d in P.forecast(cs, [drive('1', '/dl', 20 * GB, ['download']),
                                                   drive('2', '/games', 70 * GB, ['install'])])}
        self.assertEqual(out['1']['to_write'], 16 * GB)
        self.assertEqual(out['1']['short_by'], 0)
        self.assertEqual(out['2']['to_write'], 80 * GB)
        self.assertEqual((out['2']['free_after'], out['2']['short_by']), (-10 * GB, 10 * GB))
        # A card whose folder has no drive here (a NAS) adds nothing.
        cs = P.cards([row(dir='/nas')], {}, NOW, set(), drive_ids=ids)
        self.assertIsNone(cs[0]['drives']['download'])
        out = P.forecast(cs, [drive('1', '/dl', 20 * GB, ['download'])])
        self.assertEqual(out[0]['to_write'], 0)


class Sections(unittest.TestCase):
    """The Downloads page's sections (w-94): In progress, Ready to play,
    Done recently, Other torrents."""

    def setUp(self):
        self.ready = row(hash='r' * 40, slug='ready-game', done=True, percent=100.0,
                         installed=True, install_started=iso(NOW - 4000),
                         installed_at=iso(NOW - 3600))
        self.going = row()

    def split(self, rows, played=None):
        cs = P.cards(rows, {}, NOW, set())
        return P.sections(cs, rows, played or {})

    def keys(self, cards):
        return [c['hash'] for c in cards]

    def test_progress_follows_client_queue(self):
        def r(h, queue, **kw):
            return row(hash=h * 40, queue=queue, **kw)
        rows = [r('c', 2), r('x', None), r('a', 0), r('b', 1),
                r('i', 5, done=True, percent=100.0, install_started=iso(NOW - 60))]
        s = self.split(rows)
        # The install-stage card first (its download is over), then the
        # downloads in the client's order; unknown positions last.
        self.assertEqual(self.keys(s['progress']), ['i' * 40, 'a' * 40, 'b' * 40, 'c' * 40,
                                                    'x' * 40])
        self.assertEqual(s['progress'][1]['queue'], 0)
        self.assertIsNone(s['progress'][4]['queue'])
        # Stable when positions tie or are missing.
        s = self.split([r('b', None), r('a', None)])
        self.assertEqual(self.keys(s['progress']), ['b' * 40, 'a' * 40])

    def test_ready_moves_to_done_when_played_since(self):
        s = self.split([self.going, self.ready])
        self.assertEqual(self.keys(s['progress']), [H])
        self.assertEqual(self.keys(s['ready']), ['r' * 40])
        self.assertEqual(s['done'], [])
        # Played before the install: still Ready.
        s = self.split([self.going, self.ready], {'ready-game': iso(NOW - 7200)})
        self.assertEqual(self.keys(s['ready']), ['r' * 40])
        # Played since: done with, it moves to Done recently.
        s = self.split([self.going, self.ready], {'ready-game': iso(NOW - 60)})
        self.assertEqual(s['ready'], [])
        self.assertEqual([r['hash'] for r in s['done']], ['r' * 40])

    def test_done_after_24h_until_cleared(self):
        old = {**self.ready, 'installed_at': iso(NOW - 30 * 3600)}
        newer = {**old, 'hash': 'n' * 40, 'installed_at': iso(NOW - 26 * 3600)}
        s = self.split([old, newer])
        self.assertEqual((s['progress'], s['ready']), ([], []))
        self.assertEqual([r['hash'] for r in s['done']], ['n' * 40, 'r' * 40])  # newest first
        s = self.split([{**old, 'history_cleared': iso(NOW - 60)}, newer])
        self.assertEqual([r['hash'] for r in s['done']], ['n' * 40])
        # Cleared before a newer install of the same download: shown again.
        s = self.split([{**old, 'history_cleared': iso(NOW - 40 * 3600)}])
        self.assertEqual([r['hash'] for r in s['done']], ['r' * 40])

    def test_no_installed_at_is_not_history(self):
        s = self.split([row(hash='o' * 40, installed=True, done=True, percent=100.0)])
        self.assertEqual(s['done'], [])

    def test_untracked_and_left_rows_go_to_other(self):
        theirs = row(hash='t' * 40, tracked=False)
        seeding = {**self.ready, 'installed_at': iso(NOW - 30 * 3600), 'state': 'seeding'}
        gone_done = {**seeding, 'hash': 'g' * 40, 'gone': True}
        s = self.split([self.going, theirs, seeding, gone_done])
        self.assertEqual([r['hash'] for r in s['other']], ['t' * 40, 'r' * 40])
        self.assertEqual({r['hash'] for r in s['done']}, {'r' * 40, 'g' * 40})
        # A tracked download that was removed before it finished: other, so
        # it can be forgotten.
        lost = row(hash='l' * 40, gone=True)
        self.assertEqual([r['hash'] for r in self.split([lost])['other']], ['l' * 40])


class Stalled(unittest.TestCase):
    def test_no_new_bytes_20_min(self):
        seen = {}
        c = card(row(eta=None))
        self.assertEqual(P.stalled(seen, [c], NOW), set())
        self.assertEqual(P.stalled(seen, [c], NOW + P.STALL_S - 1), set())
        self.assertEqual(P.stalled(seen, [c], NOW + P.STALL_S), {H})
        # New bytes: the clock starts again.
        moved = card(row(eta=None, percent=41.0))
        self.assertEqual(P.stalled(seen, [moved], NOW + P.STALL_S + 5), set())
        self.assertEqual(P.stalled(seen, [moved], NOW + 2 * P.STALL_S + 5), {H})
        # An ETA means the client expects to finish: not stalled.
        self.assertEqual(P.stalled(seen, [card(row(percent=41.0))], NOW + 3 * P.STALL_S),
                         set())

    def test_unknown_bytes_is_not_stalled(self):
        # A magnet still fetching its metadata has no size: no bytes to compare.
        seen = {}
        c = card(row(eta=None, size=None, repack_bytes=None))
        c['download']['done'] = None
        P.stalled(seen, [c], NOW)
        self.assertEqual(P.stalled(seen, [c], NOW + 2 * P.STALL_S), set())
        self.assertEqual(seen, {})

    def test_paused_or_moving_is_not(self):
        seen = {}
        paused = card(row(state='paused', eta=None))
        P.stalled(seen, [paused], NOW)
        self.assertEqual(P.stalled(seen, [paused], NOW + 2 * P.STALL_S), set())
        self.assertEqual(seen, {})
        c = card(row(eta=None))
        P.stalled(seen, [c], NOW)
        P.stalled(seen, [], NOW + 10)                    # it left the cards
        self.assertEqual(seen, {})


class GrabFit(unittest.TestCase):
    PAGE = {'size_bytes': 10 * GB, 'original_bytes': 30 * GB, 'dest': '/home/u/dl',
            'free_bytes': 100 * GB, 'dest_drive': '1', 'dest_mount': '/home',
            'install_dest': '/home/u/Games', 'install_free': 100 * GB, 'install_drive': '1',
            'install_mount': '/home', 'committed': {'1': 65 * GB}}

    def test_same_drive_sums(self):
        fit = P.grab_fit(self.PAGE)
        self.assertTrue(fit['same'])
        self.assertEqual(len(fit['drives']), 1)
        d = fit['drives'][0]
        self.assertEqual((d['where'], d['need'], d['free'], d['committed'], d['short_by']),
                         ('/home', 40 * GB, 100 * GB, 65 * GB, 5 * GB))
        self.assertEqual(fit['short_by'], 5 * GB)
        self.assertEqual(P.grab_fit({**self.PAGE, 'committed': None})['short_by'], 0)

    def test_install_drive_short(self):
        page = {**self.PAGE, 'install_drive': '2', 'install_mount': '/games',
                'install_free': 20 * GB, 'committed': {'2': 5 * GB}}
        fit = P.grab_fit(page)
        self.assertFalse(fit['same'])
        dl, inst = fit['drives']
        self.assertEqual((dl['where'], dl['role'], dl['need'], dl['short_by']),
                         ('/home', 'download', 10 * GB, 0))
        self.assertEqual((inst['where'], inst['role'], inst['need'], inst['short_by']),
                         ('/games', 'install', 30 * GB, 15 * GB))
        self.assertEqual(fit['short_by'], 15 * GB)
        # An older CLI's page (no install keys): the download alone.
        old = {k: self.PAGE[k] for k in ('size_bytes', 'dest', 'free_bytes')}
        fit = P.grab_fit({**old, 'free_bytes': 1 * GB})
        self.assertEqual([(d['where'], d['need'], d['short_by']) for d in fit['drives']],
                         [('/home/u/dl', 10 * GB, 9 * GB)])

    def test_unknown_original_size(self):
        fit = P.grab_fit({**self.PAGE, 'original_bytes': None, 'committed': {}})
        d = fit['drives'][0]
        self.assertEqual((d['need'], d['short_by'], fit['install_known']), (10 * GB, 0, False))
        self.assertTrue(P.grab_fit(self.PAGE)['install_known'])
        # Free space unknown (no client, a folder that isn't here): no verdict.
        fit = P.grab_fit({'size_bytes': 10 * GB, 'dest': None, 'free_bytes': None})
        self.assertEqual((fit['drives'], fit['short_by']), ([], 0))


if __name__ == '__main__':
    unittest.main()
