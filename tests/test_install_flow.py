"""The repack install flow end to end, in a sandbox HOME with a fake `wine`
that "installs" a game wherever Inno's /DIR points (or a fixed folder when
the setup window is used), plus the helpers behind it: name cleanup, the
installer's ProductName, fingerprints, download completeness."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / 'bin' / 'nexus-cli'
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from tests.oskind import linux_only  # noqa: E402
from nexus import cli as fi, features  # noqa: E402
from nexus.cli import runner  # noqa: E402

FAKE_WINE = r'''#!/bin/sh
# wineboot: make a prefix. reg: log it, in order with setup. setup.exe: log
# argv (and its WINEDLLOVERRIDES, to FAKE_LOG.dll), then "install" a game.
if [ "$1" = wineboot ]; then mkdir -p "$WINEPREFIX/drive_c/windows"; exit 0; fi
printf '%s\n' "$*" >> "$FAKE_LOG"
[ "$1" = reg ] && exit 0
printf '%s\n' "$WINEDLLOVERRIDES" >> "$FAKE_LOG.dll"
# What the downloads pipeline sees while setup runs (--from-torrent).
[ -n "$FAKE_PIPE" ] && cat "$FAKE_PIPE"/*.json >> "$FAKE_LOG.pipe" 2>/dev/null
[ "$FAKE_MODE" = fail ] && exit 5
if [ "$FAKE_MODE" = hang ]; then touch "$FAKE_LOG.hang"; sleep 30; fi
dir=""
for a in "$@"; do case "$a" in /DIR=*) dir="${a#/DIR=}";; esac; done
if [ -n "$dir" ]; then
  rel=$(printf '%s' "${dir#C:\\}" | tr '\\' '/')
else
  rel="Interactive/Test Game"
fi
mkdir -p "$WINEPREFIX/drive_c/$rel/bin"
head -c 900000 /dev/zero > "$WINEPREFIX/drive_c/$rel/bin/TestGame.exe"
head -c 600000 /dev/zero > "$WINEPREFIX/drive_c/$rel/Launcher.exe"
exit 0
'''


def version_info_blob(product: str) -> bytes:
    """A minimal VERSIONINFO String entry, as Windows resources store it."""
    key = 'ProductName'.encode('utf-16-le') + b'\0\0'
    value = (product + '\0').encode('utf-16-le')
    head = len(value) // 2
    body = key
    pad = (-(6 + len(body))) % 4
    entry = (6 + len(body) + pad + len(value)).to_bytes(2, 'little') + \
        head.to_bytes(2, 'little') + (1).to_bytes(2, 'little') + body + b'\0' * pad + value
    return b'MZ' + b'\0' * 62 + entry + b'\0' * 64


class Names(unittest.TestCase):
    def test_clean_repack_name(self):
        cases = {
            "The Blood of Dawnwalker [FitGirl Monkey Repack]": ("The Blood of Dawnwalker", None),
            "End of Abyss [FitGirl Repack]": ("End of Abyss", None),
            "Cyberpunk 2077 - v2.3 + 5 DLCs + Bonus Content": ("Cyberpunk 2077", "2.3"),
            "Hades II (2025) [FitGirl Repack]": ("Hades II", None),
            "Some Game - FitGirl Repack": ("Some Game", None),
            "Lies of P - Build 15001234 + DLC": ("Lies of P", "15001234"),
            "007 First Light": ("007 First Light", None),
        }
        for raw, want in cases.items():
            self.assertEqual(fi.clean_repack_name(raw), want, raw)

    def test_product_name_from_versioninfo(self):
        with tempfile.TemporaryDirectory() as t:
            exe = Path(t) / 'setup.exe'
            exe.write_bytes(version_info_blob('Hollow Pines: Remastered        '))
            self.assertEqual(fi.setup_product_name(exe), 'Hollow Pines: Remastered')
            exe.write_bytes(b'MZ' + b'\0' * 200)
            self.assertIsNone(fi.setup_product_name(exe))
            self.assertIsNone(fi.setup_product_name(Path(t) / 'missing.exe'))

    def test_a_unity_games_small_exe_is_found(self):
        # FF XV Pocket Edition: a 0.3 MB exe beside UnityPlayer.dll is the game.
        with tempfile.TemporaryDirectory() as t:
            g = Path(t)
            (g / 'FF15 PE.exe').write_bytes(b'\0' * 300_000)
            (g / 'InstallApp.exe').write_bytes(b'')
            (g / 'unins000.exe').write_bytes(b'\0' * 1_400_000)
            self.assertEqual(fi.exe_candidates(g), [])               # no Unity runtime: too small
            (g / 'UnityPlayer.dll').write_bytes(b'\0')
            self.assertEqual(fi.exe_candidates(g), [(300_000, g / 'FF15 PE.exe')])

    def test_existing_install_root(self):
        pfx = Path('/p/pfx')
        dc = pfx / 'drive_c'
        self.assertEqual(fi.existing_install_root(str(dc / 'Games/X/bin/x.exe'), pfx),
                         dc / 'Games/X')
        self.assertEqual(fi.existing_install_root(str(dc / 'Program Files/X/x.exe'), pfx),
                         dc / 'Program Files/X')
        self.assertEqual(fi.existing_install_root(str(dc / 'Mine/sub/x.exe'), pfx), dc / 'Mine')
        self.assertIsNone(fi.existing_install_root('/elsewhere/x.exe', pfx))
        self.assertIsNone(fi.existing_install_root(None, pfx))
        self.assertEqual(fi.win_path(dc / 'Games/test-game', pfx), 'C:\\Games\\test-game')


def inno_blob(payload: bytes, compressed: bool, prefix: bytes = b'MZ' + b'\0' * 500) -> bytes:
    """A minimal Inno setup-0 block: the tag, a CRC'd block header, then
    4 KiB chunks each led by its CRC (LZMA1 raw with 5 property bytes)."""
    import lzma
    import struct
    import zlib
    if compressed:
        lc, lp, pb, dsz = 3, 0, 2, 1 << 16
        body = bytes([(pb * 5 + lp) * 9 + lc]) + struct.pack('<I', dsz) + lzma.compress(
            payload, format=lzma.FORMAT_RAW, filters=[
                {'id': lzma.FILTER_LZMA1, 'dict_size': dsz, 'lc': lc, 'lp': lp, 'pb': pb}])
    else:
        body = payload
    chunks = b''.join(struct.pack('<I', zlib.crc32(body[i:i + 4096])) + body[i:i + 4096]
                      for i in range(0, len(body), 4096))
    head = struct.pack('<IB', len(chunks), int(compressed))
    tag = b'Inno Setup Setup Data (6.2.2) (u)'.ljust(64, b'\0')
    return prefix + tag + struct.pack('<I', zlib.crc32(head)) + head + chunks


class InnoHeader(unittest.TestCase):
    RUN = ('{app}\\_Redist\\vc_redist.x64.exe {app}\\_Redist\\dxwebsetup.exe '
           '{app}\\_Redist\\oalinst.exe {app}\\_Redist\\PhysX_9.exe '
           '{app}\\_Redist\\setup.exe {app}\\_Redist\\bad;name.exe '
           '{app}\\_Redist\\xnafx40_redist.msi ').encode('utf-16-le') + b'x' * 9000

    def write(self, d, blob, name='setup.exe'):
        p = Path(d) / name
        p.write_bytes(blob)
        return p

    def test_stored_and_compressed_headers(self):
        with tempfile.TemporaryDirectory() as d:
            for compressed in (False, True):
                p = self.write(d, inno_blob(self.RUN, compressed))
                self.assertEqual(fi.inno_header(p), self.RUN, compressed)

    def test_bad_crc_reads_as_nothing(self):
        blob = bytearray(inno_blob(self.RUN, True))
        blob[-100] ^= 0xFF                                  # a damaged chunk
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(fi.inno_header(self.write(d, bytes(blob))))
            self.assertIsNone(fi.inno_header(self.write(d, b'MZ not an installer')))

    def test_only_vc_and_directx_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, inno_blob(self.RUN, True))
            skip = fi.setup_redists(p)
            self.assertIn('vc_redist.x64.exe', skip)
            self.assertIn('dxwebsetup.exe', skip)
            for kept in ('oalinst.exe', 'physx_9.exe', 'setup.exe', 'msiexec.exe'):
                self.assertNotIn(kept, skip)
            self.assertFalse([n for n in skip if set(n) & set(';=, ')])
            ov = fi.silent_setup_overrides(p)
            self.assertIn('vc_redist.x64.exe=d', ov.split(';'))
            self.assertIn('winepulse.drv=d', ov.split(';'))
            self.assertIn('conhost.exe=d', ov.split(';'))          # no host.cmd window
            self.assertNotIn('cmd.exe=d', ov.split(';'))            # {cmd} unpacks with it
            self.assertNotIn('winebrowser.exe=d', ov.split(';'))    # see browser_muted
            self.assertNotIn('setup.exe=d', ov.split(';'))


class BrowserMuted(unittest.TestCase):
    def calls(self, user_reg: str | None, fail=False):
        calls = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runner, '_prefix_reg', lambda a, *_: calls.append(a)):
            if user_reg is not None:
                (Path(d) / 'user.reg').write_text(user_reg)
            try:
                with fi.browser_muted(Path(d), {}, 'wine', None):
                    calls.append('setup')
                    if fail:
                        raise SystemExit(1)
            except SystemExit:
                pass
        return calls

    def test_mutes_then_drops_the_value(self):
        for fail in (False, True):
            c = self.calls(None, fail)
            self.assertEqual(c[0][:5], ['add', r'HKCU\Software\Wine\WineBrowser', '/v',
                                        'Browsers', '/d'])
            self.assertEqual(c[0][5], fi.NO_BROWSER)
            self.assertEqual(c[1], 'setup')
            self.assertEqual(c[2][0], 'delete')

    def test_puts_back_the_prefix_own_browser(self):
        # user.reg as Wine writes it: key names with doubled backslashes.
        reg = ('[Software\\\\Wine\\\\WineBrowser] 1792748419\n#time=1dd\n'
               '"Browsers"="firefox,xdg-open"\n\n[Software\\\\Wine\\\\X] 1\n')
        self.assertEqual(self.calls(reg)[2][:6], ['add', r'HKCU\Software\Wine\WineBrowser',
                                                  '/v', 'Browsers', '/d', 'firefox,xdg-open'])
        # Left behind by an install that was killed: not the prefix's own.
        self.assertEqual(self.calls(reg.replace('firefox,xdg-open', fi.NO_BROWSER))[2][0],
                         'delete')
        # Another key's Browsers is not this one.
        other = '[Software\\\\Wine\\\\X] 1\n"Browsers"="nope"\n'
        self.assertEqual(self.calls(other)[2][0], 'delete')


class VerifyBins(unittest.TestCase):
    """verify_bins reports bytes as the chunks are hashed, so Verify moves
    inside a big file instead of jumping per file."""

    def make(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / 'MD5').mkdir()
        lines = []
        import hashlib
        for name, data in files.items():
            (d / name).write_bytes(data)
            lines.append(f'{hashlib.md5(data).hexdigest()} *{name}')
        (d / 'MD5' / 'fitgirl-bins.md5').write_text('\n'.join(lines))
        return d

    def test_bytes_move_within_a_file(self):
        from nexus.cli import repack
        d = self.make({'a.bin': b'a' * 10_000, 'b.bin': b'b' * 5_000})
        got = []
        with mock.patch.object(repack, '_MD5_CHUNK', 1000), mock.patch('sys.stdout', io.StringIO()):
            self.assertTrue(fi.verify_bins(d, on_bytes=lambda done, total: got.append((done, total))))
        self.assertEqual(got[0], (0, 15_000))
        self.assertEqual(got[-1], (15_000, 15_000))
        self.assertGreater(len(got), 5)                      # more than one per file
        self.assertTrue(all(t == 15_000 for _d, t in got))
        self.assertEqual([x for x, _t in got], sorted(x for x, _t in got))   # never backwards
        self.assertEqual(len({x for x, _t in got}), len(got))                # no repeats
        # A bad file still fails, and the chunk callback is optional.
        (d / 'a.bin').write_bytes(b'z' * 10_000)
        with mock.patch('sys.stdout', io.StringIO()):
            self.assertFalse(fi.verify_bins(d))
        self.assertEqual(fi.compute_md5(d / 'b.bin'), __import__('hashlib').md5(b'b' * 5000).hexdigest())
        seen = []
        with mock.patch.object(repack, '_MD5_CHUNK', 2000):
            fi.compute_md5(d / 'b.bin', on_chunk=seen.append)
        self.assertEqual(seen, [2000, 2000, 1000])

    def test_report_verify_throttle_is_thread_safe(self):
        rep = fi.InstallReport(None)
        ticks = iter(x * 0.1 for x in range(100))
        rep._clock = lambda: next(ticks)
        sent = []
        rep.stage = lambda stage, step, **kw: sent.append(kw['done'])
        for done in range(0, 10):
            rep.verify(done, 10)
        rep.verify(10, 10)
        self.assertEqual(sent[0], 0)
        self.assertEqual(sent[-1], 10)
        self.assertLess(len(sent), 11)                       # throttled to about 1 s


class Report(unittest.TestCase):
    """InstallReport: the install's events, the progress file the downloads
    pipeline reads, and the torrents.json marks."""
    H = 'c' * 40

    def setUp(self):
        from nexus import torrent
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        for p in (mock.patch.object(torrent, 'TRACKED', self.d / 't.json'),
                  mock.patch.object(torrent, 'PIPELINE_DIR', self.d / 'pipeline')):
            p.start()
            self.addCleanup(p.stop)
        torrent.save_tracked({self.H: {'name': 'Test Game', 'original_bytes': 5000,
                                       'installed': False}})
        self.out = io.StringIO()
        p = mock.patch('sys.stdout', self.out)
        p.start()
        self.addCleanup(p.stop)

    def stats_setup(self):
        from nexus import torrent
        p = mock.patch.object(torrent, 'STATS', self.d / 'stats.json')
        p.start()
        self.addCleanup(p.stop)
        self.target = self.d / 'game'
        self.target.mkdir()
        (self.target / 'a.bin').write_bytes(b'x' * 1000)

    def run_install(self, *, update=False, install_s=30.0, hash=True, written=1000,
                    ok=True, verify=True):
        """One install on a clock the test sets: verify 100..140, install
        from 200, last sampled at 200 + install_s, finish 300..304."""
        class Clock:
            t = 0.0

            def __call__(self):
                return self.t
        clock = Clock()
        rep = fi.InstallReport(self.H if hash else None, interval=3600, heartbeat=3600)
        rep._clock = clock
        (self.target / 'a.bin').write_bytes(b'x' * written)
        try:
            with rep:
                rep.begin()
                if verify:
                    clock.t = 100.0
                    rep.verify(0, 800)
                    clock.t = 140.0
                    rep.verify(800, 800)
                rep.stage('install', 'Installing')
                clock.t = 200.0
                with rep.measure(self.target, 5000, update):
                    clock.t = 200.0 + install_s
                    rep.walker.tick()
                clock.t = 300.0
                rep.stage('finish', 'Finishing')
                clock.t = 304.0
                if ok:
                    rep.installed('test-game')
                else:
                    raise RuntimeError('boom')
        except RuntimeError:
            pass
        return rep

    def test_installed_records_stats(self):
        from nexus import torrent
        from nexus.cli.common import drive_of
        self.stats_setup()
        self.run_install()
        got = torrent.load_stats()
        self.assertEqual(len(got), 1)
        x = got[0]
        self.assertEqual(set(x), {'at', 'drive', 'written', 'expected', 'install_s',
                                  'verify_bytes', 'verify_s', 'finish_s'})
        self.assertEqual((x['written'], x['expected'], x['install_s']), (1000, 5000, 30.0))
        self.assertEqual((x['verify_bytes'], x['verify_s'], x['finish_s']), (800, 40.0, 4.0))
        self.assertEqual(x['drive'], drive_of(self.target)['id'])
        self.assertEqual(json.loads((self.d / 'stats.json').read_text())['v'], 1)
        # No verify ran (MD5 is opt-in): those two are null.
        self.run_install(verify=False)
        self.assertEqual([(y['verify_bytes'], y['verify_s']) for y in torrent.load_stats()[1:]],
                         [(None, None)])
        # Not recorded: an update, a failure, no hash, too quick, nothing written.
        before = len(torrent.load_stats())
        self.run_install(update=True)
        self.run_install(ok=False)
        self.run_install(hash=False)
        self.run_install(install_s=9.0)
        self.run_install(written=0)
        self.assertEqual(len(torrent.load_stats()), before)

    def test_stats_never_fail_an_install(self):
        from nexus import torrent
        self.stats_setup()
        (self.d / 'afile').write_text('')      # a folder that can't be made
        with mock.patch.object(torrent, 'STATS', self.d / 'afile' / 'stats.json'):
            rep = self.run_install()
        self.assertTrue(rep.ok)
        self.assertTrue(torrent.load_tracked()[self.H]['installed'])
        self.assertEqual(torrent.load_stats(), [])

    def test_stats_capped_and_atomic(self):
        from nexus import torrent
        self.stats_setup()
        for i in range(55):
            torrent.record_install({'at': i, 'drive': '1', 'written': 1, 'expected': 2,
                                    'install_s': 20.0, 'verify_bytes': None, 'verify_s': None,
                                    'finish_s': 1.0})
        got = torrent.load_stats()
        self.assertEqual([x['at'] for x in got], list(range(5, 55)))
        self.assertEqual([f.name for f in self.d.glob('stats.json*')],
                         ['stats.json', 'stats.json.lock'])        # no .tmp left behind
        # Junk is no history; the next sample starts clean.
        (self.d / 'stats.json').write_text('{nope')
        self.assertEqual(torrent.load_stats(), [])
        (self.d / 'stats.json').write_text(json.dumps(
            {'v': 1, 'installs': [{'at': 1, 'install_s': 'x'}, 'junk', {'at': 2, 'drive': '1',
             'written': 1, 'install_s': 20.0, 'finish_s': 1.0}]}))
        self.assertEqual([x['at'] for x in torrent.load_stats()], [2])
        torrent.record_install({'at': 3, 'drive': '1', 'written': 1, 'install_s': 20.0,
                                'finish_s': 1.0})
        self.assertEqual([x['at'] for x in torrent.load_stats()], [2, 3])
        (self.d / 'stats.json').write_text('[]')
        torrent.record_install({'at': 4, 'drive': '1', 'written': 1, 'install_s': 20.0,
                                'finish_s': 1.0})
        self.assertEqual([x['at'] for x in torrent.load_stats()], [4])

    def file(self):
        f = self.d / 'pipeline' / f'{self.H}.json'
        return json.loads(f.read_text()) if f.exists() else None

    def events(self, name):
        return [json.loads(line.split(' ', 2)[2]) for line in self.out.getvalue().splitlines()
                if line.startswith(f'@nexus {name} ')]

    def test_heartbeat_backoff_and_cleanup(self):
        from nexus import torrent
        target = self.d / 'game'
        target.mkdir()
        (target / 'a.bin').write_bytes(b'x' * 1000)
        rep = fi.InstallReport(self.H, interval=3600, heartbeat=3600)   # ticks by hand only
        with rep:
            rep.slug = 'test-game'
            rep.begin()
            self.assertIsNone(self.file())                       # nothing to say yet
            self.assertTrue(torrent.load_tracked()[self.H]['install_started'])
            rep.stage('install', 'Installing')
            self.assertEqual(self.events('stage'), [{'stage': 'install', 'step': 'Installing'}])
            self.assertEqual(rep.expected, 5000)
            with rep.measure(target, rep.expected, update=False):
                self.assertEqual(self.file()['written'], 0)
                rep.walker.tick()
                self.assertEqual((self.file()['written'], self.file()['expected']), (1000, 5000))
                self.assertEqual(self.events('progress'), [{'written': 1000, 'expected': 5000}])
                # No change: no event (the file is still rewritten).
                (self.d / 'pipeline' / f'{self.H}.json').unlink()
                rep.walker.tick()
                self.assertEqual(self.file()['written'], 1000)
                self.assertEqual(len(self.events('progress')), 1)
                (target / 'b.bin').write_bytes(b'y' * 500)
                rep.walker.tick()
                self.assertEqual(self.events('progress')[-1]['written'], 1500)
            p = self.file()
            self.assertEqual((p['v'], p['hash'], p['slug'], p['stage'], p['pid']),
                             (1, self.H, 'test-game', 'install', os.getpid()))
            self.assertEqual(set(p), {'v', 'hash', 'slug', 'stage', 'step', 'written',
                                      'expected', 'rate', 'done', 'total', 'update', 'dest',
                                      'started_at', 'updated_at', 'pid'})
            rep.installed('test-game')
        self.assertIsNone(self.file())
        meta = torrent.load_tracked()[self.H]
        self.assertTrue(meta['installed'] and meta['installed_at'])
        self.assertNotIn('install_failed', meta)
        self.assertFalse(rep.walker.is_alive())

        # A download's install backs off on a slow walk: 10x its own time,
        # at least the interval, at most 20 s.
        clock = iter([0.0, 0.1, 10.0, 15.0, 20.0, 20.05])
        t = fi.InstallProgress(target, interval=3.0, clock=lambda: next(clock))
        t.tick()
        self.assertEqual(t.wait_s(), 3.0)
        t.tick()
        self.assertEqual(t.wait_s(), 20.0)
        t.tick()
        self.assertEqual(t.wait_s(), 3.0)
        # Any other install keeps the plain 3 s.
        clock = iter([10.0, 15.0])
        t = fi.InstallProgress(target, interval=3.0, clock=lambda: next(clock), backoff=False)
        t.tick()
        self.assertEqual(t.wait_s(), 3.0)

    def test_heartbeat_survives_a_slow_walk(self):
        import threading
        from nexus import cleanup
        target = self.d / 'game'
        target.mkdir()
        walking, release = threading.Event(), threading.Event()

        def slow(path):
            walking.set()
            release.wait(10)
            return 7
        f = self.d / 'pipeline' / f'{self.H}.json'
        with mock.patch.object(cleanup, 'dir_size', slow), \
                fi.InstallReport(self.H, interval=0.01, heartbeat=0.05) as rep:
            rep.begin()
            rep.stage('install', 'Installing')
            with rep.measure(target, 5000):
                self.assertTrue(walking.wait(5))
                f.unlink()                       # the walk hangs: the heartbeat goes on
                deadline = time.monotonic() + 5
                while not f.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(f.exists())
                release.set()
            rep.installed('test-game')

    def test_nothing_marked_before_begin_and_an_abort_restores(self):
        from nexus import torrent
        before = torrent.load_tracked()
        with self.assertRaises(SystemExit):     # not a folder, the safety gate said no
            with fi.InstallReport(self.H):
                sys.exit(1)
        self.assertEqual(torrent.load_tracked(), before)
        with torrent.edit_tracked() as t:
            t[self.H]['install_started'] = '2026-01-01T00:00:00'     # claimed by the GUI
        with self.assertRaises(SystemExit):     # "Proceed? n": Aborted
            with fi.InstallReport(self.H) as rep:
                rep.begin()
                rep.stage('install', 'Getting ready')
                sys.exit(0)
        meta = torrent.load_tracked()[self.H]
        self.assertEqual(meta['install_started'], '2026-01-01T00:00:00')
        self.assertNotIn('install_failed', meta)
        self.assertIsNone(self.file())

    def test_a_failed_exit_marks_and_cleans_up(self):
        from nexus import torrent
        with self.assertRaises(SystemExit):
            with fi.InstallReport(self.H) as rep:
                rep.begin()
                rep.stage('verify', 'Checking files', done=1, total=2)
                self.assertEqual((self.file()['done'], self.file()['total']), (1, 2))
                sys.exit(1)
        self.assertIsNone(self.file())
        meta = torrent.load_tracked()[self.H]
        self.assertTrue(meta['install_failed'])
        self.assertEqual(meta['install_failed_reason'], 'error')
        self.assertFalse(meta['installed'])
        with self.assertRaises(SystemExit):
            with fi.InstallReport(self.H) as rep:
                rep.begin()
                rep.failed('uac')
                sys.exit(1)
        self.assertEqual(torrent.load_tracked()[self.H]['install_failed_reason'], 'uac')

    def test_cancel_records_the_end(self):
        # The GUI's Stop (SIGTERM, or Windows' cancel event) ends the process
        # with os._exit, past __exit__: the report's cancel hook runs first.
        from nexus import torrent
        from nexus.cli import common
        with fi.InstallReport(self.H) as rep:
            rep.begin()
            rep.stage('install', 'Installing')
            self.assertIsNotNone(self.file())
            common.run_cancel_hooks()
        self.assertIsNone(self.file())
        meta = torrent.load_tracked()[self.H]
        self.assertEqual(meta['install_failed_reason'], 'cancelled')
        self.assertFalse(meta['installed'])
        common.run_cancel_hooks()                # unregistered on exit: nothing runs

    def test_verify_events_are_throttled(self):
        rep = fi.InstallReport(None)
        clock = iter([0.0, 0.2, 1.5, 1.6])
        rep._clock = lambda: next(clock)
        with rep:
            for done in (0, 10, 20, 100):
                rep.verify(done, 100)
        self.assertEqual([e['done'] for e in self.events('stage')], [0, 20, 100])

    def test_the_report_never_fails_an_install(self):
        from nexus import torrent
        (self.d / 'pipeline').write_text('a file where the folder goes')
        with fi.InstallReport(self.H) as rep:
            rep.begin()
            rep.stage('install', 'Installing')                   # can't write: no matter
            with rep.measure(self.d, None):
                rep.walker.tick()
            rep.installed('test-game')
        with fi.InstallReport('not-a-hash') as rep:              # events only
            rep.begin()
            rep.stage('install', 'Installing')
        with fi.InstallReport(None) as rep:
            rep.begin()
            rep.stage('finish', 'Adding it to your library')
        self.assertEqual(len(self.events('stage')), 3)
        self.assertTrue(torrent.load_tracked()[self.H]['installed'])


@linux_only
class InstallFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        h = self.home = Path(self.tmp.name)
        self.bin = h / 'fakebin'
        self.bin.mkdir()
        (self.bin / 'wine').write_text(FAKE_WINE)
        (self.bin / 'gio').write_text('#!/bin/sh\nexit 1\n')     # Trash: the manual path
        for f in self.bin.iterdir():
            f.chmod(0o755)
        tests.sandbox.config_dir(h).mkdir(parents=True)
        (tests.sandbox.config_dir(h) / 'config.toml').write_text(
            '[integrations]\nsteam = false\ndesktop = false\nheroic = false\n'
            '[runner]\nbackend = "wine"\n'
            f'[install]\nbase = "{h}/Games/FitGirl"\nscan_dir = "{h}/Downloads"\n')
        self.repack = h / 'Downloads' / 'Test Game [FitGirl Repack]'
        (self.repack / 'MD5').mkdir(parents=True)
        (self.repack / 'setup.exe').write_bytes(b'MZ' + b'\0' * 100)
        (self.repack / 'fg-01.bin').write_bytes(b'x' * 10)
        (self.repack / 'MD5' / 'fitgirl-bins.md5').write_text(
            '; comment\n0123456789abcdef0123456789abcdef *..\\fg-01.bin\n')
        self.log = h / 'wine.log'

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, mode='ok', check=True):
        env = {**os.environ, **tests.sandbox.home_env(self.home), 'STEAMGRIDDB_API_KEY': '',
               'PATH': f"{self.bin}:/usr/bin:/bin", 'FAKE_LOG': str(self.log),
               'FAKE_MODE': mode, 'XDG_CACHE_HOME': str(self.home / 'cache'),
               'FAKE_PIPE': str(tests.sandbox.data_dir(self.home) / 'pipeline')}
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        r = subprocess.run([sys.executable, str(CLI), *args], env=env, text=True,
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        if check and r.returncode != 0:
            self.fail(f"{args} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def install(self, *extra, **kw):
        return self.cli(str(self.repack), '--yes', '--no-artwork', '--skip-mem-check',
                        '--skip-disk-check', *extra, **kw)

    def events(self, out, name):
        return [json.loads(line.split(' ', 2)[2]) for line in out.splitlines()
                if line.startswith(f'@nexus {name} ')]

    def registry(self):
        return json.loads((tests.sandbox.data_dir(self.home) / 'games.json').read_text())['games']

    def test_silent_install_by_default(self):
        r = self.install()
        argv = self.log.read_text()
        self.assertIn('/VERYSILENT', argv)
        self.assertIn('/DIR=C:\\Games\\test-game', argv)
        game = self.registry()['test-game']
        self.assertTrue(game['exe'].endswith('drive_c/Games/test-game/bin/TestGame.exe'))
        self.assertEqual(game['repack_fingerprint'], fi.repack_fingerprint(self.repack))
        self.assertEqual(game['repack_source'], str(self.repack))
        cands = self.events(r.stdout, 'exe-candidates')[0]
        self.assertEqual(cands['chosen'], game['exe'])
        self.assertEqual(len(cands['candidates']), 2)
        self.assertEqual(self.events(r.stdout, 'installed')[0]['slug'], 'test-game')
        self.assertFalse((self.home / 'Games/FitGirl/test-game' / fi.INCOMPLETE_MARKER).exists())

    def test_setup_runs_with_the_browser_muted(self):
        self.install()
        calls = self.log.read_text().splitlines()
        self.assertTrue(calls[0].startswith('reg add HKCU\\Software\\Wine\\WineBrowser '
                                            '/v Browsers /d nexus-no-browser'), calls)
        self.assertIn('setup.exe', calls[1])
        self.assertTrue(calls[2].startswith('reg delete '), calls)

    def test_silent_setup_opens_no_console_window(self):
        # FitGirl's last [Run] step runs host.cmd through cmd.exe; with no
        # console host, it runs without a window popping up.
        self.install()
        dlls = Path(f'{self.log}.dll').read_text().split(';')
        self.assertIn(f'{fi.CONSOLE_HOST}=d', [d.strip() for d in dlls])

    def test_interactive_shows_the_setup_window(self):
        self.install('--interactive')
        self.assertNotIn('/VERYSILENT', self.log.read_text())
        self.assertNotIn(fi.CONSOLE_HOST, Path(f'{self.log}.dll').read_text())
        self.assertIn('/Interactive/Test Game/bin/TestGame.exe', self.registry()['test-game']['exe'])

    def test_config_can_turn_silent_off(self):
        cfg = tests.sandbox.config_dir(self.home) / 'config.toml'
        cfg.write_text(cfg.read_text() + 'unattended = false\n')
        self.install()
        self.assertNotIn('/VERYSILENT', self.log.read_text())

    def test_failed_install_is_resumable_then_discardable(self):
        r = self.install('--force', mode='fail', check=False)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.events(r.stdout, 'retry-interactive'))
        game_dir = self.home / 'Games/FitGirl/test-game'
        marker = json.loads((game_dir / fi.INCOMPLETE_MARKER).read_text())
        self.assertEqual(marker['argv'][0], str(self.repack))
        self.assertNotIn('--force', marker['argv'])
        lib = json.loads(self.cli('--library', '--json').stdout)['games']
        inc = [g for g in lib if g.get('incomplete')]
        self.assertEqual([g['slug'] for g in inc], ['test-game'])
        self.assertEqual(inc[0]['incomplete']['argv'], marker['argv'])
        # Resume = the same arguments; it finishes the job.
        self.cli(*marker['argv'])
        self.assertIn('test-game', self.registry())
        self.assertFalse((game_dir / fi.INCOMPLETE_MARKER).exists())

    def test_discard_only_unfinished_installs(self):
        self.install(mode='fail', check=False)
        game_dir = self.home / 'Games/FitGirl/test-game'
        self.assertTrue(game_dir.is_dir())
        self.cli('--discard-incomplete', 'test-game')
        self.assertFalse(game_dir.exists())
        self.assertTrue(any((self.home / '.local/share/Trash/files').iterdir()))
        self.install()
        r = self.cli('--discard-incomplete', 'test-game', check=False)
        self.assertEqual(r.returncode, 1)                 # a library game: never
        self.assertTrue(game_dir.is_dir())

    def test_scan_flags_a_new_release_as_update(self):
        self.install()
        scan = json.loads(self.cli('--scan', '--json').stdout)['candidates'][0]
        self.assertTrue(scan['installed'])
        self.assertTrue(scan['same_release'])
        self.assertIsNone(scan['update_for'])
        self.assertTrue(scan['complete'])
        (self.repack / 'MD5' / 'fitgirl-bins.md5').write_text(
            'ffffffffffffffffffffffffffffffff *..\\fg-01.bin\n')
        scan = json.loads(self.cli('--scan', '--json').stdout)['candidates'][0]
        self.assertEqual(scan['update_for'], 'test-game')
        self.assertTrue(json.loads(self.cli('--detect', str(self.repack)).stdout)['update'])

    def test_update_installs_over_the_existing_game(self):
        self.install('--interactive')
        (self.repack / 'MD5' / 'fitgirl-bins.md5').write_text(
            'ffffffffffffffffffffffffffffffff *..\\fg-01.bin\n')
        self.log.unlink()
        self.install()
        self.assertIn('/DIR=C:\\Interactive', self.log.read_text())

    def test_scan_sees_unfinished_downloads(self):
        (self.repack / 'fg-01.bin').unlink()
        scan = json.loads(self.cli('--scan', '--json').stdout)['candidates'][0]
        self.assertFalse(scan['complete'])
        (self.repack / 'fg-01.bin').write_bytes(b'x')
        (self.repack / 'fg-02.bin.part').write_bytes(b'x')
        self.assertFalse(json.loads(self.cli('--scan', '--json').stdout)
                         ['candidates'][0]['complete'])

    def test_trash_repack_after_install(self):
        r = self.install('--trash-repack')
        self.assertFalse(self.repack.exists())
        self.assertTrue(self.events(r.stdout, 'installed')[0]['repack_trashed'])

    def test_trash_repack_dir_refuses_non_repacks(self):
        other = self.home / 'Documents'
        other.mkdir()
        self.assertEqual(self.cli('--trash-repack-dir', str(other), check=False).returncode, 1)
        self.assertTrue(other.exists())
        self.cli('--trash-repack-dir', str(self.repack))
        self.assertFalse(self.repack.exists())

    HASH = 'ab' * 20
    # The Wine/Proton installer's reports; the Windows one: test_install_win.
    linux_installer = unittest.skipUnless(features.has('proton'),
                                          "the Windows feature set installs with setup.exe")

    def track(self, **meta):
        f = tests.sandbox.data_dir(self.home) / 'torrents.json'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({self.HASH: {'name': 'Test Game', 'installed': False,
                                             'install_started': '2026-01-01T00:00:00',
                                             **meta}}))
        return f

    @linux_installer
    def test_from_torrent_stage_events_and_no_file_left(self):
        tracked = self.track(original_bytes=5_000_000)
        r = self.install('--from-torrent', self.HASH)
        stages = [(e['stage'], e['step']) for e in self.events(r.stdout, 'stage')]
        self.assertEqual([s for s, _ in stages], ['install', 'install', 'finish', 'finish'])
        self.assertIn(('install', 'Installing'), stages)
        # While setup ran, the progress file said so, with the size to expect.
        seen = json.loads(Path(f'{self.log}.pipe').read_text())
        self.assertEqual((seen['v'], seen['hash'], seen['slug'], seen['stage']),
                         (1, self.HASH, 'test-game', 'install'))
        self.assertEqual((seen['expected'], seen['update'], seen['written']),
                         (5_000_000, False, 0))
        self.assertTrue(seen['dest'].endswith('drive_c/Games/test-game'), seen['dest'])
        self.assertIsInstance(seen['pid'], int)
        self.assertLessEqual(seen['started_at'], seen['updated_at'])
        meta = json.loads(tracked.read_text())[self.HASH]
        self.assertTrue(meta['installed'])
        self.assertTrue(meta['installed_at'])
        self.assertNotIn('install_failed', meta)
        self.assertEqual(list((tests.sandbox.data_dir(self.home) / 'pipeline').glob('*')), [])
        # Installed on top (an update): no size to expect.
        Path(f'{self.log}.pipe').unlink()
        self.install('--from-torrent', self.HASH)
        seen = json.loads(Path(f'{self.log}.pipe').read_text())
        self.assertEqual((seen['expected'], seen['update']), (None, True))

    @linux_installer
    def test_failed_install_marks_torrent(self):
        tracked = self.track(original_bytes=5_000_000)
        r = self.install('--from-torrent', self.HASH, mode='fail', check=False)
        self.assertEqual(r.returncode, 1)
        meta = json.loads(tracked.read_text())[self.HASH]
        self.assertFalse(meta['installed'])
        self.assertGreaterEqual(meta['install_failed'], meta['install_started'])
        self.assertEqual(list((tests.sandbox.data_dir(self.home) / 'pipeline').glob('*')), [])
        # Without --from-torrent nothing is written or marked.
        self.install(mode='fail', check=False)
        self.assertEqual(json.loads(tracked.read_text())[self.HASH], meta)

    @linux_installer
    def test_stop_from_the_gui_records_the_end(self):
        import signal
        tracked = self.track(original_bytes=5_000_000)
        env = {**os.environ, **tests.sandbox.home_env(self.home), 'STEAMGRIDDB_API_KEY': '',
               'PATH': f"{self.bin}:/usr/bin:/bin", 'FAKE_LOG': str(self.log),
               'FAKE_MODE': 'hang', 'XDG_CACHE_HOME': str(self.home / 'cache')}
        proc = subprocess.Popen(
            [sys.executable, str(CLI), str(self.repack), '--yes', '--no-artwork',
             '--skip-mem-check', '--skip-disk-check', '--from-torrent', self.HASH],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            hang = Path(f'{self.log}.hang')
            deadline = time.monotonic() + 30
            while not hang.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(hang.exists())
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=20), 130)
        finally:
            if proc.poll() is None:
                proc.kill()
        meta = json.loads(tracked.read_text())[self.HASH]
        self.assertEqual(meta['install_failed_reason'], 'cancelled')
        self.assertEqual(list((tests.sandbox.data_dir(self.home) / 'pipeline').glob('*')), [])

    def test_nothing_marked_when_the_folder_is_missing(self):
        tracked = self.track()
        before = tracked.read_text()
        r = self.cli(str(self.home / 'nope'), '--yes', '--from-torrent', self.HASH, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(tracked.read_text(), before)

    @linux_installer
    def test_verify_reports_bytes(self):
        self.track()
        md5 = __import__('hashlib').md5(b'x' * 10).hexdigest()
        (self.repack / 'MD5' / 'fitgirl-bins.md5').write_text(f'{md5} *..\\fg-01.bin\n')
        r = self.install('--from-torrent', self.HASH, '--verify')
        verify = [e for e in self.events(r.stdout, 'stage') if e['stage'] == 'verify']
        self.assertEqual(verify[-1], {'stage': 'verify', 'step': 'Checking files',
                                      'done': 10, 'total': 10})
        seen = json.loads(Path(f'{self.log}.pipe').read_text())
        self.assertEqual((seen['stage'], seen['total']), ('install', 10))     # it ran

    def test_scan_dirs_list(self):
        cfg = tests.sandbox.config_dir(self.home) / 'config.toml'
        more = self.home / 'Games/Install'
        more.mkdir(parents=True)
        cfg.write_text(cfg.read_text() + f'scan_dirs = ["{more}"]\n')
        data = json.loads(self.cli('--scan', '--json').stdout)
        self.assertEqual(data['scan_dirs'], [str(self.home / 'Downloads'), str(more)])


if __name__ == '__main__':
    unittest.main()
