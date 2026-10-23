"""Shared by every part of the CLI: paths and constants, the logger, atomic
writes and backups, slugs, running Wine/Proton processes."""

import codecs
import hashlib
import io
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from nexus import paths as NX
from nexus import platform as nxplatform


# The executable stub (bin/nexus-cli runs this module); used to re-invoke the
# CLI, e.g. for the detached --track-session play-time tracker.
CLI_BIN = Path(__file__).resolve().parents[2] / 'bin' / 'nexus-cli'


def default_install_base() -> Path:
    """Where games install when neither --install-dir nor config overrides it:
    ~/Games/FitGirl, or a Games folder on a roomy drive on Windows (created on
    demand). Kept as a function so it is evaluated per-user rather than baked
    in at import time."""
    return nxplatform.default_install_base(Path.home())


def default_scan_dir() -> Path:
    """Where --scan looks for extracted repack folders when neither --scan-dir
    nor config overrides it. Downloads is where a browser or torrent client
    lands a repack, and where its folder name still carries the game name."""
    return Path.home() / "Downloads"


INSTALL_BASE = default_install_base()


# What parsing a Steam .vdf can raise: python-vdf throws SyntaxError on a
# truncated or malformed file (Steam rewriting it), not just ValueError.
VDF_ERRORS = (OSError, ValueError, SyntaxError, EOFError, KeyError, TypeError,
              AttributeError, struct.error, ImportError)
DATA_DIR = NX.DATA_DIR
LOG_DIR = NX.LOG_DIR
ARTWORK_DIR = NX.ARTWORK_DIR
LAUNCHERS_DIR = NX.LAUNCHERS_DIR
SAVES_DIR = NX.SAVES_DIR
CONFIG_PATH = NX.CONFIG_PATH
APPLICATIONS_DIR = NX.APPLICATIONS_DIR
STEAM_ROOT = NX.STEAM_ROOT
STEAM_CONFIG_VDF = STEAM_ROOT / "config" / "config.vdf"

# Heroic Games Launcher config (native install first, then Flatpak)
SGDB_API = "https://www.steamgriddb.com/api/v2"


# ---------------------------------------------------------------------------
# Logger — tees all output to terminal + optional log file
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self, log_file: Path | None = None):
        self._log_file: io.TextIOWrapper | None = None
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log_file, 'w', encoding='utf-8', errors='replace')
            self.log_path = log_file
        else:
            self.log_path = None

    def _ts(self) -> str:
        return datetime.now().strftime('%H:%M:%S')

    def print(self, msg: str = '', end: str = '\n', flush: bool = False) -> None:
        print(msg, end=end, flush=flush)
        if self._log_file:
            # Strip carriage returns from progress lines before writing to file
            clean = msg.replace('\r', '')
            if clean:
                self._log_file.write(f"[{self._ts()}] {clean}{end}")
                if flush:
                    self._log_file.flush()

    def section(self, title: str) -> None:
        self.print(f"\n{'='*40}")
        self.print(f"  {title}")
        self.print(f"{'='*40}")

    def write_raw(self, text: str) -> None:
        """Write subprocess output directly (no timestamp prefix). A child (the
        installer, Wine) can't fake an @nexus event line for the GUI."""
        text = _CHILD_EVENT_RE.sub(r'\1(child) @nexus', text)
        with _OUT_LOCK:                  # never splits an @nexus event line
            sys.stdout.write(text)
            sys.stdout.flush()
        if self._log_file:
            self._log_file.write(text)
            self._log_file.flush()

    def close(self) -> None:
        if self._log_file:
            self._log_file.close()

    def system_info(self, repack_dir: Path, prefix: Path, runner_label: str,
                    base_env: dict[str, str]) -> None:
        """Write a system info block at the top of the log."""
        if not self._log_file:
            return
        lines = [
            "=" * 60,
            "Nexus install diagnostic log",
            f"Date:       {datetime.now().isoformat(timespec='seconds')}",
            f"OS:         {platform.platform()}",
            f"Python:     {sys.version.split()[0]}",
            "",
            f"Repack dir: {repack_dir}",
            f"Prefix:     {prefix}",
            f"Runner:     {runner_label}",
            "",
            "Environment passed to Wine/Proton:",
        ]
        for k, v in sorted(base_env.items()):
            lines.append(f"  {k}={v}")
        if runner_label.startswith('Wine'):
            lines += ["", "Wine version:"]
            try:
                wv = subprocess.check_output(['wine', '--version'], timeout=15,
                                             stderr=subprocess.DEVNULL, text=True).strip()
                lines.append(f"  {wv}")
            except Exception:
                lines.append("  (not found)")
        lines += ["=" * 60, ""]
        self._log_file.write('\n'.join(lines) + '\n')
        self._log_file.flush()


