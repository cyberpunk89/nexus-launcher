"""Linux side of nexus.platform: what the callers did inline before."""

import fcntl
import functools
import os
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

APP_NAME = 'nexus'
TRASH = Path.home() / '.local/share/Trash'


def data_dir(home: Path) -> Path:
    return home / '.local/share' / APP_NAME


def config_dir(home: Path) -> Path:
    return home / '.config' / APP_NAME


def cache_dir(home: Path) -> Path:
    return Path(os.environ.get('XDG_CACHE_HOME') or home / '.cache') / APP_NAME


def app_menu_dir(home: Path) -> Path:
    return home / ".local/share/applications"


def steam_root(home: Path) -> Path:
    """The usual ~/.local/share/Steam, Debian and Ubuntu's packaged Steam
    (~/.steam/debian-installation), or Flatpak Steam. The first that has a
    library, else the first that exists, else the usual one."""
    candidates = (home / '.local/share/Steam',
                  home / '.steam/debian-installation',
                  home / '.var/app/com.valvesoftware.Steam/.local/share/Steam')
    return next((c for c in candidates if (c / 'steamapps').is_dir()),
                next((c for c in candidates if c.is_dir()), candidates[0]))



def default_install_base(home: Path) -> Path:
    return home / "Games" / "FitGirl"


def install_base_warnings(base: Path) -> list[str]:
    """Nothing to say on Linux (the free-space and btrfs checks live in
    Doctor)."""
    return []

def torrent_client_settings() -> list:
    """Linux finds a client by probing its port (torrent.detect)."""
    return []


def appx_registered(family: str) -> bool:
    return False


def long_paths_enabled() -> bool:
    return True


def defender_status() -> dict | None:
    return None


def steam_running_appid() -> int | None:
    """Linux finds Steam games by the reaper's AppId= (steamlib.running)."""
    return None


def lock(fh, shared=False, blocking=True):
    flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(fh, flags)


def unlock(fh):
    fcntl.flock(fh, fcntl.LOCK_UN)


class Proc:
    """One process from /proc. Each field is read the first time it's used,
    so a scan that only needs names reads only `comm` (a few ms in all).
    Fields of a process that's gone (or not ours to read) come back empty."""

    def __init__(self, pid: int):
        self.pid = pid

    def _read(self, name: str) -> bytes:
        try:
            with open(f'/proc/{self.pid}/{name}', 'rb') as f:
                return f.read()
        except OSError:
            return b''

    @functools.cached_property
    def name(self) -> str:
        """The kernel's short name (comm): at most 15 characters."""
        return self._read('comm').strip().decode('utf-8', 'replace')

    @functools.cached_property
    def cmdline(self) -> list[str]:
        # Usually NUL-terminated; a process that rewrote its argv may not be.
        raw = self._read('cmdline').removesuffix(b'\0')
        return [a.decode('utf-8', 'replace') for a in raw.split(b'\0')] if raw else []

    @functools.cached_property
    def exe(self) -> str:
        try:
            return os.readlink(f'/proc/{self.pid}/exe')
        except OSError:
            return ''

    def environ(self) -> dict[bytes, bytes]:
        out = {}
        for var in self._read('environ').split(b'\0'):
            k, sep, v = var.partition(b'=')
            if sep:
                out[k] = v
        return out


def processes():
    try:
        with os.scandir('/proc') as it:
            pids = [int(e.name) for e in it if e.name.isdigit()]
    except OSError:
        return
    for pid in pids:
        yield Proc(pid)


def new_group() -> dict:
    return {'start_new_session': True}


