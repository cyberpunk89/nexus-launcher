"""Which Nexus processes are running, without Qt. The window listens on a
socket (nexus/gui/app.py InstanceServer, a QLocalServer — a plain Unix
socket on Linux), and a second `nexus` launch or the background agent hands
it an argv as one JSON line. The background agent (nexus/cli/agent.py)
holds an flock on AGENT_LOCK while it runs."""

import fcntl
import json
import os
import socket

from . import paths as NX

# In the user's private runtime dir. Without one, a 0700 folder of our own,
# never a name in shared /tmp another user could take first.
INSTANCE = os.environ.get("NEXUS_INSTANCE") or (
    os.path.join(os.environ['XDG_RUNTIME_DIR'], 'nexus.sock')
    if os.path.isdir(os.environ.get('XDG_RUNTIME_DIR') or '')
    else str(NX.CACHE_DIR / 'run' / 'nexus.sock'))
INSTANCE_MAX = 64 << 10           # bytes one forwarded launch may send


def forward(argv: list[str], timeout: float = 0.3) -> bool:
    """Give argv to the running Nexus window. True if one took it."""
    data = json.dumps({'argv': [str(a) for a in argv]}).encode() + b'\n'
    if len(data) > INSTANCE_MAX:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(INSTANCE)
            s.settimeout(2)
            s.sendall(data)
    except OSError:
        return False
    return True


def window_running() -> bool:
    """Whether a Nexus window is listening (connects, sends nothing)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(INSTANCE)
    except OSError:
        return False
    return True


AGENT_LOCK = NX.DATA_DIR / 'agent.lock'


def agent_running() -> bool:
    """Whether the background agent holds its lock (a shared, non-blocking
    probe; the agent retries its own lock for a moment, see _take_lock)."""
    try:
        fh = open(AGENT_LOCK, 'a')
    except OSError:
        return False
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False