class _LogProxy:
    """The `log` every module prints through. Commands that need their own
    (a fresh console logger, an install's log file) swap the target with
    use_logger(); the proxy itself never changes, so code split across
    modules always reaches the current one."""

    def __init__(self, target: Logger):
        object.__setattr__(self, '_target', target)

    def __getattr__(self, name):
        return getattr(self._target, name)


log = _LogProxy(Logger())


def use_logger(logger: Logger) -> Logger:
    """Make `logger` the one `log` writes to; returns it."""
    object.__setattr__(log, '_target', logger)
    return logger


def win_path(path: Path, wine_prefix: Path) -> str:
    """A path inside the prefix as Windows sees it (C:\\...)."""
    rel = path.relative_to(wine_prefix / 'drive_c')
    return 'C:\\' + '\\'.join(rel.parts)


_OUT_LOCK = threading.Lock()


# '@nexus' at the start of any line (the GUI also splits on \r).
_CHILD_EVENT_RE = re.compile(r'(^|[\r\n])@nexus', re.M)


def emit_event(name: str, data: dict) -> None:
    """A machine-readable line for the GUI ("@nexus <event> <json>"); people
    reading a terminal can ignore it."""
    with _OUT_LOCK:
        sys.stdout.write(f"@nexus {name} {json.dumps(data)}\n")
        sys.stdout.flush()


def atomic_write(path: Path, data: bytes | str) -> None:
    """Replace `path` with `data` atomically: write a sibling temp file, fsync,
    then os.replace. A crash mid-write leaves the old file intact instead of a
    truncated one — which matters for Steam's shortcuts.vdf / config.vdf and
    Heroic's library, where a half-written file loses the user's whole library.
    Keeps the original file's permissions. Raises OSError on failure."""
    from nexus import net
    net.write_atomic(path, data, fsync=True, keep_mode=True)


BACKUP_KEEP = 5
_BACKUP_TS_RE = re.compile(r'\.bak-\d{8}-\d{6}(\d{6})?$')
# path -> monotonic time of this process's last backup of it. An operation that
# rewrites one file several times (a Proton migrate touches config.vdf once per
# game) keeps the copy from before the first write instead of piling up
# intermediate states that prune the original away.
_BACKED_UP: dict[str, float] = {}
_BACKUP_WINDOW = 60.0


def backup_file(path: Path, keep: int = BACKUP_KEEP) -> None:
    """Copy `path` to `<name>.bak-<timestamp>` before we rewrite it, keeping only
    the newest `keep` such copies (they used to pile up forever inside Steam's
    and Heroic's config dirs). Only our own timestamped copies are pruned.
    Best-effort; never raises."""
    if not path.exists():
        return
    key, now = str(path.resolve()), time.monotonic()
    if now - _BACKED_UP.get(key, -_BACKUP_WINDOW) < _BACKUP_WINDOW:
        return
    ts = datetime.now().strftime('%Y%m%d-%H%M%S%f')
    try:
        shutil.copyfile(path, path.with_name(f"{path.name}.bak-{ts}"))
    except OSError:
        return
    _BACKED_UP[key] = now
    try:
        olds = sorted(p for p in path.parent.glob(f"{glob_escape(path.name)}.bak-*")
                      if _BACKUP_TS_RE.search(p.name))
        for p in olds[:-keep]:
            p.unlink()
    except OSError:
        pass


def glob_escape(s: str) -> str:
    """Escape glob metacharacters so a literal filename can be globbed."""
    return re.sub(r'([*?\[])', r'[\1]', s)


