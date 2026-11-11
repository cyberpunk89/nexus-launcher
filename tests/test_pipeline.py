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
