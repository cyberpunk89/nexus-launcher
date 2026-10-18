"""Tests for nexus/protons.py: asset choice, verified install, unsafe archives."""

import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from nexus import protons as PR  # noqa: E402


def make_tar(path: Path, members: dict, symlinks: dict | None = None):
    with tarfile.open(path, 'w:gz') as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o755
            tf.addfile(ti, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            tf.addfile(ti)


class Assets(unittest.TestCase):
    def test_pick_host_arch_never_other(self):
        assets = [{'name': 'GE-Proton11-7-aarch64.tar.gz'}, {'name': 'GE-Proton11-7-x86_64.tar.gz'}]
        self.assertEqual(PR.pick_asset(assets, ('.tar.gz',), 'x86_64')['name'],
                         'GE-Proton11-7-x86_64.tar.gz')
        self.assertIsNone(PR.pick_asset(assets[:1], ('.tar.gz',), 'x86_64'))
        self.assertEqual(PR.pick_asset([{'name': 'GE-Proton10-3.tar.gz'}], ('.tar.gz',),
                                       'x86_64')['name'], 'GE-Proton10-3.tar.gz')

    def test_version_key(self):
        self.assertGreater(PR.version_key('GE-Proton11-7'), PR.version_key('GE-Proton11-5-x86_64')[:2])


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.dest = self.d / 'compatibilitytools.d'
        self.dest.mkdir()
        self.src = self.d / 'GE-ProtonT-1.tar.gz'
        make_tar(self.src, {'GE-ProtonT-1/proton': b'#!/bin/sh\n',
                            'GE-ProtonT-1/files/bin/wine': b'\x7fELF'},
                 {'GE-ProtonT-1/files/bin/wine64': 'wine'})
        self.sha = hashlib.sha512(self.src.read_bytes()).hexdigest()
        self.rel = {'tag': 'GE-ProtonT-1', 'tar_name': self.src.name,
                    'tar_url': 'https://github.com/x/releases/download/a.tar.gz',
                    'sha_url': 'https://github.com/x/releases/download/a.sha512sum'}

    def tearDown(self):
        self.tmp.cleanup()

    def _patched(self, sha_text):
        def fake_download(url, dest, progress=None, timeout=60):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.src, dest)
            if progress:
                progress(50, 100)
            return dest.stat().st_size
        return (mock.patch.object(PR.net, 'download', fake_download),
                mock.patch.object(PR.net, 'get', return_value=sha_text.encode()),
                mock.patch.object(PR, 'CACHE_DIR', self.d / 'cache'))

    def test_verified_install(self):
        lines = []
        a, b, c = self._patched(f"{self.sha}  {self.src.name}\n")
        with a, b, c:
            final = PR.install(self.rel, say=lines.append, dest_dir=self.dest)
        self.assertEqual(final, self.dest / 'GE-ProtonT-1')
        self.assertTrue((final / 'proton').exists())
        self.assertTrue((final / 'files/bin/wine64').is_symlink())
        self.assertTrue(any(l.startswith('[3/3]') for l in lines))
        self.assertFalse(list((self.d / 'cache' / 'downloads').glob('*')))   # archive cleaned

    def test_tampered_download_is_refused(self):
        a, b, c = self._patched(f"{'0' * 128}  {self.src.name}\n")
        with a, b, c, self.assertRaises(PR.InstallError):
            PR.install(self.rel, say=lambda *_: None, dest_dir=self.dest)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_path_traversal_is_refused(self):
        bad = self.d / 'bad.tar.gz'
        make_tar(bad, {'GE-X/proton': b'x', '../../evil': b'x'})
        with self.assertRaises(PR.InstallError):
            PR.extract(bad, self.dest)
        self.assertFalse((self.d / 'evil').exists())

    def test_escaping_symlink_is_refused(self):
        bad = self.d / 'bad2.tar.gz'
        make_tar(bad, {'GE-Y/proton': b'x'}, {'GE-Y/link': '/etc/passwd'})
        with self.assertRaises(PR.InstallError):
            PR.extract(bad, self.dest)
        self.assertFalse((self.dest / 'GE-Y').exists())

    def test_archive_without_proton_leaves_nothing_behind(self):
        bad = self.d / 'notproton.tar.gz'
        make_tar(bad, {'Something-1/readme.txt': b'x'})
        with self.assertRaises(PR.InstallError):
            PR.extract(bad, self.dest)
        # Nothing for Steam to list, and the next try isn't "already exists".
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_reinstall_replaces_only_after_the_new_copy_is_ready(self):
        old = self.dest / 'GE-ProtonT-1'
        (old / 'files').mkdir(parents=True)
        (old / 'proton').write_text('old')
        moved = []

        def to_trash(p):
            moved.append(p.name)
            shutil.move(str(p), str(self.d / 'trash'))
        a, b, c = self._patched(f"{self.sha}  {self.src.name}\n")
        with a, b, c:
            with self.assertRaises(PR.InstallError):            # no replace: refused
                PR.install(self.rel, say=lambda *_: None, dest_dir=self.dest)
            final = PR.install(self.rel, say=lambda *_: None, dest_dir=self.dest,
                               replace=to_trash)
        self.assertEqual(moved, ['GE-ProtonT-1'])
        self.assertEqual((final / 'proton').read_bytes(), b'#!/bin/sh\n')
        self.assertEqual((self.d / 'trash' / 'proton').read_text(), 'old')
        # A bad download never touches the installed copy.
        a, b, c = self._patched(f"{'0' * 128}  {self.src.name}\n")
        with a, b, c, self.assertRaises(PR.InstallError):
            PR.install(self.rel, say=lambda *_: None, dest_dir=self.dest, replace=to_trash)
        self.assertEqual(moved, ['GE-ProtonT-1'])
        self.assertTrue((final / 'proton').exists())