_FILE_CACHE: dict[tuple, tuple] = {}


def _cached_read(path: Path, load):
    """`load(path)`, memoised on the file's (mtime, size): one command used to
    re-parse the same file once per game. The result is shared — callers must
    only read it. A missing file isn't cached; `load` decides what that means.
    `load` must be a module-level function (it's part of the key)."""
    try:
        st = os.stat(path)
    except OSError:
        return load(path)
    key, sig = (str(path), load), (st.st_mtime_ns, st.st_size)
    hit = _FILE_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    val = load(path)
    _FILE_CACHE[key] = (sig, val)
    return val


def _load_toml(path: Path) -> dict:
    import tomllib
    return tomllib.loads(path.read_text(encoding='utf-8-sig'))     # a BOM (Notepad) is fine


def dir_size_bytes(path: Path) -> int:
    """Total size of files under path, recursively. Only stats — never reads
    file contents — so it stays cheap even across a multi-part repack. One
    stat per file, symlinks not followed (a prefix's dosdevices point at /)."""
    from nexus import cleanup as C
    return C.dir_size(path)


def existing_ancestor(path: Path) -> Path:
    """Walk up from path to the nearest directory that actually exists, for
    shutil.disk_usage() on a location that hasn't been created yet."""
    p = path
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return p


# The subprocess `run()` is currently waiting on, if any — tracked so a SIGTERM
# (sent by the GUI's Cancel button) can stop the *actual* long-running work
# (umu-run/wine/winetricks), not just this Python process, and so it stops the
# right thing whichever of the two subprocess styles below is in flight.
_ACTIVE_PROC: subprocess.Popen | None = None


def _terminate_active_proc() -> None:
    proc = _ACTIVE_PROC
    if proc is None or proc.poll() is not None:
        return
    nxplatform.kill_tree(proc)


def _handle_sigterm(signum, frame) -> None:
    try:
        log.print("\n\n  [!] Cancelled — stopping the running step...")
    except Exception:
        pass
    _terminate_active_proc()
    os._exit(130)  # 128+SIGTERM, the usual shell convention for "killed"


def _cancelled_by_gui() -> None:
    _handle_sigterm(None, None)


def watch_gui_cancel() -> None:
    """The Windows GUI's Stop sets a named event instead (it can't send this
    process alone a signal, see platform.cancel_event): handled like SIGTERM,
    from a thread, so at once. Taken out of the environment, so nothing we
    start sees it."""
    name = os.environ.pop(nxplatform.CANCEL_EVENT_ENV, None)
    if name:
        nxplatform.watch_cancel(name, _cancelled_by_gui)


WINE_ABORTS: list[str] = []      # "wine: Call from … to unimplemented function X, aborting"
# Windows-only variables that change how Windows programs behave and must
# never reach Wine from whatever started Nexus. NoDefaultCurrentDirectoryInExePath
# (set by some tools for their own safety) stops CreateProcess finding an exe
# in the current folder — FitGirl's unpacker starts xtool.exe and friends
# exactly that way, fails at once, and the installer then deletes the game.
WINE_ENV_DROP = ('NoDefaultCurrentDirectoryInExePath',)
# Nexus's own credentials: no installer or game needs to see them.
SECRET_ENV = ('GITHUB_TOKEN', 'NEXUS_GAMEDB_TOKEN', 'STEAMGRIDDB_API_KEY')


def wine_env(base: dict | None = None) -> dict:
    """os.environ (or `base`) without WINE_ENV_DROP and SECRET_ENV."""
    env = dict(os.environ if base is None else base)
    drop = {d.lower() for d in (*WINE_ENV_DROP, *SECRET_ENV)}
    for k in list(env):
        if k.lower() in drop:
            del env[k]
    return env


def _oem_fallback(err: UnicodeDecodeError):
    """Bytes that aren't UTF-8: the console's OEM code page on Windows (what
    its own tools print), U+FFFD elsewhere."""
    bad = err.object[err.start:err.end]
    return bad.decode(nxplatform.console_encoding(), 'replace'), err.end


codecs.register_error('nexus-oem', _oem_fallback)


