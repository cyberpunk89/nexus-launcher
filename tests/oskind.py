"""Skip decorators for tests that only make sense on one OS: Proton, umu,
/proc, compatdata and btrfs are Linux-only; the Windows platform layer is
tested only on Windows."""

import sys
import unittest

IS_WINDOWS = sys.platform == 'win32'

linux_only = unittest.skipIf(IS_WINDOWS, "Linux-only")
windows_only = unittest.skipUnless(IS_WINDOWS, "Windows-only")


def windows_todo(task: str, why: str):
    """Skip on Windows until the port task `task` (a tracker id like 'p2-09')
    makes the test pass there. Grep for the id to find what a task unblocks."""
    return unittest.skipIf(IS_WINDOWS, f"Windows port {task}: {why}")


def why_not_private(path) -> str:
    """'' when only this user can read `path`: mode 0600 on Linux; on Windows
    an ACL with nothing inherited and no other ordinary account on it
    (platform.keep_private). SYSTEM and Administrators may stay: they can
    read every file anyway, and it's what Python's own 0o700 gives.
    Otherwise what's wrong, for the assertion message."""
    import os
    import subprocess
    if not IS_WINDOWS:
        mode = os.stat(path).st_mode & 0o777
        return '' if mode == 0o600 else f"mode {mode:o}"
    out = subprocess.run(['icacls', str(path)], capture_output=True, text=True,
                         errors='replace').stdout
    me = os.environ.get('USERNAME', '').lower()
    aces = [ln.strip() for ln in out.replace(str(path), '', 1).splitlines()
            if ':(' in ln]
    allowed = (me, 'nt authority\\system', 'builtin\\administrators', 'owner rights')
    ok = bool(aces) and all('(I)' not in a and any(n in a.lower().split(':(')[0] for n in allowed)
                            for a in aces)
    return '' if ok else f"icacls (user {me!r}):\n{out}"


def is_private(path) -> bool:
    return not why_not_private(path)
