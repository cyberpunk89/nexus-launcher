"""The repack install flow end to end, in a sandbox HOME with a fake `wine`
that "installs" a game wherever Inno's /DIR points (or a fixed folder when
the setup window is used), plus the helpers behind it: name cleanup, the
installer's ProductName, fingerprints, download completeness."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / 'bin' / 'nexus-cli'
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import cli as fi  # noqa: E402

FAKE_WINE = r'''#!/bin/sh
# wineboot: make a prefix. setup.exe: log argv, then "install" a game.
if [ "$1" = wineboot ]; then mkdir -p "$WINEPREFIX/drive_c/windows"; exit 0; fi
printf '%s\n' "$*" >> "$FAKE_LOG"
[ "$FAKE_MODE" = fail ] && exit 5
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
            self.assertIn('winebrowser.exe=d', ov.split(';'))
            self.assertNotIn('setup.exe=d', ov.split(';'))


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
        (h / '.config/nexus').mkdir(parents=True)
        (h / '.config/nexus/config.toml').write_text(
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
        env = {**os.environ, 'HOME': str(self.home), 'STEAMGRIDDB_API_KEY': '',
               'PATH': f"{self.bin}:/usr/bin:/bin", 'FAKE_LOG': str(self.log),
               'FAKE_MODE': mode, 'XDG_CACHE_HOME': str(self.home / 'cache')}
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
        return json.loads((self.home / '.local/share/nexus/games.json').read_text())['games']

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

    def test_interactive_shows_the_setup_window(self):
        self.install('--interactive')
        self.assertNotIn('/VERYSILENT', self.log.read_text())
        self.assertIn('/Interactive/Test Game/bin/TestGame.exe', self.registry()['test-game']['exe'])

    def test_config_can_turn_silent_off(self):
        cfg = self.home / '.config/nexus/config.toml'
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

    def test_scan_dirs_list(self):
        cfg = self.home / '.config/nexus/config.toml'
        more = self.home / 'Games/Install'
        more.mkdir(parents=True)
        cfg.write_text(cfg.read_text() + f'scan_dirs = ["{more}"]\n')
        data = json.loads(self.cli('--scan', '--json').stdout)
        self.assertEqual(data['scan_dirs'], [str(self.home / 'Downloads'), str(more)])


if __name__ == '__main__':
    unittest.main()
