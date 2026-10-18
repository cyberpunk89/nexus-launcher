"""While a game runs (or an install unpacks): Discord Rich Presence and
keeping the machine awake.

Discord: the local IPC socket every desktop client (and Vesktop's arRPC)
opens — no library, no network. It needs the id of a Discord application
the user created (config `[discord] client_id`); without one nothing is
sent.

Awake: a small holder process. `systemd-inhibit` blocks sleep through
logind, and inside it the holder asks the session's
org.freedesktop.ScreenSaver (niri implements it) to inhibit idle, when
dbus-python is available. The holder reads stdin until EOF, so it lets go
the moment its parent exits — however that happens.

Stdlib only at import: this file is also run directly as the holder."""

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

DISCORD_DIRS = ('', 'app/com.discordapp.Discord', '.flatpak/dev.vencord.Vesktop/xdg-run',
                'app/dev.vencord.Vesktop', 'snap.discord')
STEAM_HEADER = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


def _discord_sockets() -> list[Path]:
    bases = [os.environ.get('XDG_RUNTIME_DIR') or f"/run/user/{os.getuid()}",
             os.environ.get('TMPDIR') or '/tmp']
    out = []
    for base in bases:
        for sub in DISCORD_DIRS:
            for i in range(10):
                p = Path(base, sub, f"discord-ipc-{i}")
                if p.is_socket():
                    out.append(p)
    return out


class Discord:
    """A minimal Rich Presence client: handshake, SET_ACTIVITY, close."""

    def __init__(self, client_id: str):
        self.client_id = str(client_id)
        self.sock = None

    def _send(self, op: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.sock.sendall(struct.pack('<II', op, len(data)) + data)

    def _recv(self) -> dict:
        head = b''
        while len(head) < 8:
            chunk = self.sock.recv(8 - len(head))
            if not chunk:
                raise OSError("Discord closed the connection")
            head += chunk
        _op, n = struct.unpack('<II', head)
        body = b''
        while len(body) < n:
            chunk = self.sock.recv(n - len(body))
            if not chunk:
                raise OSError("Discord closed the connection")
            body += chunk
        return json.loads(body or b'{}')

    def connect(self, timeout: float = 3) -> bool:
        if not self.client_id.isdigit():
            return False
        for path in _discord_sockets():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                s.connect(str(path))
                self.sock = s
                self._send(0, {'v': 1, 'client_id': self.client_id})
                if self._recv().get('evt') == 'READY':
                    return True
            except (OSError, ValueError):
                pass
            s.close()
            self.sock = None
        return False

    def set_activity(self, details: str, start: float | None = None,
                     image: str | None = None, image_text: str | None = None) -> bool:
        if self.sock is None:
            return False
        activity = {'type': 0, 'details': details[:128],
                    'timestamps': {'start': int(start or time.time())}}
        if image:
            activity['assets'] = {'large_image': image, 'large_text': (image_text or details)[:128]}
        try:
            self._send(1, {'cmd': 'SET_ACTIVITY', 'nonce': str(uuid.uuid4()),
                           'args': {'pid': os.getpid(), 'activity': activity}})
            return self._recv().get('evt') != 'ERROR'
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._send(2, {})
            except OSError:
                pass
            self.sock.close()
            self.sock = None


def discord_presence(client_id: str | None, name: str, store_appid: int | None = None):
    """Connect and show "Playing <name>"; returns the client to close later,
    or None when Discord isn't running or no client id is configured."""
    if not client_id:
        return None
    d = Discord(client_id)
    if not d.connect():
        return None
    image = STEAM_HEADER.format(appid=store_appid) if store_appid else None
    if not d.set_activity(name, image=image, image_text=name):
        d.close()
        return None
    return d


def inhibit(why: str) -> subprocess.Popen | None:
    """Keep the machine awake until the returned process ends (or this
    process exits). None when there is nothing to inhibit with."""
    holder = [sys.executable, '-I', str(Path(__file__).resolve()), 'hold', why]
    if shutil.which('systemd-inhibit'):
        cmd = ['systemd-inhibit', '--what=sleep:idle', '--who=Nexus', f'--why={why}',
               '--mode=block', *holder]
    else:
        cmd = holder
    try:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


def release(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.stdin.close()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


def _hold(why: str) -> None:
    """The holder: an idle inhibit on the session bus (if dbus-python is
    there), kept until stdin closes."""
    try:
        import dbus                            # optional system package
        bus = dbus.SessionBus()
        ss = bus.get_object('org.freedesktop.ScreenSaver', '/org/freedesktop/ScreenSaver')
        dbus.Interface(ss, 'org.freedesktop.ScreenSaver').Inhibit('Nexus', why)
    except Exception:                          # noqa: BLE001 — logind still blocks sleep
        pass
    try:
        sys.stdin.buffer.read()
    except (OSError, KeyboardInterrupt):
        pass


if __name__ == '__main__' and sys.argv[1:2] == ['hold']:
    _hold(' '.join(sys.argv[2:]) or 'Playing')