def kill_tree(proc, timeout=5):
    """SIGTERM the child's process group (it leads one: new_group), then
    SIGKILL if it's still there after `timeout` seconds."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=timeout)
    except (ProcessLookupError, OSError):
        pass
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _trash_manual(path: Path) -> None:
    """freedesktop Trash spec, for the home trash (used when gio is absent)."""
    files, info = TRASH / 'files', TRASH / 'info'
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    name, n = path.name, 1
    while (files / name).exists() or (info / f"{name}.trashinfo").exists():
        n += 1
        name = f"{path.name}.{n}"
    (info / f"{name}.trashinfo").write_text(
        "[Trash Info]\n"
        f"Path={quote(str(path.resolve()))}\n"
        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n", encoding='utf-8')
    shutil.move(str(path), str(files / name))


def trash(path: Path) -> None:
    """gio picks the right trash for the volume — on the same btrfs volume
    that's an instant rename. The manual home trash when gio is missing or
    refuses (it does for ~/Games: "system internal mount")."""
    if shutil.which('gio'):
        try:
            # Same volume: a rename. Across volumes gio copies, which can take
            # a while for a big folder — but never forever.
            r = subprocess.run(['gio', 'trash', '--', str(path)], capture_output=True,
                               text=True, timeout=1800)
            if r.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
    _trash_manual(path)


def trash_size() -> int:
    """Bytes in the home trash (stats only, symlinks not followed)."""
    total = 0
    for root, _dirs, files in os.walk(TRASH / 'files', followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def meminfo() -> dict[str, int]:
    try:
        with open('/proc/meminfo', encoding='utf-8') as f:
            return {k.rstrip(':'): int(v.split()[0])
                    for k, v in (l.split(':', 1) for l in f if ':' in l)}
    except OSError:
        return {}


def open_url(url: str) -> bool:
    """`steam URL` reaches a running client (or starts one); anything else
    goes through xdg-open so whatever registered the scheme handles it.
    Steam's overlay LD_PRELOAD isn't passed on."""
    if url.startswith('steam://') and shutil.which('steam'):
        cmd = ['steam', url]
    elif shutil.which('xdg-open'):
        cmd = ['xdg-open', url]
    else:
        return False
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **new_group(),
                     env={k: v for k, v in os.environ.items() if k != 'LD_PRELOAD'})
    return True


def cli_command(repo: Path, same_python: bool) -> list[str]:
    script = str(repo / 'bin' / 'nexus-cli')
    return [sys.executable, script] if same_python else [script]


def run_elevated(exe, args, cwd=None):
    """Not used on Linux: setup.exe runs under Wine as the user."""
    raise NotImplementedError("run_elevated is Windows-only")


def create_shortcut(path, target, args='', workdir=None, icon=None):
    """Windows .lnk files; Linux writes .desktop entries (cli/desktop.py)."""
    raise NotImplementedError("create_shortcut is Windows-only")


def defender_detections(folders) -> list[dict]:
    """Windows Defender only."""
    return []


def console_encoding() -> str:
    return 'utf-8'


def copy_tree(src: Path, dest: Path) -> None:
    """cp -a, reflinked where the filesystem can (instant on btrfs)."""
    r = subprocess.run(['cp', '-a', '--reflink=auto', f"{src}/.", f"{dest}/"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or f"cp exited {r.returncode}")


def replace(src, dst) -> None:
    os.replace(src, dst)


def keep_private(path) -> None:
    """Nothing to add: callers create the file with mode 0600."""


def instance_address(cache_dir: Path) -> str:
    """In the user's private runtime dir. Without one, a 0700 folder of our
    own, never a name in shared /tmp another user could take first."""
    run = os.environ.get('XDG_RUNTIME_DIR') or ''
    if os.path.isdir(run):
        return os.path.join(run, 'nexus.sock')
    return str(cache_dir / 'run' / 'nexus.sock')


def send_local(address: str, data: bytes, timeout: float) -> None:
    """Connect to a QLocalServer's Unix socket and send data. Raises OSError."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(address)
        s.settimeout(2)
        s.sendall(data)


def discord_ipc_paths() -> list[Path]:
    """Discord's IPC sockets that exist now: the desktop client's, Flatpak's,
    Vesktop's (arRPC) and Snap's, under XDG_RUNTIME_DIR and TMPDIR."""
    subs = ('', 'app/com.discordapp.Discord', '.flatpak/dev.vencord.Vesktop/xdg-run',
            'app/dev.vencord.Vesktop', 'snap.discord')
    bases = [os.environ.get('XDG_RUNTIME_DIR') or f"/run/user/{os.getuid()}",
             os.environ.get('TMPDIR') or '/tmp']
    return [p for base in bases for sub in subs for i in range(10)
            if (p := Path(base, sub, f"discord-ipc-{i}")).is_socket()]


def inhibit_sleep(why: str):
    """None: on Linux a holder process keeps the machine awake (logind and
    the session's ScreenSaver; see presence.inhibit)."""
    return None


def top_windows() -> list[dict]:
    """Windows' own window list; Linux asks the compositor (winfocus, niri)."""
    return []


def focus_window(window_id: int) -> bool:
    return False


def sdl_library_paths() -> list[str]:
    return ['libSDL2-2.0.so.0']


def sdl_hints() -> dict[bytes, bytes]:
    """Read controllers through evdev only. SDL's HIDAPI drivers talk to the
    device directly (lightbar, player LEDs, rumble setup on a DualSense) and
    could fight a running game or Steam Input over it."""
    return {b"SDL_JOYSTICK_HIDAPI": b"0"}


def notify_local(title: str, body: str) -> None:
    """A desktop notification from a process without a window (the agent,
    the CLI): notify-send, when there is one. Raises OSError when it can't
    be run; no notifier at all is not an error."""
    if not shutil.which('notify-send'):
        return
    try:
        subprocess.run(['notify-send', '-a', 'Nexus', '-i', 'nexus', title, body],
                       capture_output=True, timeout=5)
    except subprocess.TimeoutExpired as e:
        raise OSError(f"notify-send: {e}") from e


def startup_dir(home: Path) -> None:
    """None: on Linux the agent starts with the session as a systemd user
    service instead (cli/agent.py)."""
    return None


def gui_command(repo: Path) -> list[str]:
    """argv that starts the Nexus window."""
    return [str(repo / 'bin' / 'nexus')]


def cancel_event() -> None:
    """Windows only (see windows.cancel_event): None here, a job's Stop is
    SIGTERM."""
    return None


def watch_cancel(name: str, on_cancel) -> bool:
    """Windows only: False here (SIGTERM reaches the CLI's handler)."""
    return False


TITLE_BAR_COLORS = False


def style_title_bar(hwnd: int, caption: str, text: str, border: str, dark: bool) -> bool:
    """Windows only: False here (the compositor draws the decorations)."""
    return False