class Damage(unittest.TestCase):
    """A build whose Windows DLLs were rewritten after it was installed and
    now equal another build's (as Steam left GE-Proton11-9 on 2026-10-17)."""

    def build(self, root: Path, name: str, dlls: dict, born: float, changed: float | None):
        b = root / name
        pe = b / 'files/lib/wine/x86_64-windows'
        pe.mkdir(parents=True)
        for f in ('proton', 'version'):
            (b / f).write_text(name)
            os.utime(b / f, (born, born))
        for n, data in dlls.items():
            (pe / n).write_bytes(data)
            t = changed or born
            os.utime(pe / n, (t, t))
        return b

    def test_damage(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            t = 1_700_000_000
            cachy = self.build(root, 'proton-cachyos', {'win32u.dll': b'C', 'user32.dll': b'CU'},
                               t, None)
            ge = self.build(root, 'GE-Proton11-9-x86_64',
                            {'win32u.dll': b'C', 'user32.dll': b'CU'}, t, t + 86400)
            fresh = self.build(root, 'GE-Proton11-7-x86_64',
                               {'win32u.dll': b'G', 'user32.dll': b'GU'}, t, None)
            touched = self.build(root, 'GE-Proton10-1-x86_64',        # newer, but its own
                                 {'win32u.dll': b'X'}, t, t + 86400)
            builds = [cachy, ge, fresh, touched]
            why = PR.damage(ge, builds)
            self.assertIn("match proton-cachyos's", why)
            self.assertIn('win32u.dll', why)
            self.assertIsNone(PR.damage(cachy, builds))
            self.assertIsNone(PR.damage(fresh, builds))
            self.assertIsNone(PR.damage(touched, builds))
            self.assertIsNone(PR.damage(ge, [ge]))                  # nothing to compare with

    def test_tag_of(self):
        self.assertEqual(PR.tag_of('GE-Proton11-9-x86_64'), 'GE-Proton11-9')
        self.assertEqual(PR.tag_of('GE-Proton10-34'), 'GE-Proton10-34')
        self.assertIsNone(PR.tag_of('proton-cachyos-slr'))


if __name__ == '__main__':
    unittest.main()