def decoded(stream) -> io.TextIOWrapper:
    """A child's binary stdout as text: UTF-16 when it starts like UTF-16
    (some Windows tools, wsl.exe for one), else UTF-8 with the OEM code page
    for anything that isn't. Universal newlines, as text=True gives."""
    head = stream.peek(256)[:256] if hasattr(stream, 'peek') else b''
    if head[:2] in (b'\xff\xfe', b'\xfe\xff'):
        enc, errors = 'utf-16', 'replace'                      # the BOM says which
    elif len(head) >= 8 and head[1::2].count(0) >= len(head) // 4:
        enc, errors = 'utf-16-le', 'replace'                   # ASCII as UTF-16, no BOM
    else:
        enc, errors = 'utf-8', 'nexus-oem'
    return io.TextIOWrapper(stream, encoding=enc, errors=errors, newline=None)


def run(cmd: list[str], extra_env: dict | None = None,
        capture: bool = False, check: bool = False,
        diagnose: bool = False, label: str | None = None) -> int:
    """Run a command, optionally streaming output to the log."""
    global _ACTIVE_PROC
    env = wine_env()
    if extra_env:
        env.update(wine_env(extra_env))
    started_at = datetime.now()

    # new_group() makes the child its own process group leader, so
    # _terminate_active_proc() can kill it (and anything it spawned in turn,
    # e.g. umu-run's own children) in one go.
    if (capture and log.log_path) or not sys.stdout.isatty():
        # Stream stdout+stderr to both terminal and log file in real time (and
        # through write_raw whenever the GUI is reading, see _CHILD_EVENT_RE)
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **nxplatform.new_group(),
        )
        _ACTIVE_PROC = proc
        try:
            assert proc.stdout
            for line in decoded(proc.stdout):
                log.write_raw(line)
                if 'unimplemented function' in line and 'aborting' in line:
                    WINE_ABORTS.append(line.strip())
            proc.wait()
        finally:
            _ACTIVE_PROC = None
        rc = proc.returncode
    else:
        proc = subprocess.Popen(cmd, env=env, **nxplatform.new_group())
        _ACTIVE_PROC = proc
        try:
            rc = proc.wait()
        finally:
            _ACTIVE_PROC = None

    if rc < 0 and diagnose:
        _diagnose_abnormal_exit(label or cmd[0], rc, started_at)
    elif check and rc != 0:
        log.print(f"  [!] Command exited {rc}: {' '.join(cmd)}")
    return rc


def disable_cow(path: Path) -> bool:
    """Best-effort `chattr +C` on a directory: turns off copy-on-write (and,
    implicitly, compression) for files created inside it afterwards.

    Game data is already compressed, so on a CoW filesystem the default costs
    CPU compressing incompressible bytes and fragments the prefix's registry and
    save writes. The flag only affects files created *after* it is set, so this
    must run while the directory is still empty. No-op on filesystems that don't
    support it."""
    if not path.is_dir():
        return False
    try:
        if any(path.iterdir()):     # +C on a populated dir would be a silent no-op
            return False
    except OSError:
        return False
    try:
        rc = subprocess.run(['chattr', '+C', str(path)],
                            capture_output=True).returncode
    except OSError:
        return False
    return rc == 0


def make_slug(name: str) -> str:
    slug = re.sub(r'[^\w\s-]', '', name.lower())
    slug = re.sub(r'[\s_]+', '-', slug).strip('-')
    # A name of nothing but symbols ("!!!") would slug to '' — and an empty
    # slug makes the game's folder the install base itself.
    return slug or 'game-' + hashlib.sha1(name.encode()).hexdigest()[:8]


_SLUG_RE = re.compile(r'[^\W_][\w-]*')


def valid_slug(slug: str) -> bool:
    """A slug is one path component this tool could have made: word
    characters and hyphens, nothing that walks out of a folder ('..', '/')."""
    return bool(slug) and _SLUG_RE.fullmatch(slug) is not None


def require_slug(slug: str, flag: str) -> str:
    if not valid_slug(slug):
        print(f"{flag}: {slug!r} is not a game slug (see --list)", file=sys.stderr)
        sys.exit(2)
    return slug


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .repack import _diagnose_abnormal_exit  # noqa: E402
