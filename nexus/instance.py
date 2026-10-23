"""Which Nexus processes are running, without Qt. The window listens on a
socket (nexus/gui/app.py InstanceServer, a QLocalServer — a plain Unix
socket on Linux, a named pipe on Windows), and a second `nexus` launch or the background agent hands
it an argv as one JSON line. The background agent (nexus/cli/agent.py)
holds an flock on AGENT_LOCK while it runs."""

import json
import os

from . import paths as NX, platform

INSTANCE = os.environ.get("NEXUS_INSTANCE") or platform.instance_address(NX.CACHE_DIR)
INSTANCE_MAX = 64 << 10           # bytes one forwarded launch may send


def forward(argv: list[str], timeout: float = 0.3) -> bool:
    """Give argv to the running Nexus window. True if one took it."""
    data = json.dumps({'argv': [str(a) for a in argv]}).encode() + b'\n'
    if len(data) > INSTANCE_MAX:
        return False
    try:
        platform.send_local(INSTANCE, data, timeout)
    except OSError:
        return False
    return True


def window_running() -> bool:
    """Whether a Nexus window is listening (connects, sends nothing)."""
    try:
        platform.send_local(INSTANCE, b'', 0.3)
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
            platform.lock(fh, shared=True, blocking=False)
        except OSError:
            return True
        platform.unlock(fh)
    return False
