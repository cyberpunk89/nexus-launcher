#!/usr/bin/env python3
"""nexus-cli — the Nexus backend: installs FitGirl repacks via umu/Proton/Wine,
manages Steam/Heroic entries, Proton builds, cleanup and the game library."""

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import shutil
import shlex
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

from nexus import paths as NX  # noqa: E402

# The executable stub (bin/nexus-cli runs this module); used to re-invoke the
# CLI, e.g. for the detached --track-session play-time tracker.
CLI_BIN = Path(__file__).resolve().parent.parent / 'bin' / 'nexus-cli'

def default_install_base() -> Path:
    """Where games install when neither --install-dir nor config overrides it:
    ~/Games/FitGirl (created on demand). Kept as a function so it is evaluated
    per-user rather than baked in at import time."""
    return Path.home() / "Games" / "FitGirl"


def default_scan_dir() -> Path:
    """Where --scan looks for extracted repack folders when neither --scan-dir
    nor config overrides it. Downloads is where a browser or torrent client
    lands a repack, and where its folder name still carries the game name."""
    return Path.home() / "Downloads"


INSTALL_BASE = default_install_base()

def _net_errors() -> tuple:
    """What a urllib request can fail with: http.client's IncompleteRead /
    BadStatusLine (a connection dropped mid-body) aren't OSError. A function
    so http.client is only imported once something actually failed."""
    import http.client
    return (OSError, TimeoutError, http.client.HTTPException)   # URLError is an OSError


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
APPLICATIONS_DIR = Path.home() / ".local/share/applications"
STEAM_ROOT = NX.STEAM_ROOT
STEAM_CONFIG_VDF = STEAM_ROOT / "config" / "config.vdf"

# Heroic Games Launcher config (native install first, then Flatpak)
SGDB_API = "https://www.steamgriddb.com/api/v2"

PROTON_SEARCH_DIRS = list(dict.fromkeys([
    STEAM_ROOT / "compatibilitytools.d",
    Path.home() / ".local/share/Steam/compatibilitytools.d",
    Path.home() / ".steam/root/compatibilitytools.d",
    Path("/usr/share/steam/compatibilitytools.d"),
]))

FITGIRL_PATTERNS = [
    # [FitGirl Repack], [FitGirl Monkey Repack], (FitGirl Repack), [FitGirl]
    r'\s*[\[(][^\[\]()]*\bFitGirl\b[^\[\]()]*[\])]',
    r'\s*[-–—]?\s*\bFitGirl\b(\s+\w+)*?\s+Repack\b.*$',
    # "+ 5 DLCs", "+ All DLCs + Bonus Content", "+ Bonus OST"
    r'\s*\+\s*(\d+\s+|All\s+)?(DLCs?|Bonus(es)?|OSTs?|Soundtracks?|Updates?)\b.*$',
    r'\s*\(\d{4}\)\s*$',
]
# A version or build at the end of the name ("– v1.2.3", "Build 15001234"). It's
# kept (as the repack's version), not part of the name or the slug.
VERSION_RE = re.compile(r'\s*[-–—]?\s*\(?(?:v(\d[\w.\-]*)|Build\s+(\d+))\)?\s*$', re.IGNORECASE)

# Only used to expand `--deps all`. Deliberately does NOT include dxvk: every
# supported Proton ships its own (proton-cachyos ships a low-latency build), and
# the winetricks verb pins native DLL overrides in the prefix that stop Proton
# managing DXVK at all. Modern Proton also ships d3dcompiler_47 and the VC++
# runtimes, so this whole list is a fallback for the rare game that needs more.
COMMON_DEPS = [
    ("vcrun2019", "Visual C++ 2019 Runtime"),
    ("d3dcompiler_47", "DirectX Shader Compiler"),
]

SKIP_EXE_NAMES = frozenset([
    'unins', 'setup', 'redist', 'vc_', 'dxsetup', 'crashpad',
    'uninstall', 'uplay', 'easyanticheat', 'battleye',
    'crashhandler', 'crashreport',
    # Wine/Windows built-in stub executables that live inside every prefix
    'wordpad', 'iexplore', 'notepad', 'wmplayer', 'winhlp32', 'regedit',
    'winemine', 'winecfg', 'explorer', 'oobe', 'hh',
])

SKIP_EXE_DIRS = frozenset(['windows', 'users', 'temp', 'installer', 'steam'])

MAX_SETUP_RETRIES = 2            # up to 3 total attempts at running setup.exe

HARD_MIN_AVAILABLE_MB = 3072     # floor: below this, MemAvailable alone is too risky
HARD_MIN_EFFECTIVE_MB = 4608     # target: MemAvailable + credited swap headroom
ZRAM_SWAP_CREDIT = 0.4           # zram swap is compressed RAM, not real extra capacity
DISK_SWAP_CREDIT = 0.75          # real disk swap credited more, still discounted (thrashing risk)

DISK_SPACE_WARN_MULTIPLIER = 1.5   # soft early warning: extraction often needs more than
                                    # the repack's own size, so warn well before the floor


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


# Global logger — replaced in main() once args are parsed
log = Logger()


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def clean_repack_name(raw: str) -> tuple[str, str | None]:
    """A repack folder (or installer) title without the FitGirl tags, DLC
    lists and version. Returns (name, version-or-None)."""
    name, version = raw, None
    for pattern in FITGIRL_PATTERNS[:3]:
        name = re.sub(pattern, '', name, flags=re.IGNORECASE).strip()
    m = VERSION_RE.search(name)
    if m and m.start() > 0:
        version = m.group(1) or m.group(2)
        name = name[:m.start()]
    for pattern in FITGIRL_PATTERNS[3:]:
        name = re.sub(pattern, '', name, flags=re.IGNORECASE).strip()
    name = name.strip(' -–—_[]()').strip()
    return ' '.join(name.split()), version


def setup_product_name(setup_exe: Path | None) -> str | None:
    """The installer's VERSIONINFO ProductName — FitGirl sets it to the clean
    game title ("End of Abyss") and blanks the version fields. Read from the
    head of the file only: the resources sit in the Inno stub, before the
    appended setup data."""
    if not setup_exe:
        return None
    try:
        with open(setup_exe, 'rb') as f:
            data = f.read(8 * 1024 * 1024)
    except OSError:
        return None
    key = 'ProductName'.encode('utf-16-le') + b'\0\0'
    i = data.find(key)
    if i < 6:
        return None
    start = i - 6                                  # wLength, wValueLength, wType
    value_len = int.from_bytes(data[start + 2:start + 4], 'little')
    off = start + ((i + len(key) - start + 3) & ~3)  # value is 32-bit aligned
    raw = data[off:off + value_len * 2]
    try:
        text = raw.decode('utf-16-le').split('\0')[0].strip()
    except UnicodeDecodeError:
        return None
    if not text or not text.isprintable() or len(text) > 120:
        return None
    return text


def repack_info(repack_dir: Path) -> dict:
    """Name, slug and version a repack folder installs as. The installer's own
    title wins over the folder name, except that a slug already in the
    library is kept, so reinstalling or updating a game maps to the same
    prefix it was installed under."""
    folder_name, version = clean_repack_name(repack_dir.name)
    folder_slug = make_slug(folder_name)
    product = setup_product_name(find_setup_exe(repack_dir)) if repack_dir.is_dir() else None
    name, slug = folder_name, folder_slug
    if product:
        product_name, product_version = clean_repack_name(product)
        product_slug = make_slug(product_name)
        known = load_registry()['games']
        if product_slug and folder_slug not in known:
            name, slug = product_name, product_slug
        version = version or product_version
    return {'name': name, 'slug': slug, 'version': version}


def detect_game_name(repack_dir: Path) -> tuple[str, str]:
    # The slug keys the install dir and the registry, so an update/reinstall
    # of the same repack must map to it (see repack_info).
    info = repack_info(repack_dir)
    return info['name'], info['slug']


def repack_fingerprint(repack_dir: Path) -> str | None:
    """Identifies one exact repack release: FitGirl's MD5 list holds the hash
    of every .bin, so a new release of the same game gets a new fingerprint
    (the folder name and installer carry no version). Falls back to the .bin
    names and sizes when there's no MD5 folder."""
    h = hashlib.sha1()
    md5_dir = repack_dir / 'MD5'
    try:
        lists = sorted(md5_dir.glob('*.md5')) if md5_dir.is_dir() else []
        if lists:
            for f in lists:
                h.update(f.read_bytes())
        else:
            bins = sorted(e for e in repack_dir.iterdir() if e.suffix.lower() == '.bin')
            if not bins:
                return None
            for b in bins:
                h.update(f"{b.name}:{b.stat().st_size}\n".encode())
    except OSError:
        return None
    return h.hexdigest()[:16]


_PARTIAL_SUFFIXES = ('.part', '.!qb', '.!ut', '.crdownload', '.aria2', '.tmp', '.partial')


def repack_complete(repack_dir: Path) -> bool:
    """Has the download finished? No torrent/browser partial files, and every
    non-optional file the MD5 list names is there (FitGirl's selective and
    optional .bins may be skipped on purpose)."""
    try:
        names = {e.name.lower() for e in repack_dir.iterdir()}
    except OSError:
        return False
    if any(n.endswith(_PARTIAL_SUFFIXES) for n in names):
        return False
    md5_dir = repack_dir / 'MD5'
    for f in (sorted(md5_dir.glob('*.md5')) if md5_dir.is_dir() else []):
        for want in parse_md5_file(f):
            low = want.lower()
            if low not in names and not any(t in low for t in ('selective', 'optional')):
                return False
    return True


# Inno Setup's unattended switches. FitGirl's installer is Inno: /VERYSILENT
# hides every page (and its progress window), /DIR fixes where the game lands
# inside the prefix, so the exe search afterwards only has to look there.
INNO_SILENT = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-']


def inno_args(win_dir: str) -> list[str]:
    return [*INNO_SILENT, f'/DIR={win_dir}']


# What FitGirl's [Run] section launches once the game is copied: DirectX
# (dxwebsetup even goes online), the VC++ runtimes, a QuickSFV check. Proton
# ships VC++ and DirectX, so in a silent install those are minutes of dead
# time — and only those are skipped: OpenAL, PhysX, XNA, .NET and anything
# run through msiexec are runtimes Proton doesn't bring, so they still run.
# The repack's own list comes from its header (setup_redists); these cover a
# header we can't read.
REDIST_EXES = ('dxwebsetup.exe', 'dxsetup.exe', 'vc_redist.x64.exe', 'vc_redist.x86.exe',
               'vcredist_x64.exe', 'vcredist_x86.exe', 'quicksfv.exe')
_SKIPPABLE_REDIST = re.compile(
    r'^(?:vc_?redist[\w.+-]*|dxwebsetup|dxsetup|directx[\w.+-]*|dx\w*redist[\w.+-]*'
    r'|quicksfv)\.exe$', re.I)
# The installer's background music needs one of these; without them Wine has
# no audio device, the music never starts and setup carries on regardless.
AUDIO_DRIVERS = ('winealsa.drv', 'wineoss.drv', 'winepulse.drv')
# The last [Run] step opens FitGirl's site; Wine hands URLs to winebrowser,
# which passes them to xdg-open. Disabled, the browser tab never opens.
NO_BROWSER = 'winebrowser.exe=d'

_INNO_TAG = b'Inno Setup Setup Data ('
_INNO_MAX = 64 << 20                     # a setup header is KBs; never inflate past this


def inno_header(setup_exe: Path) -> bytes | None:
    """The decompressed setup header of an Inno installer (its components,
    [Files] and [Run] entries), or None when it isn't one we can read. The
    header follows the 64-byte "Inno Setup Setup Data (x.y.z)" tag: a CRC of
    the next 5 bytes, the stored size, a compressed flag, then 4 KiB chunks
    each led by their own CRC — LZMA1, 5 bytes of properties first. Mapped,
    not read: a single-file installer can be gigabytes. Every CRC is checked,
    so a stray copy of the tag (or a format we don't know) reads as None."""
    import lzma
    import mmap
    try:
        with open(setup_exe, 'rb') as fh, \
                mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
            p = data.find(_INNO_TAG)
            while p >= 0:
                out = _inno_block(data, p + 64, lzma)
                if out is not None:
                    return out
                p = data.find(_INNO_TAG, p + 1)
    except (OSError, ValueError, MemoryError):
        pass
    return None


def _inno_block(data, p: int, lzma) -> bytes | None:
    try:
        crc, size, compressed = struct.unpack_from('<IIB', data, p)
        if zlib.crc32(data[p + 4:p + 9]) != crc or size > _INNO_MAX:
            return None
        p += 9
        end, raw = p + size, bytearray()
        while p < end:
            ccrc = struct.unpack_from('<I', data, p)[0]
            chunk = data[p + 4:min(p + 4 + 4096, end)]
            if zlib.crc32(chunk) != ccrc:
                return None
            raw += chunk
            p += 4 + 4096
        if not compressed:
            return bytes(raw)
        lclppb, dict_size = raw[0], struct.unpack_from('<I', raw, 1)[0]
        lc, lp, pb = lclppb % 9, lclppb // 9 % 5, lclppb // 45
        dec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[
            {'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': lp, 'pb': pb}])
        return dec.decompress(bytes(raw[5:]), max_length=_INNO_MAX)
    except (struct.error, IndexError, lzma.LZMAError, ValueError, MemoryError):
        return None


# A name that can go into WINEDLLOVERRIDES as is: no ; = , or spaces.
_REDIST_RE = re.compile(r'\\_(?:Common)?Redist\\(?:[^\\]{1,60}\\)?([A-Za-z0-9._+-]{1,80}?\.exe)',
                        re.I)


def setup_redists(setup_exe: Path) -> set[str]:
    """Lower-cased names of the VC++/DirectX installers (plus QuickSFV) a
    repack runs from {app}\\_Redist after the game is copied — from its
    header, plus the usual names (REDIST_EXES). Never the installer itself."""
    names = set(REDIST_EXES)
    hdr = inno_header(setup_exe) or b''
    for text in (hdr.decode('utf-16-le', 'ignore'), hdr[1:].decode('utf-16-le', 'ignore'),
                 hdr.decode('latin-1')):
        names.update(m.lower() for m in _REDIST_RE.findall(text))
    own = {setup_exe.name.lower(), 'setup.exe', 'setup.tmp', 'msiexec.exe'}
    return {n for n in names if _SKIPPABLE_REDIST.match(n) and n not in own}


def silent_setup_overrides(setup_exe: Path, skip: set[str] | None = None) -> str:
    """WINEDLLOVERRIDES for a silent install. Wine won't start an exe whose
    override is "disabled", so setup's [Run] step skips straight past the
    redists; the disabled audio drivers mute its music. Only setup.exe (and
    what it spawns) gets these; the game itself is untouched."""
    names = sorted(setup_redists(setup_exe) if skip is None else skip) + list(AUDIO_DRIVERS)
    return ';'.join([*(f'{n}=d' for n in names), NO_BROWSER])


def silent_target(wine_prefix: Path, slug: str) -> Path:
    """Where a new silent install puts the game: C:\\Games\\<slug>."""
    return wine_prefix / 'drive_c' / 'Games' / slug


def win_path(path: Path, wine_prefix: Path) -> str:
    """A path inside the prefix as Windows sees it (C:\\...)."""
    rel = path.relative_to(wine_prefix / 'drive_c')
    return 'C:\\' + '\\'.join(rel.parts)


def existing_install_root(exe: str | None, wine_prefix: Path) -> Path | None:
    """The folder an installed game lives in, from its exe — so a silent
    update installs over it instead of next to it. C:\\Games\\X\\bin\\x.exe and
    C:\\Program Files\\X\\x.exe both give X; anything else its top folder."""
    if not exe:
        return None
    drive_c = wine_prefix / 'drive_c'
    try:
        rel = Path(exe).relative_to(drive_c)
    except ValueError:
        return None
    parts = rel.parts[:-1]
    if not parts:
        return None
    depth = 2 if parts[0].lower() in ('games', 'program files', 'program files (x86)') \
        and len(parts) > 1 else 1
    return drive_c.joinpath(*parts[:depth])


_OUT_LOCK = threading.Lock()


# '@nexus' at the start of any line (the GUI also splits on \r).
_CHILD_EVENT_RE = re.compile(r'(^|[\r\n])@nexus', re.M)


def emit_event(name: str, data: dict) -> None:
    """A machine-readable line for the GUI ("@nexus <event> <json>"); people
    reading a terminal can ignore it."""
    with _OUT_LOCK:
        sys.stdout.write(f"@nexus {name} {json.dumps(data)}\n")
        sys.stdout.flush()


INCOMPLETE_MARKER = '.nexus-incomplete'


_INSTALL_AWAKE = None


def _install_session(slug: str, name: str, game_dir: Path) -> None:
    """For the rest of this install: keep the machine awake, and when the
    process exits — finished, failed or aborted — push the outcome to the
    phone if it took a while (config [notify])."""
    global _INSTALL_AWAKE
    import atexit
    from nexus import presence, notify
    if load_config()['keep_awake']:
        _INSTALL_AWAKE = presence.inhibit(f"Installing {name}")
    started = time.monotonic()
    s = notify.settings()
    if not notify.configured(s):
        return

    def done():
        minutes = (time.monotonic() - started) / 60
        if minutes < s['min_minutes']:
            return
        ok = registry_get(slug) is not None and not (game_dir / INCOMPLETE_MARKER).exists()
        title = f"{name} installed" if ok else f"{name}: install didn't finish"
        body = (f"Ready to play after {minutes:.0f} min." if ok else
                "Open Nexus to resume or discard it.")
        for err in notify.push(title, body, ok, s):
            print(f"  [!] push notification: {err}", file=sys.stderr)
    atexit.register(done)


def write_incomplete_marker(game_dir: Path, info: dict) -> None:
    """Dropped into the game dir before setup.exe runs and removed once the
    game is registered: a cancelled, crashed or failed install leaves it, so
    the library can offer Resume / Discard instead of a mystery folder."""
    try:
        atomic_write(game_dir / INCOMPLETE_MARKER, json.dumps(info, indent=1))
    except OSError:
        pass


def read_incomplete_marker(game_dir: Path) -> dict | None:
    try:
        data = json.loads((game_dir / INCOMPLETE_MARKER).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_incomplete_marker(game_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (game_dir / INCOMPLETE_MARKER).unlink()


def incomplete_installs(install_base: Path) -> list[dict]:
    """Install dirs with a marker whose game never made it into the library
    (an install on top of a registered game that fails leaves that game
    playable, so it isn't listed here)."""
    known = load_registry()['games']
    out = []
    try:
        dirs = sorted(d for d in install_base.iterdir() if d.is_dir())
    except OSError:
        return []
    for d in dirs:
        if d.name in known:
            continue
        info = read_incomplete_marker(d)
        if info is None:
            continue
        out.append({'slug': d.name, 'display_name': info.get('name') or d.name,
                    'source': 'nexus', 'installed': True, 'compatdata': str(d),
                    'incomplete': {'argv': info.get('argv') or [],
                                   'source': info.get('source'),
                                   'started': info.get('started')}})
    return out


class InstallProgress(threading.Thread):
    """While the silent installer runs: how much it has written, every few
    seconds, as a `progress` event (the size of the target dir)."""

    def __init__(self, target: Path, interval: float = 3.0):
        super().__init__(daemon=True)
        self.target, self.interval = target, interval
        self._stop_evt = threading.Event()

    def run(self):
        from nexus import cleanup as C
        last = -1
        while not self._stop_evt.wait(self.interval):
            n = C.dir_size(self.target) if self.target.exists() else 0
            if n != last:
                last = n
                emit_event('progress', {'written': n})

    def stop(self):
        self._stop_evt.set()


def parse_md5_file(md5_path: Path) -> dict[str, str]:
    hashes = {}
    for line in md5_path.read_text(errors='ignore').splitlines():
        line = line.strip()
        if not line or line.startswith(';'):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        expected_hash, raw_path = parts
        filename = raw_path.lstrip('*').replace('..\\', '').replace('\\', '/').split('/')[-1]
        if filename:
            hashes[filename] = expected_hash.lower()
    return hashes


def compute_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _parallel_md5(paths: list[Path]):
    """MD5 every path in `paths`, yielding hex digests in the same order as
    `paths` (not completion order). hashlib releases the GIL, so a thread pool
    genuinely parallelises this — repacks and installed games are tens of GB
    across many files, and hashing them one at a time leaves most of the
    machine idle. Capped so we don't thrash a spinning disk."""
    workers = max(1, min(6, os.cpu_count() or 4, len(paths)))
    from concurrent.futures import ThreadPoolExecutor   # lazy: ~10 ms import
    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(compute_md5, paths)


def verify_bins(repack_dir: Path) -> bool:
    md5_dir = repack_dir / 'MD5'
    md5_files = list(md5_dir.glob('*.md5')) if md5_dir.exists() else []
    if not md5_files:
        log.print("  [!] No MD5 file found — skipping verification")
        return True

    expected: dict[str, str] = {}
    for p in md5_files:
        expected.update(parse_md5_file(p))

    if not expected:
        log.print("  [!] MD5 file empty — skipping verification")
        return True

    todo = []
    for filename, expected_hash in expected.items():
        filepath = repack_dir / filename
        if not filepath.exists():
            log.print(f"  [-] {filename}: missing (may be optional per FitGirl)")
            continue
        todo.append((filename, filepath, expected_hash))

    if not todo:
        return True

    total_mb = sum(fp.stat().st_size for _, fp, _ in todo) / 1024 / 1024
    log.print(f"  Hashing {len(todo)} file(s), {total_mb:.0f} MB...")

    all_ok = True
    filepaths = [fp for _, fp, _ in todo]
    for done, ((filename, _, expected_hash), actual) in enumerate(
            zip(todo, _parallel_md5(filepaths)), 1):
        if actual == expected_hash:
            log.print(f"  [{done}/{len(todo)}] {filename}: OK")
        else:
            log.print(f"  [{done}/{len(todo)}] {filename}: FAILED\n"
                      f"       expected: {expected_hash}\n"
                      f"       got:      {actual}")
            all_ok = False

    return all_ok


def atomic_write(path: Path, data: bytes | str) -> None:
    """Replace `path` with `data` atomically: write a sibling temp file, fsync,
    then os.replace. A crash mid-write leaves the old file intact instead of a
    truncated one — which matters for Steam's shortcuts.vdf / config.vdf and
    Heroic's library, where a half-written file loses the user's whole library.
    Keeps the original file's permissions. Raises OSError on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # pid + thread: --fill-art writes the same caches from several threads.
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        if isinstance(data, bytes):
            with open(tmp, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        else:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        if path.exists():
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


BACKUP_KEEP = 5
_BACKUP_TS_RE = re.compile(r'\.bak-\d{8}-\d{6}$')


def backup_file(path: Path, keep: int = BACKUP_KEEP) -> None:
    """Copy `path` to `<name>.bak-<timestamp>` before we rewrite it, keeping only
    the newest `keep` such copies (they used to pile up forever inside Steam's
    and Heroic's config dirs). Only our own timestamped copies are pruned.
    Best-effort; never raises."""
    if not path.exists():
        return
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    try:
        shutil.copyfile(path, path.with_name(f"{path.name}.bak-{ts}"))
    except OSError:
        return
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


def _proton_version_tuple(name: str) -> tuple[int, ...]:
    """Extract numeric components from a Proton dir name, e.g. 'GE-Proton10-34' -> (10, 34)."""
    return tuple(int(n) for n in re.findall(r'\d+', name))


def _proton_rank_key(name: str) -> tuple:
    lname = name.lower()
    is_latest = 'latest' in lname
    is_ge = 'ge' in lname
    nums = _proton_version_tuple(name)
    has_version = bool(nums)
    # Pad to a fixed width before negating so tuple comparison behaves like
    # real version comparison instead of comparing tuples of different lengths.
    padded = (nums + (0, 0, 0, 0))[:4]
    return (
        0 if is_latest else 1,      # "latest"-named dirs win outright
        0 if has_version else 1,    # parseable version beats no version
        tuple(-n for n in padded),  # higher numeric version sorts first
        0 if is_ge else 1,          # GE preferred as a final tiebreak
        name,                       # deterministic last resort
    )


# ELF e_machine value for the host CPU (used to reject wrong-arch Proton builds,
# e.g. an accidentally-installed arm64 GE-Proton on an x86-64 box).
_HOST_ELF_MACHINE = {
    'x86_64': 0x3E, 'amd64': 0x3E, 'aarch64': 0xB7, 'arm64': 0xB7,
}.get(platform.machine().lower())


def _elf_machine(path: Path) -> int | None:
    """Read an ELF file's e_machine field, or None if not a readable ELF."""
    try:
        with open(path, 'rb') as f:
            head = f.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b'\x7fELF':
        return None
    little = head[5] == 1          # EI_DATA: 1 = little-endian
    return int.from_bytes(head[18:20], 'little' if little else 'big')


def _proton_wine_is_native(proton_dir: Path) -> bool:
    """True unless the build's wine binary is a valid ELF for a *different* CPU.
    Unknown/unreadable binaries get the benefit of the doubt (return True)."""
    if _HOST_ELF_MACHINE is None:
        return True
    for rel in ('files/bin/wine', 'dist/bin/wine', 'files/bin/wine64', 'dist/bin/wine64'):
        m = _elf_machine(proton_dir / rel)
        if m is not None:
            return m == _HOST_ELF_MACHINE
    return True  # no wine binary found to inspect — don't exclude on that basis


_WARNED_ARCH = False


_WARNED_DAMAGE: set[str] = set()


def damaged_protons() -> dict[str, str]:
    """{build folder name: why} for installed builds whose files were changed
    after they were installed (see protons.damage) — they can't run anything."""
    from nexus import protons as PR
    builds = [b.parent for b in list_protons().values()]
    out = {}
    for b in builds:
        why = PR.damage(b, builds)
        if why:
            out[b.name] = why
    return out


def find_proton() -> Path | None:
    candidates: list[tuple[str, Path]] = []
    skipped: list[str] = []
    broken = damaged_protons()
    for search_dir in PROTON_SEARCH_DIRS:
        if not search_dir.is_dir():
            continue
        for proton_bin in search_dir.glob("*/proton"):
            if not proton_bin.is_file():
                continue
            if not _proton_wine_is_native(proton_bin.parent):
                skipped.append(proton_bin.parent.name)
                continue
            name = proton_bin.parent.name
            if name in broken:
                if name not in _WARNED_DAMAGE:      # stderr, once: --json stdout is parsed
                    _WARNED_DAMAGE.add(name)
                    tag = proton_repair_tag(name)
                    print(f"  [!] Skipping {name}: {broken[name]}."
                          + (f"\n      Repair it: nexus-cli --proton-install {tag} --force"
                             if tag else ''), file=sys.stderr)
                continue
            candidates.append((name, proton_bin))
    global _WARNED_ARCH
    if skipped and not _WARNED_ARCH:
        # stderr, once: this runs inside --json commands whose stdout is parsed.
        _WARNED_ARCH = True
        uniq = sorted(set(skipped))
        print(f"  [!] Ignoring wrong-architecture Proton build(s): {', '.join(uniq)}\n"
              f"      (their wine binary isn't {platform.machine()} — reinstall the "
              f"correct build via ProtonPlus/protonup-qt)", file=sys.stderr)
    if not candidates:
        return None
    candidates.sort(key=lambda item: _proton_rank_key(item[0]))
    return candidates[0][1]


def proton_repair_tag(name: str) -> str | None:
    """The GE-Proton tag `--proton-install --force` can reinstall a build
    from: only GE builds in the user's compatibilitytools.d."""
    from nexus import protons as PR
    binp = list_protons().get(name)
    return PR.tag_of(name) if binp and PR.is_user_build(binp.parent) else None


def list_protons() -> dict:
    """All installed Proton builds as {dir_name: proton_binary_path}, across the
    Steam compat-tool dirs Heroic also scans. Wrong-arch builds are skipped."""
    found: dict[str, Path] = {}
    for search_dir in PROTON_SEARCH_DIRS:
        if not search_dir.is_dir():
            continue
        for proton_bin in search_dir.glob("*/proton"):
            if proton_bin.is_file() and _proton_wine_is_native(proton_bin.parent):
                found.setdefault(proton_bin.parent.name, proton_bin)
    return found


def proton_tool_name(proton: Path) -> str:
    """The name Steam keys this compat tool by in config.vdf's CompatToolMapping:
    the internal name inside its compatibilitytool.vdf. That usually equals the
    directory name, but packaged/renamed builds can differ — and a mapping that
    names a tool Steam doesn't know is silently ignored."""
    manifest = proton.parent / 'compatibilitytool.vdf'
    tools = _load_vdf_text(manifest).get('compatibilitytools', {}).get('compat_tools', {})
    if isinstance(tools, dict):
        for name in tools:
            return name
    return proton.parent.name


def proton_by_name(name: str | None, protons: dict | None = None) -> Path | None:
    """An installed Proton by dir name or compat-tool name (exact, then
    case-insensitive, then as a whole word inside a longer name — so
    "proton-cachyos" finds proton-cachyos-slr, but GE-Proton9-2 never
    resolves to GE-Proton9-20). None if `name` is empty or not installed."""
    name = (name or '').strip()
    if not name:
        return None
    protons = list_protons() if protons is None else protons
    if name in protons:
        return protons[name]
    for p in protons.values():
        if proton_tool_name(p) == name:
            return p
    for n, p in protons.items():
        if n.lower() == name.lower():
            return p
    word = re.compile(rf'(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])')
    for n, p in protons.items():
        if word.search(n.lower()):
            return p
    return None


def resolve_game_proton(entry: dict | None, config: dict,
                        quiet: bool = False) -> Path | None:
    """The one Proton a game is *played* with — launch.sh, the Steam shortcut's
    forced compat tool and the Heroic entry all use this, so a prefix is never
    bounced between builds depending on which launcher started it.

    Precedence: the game's own launch profile (`launch.proton`) > config
    `[runner] proton` (or legacy `[heroic] proton`) > the build recorded for the
    game at install time (`proton`) > the newest installed build (find_proton).
    A named build that isn't installed is reported and skipped."""
    protons = list_protons()
    entry = entry or {}
    candidates = [
        ('launch profile', (entry.get('launch') or {}).get('proton')),
        ('config', config.get('proton') or config.get('heroic_proton')),
        ('recorded', entry.get('proton')),
    ]
    for why, name in candidates:
        if not name:
            continue
        hit = proton_by_name(name, protons)
        if hit:
            return hit
        if not quiet:
            log.print(f"  [!] Proton '{name}' ({why}) is not installed — skipping it. "
                      f"Installed: {', '.join(sorted(protons)) or 'none'}")
    return find_proton()


def find_umu() -> str | None:
    """Path to umu-run, or None. umu bootstraps the Steam Linux Runtime that
    modern GE-Proton builds require, so `proton` can't run standalone."""
    return shutil.which('umu-run')


def select_runner(backend: str, proton: Path | None) -> str:
    """Resolve the effective backend ('umu' | 'proton' | 'wine') from the
    configured preference and what's actually available.

    'auto' (default): umu if both umu-run and a Proton build are present,
    else raw proton if a build is present, else system wine."""
    backend = (backend or 'auto').lower()
    have_umu = find_umu() is not None
    if backend == 'wine':
        return 'wine'
    if backend == 'proton':
        return 'proton' if proton else 'wine'
    if backend == 'umu':
        if have_umu and proton:
            return 'umu'
        return 'proton' if proton else 'wine'
    # auto
    if have_umu and proton:
        return 'umu'
    if proton:
        return 'proton'
    return 'wine'


def _umu_env(base_env: dict, wine_prefix: Path, proton: Path) -> dict:
    """Environment for a umu-run invocation. umu treats WINEPREFIX as the
    prefix root (drive_c lands directly inside it, with a `pfx -> .` symlink),
    so passing game_dir/pfx keeps the exact layout the rest of the tool
    expects. STEAM_COMPAT_DATA_PATH is dropped — umu derives it itself."""
    env = {k: v for k, v in base_env.items() if k != 'STEAM_COMPAT_DATA_PATH'}
    env['WINEPREFIX'] = str(wine_prefix)
    env['GAMEID'] = 'umu-0'
    env['PROTONPATH'] = str(proton.parent)   # the GE-Proton dir, not the `proton` binary
    env.setdefault('STEAM_COMPAT_CLIENT_INSTALL_PATH', str(STEAM_ROOT))
    return env


UMU_DB_URL = ('https://raw.githubusercontent.com/Open-Wine-Components/umu-database/'
              'main/umu-database.csv')
STEAM_SEARCH_URL = 'https://store.steampowered.com/api/storesearch/?l=english&cc=us&term='
_UMU_ID_RE = re.compile(r'umu-\d+')


def _title_key(name: str) -> str:
    """Titles compared loosely: case, ™/®, punctuation and spacing aside."""
    return re.sub(r'[\W_]+', ' ', name.lower()).strip()


def resolve_umu_gameid(display_name: str) -> str | None:
    """The umu GAMEID for a game, so umu-protonfixes applies its fixes:
    umu-database's own entry first, then the Steam store's exact title match
    (umu-<steam appid> picks up the Steam fixes). None offline or unknown —
    the launcher then keeps umu-0."""
    from nexus import net
    import csv
    key = _title_key(display_name)
    if not key:
        return None
    try:
        text, _at, _stale = net.cached(UMU_DB_URL, 'umu-database.csv', 7 * 86400,
                                       as_json=False, timeout=10)
        rank = {'steam': 0, 'none': 1}
        hits = sorted((rank.get((r.get('STORE') or '').lower(), 2), r['UMU_ID'])
                      for r in csv.DictReader(io.StringIO(text))
                      if _title_key(r.get('TITLE') or '') == key
                      and _UMU_ID_RE.fullmatch(r.get('UMU_ID') or ''))
        if hits:
            return hits[0][1]
    except (net.NetError, csv.Error, KeyError):
        pass
    import urllib.parse
    try:
        data, _at, _stale = net.cached(
            STEAM_SEARCH_URL + urllib.parse.quote(display_name, safe=''),
            'steam-search-' + hashlib.sha1(key.encode()).hexdigest()[:16] + '.json',
            30 * 86400, timeout=10)
        for item in (data or {}).get('items') or []:
            if _title_key(str(item.get('name', ''))) == key and str(item.get('id', '')).isdigit():
                return f"umu-{item['id']}"
    except (net.NetError, AttributeError):
        pass
    return None


def find_setup_exe(repack_dir: Path) -> Path | None:
    for name in ['setup.exe', 'Setup.exe', 'SETUP.EXE']:
        p = repack_dir / name
        if p.exists():
            return p
    return None


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


def _signal_name(rc: int) -> str:
    try:
        return signal.Signals(-rc).name
    except ValueError:
        return f"signal {-rc}"


def _check_oom_journal(since: datetime) -> str | None:
    """Best-effort look for a kernel OOM-kill event near `since`.

    Never escalates privileges; any failure (permission, missing binary,
    timeout) degrades to 'inconclusive' (None), not an error.
    """
    try:
        proc = subprocess.run(
            ['journalctl', '-k', '--since', since.strftime('%Y-%m-%d %H:%M:%S'),
             '--no-pager', '-q'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None  # permission denied / journalctl unavailable — degrade silently, no sudo
    hits = [l for l in proc.stdout.splitlines()
            if any(s in l.lower() for s in ('out of memory', 'oom-kill', 'killed process'))]
    return hits[-1].strip() if hits else None


def _diagnose_abnormal_exit(label: str, rc: int, started_at: datetime) -> None:
    sig = _signal_name(rc)
    log.print(f"\n  [!] {label} was TERMINATED ABNORMALLY (killed by {sig}, exit code {rc}).",
              flush=True)
    if -rc == signal.SIGKILL:
        log.print("      SIGKILL almost always means the kernel OOM-killer stepped in.")
        note = _check_oom_journal(started_at)
        if note:
            log.print(f"      Confirmed via kernel log: {note}")
        else:
            log.print("      Could not confirm via journalctl -k (no matching entry,")
            log.print("      insufficient permissions, or journalctl unavailable) — but")
            log.print("      OOM is still the most likely cause given the symptoms.")
        log.print("      This is NOT a bug in the installer — see memory guidance above.")
    else:
        log.print("      This was not the installer's own error; it was killed by the OS/user.")


# The subprocess `run()` is currently waiting on, if any — tracked so a SIGTERM
# (sent by the GUI's Cancel button) can stop the *actual* long-running work
# (umu-run/wine/winetricks), not just this Python process, and so it stops the
# right thing whichever of the two subprocess styles below is in flight.
_ACTIVE_PROC: subprocess.Popen | None = None


def _terminate_active_proc() -> None:
    proc = _ACTIVE_PROC
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, OSError):
        pass
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _handle_sigterm(signum, frame) -> None:
    try:
        log.print("\n\n  [!] Cancelled — stopping the running step...")
    except Exception:
        pass
    _terminate_active_proc()
    os._exit(130)  # 128+SIGTERM, the usual shell convention for "killed"


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


def run(cmd: list[str], extra_env: dict | None = None,
        capture: bool = False, check: bool = False,
        diagnose: bool = False, label: str | None = None) -> int:
    """Run a command, optionally streaming output to the log."""
    global _ACTIVE_PROC
    env = wine_env()
    if extra_env:
        env.update(wine_env(extra_env))
    started_at = datetime.now()

    # start_new_session makes the child its own process group leader, so
    # _terminate_active_proc() can kill it (and anything it spawned in turn,
    # e.g. umu-run's own children) with a single killpg.
    if (capture and log.log_path) or not sys.stdout.isatty():
        # Stream stdout+stderr to both terminal and log file in real time (and
        # through write_raw whenever the GUI is reading, see _CHILD_EVENT_RE)
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='replace', start_new_session=True,
        )
        _ACTIVE_PROC = proc
        try:
            assert proc.stdout
            for line in proc.stdout:
                log.write_raw(line)
                if 'unimplemented function' in line and 'aborting' in line:
                    WINE_ABORTS.append(line.strip())
            proc.wait()
        finally:
            _ACTIVE_PROC = None
        rc = proc.returncode
    else:
        proc = subprocess.Popen(cmd, env=env, start_new_session=True)
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


def init_prefix(game_dir: Path, base_env: dict, proton: Path | None,
                runner: str, wine_prefix: Path) -> None:
    # game_dir is the Proton compat-data dir; Proton/umu create game_dir/pfx
    # (== WINEPREFIX) itself. The dir must exist first for Proton's pfx.lock.
    game_dir.mkdir(parents=True, exist_ok=True)
    log.print("  Initializing prefix...", end=' ', flush=True)
    if runner == 'umu':
        # An empty exe arg makes umu just create/upgrade the prefix (and, on
        # first ever run, download the Steam Linux Runtime GE-Proton needs).
        rc = run(['umu-run', ''], extra_env=_umu_env(base_env, wine_prefix, proton),
                 capture=True)
    elif runner == 'proton':
        rc = run([str(proton), 'run', 'wineboot', '--init'], extra_env=base_env, capture=True)
    else:
        rc = run(['wine', 'wineboot', '--init'], extra_env=base_env, capture=True)
    log.print("done" if rc == 0 else f"exited {rc} (may be OK)")


def _proton_wine_bins(proton: Path) -> dict:
    """WINE/WINESERVER pointing at a Proton build's own binaries, so winetricks
    drives the prefix with the same Wine that created it (system wine would
    touch a Proton prefix with a mismatched version/wineserver)."""
    for sub in ('files', 'dist'):
        wine = proton.parent / sub / 'bin' / 'wine'
        if wine.exists():
            return {'WINE': str(wine), 'WINESERVER': str(wine.parent / 'wineserver')}
    return {}


def install_deps(prefix: Path, base_env: dict, spec: str,
                 runner: str = 'wine', proton: Path | None = None) -> None:
    """Install winetricks verbs into the prefix. `spec` is "all", "none"/"" or a
    comma-separated list of verb names (any winetricks verb, not just
    COMMON_DEPS). Only ever runs when the user asked for it via --deps.
    Under umu, runs `umu-run winetricks` (umu's supported path); under raw
    Proton, points winetricks at Proton's own wine."""
    wt_env = {**base_env, 'WINEDLLOVERRIDES': 'winemenubuilder.exe=d'}
    spec = (spec or '').strip().lower()
    if spec in ('none', ''):
        return
    if spec == 'all':
        selected = [pkg for pkg, _ in COMMON_DEPS]
    else:
        selected = [x.strip() for x in spec.split(',') if x.strip()]

    if runner == 'umu' and proton:
        wt_env = _umu_env(wt_env, prefix, proton)
        base_cmd = ['umu-run', 'winetricks', '-q']
    else:
        if runner == 'proton' and proton:
            wt_env.update(_proton_wine_bins(proton))
        base_cmd = ['winetricks', '--unattended']

    for pkg in selected:
        if pkg == 'dxvk':
            log.print("  [!] dxvk: Proton ships (and manages) its own DXVK. Installing it")
            log.print("      via winetricks pins DLL overrides that override Proton's —")
            log.print("      including proton-cachyos' low-latency build. Installing anyway.")
        log.print(f"  Installing {pkg}...", flush=True)
        rc = run(base_cmd + [pkg], extra_env=wt_env, capture=True)
        if rc != 0:
            log.print(f"  [!] winetricks {pkg} exited {rc}.")


def exe_candidates(root: Path) -> list[tuple[int, Path]]:
    """Likely game exes under `root`, largest first: every .exe over 500 KB
    that isn't an uninstaller / redist / crash handler / setup helper."""
    if not root.is_dir():
        return []
    candidates: list[tuple[int, Path]] = []
    for exe in root.rglob('*.exe'):
        parts_lower = [p.lower() for p in exe.relative_to(root).parts[:-1]]
        # Word-boundary match so "Windows NT" / "Windows Media Player" / a
        # Steam folder are all skipped, without also matching a skip term
        # that's merely embedded in a longer name (e.g. a game build dir
        # like "Win64MasterMasterSteamPGO" contains "steam" but isn't one).
        if any(re.search(rf'\b{re.escape(skip)}\b', part)
               for part in parts_lower for skip in SKIP_EXE_DIRS):
            continue
        if any(x in exe.stem.lower() for x in SKIP_EXE_NAMES):
            continue
        try:
            size = exe.stat().st_size
        except OSError:
            continue
        if size < 500_000:
            continue
        candidates.append((size, exe))
    candidates.sort(reverse=True)
    return candidates


def find_exe_in_dir(root: Path) -> Path | None:
    """The most likely game exe under `root` (the largest candidate)."""
    cands = exe_candidates(root)
    return cands[0][1] if cands else None


def find_game_exe(prefix: Path) -> Path | None:
    return find_exe_in_dir(prefix / 'drive_c')


def detect_compatdata(exe: Path) -> tuple[Path | None, Path]:
    """Given a game exe already installed in a prefix, work out how to launch
    it. Returns (compatdata, wine_prefix):
      - Proton layout: exe under <A>/pfx/drive_c → (A, A/pfx)
      - raw-wine layout: exe under <P>/drive_c    → (None, P)
    Falls back to (None, exe.parent) if neither marker is found."""
    for parent in exe.parents:
        if parent.name == 'drive_c':
            wine_prefix = parent.parent
            # A Proton prefix's drive_c lives at <compatdata>/pfx/drive_c.
            if wine_prefix.name == 'pfx':
                return wine_prefix.parent, wine_prefix
            return None, wine_prefix
    return None, exe.parent


def _comment_safe(s: str) -> str:
    """One line of text safe to put after '#' in a shell script."""
    return re.sub(r'[\r\n]+', ' ', s)


BENCH_HOOK = 'nexus-bench-hook'


def write_launch_script(launch_path: Path, compatdata: Path | None,
                        wine_prefix: Path, game_exe: Path | None,
                        slug: str, proton: Path | None,
                        display_name: str | None = None,
                        runner: str = 'proton',
                        launch: dict | None = None,
                        gameid: str | None = None) -> Path:
    """Write a launcher for running the game outside Steam (app-menu entry,
    `--launch`). With runner='umu', umu-run launches the exe (and, at first
    play, bootstraps the Steam Linux Runtime GE-Proton needs). With
    runner='proton' + compatdata, Proton runs it via STEAM_COMPAT_DATA_PATH.
    Otherwise falls back to raw `wine` with WINEPREFIX.

    `launch` is the game's launch profile: its env is exported, its wrappers
    run the game, its args follow the exe. Every path/value is shell-quoted;
    the script cd's to the exe's directory (as Steam's StartDir does) and
    forwards its own arguments to the game."""
    q = shlex.quote
    steam_dq = re.sub(r'([\\"$`])', r'\\\1', str(STEAM_ROOT))   # safe inside "..."
    home_steam = f'"${{STEAM_COMPAT_CLIENT_INSTALL_PATH:-{steam_dq}}}"'
    lines = []
    if runner == 'umu' and proton:
        # GAMEID/PROTONPATH stay overridable from the calling environment.
        lines += [f'export WINEPREFIX={q(str(wine_prefix))}',
                  f'export GAMEID="${{GAMEID:-{gameid if gameid and _UMU_ID_RE.fullmatch(gameid) else "umu-0"}}}"',
                  f'DEFAULT_PROTONPATH={q(str(proton.parent))}',
                  'export PROTONPATH="${PROTONPATH:-$DEFAULT_PROTONPATH}"',
                  f'export STEAM_COMPAT_CLIENT_INSTALL_PATH={home_steam}']
        cmd = ['umu-run']
    elif runner == 'proton' and proton and compatdata:
        lines += [f'export STEAM_COMPAT_DATA_PATH={q(str(compatdata))}',
                  f'export STEAM_COMPAT_CLIENT_INSTALL_PATH={home_steam}']
        cmd = [q(str(proton)), 'run']
    else:
        lines += [f'export WINEPREFIX={q(str(wine_prefix))}']
        cmd = ['wine']

    prof = launch or {}
    if prof.get('env'):
        lines.append('# Launch profile environment (nexus-cli --set-launch)')
        lines += [f'export {k}={q(v)}' for k, v in prof['env']]
    lines += [f'# {BENCH_HOOK}: MangoHud logging for one benchmark run (nexus-cli --benchmark)',
              'if [ -n "${NEXUS_MANGOHUD_EXTRA:-}" ]; then',
              '  export MANGOHUD=1 MANGOHUD_CONFIG="${MANGOHUD_CONFIG:+$MANGOHUD_CONFIG,}'
              '$NEXUS_MANGOHUD_EXTRA"',
              'fi']
    wrappers = ' '.join(q(t) for t in prof.get('wrappers', []))
    args = ' '.join(q(t) for t in prof.get('args', []))

    if game_exe:
        body = '\n'.join([
            f'GAME_EXE={q(str(game_exe))}',
            'cd "$(dirname "$GAME_EXE")" || exit 1',
            *lines,
            ' '.join(x for x in ['exec', wrappers, *cmd, '"$GAME_EXE"', args, '"$@"'] if x),
        ])
    else:
        body = '\n'.join([
            '# Could not auto-detect the game executable. Set it with:',
            f'#   nexus-cli --set-exe {slug} "/path/to/Game.exe"',
            'echo "nexus: no game executable configured" >&2',
            'exit 1',
        ])

    launch_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(launch_path, f"""\
#!/bin/bash
# Launch script for {_comment_safe(slug)}
# Game: {_comment_safe(display_name or slug)}
# Generated by Nexus — regenerate with: nexus-cli --apply-launch {_comment_safe(slug)}
#
# This is the launcher for running the game OUTSIDE Steam (the app-menu entry
# and `nexus-cli --launch`). Steam shortcuts point straight at the .exe.

unset {' '.join(WINE_ENV_DROP)}   # Windows-only; breaks how programs find their helpers
unset {' '.join(SECRET_ENV)}   # Nexus's own credentials
{body}
""")
    launch_path.chmod(0o755)
    return launch_path


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


def read_meminfo() -> dict[str, int]:
    try:
        with open('/proc/meminfo') as f:
            return {k.rstrip(':'): int(v.split()[0])
                    for k, v in (l.split(':', 1) for l in f if ':' in l)}
    except OSError:
        return {}


def _swap_is_compressed() -> bool:
    """True if active swap is zram (compressed, RAM-backed) rather than real disk swap."""
    try:
        lines = Path('/proc/swaps').read_text().splitlines()[1:]
    except OSError:
        return False
    return any(line.startswith('/dev/zram') for line in lines)


def _memory_headroom(meminfo: dict[str, int]) -> tuple[bool, str]:
    avail_mb = meminfo.get('MemAvailable', 0) // 1024
    swap_free_mb = meminfo.get('SwapFree', 0) // 1024
    compressed = _swap_is_compressed()
    credit = ZRAM_SWAP_CREDIT if compressed else DISK_SWAP_CREDIT
    effective_mb = avail_mb + int(swap_free_mb * credit)
    ok = avail_mb >= HARD_MIN_AVAILABLE_MB and effective_mb >= HARD_MIN_EFFECTIVE_MB
    detail = (f"MemAvailable={avail_mb} MB, SwapFree={swap_free_mb} MB "
              f"({'zram/compressed' if compressed else 'disk'}, credited at "
              f"{int(credit*100)}%) -> effective headroom {effective_mb} MB "
              f"(need >= {HARD_MIN_AVAILABLE_MB} MB available, "
              f">= {HARD_MIN_EFFECTIVE_MB} MB effective)")
    return ok, detail


def preflight_hard_memory_check(skip: bool = False) -> bool:
    """Hard stop before launching setup.exe. Returns True = OK to proceed."""
    if skip:
        log.print("  [i] Memory check skipped (--skip-mem-check).")
        return True
    meminfo = read_meminfo()
    if not meminfo:
        return True  # can't read meminfo — don't block on missing data
    ok, detail = _memory_headroom(meminfo)
    if ok:
        return True
    log.print("\n  [!] LOW MEMORY — installer likely to be OOM-killed mid-extraction")
    log.print(f"      {detail}")
    log.print("      FitGirl's solid-archive decompressor commonly needs several GB")
    log.print("      of working memory; this exact pattern has previously been")
    log.print("      silently killed by the kernel OOM-killer on this system.")
    log.print("      Suggested fixes: close other apps (browser, Heroic, etc.),")
    log.print("      or add real disk-backed swap (zram alone isn't equivalent).")
    try:
        ans = input("  Proceed anyway despite low memory? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = 'n'
    return ans in ('y', 'yes')


def preflight_hard_disk_check(install_base: Path, repack_size: int, skip: bool = False) -> bool:
    """Hard stop before launching setup.exe: is there at least the repack's own
    size free at the install location? Re-checked per retry like the memory
    check, since a partially-extracted attempt can itself eat the free space.
    Returns True = OK to proceed."""
    if skip:
        log.print("  [i] Disk space check skipped (--skip-disk-check).")
        return True
    if not repack_size:
        return True  # couldn't size the repack — don't block on missing data
    target = existing_ancestor(install_base)
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        return True
    if free >= repack_size:
        return True
    log.print(f"\n  [!] LOW DISK SPACE at {target}")
    log.print(f"      {free / 1e9:.1f} GB free, but the repack itself is "
              f"{repack_size / 1e9:.1f} GB — extraction needs at least that much room.")
    log.print("      Free up space, or point --install-dir at a location with more,")
    log.print("      and retry.")
    try:
        ans = input("  Proceed anyway despite low disk space? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = 'n'
    return ans in ('y', 'yes')


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load ~/.config/nexus/config.toml. Returns a flat dict with
    keys: api_key, steam, steam_account, desktop, install_base, runner.
    Missing file / parse error -> defaults."""
    cfg = {'api_key': None, 'steam': True, 'steam_account': None,
           'desktop': True, 'heroic': True, 'proton': None,
           'heroic_proton': None, 'install_base': None, 'scan_dir': None, 'scan_dirs': [],
           'unattended': True, 'trash_repack': False, 'runner': 'auto',
           'discord_client_id': None, 'keep_awake': True}

    def as_bool(v, default=True):
        # `steam = "no"` is a truthy string; only real TOML booleans count.
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ('1', 'true', 'yes', 'on')
        return default

    try:
        import tomllib
    except ImportError:
        print("  [!] Python < 3.11 (no tomllib) — config.toml ignored.", file=sys.stderr)
        tomllib = None
    try:
        if tomllib is None:
            raise OSError
        with open(CONFIG_PATH, 'rb') as f:
            data = tomllib.load(f)
        cfg['api_key'] = data.get('steamgriddb', {}).get('api_key') or None
        integ = data.get('integrations', {})
        cfg['steam'] = as_bool(integ.get('steam', True))
        # steam_mode is gone -- Steam shortcuts always point at the game .exe
        # now. Warn once so an old config doesn't look like it still does
        # something.
        if (integ.get('steam_mode') or '').lower() == 'launcher':
            print("  [i] config [integrations] steam_mode is no longer used — Steam "
                  "shortcuts always point at the game .exe now.", file=sys.stderr)
        cfg['steam_account'] = (integ.get('steam_account') or None)
        cfg['desktop'] = as_bool(integ.get('desktop', True))
        cfg['heroic'] = as_bool(integ.get('heroic', True))
        cfg['heroic_proton'] = data.get('heroic', {}).get('proton') or None
        cfg['install_base'] = data.get('install', {}).get('base') or None
        cfg['scan_dir'] = data.get('install', {}).get('scan_dir') or None
        dirs = data.get('install', {}).get('scan_dirs') or []
        cfg['scan_dirs'] = [str(d) for d in dirs] if isinstance(dirs, list) else [str(dirs)]
        # Silent installs (no setup window) unless turned off.
        cfg['unattended'] = as_bool(data.get('install', {}).get('unattended', True))
        cfg['trash_repack'] = as_bool(data.get('install', {}).get('trash_repack', False),
                                      default=False)
        cfg['runner'] = (data.get('runner', {}).get('backend') or 'auto').lower()
        # The Proton games are *played* with (launch.sh, Steam, Heroic). The
        # older [heroic] proton key means the same thing and is still honoured.
        cfg['proton'] = data.get('runner', {}).get('proton') or None
        # Discord Rich Presence needs the id of an application you created at
        # discord.com/developers; none set = no presence.
        cid = str(data.get('discord', {}).get('client_id') or '').strip()
        cfg['discord_client_id'] = cid if cid.isdigit() else None
        cfg['keep_awake'] = as_bool(integ.get('keep_awake', True))
    except OSError:
        pass
    except ValueError as e:
        print(f"  [!] {CONFIG_PATH} is not valid TOML ({e}) — using defaults.",
              file=sys.stderr)
    # Environment override for the API key
    cfg['api_key'] = os.environ.get('STEAMGRIDDB_API_KEY') or cfg['api_key']
    return cfg


def resolve_install_base(cli_value: str | None) -> Path:
    """Resolve the install base with precedence: --install-dir > config
    [install] base > default. `cli_value` is None when the flag is absent."""
    if cli_value:
        return Path(cli_value).expanduser()
    configured = load_config().get('install_base')
    if configured:
        return Path(configured).expanduser()
    return default_install_base()


def resolve_scan_dirs(cli_value: str | None) -> list[Path]:
    """Folders --scan looks in: --scan-dir > config [install] scan_dir plus
    scan_dirs (a list, for repacks kept in more than one place) >
    ~/Downloads."""
    if cli_value:
        return [Path(cli_value).expanduser()]
    cfg = load_config()
    out: list[Path] = []
    for d in [cfg.get('scan_dir'), *(cfg.get('scan_dirs') or [])]:
        if d and Path(d).expanduser() not in out:
            out.append(Path(d).expanduser())
    return out or [default_scan_dir()]


# ---------------------------------------------------------------------------
# Game registry (games.json) — the source of truth for "managed games"
# ---------------------------------------------------------------------------

REGISTRY_PATH = NX.REGISTRY_PATH


_REGISTRY_LOCK_DEPTH = 0
_REGISTRY_LOCK_FH = None


@contextlib.contextmanager
def registry_lock():
    """Exclusive lock around a registry load -> modify -> save, so two
    nexus-cli processes (e.g. the GUI's size scan and a CLI command)
    don't silently drop each other's updates. Re-entrant within a process."""
    global _REGISTRY_LOCK_DEPTH, _REGISTRY_LOCK_FH
    if _REGISTRY_LOCK_DEPTH == 0:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _REGISTRY_LOCK_FH = open(DATA_DIR / 'games.json.lock', 'a')
        fcntl.flock(_REGISTRY_LOCK_FH, fcntl.LOCK_EX)
    _REGISTRY_LOCK_DEPTH += 1
    try:
        yield
    finally:
        _REGISTRY_LOCK_DEPTH -= 1
        if _REGISTRY_LOCK_DEPTH == 0 and _REGISTRY_LOCK_FH:
            fcntl.flock(_REGISTRY_LOCK_FH, fcntl.LOCK_UN)
            _REGISTRY_LOCK_FH.close()
            _REGISTRY_LOCK_FH = None


def load_registry() -> dict:
    try:
        with open(REGISTRY_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('games'), dict):
            return data
        raise ValueError('unexpected structure')
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"  [!] Could not read {REGISTRY_PATH}: {e}", file=sys.stderr)
    except ValueError as e:
        # Never let a damaged registry be silently replaced by an empty one on
        # the next save — set it aside where it can be recovered by hand.
        dest = REGISTRY_PATH.with_name(
            f"games.json.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(REGISTRY_PATH, dest)
            print(f"  [!] {REGISTRY_PATH} was unreadable ({e}); moved it to {dest}. "
                  f"Run --reindex to rebuild from the installed games.", file=sys.stderr)
        except OSError:
            pass
    return {'version': 1, 'games': {}}


def save_registry(reg: dict) -> None:
    atomic_write(REGISTRY_PATH, json.dumps(reg, indent=2))


# Registry keys that register_game() doesn't compute itself; carried over from
# the existing entry so re-registering (add-steam, add-heroic, reindex, ...)
# doesn't wipe data written elsewhere.
_CARRIED_KEYS = ('disk_bytes', 'disk_bytes_at', 'last_played', 'launch', 'proton',
                 'playtime_seconds', 'repack_fingerprint', 'repack_version',
                 'repack_source', 'repack_installed_at', 'umu_gameid')


def register_game(*, slug: str, display_name: str, exe, compatdata, wine_prefix,
                  launcher, source: str, install_base, steam_appid,
                  has_desktop: bool, artwork_dir, heroic_app_name=None,
                  proton: str | None = None, repack: dict | None = None,
                  umu_gameid: str | None = None) -> None:
    with registry_lock():
        reg = load_registry()
        existing = reg['games'].get(slug, {})
        added_at = existing.get('added_at') or datetime.now().isoformat(timespec='seconds')
        entry = {
            'slug': slug,
            'display_name': display_name,
            'exe': str(exe) if exe else None,
            'compatdata': str(compatdata) if compatdata else None,
            'wine_prefix': str(wine_prefix) if wine_prefix else None,
            'launcher': str(launcher) if launcher else None,
            'source': source,
            'install_base': str(install_base) if install_base else None,
            'steam_appid': steam_appid,
            'has_desktop': bool(has_desktop),
            'artwork_dir': str(artwork_dir) if artwork_dir else None,
            # Keep an existing Heroic id if this call didn't (re)compute one.
            'heroic_app_name': heroic_app_name if heroic_app_name is not None
                               else existing.get('heroic_app_name'),
            'added_at': added_at,
        }
        for k in _CARRIED_KEYS:
            if existing.get(k) is not None:
                entry[k] = existing[k]
        if proton:
            entry['proton'] = proton
        if umu_gameid:
            entry['umu_gameid'] = umu_gameid
        if repack:                   # which repack release this is (update detection)
            entry.update({k: v for k, v in repack.items() if v is not None})
        reg['games'][slug] = entry
        save_registry(reg)


def unregister_game(slug: str) -> None:
    with registry_lock():
        reg = load_registry()
        if reg['games'].pop(slug, None) is not None:
            save_registry(reg)


def registry_get(slug: str) -> dict | None:
    return load_registry()['games'].get(slug)


def update_registry_entry(slug: str, **fields) -> bool:
    """Set fields on an existing registry entry (a value of None deletes the
    key). Returns False if the slug isn't registered."""
    with registry_lock():
        reg = load_registry()
        entry = reg['games'].get(slug)
        if entry is None:
            return False
        for k, v in fields.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        save_registry(reg)
        return True


def set_registry_steam_appid(slug: str, appid) -> None:
    with registry_lock():
        reg = load_registry()
        if slug in reg['games']:
            reg['games'][slug]['steam_appid'] = appid
            save_registry(reg)


def update_registry_size(slug: str, size_bytes: int) -> None:
    update_registry_entry(slug, disk_bytes=size_bytes,
                          disk_bytes_at=datetime.now().isoformat(timespec='seconds'))


def touch_last_played(slug: str) -> None:
    update_registry_entry(slug, last_played=datetime.now().isoformat(timespec='seconds'))


# ---------------------------------------------------------------------------
# SteamGridDB artwork
# ---------------------------------------------------------------------------

def _sgdb_get(path: str, api_key: str) -> dict | None:
    """GET a SteamGridDB API endpoint, return parsed JSON 'data' or None."""
    data, err = _sgdb_json(path, api_key)
    return None if err else data


ART_MAX_BYTES = 32 << 20


def _download(url: str, dest: Path) -> Path | None:
    """An image download: https only, size-capped, through the shared client."""
    from nexus import net
    try:
        net.download(url, dest, timeout=30, limit=ART_MAX_BYTES)
    except net.NetError:
        return None
    return dest


def fetch_artwork(display_name: str, slug: str, config: dict) -> dict | None:
    """Best-effort SteamGridDB fetch. Returns {kind: Path} for the assets it
    could download (cover/hero/logo/icon), or None. Never raises."""
    api_key = config.get('api_key')
    if not api_key:
        return None

    import urllib.parse
    forced = read_sgdb_match(ARTWORK_DIR / slug)
    if forced:
        game_id = forced['id']
    else:
        term = urllib.parse.quote(display_name, safe='')
        results = _sgdb_get(f"/search/autocomplete/{term}", api_key)
        if not results:
            return None
        game_id = results[0].get('id')
    if not game_id:
        return None

    # (kind, endpoint, extra query) — first asset of each is used
    wanted = [
        ('cover', f"/grids/game/{game_id}?dimensions=600x900,660x930&types=static"),
        ('hero',  f"/heroes/game/{game_id}?types=static"),
        ('logo',  f"/logos/game/{game_id}?types=static"),
        ('icon',  f"/icons/game/{game_id}"),
    ]
    dest_dir = ARTWORK_DIR / slug

    def fetch_one(item):
        """Metadata lookup + download for one art kind. Returns (kind, path,
        url) or None. Runs off-thread, so it must not touch shared state."""
        kind, endpoint = item
        data = _sgdb_get(endpoint, api_key)
        if not data:
            return None
        url = data[0].get('url')
        if not url:
            return None
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or '.png'
        saved = _download(url, dest_dir / f"{kind}{ext}")
        return (kind, saved, url) if saved else None

    # Four independent lookup+download pairs; running them serially made this
    # eight round trips back to back. These are network-bound, so threads are
    # the right tool.
    out: dict[str, Path] = {}
    from concurrent.futures import ThreadPoolExecutor   # lazy: ~10 ms import
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        for res in pool.map(fetch_one, wanted):
            if not res:
                continue
            kind, saved, url = res
            out[kind] = saved
            # Stash the source URL too (Heroic prefers a remote art URL). These
            # str values sit alongside the Path values; callers read art by the
            # bare kind keys, so the extra '<kind>_url' keys are harmless.
            out[f'{kind}_url'] = url
    return out or None


# ---------------------------------------------------------------------------
# SteamGridDB for every game: variants to choose from, picks, gap filling
# ---------------------------------------------------------------------------
#
# Art lives in ARTWORK_DIR/<slug>/<kind>.<ext> for every game (repacks have
# always used it; Steam/Heroic games get one when art is picked or filled).
# picks.json there records what the user chose, so `--library` can put a
# pick ahead of Steam's own library-cache art. Steam's files are never touched.

SGDB_CACHE = NX.CACHE_DIR / 'sgdb'
SGDB_KINDS = {                     # kind -> (endpoint, query)
    'cover': ('grids', 'dimensions=600x900,660x930&types=static'),
    'wide':  ('grids', 'dimensions=920x430,460x215&types=static'),
    'hero':  ('heroes', 'types=static'),
    'logo':  ('logos', 'types=static'),
    'icon':  ('icons', 'types=static'),
}
_SGDB_CDN = re.compile(r'^https://cdn2?\.steamgriddb\.com/[A-Za-z0-9_./-]+$')
VARIANTS_TTL = 24 * 3600
MISS_TTL = 7 * 24 * 3600


def _sgdb_json(path: str, api_key: str) -> tuple[object, str | None]:
    """GET an SGDB endpoint: (data, None) or (None, 'auth'|'rate'|'none'|'net').
    Through net.get: the key never follows a redirect to another host, and the
    reply is size-capped."""
    from nexus import net
    try:
        payload = json.loads(net.get(f"{SGDB_API}{path}", timeout=15, limit=8 << 20,
                                     headers={'Authorization': f'Bearer {api_key}'}))
    except net.NetError as e:
        return None, {401: 'auth', 403: 'auth', 404: 'none', 429: 'rate'}.get(e.status, 'net')
    except ValueError:
        return None, 'net'
    if not isinstance(payload, dict) or not payload.get('success'):
        return None, 'none'
    return payload.get('data'), None


def _cache_json(name: str) -> dict:
    try:
        return json.loads((SGDB_CACHE / name).read_text())
    except (OSError, ValueError):
        return {}


def _cache_put(name: str, data) -> None:
    try:
        atomic_write(SGDB_CACHE / name, json.dumps(data))
    except OSError:
        pass


_SGDB_LOCK = threading.Lock()


def _cache_set(name: str, key: str, value) -> None:
    """Add one key to a shared cache file. --fill-art looks games up from
    several threads: read-modify-write under a lock, or the last writer
    drops everyone else's ids."""
    with _SGDB_LOCK:
        data = _cache_json(name)
        data[key] = value
        _cache_put(name, data)


def sgdb_game_id(g: dict, api_key: str) -> tuple[int | None, str | None]:
    """The SteamGridDB game id for a library game: by Steam appid when it has
    a real one (a repack's steam_appid is a *shortcut* id, not a store id),
    otherwise the best name match. Cached; misses are remembered a week."""
    forced = read_sgdb_match(art_dir_for(g)) if g.get('slug') else None
    if forced:
        return forced['id'], None
    appid = g.get('appid') if g.get('source') == 'steam' else None
    key = f"steam:{appid}" if appid else f"name:{(g.get('display_name') or '').lower()}"
    ids, misses = _cache_json('ids.json'), _cache_json('misses.json')
    if key in ids:
        return ids[key], None
    if time.time() - misses.get(key, 0) < MISS_TTL:
        return None, 'none'
    if appid:
        data, err = _sgdb_json(f"/games/steam/{appid}", api_key)
        gid = data.get('id') if isinstance(data, dict) else None
    else:
        import urllib.parse
        name = g.get('display_name') or ''
        data, err = _sgdb_json(f"/search/autocomplete/{urllib.parse.quote(name, safe='')}", api_key)
        gid = data[0].get('id') if isinstance(data, list) and data \
            and isinstance(data[0], dict) else None
    if gid:
        _cache_set('ids.json', key, gid)
        return gid, None
    if err in (None, 'none'):
        _cache_set('misses.json', key, time.time())
    return None, err or 'none'


def _variant(item: dict) -> dict | None:
    url = item.get('url') or ''
    if not _SGDB_CDN.match(url):
        return None
    thumb = item.get('thumb') or url
    return {'id': item.get('id'), 'url': url,
            'thumb': thumb if _SGDB_CDN.match(thumb) else url,
            'width': item.get('width'), 'height': item.get('height'),
            'style': item.get('style'), 'mime': item.get('mime'),
            'score': item.get('score'),
            'author': (item.get('author') or {}).get('name')}


def art_variants(g: dict, kind: str, page: int, api_key: str) -> dict:
    gid, err = sgdb_game_id(g, api_key)
    if not gid:
        return {'game_id': None, 'kind': kind, 'items': [], 'error': err}
    name = f"{gid}-{kind}-{page}.json"
    cached = _cache_json(name)
    if cached and time.time() - cached.get('at', 0) < VARIANTS_TTL:
        return cached['result']
    endpoint, query = SGDB_KINDS[kind]
    data, err = _sgdb_json(f"/{endpoint}/game/{gid}?{query}&page={page}", api_key)
    items = [v for v in (_variant(i) for i in (data or []) if isinstance(i, dict)) if v]
    result = {'game_id': gid, 'kind': kind, 'page': page, 'items': items, 'error': err}
    if err is None:
        _cache_put(name, {'at': time.time(), 'result': result})
    return result


def art_dir_for(g: dict) -> Path:
    return Path(g.get('artwork_dir') or (ARTWORK_DIR / g['slug']))


def read_picks(d: Path) -> dict:
    try:
        data = json.loads((d / 'picks.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_sgdb_match(d: Path) -> dict | None:
    """The SteamGridDB game the user chose for this game (sgdb.json), if any."""
    try:
        data = json.loads((d / 'sgdb.json').read_text())
        return data if isinstance(data, dict) and int(data.get('id')) > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def sgdb_search(term: str, api_key: str) -> dict:
    """SteamGridDB games matching a name, for choosing the right one."""
    import urllib.parse
    data, err = _sgdb_json(f"/search/autocomplete/{urllib.parse.quote(term, safe='')}", api_key)
    items = []
    for i in (data if isinstance(data, list) else []):
        if not isinstance(i, dict) or not i.get('id'):
            continue
        ts = i.get('release_date')
        year = datetime.fromtimestamp(ts).year if isinstance(ts, (int, float)) else None
        items.append({'id': i['id'], 'name': i.get('name') or '', 'year': year,
                      'verified': bool(i.get('verified'))})
    return {'items': items, 'error': err if not items else None}


def _art_file(d: Path, kind: str) -> Path | None:
    hits = sorted(p for p in d.glob(f'{kind}.*') if p.suffix != '.part')
    return hits[0] if hits else None


def save_art(d: Path, kind: str, url: str) -> Path | None:
    """Download `url` as d/<kind>.<ext>, replacing any other <kind>.* file."""
    import urllib.parse
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() or '.png'
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        ext = '.png'
    tmp = d / f"{kind}{ext}.part"
    if not _download(url, tmp):
        return None
    for old in d.glob(f'{kind}.*'):
        if old != tmp:
            old.unlink(missing_ok=True)
    dest = d / f"{kind}{ext}"
    os.replace(tmp, dest)
    return dest


def overlay_art(g: dict) -> None:
    """Point a library game at its artwork dir, with the user's picks taking
    precedence over the source's own art (Steam library cache, Heroic)."""
    d = art_dir_for(g)
    if not d.is_dir():
        return
    g['artwork_dir'] = str(d)
    for kind in read_picks(d):
        f = _art_file(d, kind) if kind in SGDB_KINDS else None
        if f:
            g[kind] = str(f)
            g.pop(f'{kind}_url', None)


def cmd_art_variants(ident: str, kind: str, page: int, as_json: bool) -> None:
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if kind not in SGDB_KINDS:
        print(f"Unknown art kind {kind!r}", file=sys.stderr)
        sys.exit(2)
    key = load_config().get('api_key')
    res = art_variants(g, kind, page, key) if key else \
        {'game_id': None, 'kind': kind, 'items': [], 'error': 'no-key'}
    picks = read_picks(art_dir_for(g))
    res['current'] = (picks.get(kind) or {}).get('url')
    if as_json:
        print(json.dumps(res))
        return
    for v in res['items']:
        print(f"{v['id']:>10}  {v['width']}x{v['height']}  {v['style'] or '':<10} {v['url']}")
    if res.get('error'):
        print(f"({res['error']})", file=sys.stderr)


def cmd_set_art(ident: str, kind: str, url: str | None, reset: bool) -> None:
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if kind not in SGDB_KINDS:
        print(f"Unknown art kind {kind!r}", file=sys.stderr)
        sys.exit(2)
    d = art_dir_for(g)
    d.mkdir(parents=True, exist_ok=True)
    picks = read_picks(d)
    if reset:
        picks.pop(kind, None)
        if g.get('source') == 'nexus':
            # A repack's "default" is SteamGridDB's first match: fetch it again.
            key = load_config().get('api_key')
            first = (art_variants(g, kind, 0, key)['items'] or [None])[0] if key else None
            saved = save_art(d, kind, first['url']) if first else None
        else:
            for old in d.glob(f'{kind}.*'):
                old.unlink(missing_ok=True)
            saved = None
        print(f"Reset the {kind} art for {g.get('display_name')}.")
    else:
        if not url or not _SGDB_CDN.match(url):
            print("Only SteamGridDB image URLs can be set.", file=sys.stderr)
            sys.exit(2)
        saved = save_art(d, kind, url)
        if not saved:
            print("Download failed.", file=sys.stderr)
            sys.exit(1)
        picks[kind] = {'url': url, 'at': datetime.now().isoformat(timespec='seconds')}
        print(f"Set the {kind} art for {g.get('display_name')}.")
    atomic_write(d / 'picks.json', json.dumps(picks, indent=2))
    appid = g.get('steam_appid') if g.get('source') == 'nexus' else None
    if saved and appid and kind in ('cover', 'hero', 'logo', 'icon'):
        cfg_dir = find_steam_userdata(announce=False)
        if cfg_dir:
            _copy_grid_art(cfg_dir / 'grid', appid, {kind: saved})
            print("Updated the Steam shortcut's artwork too.")


def cmd_sgdb_search(term: str, as_json: bool) -> None:
    key = load_config().get('api_key')
    res = sgdb_search(term, key) if key else {'items': [], 'error': 'no-key'}
    if as_json:
        print(json.dumps(res))
        return
    for v in res['items']:
        print(f"{v['id']:>8}  {v['year'] or '----'}  {v['name']}")
    if res.get('error'):
        print(f"({res['error']})", file=sys.stderr)


def cmd_set_sgdb(ident: str, game_id: int | None, name: str | None, reset: bool) -> None:
    """Pin the SteamGridDB game used for a library game's artwork (or --reset
    to automatic matching). Art picked from the old game is dropped."""
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if not reset and not (game_id and game_id > 0):
        print("--set-sgdb needs --id (or --reset).", file=sys.stderr)
        sys.exit(2)
    d = art_dir_for(g)
    d.mkdir(parents=True, exist_ok=True)
    match = d / 'sgdb.json'
    if reset:
        match.unlink(missing_ok=True)
    else:
        atomic_write(match, json.dumps({'id': game_id, 'name': name or ''}))
    # Anything picked or auto-fetched belongs to the previous match.
    (d / 'picks.json').unlink(missing_ok=True)
    for kind in SGDB_KINDS:
        for old in d.glob(f'{kind}.*'):
            old.unlink(missing_ok=True)
    if g.get('source') == 'nexus':
        cmd_refetch_art(g['slug'], load_config())
    print(f"{'Reset' if reset else 'Set'} the SteamGridDB match for {g.get('display_name')}.")


def cmd_fill_art(install_base: Path, as_json: bool, if_stale: bool) -> None:
    """Fill missing hero / logo / wide art from SteamGridDB for every game
    (first match). Runs at most once a day with --if-stale; stops on a rate
    limit or a bad key."""
    stamp = SGDB_CACHE / 'fill-stamp'
    key = load_config().get('api_key')
    out = {'filled': 0, 'error': None, 'skipped': False}
    if not key:
        out['error'] = 'no-key'
    elif if_stale and stamp.exists() and time.time() - stamp.stat().st_mtime < 24 * 3600:
        out['skipped'] = True
    else:
        from nexus import steamlib
        games = [{**g, 'source': 'nexus'} for g in managed_entries(install_base)]
        games += steamlib.scan(STEAM_ROOT, _read_compat_mapping())
        games += steamlib.heroic_store_games(find_heroic_config())
        todo = []
        for g in games:
            overlay_art(g)
            d = art_dir_for(g)
            for kind in ('hero', 'logo', 'wide'):
                local = g.get(kind) and Path(g[kind]).exists()
                if local or g.get(f'{kind}_url') or (d.is_dir() and _art_file(d, kind)):
                    continue
                todo.append((g, kind))
        stop = {'err': None}

        def one(job):
            g, kind = job
            if stop['err']:
                return 0
            res = art_variants(g, kind, 0, key)
            if res.get('error') in ('rate', 'auth'):
                stop['err'] = res['error']
                return 0
            first = (res.get('items') or [None])[0]
            if not first:
                return 0
            d = art_dir_for(g)
            d.mkdir(parents=True, exist_ok=True)
            return 1 if save_art(d, kind, first['url']) else 0
        from concurrent.futures import ThreadPoolExecutor   # lazy: ~10 ms import
        with ThreadPoolExecutor(max_workers=4) as pool:
            out['filled'] = sum(pool.map(one, todo))
        out['error'] = stop['err']
        try:
            SGDB_CACHE.mkdir(parents=True, exist_ok=True)
            stamp.touch()
        except OSError:
            pass
    if as_json:
        print(json.dumps(out))
    else:
        print(f"Filled {out['filled']} image(s)." + (f" ({out['error']})" if out['error'] else ""))


# ---------------------------------------------------------------------------
# Per-game launch profile — one tuning applied to launch.sh, Steam and Heroic
# ---------------------------------------------------------------------------
#
# Stored in games.json as entry['launch']:
#   {"proton": str|None,          # per-game Proton build (overrides config)
#    "env": [[KEY, VALUE], ...],  # environment variables
#    "wrappers": [argv...],       # wrapper programs before the game, flat argv
#                                 #   e.g. ["gamescope","-W","1920","--","mangohud"]
#    "args": [argv...],           # arguments after the game exe
#    "source": "manual"|"protongen", "updated_at": iso8601}
# An entry without 'launch' has no profile: Steam launch options and Heroic
# env/wrappers are then left exactly as the user set them. A present-but-empty
# profile actively clears them.

PROTONGEN_STATE = Path.home() / ".config/protongen/state.toml"
PROTONGEN_CODE_PREFIX = "protongen:v1:"

# Wrapper programs a flat wrapper argv is split on (for Heroic's per-wrapper
# list), with protongen's outer->inner rank (params.toml `order`).
WRAPPER_RANK = {
    'gamescope': 0, 'prime-run': 10, 'game-performance': 20, 'gamemoderun': 30,
    'dlss-swapper': 40, 'obs-gamecapture': 45, 'strangle': 45, 'pw-jack': 45,
    'mangohud': 50,
}
_DEFAULT_WRAPPER_RANK = 45

# Env vars the launchers set themselves; a profile must not override them or
# the game would run against a different prefix / Steam root.
_RESERVED_ENV = {'WINEPREFIX', 'STEAM_COMPAT_DATA_PATH',
                 'STEAM_COMPAT_CLIENT_INSTALL_PATH', 'PROTONPATH'}
_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
# Env vars a shared preset code may not set: each one can make the loader, a
# shell, Python (Proton and umu-run are Python) or GIO run something else.
_UNSAFE_ENV_RE = re.compile(r'^(LD_|PYTHON|PERL5|GIO_|DYLD_)|^(BASH_ENV|ENV|BROWSER|PATH|'
                            r'SHELL|IFS|PROMPT_COMMAND|NODE_OPTIONS|RUBYOPT|GCONV_PATH)$')


class LaunchParseError(ValueError):
    pass


def _is_env_token(tok: str) -> bool:
    k, sep, _ = tok.partition('=')
    return bool(sep) and bool(_ENV_KEY_RE.match(k))


def empty_profile() -> dict:
    return {'proton': None, 'env': [], 'wrappers': [], 'args': []}


def parse_launch_options(text: str) -> dict:
    """Parse a launch line into a profile (env/wrappers/args, plus proton when
    the line names one via PROTONPATH). Understands:
      - a Steam launch-options string:  ENV=v ... wrappers %command% args
      - a umu command (protongen's umu mode):  ENV=v ... wrappers umu-run <exe> args
      - bare env assignments / game args with no %command% (Steam semantics:
        leading KEY=value are env, anything else is passed to the game).
    Raises LaunchParseError on unbalanced quotes."""
    try:
        toks = shlex.split(text or '')
    except ValueError as e:
        raise LaunchParseError(f"can't parse launch options: {e}") from None
    prof = empty_profile()
    before, after = toks, []
    if '%command%' in toks:
        i = toks.index('%command%')
        before, after = toks[:i], toks[i + 1:]
    elif any(os.path.basename(t) == 'umu-run' for t in toks):
        i = next(i for i, t in enumerate(toks) if os.path.basename(t) == 'umu-run')
        before, after = toks[:i], toks[i + 2:]   # skip umu-run and its exe
    else:
        n = 0
        while n < len(toks) and _is_env_token(toks[n]):
            n += 1
        before, after = toks[:n], toks[n:]
    n = 0
    while n < len(before) and _is_env_token(before[n]):
        n += 1
    for tok in before[:n]:
        k, _, v = tok.partition('=')
        if k == 'PROTONPATH':
            prof['proton'] = os.path.basename(v.rstrip('/')) or None
        elif k in _RESERVED_ENV:
            continue
        else:
            prof['env'] = [e for e in prof['env'] if e[0] != k] + [[k, v]]
    prof['wrappers'] = before[n:]
    prof['args'] = after
    return prof


def profile_is_empty(prof: dict | None) -> bool:
    return not prof or not (prof.get('env') or prof.get('wrappers') or prof.get('args'))


def format_launch_options(prof: dict | None) -> str:
    """The Steam launch-options string for a profile ('' when it sets nothing)."""
    if profile_is_empty(prof):
        return ''
    parts = [f"{k}={shlex.quote(v)}" for k, v in prof.get('env', [])]
    parts += [shlex.quote(t) for t in prof.get('wrappers', [])]
    parts.append('%command%')
    parts += [shlex.quote(t) for t in prof.get('args', [])]
    return ' '.join(parts)


def split_wrappers(wrappers: list[str]) -> list[list[str]]:
    """Split a flat wrapper argv into one argv per program: a new program starts
    at the first token, after a `--`, or at a known wrapper name / absolute path
    to an executable."""
    segs: list[list[str]] = []
    for tok in wrappers:
        starts = (not segs or segs[-1][-1:] == ['--']
                  or os.path.basename(tok) in WRAPPER_RANK
                  or (tok.startswith('/') and os.access(tok, os.X_OK)))
        if starts:
            segs.append([tok])
        else:
            segs[-1].append(tok)
    return segs


# ---------------------------------------------------------------------------
# Performance presets: named switches over a profile's env and wrappers
# ---------------------------------------------------------------------------
#
# Nothing extra is stored: perf_state() reads the switches back out of the
# profile, so hand-written options, protongen presets and these toggles all
# describe one command line.

FPS_ENV = ('DXVK_FRAME_RATE', 'VKD3D_FRAME_RATE')     # D3D8-11 / D3D12 frame caps
PERF_MODES = ('game-performance', 'gamemoderun')     # CachyOS power profile / Feral
UPSCALERS = {'off': [], 'fsr4': [('PROTON_FSR4_UPGRADE', '1')],
             'optiscaler': [('PROTON_USE_OPTISCALER', '1'), ('PROTON_FSR4_UPGRADE', '1')]}
_UPSCALER_KEYS = {'PROTON_FSR4_UPGRADE', 'PROTON_USE_OPTISCALER', 'PROTON_OPTISCALER_CONFIG'}
_GAMESCOPE_RE = re.compile(r'^(\d{3,5})x(\d{3,5})(?:@(\d{2,3}))?$')


def _env_get(prof: dict, key: str) -> str | None:
    return next((v for k, v in prof.get('env', []) if k == key), None)


def _env_set(env: list, key: str, value: str | None) -> list:
    out = [e for e in env if e[0] != key]
    if value is not None:
        out.append([key, value])
    return out


def parse_gamescope(seg: list[str]) -> dict:
    """{w, h, r, render_w, render_h, fsr, fullscreen, hdr} from a gamescope
    wrapper argv (its own options, up to `--`)."""
    g = {'w': None, 'h': None, 'r': None, 'render_w': None, 'render_h': None,
         'fsr': False, 'fullscreen': False, 'hdr': False}
    flags = {'-W': 'w', '--output-width': 'w', '-H': 'h', '--output-height': 'h',
             '-r': 'r', '--nested-refresh': 'r', '-w': 'render_w', '--nested-width': 'render_w',
             '-h': 'render_h', '--nested-height': 'render_h'}
    it = iter(seg[1:])
    for tok in it:
        if tok == '--':
            break
        if tok in flags:
            v = next(it, '')
            g[flags[tok]] = int(v) if v.isdigit() else None
        elif tok in ('-F', '--filter'):
            g['fsr'] = next(it, '') == 'fsr'
        elif tok in ('-U', '--fsr-upscaling'):
            g['fsr'] = True
        elif tok in ('-f', '--fullscreen'):
            g['fullscreen'] = True
        elif tok == '--hdr-enabled':
            g['hdr'] = True
    return g


def gamescope_argv(g: dict) -> list[str]:
    out = ['gamescope']
    for flag, key in (('-W', 'w'), ('-H', 'h'), ('-r', 'r'), ('-w', 'render_w'),
                      ('-h', 'render_h')):
        if g.get(key):
            out += [flag, str(int(g[key]))]
    if g.get('fsr'):
        out += ['-F', 'fsr']
    if g.get('fullscreen'):
        out.append('-f')
    if g.get('hdr'):
        out.append('--hdr-enabled')
    return out + ['--']


def perf_state(prof: dict | None) -> dict:
    prof = prof or {}
    segs = split_wrappers(prof.get('wrappers', []))
    names = [os.path.basename(s[0]) for s in segs]
    fps = _env_get(prof, 'DXVK_FRAME_RATE') or _env_get(prof, 'VKD3D_FRAME_RATE')
    up = ('optiscaler' if _env_get(prof, 'PROTON_USE_OPTISCALER') == '1' else
          'fsr4' if _env_get(prof, 'PROTON_FSR4_UPGRADE') == '1' else 'off')
    gs = next((parse_gamescope(s) for s in segs if os.path.basename(s[0]) == 'gamescope'),
              None)
    return {'fps': int(fps) if fps and fps.isdigit() else 0,
            'mode': next((m for m in PERF_MODES if m in names), 'off'),
            'mangohud': 'mangohud' in names or _env_get(prof, 'MANGOHUD') == '1',
            'wayland': _env_get(prof, 'PROTON_ENABLE_WAYLAND') == '1',
            'no_hidraw': _env_get(prof, 'PROTON_DISABLE_HIDRAW') == '1',
            'upscaler': up, 'gamescope': gs}


def _rank(seg: list[str]) -> int:
    return WRAPPER_RANK.get(os.path.basename(seg[0]), _DEFAULT_WRAPPER_RANK)


def apply_perf(prof: dict | None, changes: dict) -> dict:
    """A copy of `prof` with the given switches set (keys as perf_state);
    everything else in it is kept. Wrappers stay in protongen's outer→inner
    order."""
    prof = {**empty_profile(), **(prof or {})}
    env = [list(e) for e in prof.get('env', [])]
    segs = split_wrappers(prof.get('wrappers', []))
    if 'fps' in changes:
        n = int(changes['fps'] or 0)
        for k in FPS_ENV:
            env = _env_set(env, k, str(n) if n > 0 else None)
    if 'mode' in changes:
        segs = [s for s in segs if os.path.basename(s[0]) not in PERF_MODES]
        if changes['mode'] in PERF_MODES:
            segs.append([changes['mode']])
    if 'mangohud' in changes:
        segs = [s for s in segs if os.path.basename(s[0]) != 'mangohud']
        env = _env_set(env, 'MANGOHUD', None)
        if changes['mangohud']:
            segs.append(['mangohud'])
    if 'wayland' in changes:
        env = _env_set(env, 'PROTON_ENABLE_WAYLAND', '1' if changes['wayland'] else None)
    if 'no_hidraw' in changes:
        env = _env_set(env, 'PROTON_DISABLE_HIDRAW', '1' if changes['no_hidraw'] else None)
    if 'upscaler' in changes:
        want = changes['upscaler'] if changes['upscaler'] in UPSCALERS else 'off'
        keep_cfg = _env_get(prof, 'PROTON_OPTISCALER_CONFIG') if want == 'optiscaler' else None
        env = [e for e in env if e[0] not in _UPSCALER_KEYS]
        env += [[k, v] for k, v in UPSCALERS[want]]
        if keep_cfg:
            env.append(['PROTON_OPTISCALER_CONFIG', keep_cfg])
    if 'gamescope' in changes:
        segs = [s for s in segs if os.path.basename(s[0]) != 'gamescope']
        if changes['gamescope']:
            segs.append(gamescope_argv(changes['gamescope']))
    segs.sort(key=_rank)
    prof['env'] = env
    prof['wrappers'] = [t for s in segs for t in s]
    return prof


def parse_perf_args(items: list[str]) -> dict:
    """`fps=60 mode=game-performance mangohud=on wayland=off hidraw=off
    upscaler=fsr4 gamescope=2560x1440@144,fsr,fullscreen,render=1920x1080`
    -> changes for apply_perf. Raises LaunchParseError."""
    on = {'on': True, 'yes': True, 'true': True, '1': True,
          'off': False, 'no': False, 'false': False, '0': False}
    out = {}
    for item in items:
        key, sep, val = item.partition('=')
        key, val = key.strip().lower(), val.strip()
        if not sep:
            raise LaunchParseError(f"expected KEY=VALUE, got {item!r}")
        if key == 'fps':
            if not (val.isdigit() and int(val) <= 1000) and val != 'off':
                raise LaunchParseError("fps= takes a number (0 or off = no cap)")
            out['fps'] = 0 if val == 'off' else int(val)
        elif key == 'mode':
            if val not in (*PERF_MODES, 'off'):
                raise LaunchParseError(f"mode= is one of {', '.join(PERF_MODES)}, off")
            out['mode'] = val
        elif key in ('mangohud', 'wayland', 'hidraw'):
            if val.lower() not in on:
                raise LaunchParseError(f"{key}= is on or off")
            out['no_hidraw' if key == 'hidraw' else key] = \
                (not on[val.lower()]) if key == 'hidraw' else on[val.lower()]
        elif key == 'lsfg':
            if val not in ('off', '2', '3', '4'):
                raise LaunchParseError("lsfg= is off, 2, 3 or 4 (frame multiplier)")
            out['lsfg'] = None if val == 'off' else int(val)
        elif key == 'upscaler':
            if val not in UPSCALERS:
                raise LaunchParseError(f"upscaler= is one of {', '.join(UPSCALERS)}")
            out['upscaler'] = val
        elif key == 'gamescope':
            if val == 'off':
                out['gamescope'] = None
                continue
            parts = [x.strip() for x in val.split(',') if x.strip()]
            m = _GAMESCOPE_RE.match(parts[0]) if parts else None
            if not m:
                raise LaunchParseError("gamescope= starts with WIDTHxHEIGHT[@REFRESH]")
            g = {'w': int(m.group(1)), 'h': int(m.group(2)),
                 'r': int(m.group(3)) if m.group(3) else None}
            for opt in parts[1:]:
                if opt in ('fsr', 'fullscreen', 'hdr'):
                    g[opt] = True
                elif opt.startswith('render='):
                    rm = _GAMESCOPE_RE.match(opt[7:])
                    if not rm or rm.group(3):
                        raise LaunchParseError("render= is WIDTHxHEIGHT")
                    g['render_w'], g['render_h'] = int(rm.group(1)), int(rm.group(2))
                else:
                    raise LaunchParseError(f"unknown gamescope option {opt!r}")
            out['gamescope'] = g
        else:
            raise LaunchParseError(f"unknown setting {key!r} (fps, mode, mangohud, wayland, "
                                   f"hidraw, upscaler, gamescope, lsfg)")
    return out


# lsfg-vk (Lossless Scaling frame generation as a Vulkan layer): one
# [[profile]] per game in its conf.toml, matched on the game's exe name.

LSFG_CONF = Path.home() / '.config/lsfg-vk/conf.toml'
LSFG_LAYER = 'VkLayer_LSFGVK_frame_generation.json'
LSFG_PREFIX = 'Nexus: '


def lsfg_installed() -> bool:
    dirs = [Path.home() / '.local/share/vulkan/implicit_layer.d',
            Path('/usr/share/vulkan/implicit_layer.d'), Path('/etc/vulkan/implicit_layer.d')]
    return any((d / LSFG_LAYER).exists() for d in dirs) and LSFG_CONF.exists()


def _toml_chunks(text: str) -> list[str]:
    """The file split before every table header line ([x] / [[x]]); the
    first chunk holds whatever precedes the first header."""
    chunks, cur = [], []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('[') and cur:
            chunks.append(''.join(cur))
            cur = []
        cur.append(line)
    if cur:
        chunks.append(''.join(cur))
    return chunks


def _lsfg_name(entry: dict) -> str:
    return LSFG_PREFIX + (entry.get('display_name') or entry['slug'])


def lsfg_state(entry: dict) -> dict | None:
    """This game's lsfg-vk profile ({multiplier, flow_scale, performance_mode}),
    {} when it has none, None when lsfg-vk isn't set up."""
    if not lsfg_installed():
        return None
    import tomllib
    try:
        data = tomllib.loads(LSFG_CONF.read_text())
    except (OSError, ValueError):
        return None
    name = _lsfg_name(entry)
    hit = next((p for p in data.get('profile', []) if p.get('name') == name), None)
    return ({k: hit.get(k) for k in ('multiplier', 'flow_scale', 'performance_mode')}
            if hit else {})


def lsfg_set(entry: dict, multiplier: int | None, flow: float = 1.0,
             performance: bool = False) -> None:
    """Add/replace (multiplier 2–4) or remove (None) this game's profile.
    The rest of the file is kept byte for byte; an unparseable file, or a
    result lsfg-vk-cli rejects, is never left in place."""
    import tomllib
    exe = _entry_paths(entry)[0]
    if multiplier and not exe:
        raise ValueError("the game's exe isn't known (set it in Game settings)")
    text = LSFG_CONF.read_text()
    try:
        data = tomllib.loads(text)
    except ValueError as e:
        raise ValueError(f"{LSFG_CONF} can't be parsed ({e}) — not touching it") from None
    if data.get('version') not in (None, 2):
        raise ValueError(f"lsfg-vk config version {data.get('version')} isn't supported")
    name = _lsfg_name(entry)
    keep = []
    for chunk in _toml_chunks(text):
        if chunk.lstrip().startswith('[[profile]]'):
            try:
                if (tomllib.loads(chunk.replace('[[profile]]', '[p]', 1))
                        .get('p', {}).get('name')) == name:
                    continue
            except ValueError:
                pass
        keep.append(chunk)
    new = ''.join(keep).rstrip('\n') + '\n'
    if multiplier:
        base = next((p for p in data.get('profile', []) if not p.get('active_in')), {})
        q = json.dumps
        block = ["[[profile]]", f"name = {q(name)}", f"active_in = {q(exe.name)}",
                 f"multiplier = {int(multiplier)}", f"flow_scale = {float(flow)!r}",
                 f"performance_mode = {'true' if performance else 'false'}"]
        for k in ('pacing_mode', 'override_present_mode', 'preserve_swapchain_image_count'):
            if k in base:
                v = base[k]
                block.append(f"{k} = {'true' if v is True else 'false' if v is False else q(v)}")
        new = new.rstrip('\n') + '\n\n' + '\n'.join(block) + '\n'
    tomllib.loads(new)                              # never write something unparseable
    backup_file(LSFG_CONF)
    old = text
    atomic_write(LSFG_CONF, new)
    cli = shutil.which('lsfg-vk-cli')
    if cli:
        r = subprocess.run([cli, 'validate', '-c', str(LSFG_CONF)], capture_output=True,
                           text=True, timeout=20)
        if r.returncode != 0:
            atomic_write(LSFG_CONF, old)
            raise ValueError("lsfg-vk-cli rejected the change: "
                             + (r.stderr or r.stdout).strip()[:300])


def heroic_launch_fields(prof: dict) -> dict:
    """The GamesConfig keys a profile owns — the same four protongen's own
    "Apply to Heroic" writes (plus launcherArgs for game arguments). mangohud
    and gamemoderun map to Heroic's native toggles; everything else is a
    wrapperOptions entry."""
    mangohud = gamemode = False
    wrapper_opts = []
    for seg in split_wrappers(prof.get('wrappers', [])):
        name, rest = os.path.basename(seg[0]), seg[1:]
        if name == 'mangohud' and not rest:
            mangohud = True
        elif name == 'gamemoderun' and not rest:
            gamemode = True
        else:
            wrapper_opts.append({'exe': seg[0], 'args': shlex.join(rest)})
    return {
        'enviromentOptions': [{'key': k, 'value': v} for k, v in prof.get('env', [])],
        'wrapperOptions': wrapper_opts,
        'showMangohud': mangohud,
        'useGameMode': gamemode,
        'launcherArgs': shlex.join(prof.get('args', [])),
    }


def profile_from_protongen_config(cfg: dict) -> dict:
    """Convert a protongen Config (state.toml game_memory entry, or a share
    code's `config`) to a profile, emitting wrappers in protongen's own
    outer->inner order so the command matches what protongen shows."""
    prof = empty_profile()
    env: dict[str, str] = {}
    for pair in cfg.get('env') or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            k, v = str(pair[0]), str(pair[1])
            if _ENV_KEY_RE.match(k) and v != '' and k not in _RESERVED_ENV:
                env[k] = v
    try:
        extra = shlex.split(str(cfg.get('extra_env') or ''))
    except ValueError:
        extra = []
    for tok in extra:
        if _is_env_token(tok):
            k, _, v = tok.partition('=')
            if k not in _RESERVED_ENV:
                env.setdefault(k, v)
    prof['env'] = [[k, v] for k, v in env.items()]
    wr = [(str(p[0]), str(p[1])) for p in (cfg.get('wrappers') or [])
          if isinstance(p, (list, tuple)) and len(p) == 2]
    wr.sort(key=lambda p: WRAPPER_RANK.get(p[0], _DEFAULT_WRAPPER_RANK))
    for key, arg in wr:
        if key == 'gamescope':
            try:
                prof['wrappers'] += ['gamescope', *shlex.split(arg), '--']
            except ValueError:
                prof['wrappers'] += ['gamescope', '--']
        else:
            prof['wrappers'].append(key)
    try:
        prof['args'] = shlex.split(str(cfg.get('game_args') or ''))
    except ValueError:
        prof['args'] = []
    runtime = cfg.get('runtime')
    prof['proton'] = str(runtime) if runtime else None
    return prof


def decode_protongen_code(code: str) -> tuple[str, dict]:
    """Decode a `protongen:v1:<base64 JSON>` preset share code into
    (preset name, profile). Validates like protongen's own decoder."""
    import base64
    text = re.sub(r'\s+', '', code or '')
    if not text.startswith(PROTONGEN_CODE_PREFIX):
        raise LaunchParseError("not a protongen preset code")
    if len(text) > 32 * 1024:
        raise LaunchParseError("preset code is too large")
    try:
        raw = json.loads(base64.b64decode(text[len(PROTONGEN_CODE_PREFIX):],
                                          validate=True).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise LaunchParseError("preset code is damaged or incomplete") from None
    if not isinstance(raw, dict) or not isinstance(raw.get('config'), dict):
        raise LaunchParseError("preset code has no config in it")
    _check_shared_config(raw['config'])
    name = str(raw.get('name') or 'protongen preset')[:64]
    return name, profile_from_protongen_config(raw['config'])


def _check_shared_config(cfg: dict) -> None:
    """A share code comes from someone else: refuse env vars that can run code
    and wrappers that aren't known launch tools, instead of writing them into
    the game's launcher."""
    keys = [str(p[0]) for p in cfg.get('env') or []
            if isinstance(p, (list, tuple)) and len(p) == 2]
    try:
        keys += [t.partition('=')[0] for t in shlex.split(str(cfg.get('extra_env') or ''))
                 if _is_env_token(t)]
    except ValueError:
        pass
    bad = sorted({k for k in keys if _UNSAFE_ENV_RE.match(k)})
    if bad:
        raise LaunchParseError(f"preset code sets unsafe environment variables: {', '.join(bad)}")
    wrappers = sorted({str(p[0]) for p in cfg.get('wrappers') or []
                       if isinstance(p, (list, tuple)) and len(p) == 2} - set(WRAPPER_RANK))
    if wrappers:
        raise LaunchParseError(f"preset code runs unknown programs: {', '.join(wrappers)}")


def protongen_heroic_id(app_name: str) -> int:
    """protongen's synthetic id for a Heroic game (games.rs heroic_app_id):
    FNV-1a-32 of the app_name with the high bit set."""
    h = 0x811c9dc5
    for b in app_name.encode('utf-8'):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h | 0x80000000


def protongen_memory_for(entry: dict) -> tuple[str, dict] | None:
    """protongen's remembered tuning for a managed game, read-only from its
    state.toml: looked up by the Steam shortcut appid first, then by the Heroic
    entry's id. Returns (which, config) or None. Never writes protongen's files
    — it rewrites state.toml wholesale on save."""
    try:
        import tomllib
        with open(PROTONGEN_STATE, 'rb') as f:
            memory = tomllib.load(f).get('game_memory', {})
    except (OSError, ValueError, ImportError):
        return None
    ids = []
    if entry.get('steam_appid'):
        ids.append(('Steam', str(int(entry['steam_appid']) & 0xFFFFFFFF)))
    heroic_names = [entry['heroic_app_name']] if entry.get('heroic_app_name') else []
    exe = entry.get('exe') or (str(_entry_paths(entry)[0] or '') or None)
    cfg_dir = find_heroic_config()
    if exe and cfg_dir:
        # A Heroic entry registered before we tracked its app_name.
        lib = _load_heroic_library(_heroic_lib_path(cfg_dir)) or {}
        heroic_names += [g['app_name'] for g in lib.get('games', [])
                         if isinstance(g, dict) and g.get('app_name')
                         and g.get('install', {}).get('executable') == exe]
    ids += [('Heroic', str(protongen_heroic_id(n))) for n in heroic_names]
    for which, key in ids:
        cfg = memory.get(key)
        if isinstance(cfg, dict):
            return which, cfg
    # Last resort: an entry built in protongen's umu mode for this exact exe.
    for cfg in memory.values():
        if isinstance(cfg, dict) and exe and cfg.get('umu_exe') == exe:
            return 'umu', cfg
    return None


def protongen_default_config() -> dict | None:
    """protongen's default profile (Settings → "Save as default profile",
    `global_profile` in state.toml) — what it offers to apply to any game it
    has no tuning for. Read-only; None when none is saved."""
    try:
        import tomllib
        with open(PROTONGEN_STATE, 'rb') as f:
            gp = tomllib.load(f).get('global_profile')
    except (OSError, ValueError, ImportError):
        return None
    return gp if isinstance(gp, dict) else None


def protongen_default_profile(entry: dict | None = None) -> dict | None:
    """The default profile as a launch profile for `entry`: its Proton named
    by folder when installed, else the game's current pin is kept."""
    cfg = protongen_default_config()
    if cfg is None:
        return None
    prof = profile_from_protongen_config(cfg)
    hit = proton_by_name(prof.get('proton'))
    prof['proton'] = (hit.parent.name if hit
                      else ((entry or {}).get('launch') or {}).get('proton'))
    return prof


def protongen_bin() -> str | None:
    return shutil.which('protongen') or (
        str(p) if (p := Path.home() / '.local/bin/protongen').exists() else None)


# ---------------------------------------------------------------------------
# Steam non-Steam-game shortcut
# ---------------------------------------------------------------------------

def shortcut_appid(exe: str, appname: str) -> int:
    """Steam's non-Steam appid: unsigned 32-bit (high bit set)."""
    crc = zlib.crc32(f'"{exe}"{appname}'.encode('utf-8')) & 0xffffffff
    return crc | 0x80000000


def _stored_appid(slug: str) -> int | None:
    """The appid already recorded for this game in games.json, if it is a valid
    non-Steam shortcut id (unsigned 32-bit with the high bit set)."""
    try:
        val = int((registry_get(slug) or {}).get('steam_appid') or 0)
    except (TypeError, ValueError):
        return None
    return val if 0x80000000 <= val <= 0xFFFFFFFF else None


def _appid_signed(unsigned: int) -> int:
    """shortcuts.vdf stores appid as a signed int32."""
    return unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned


def steam_is_running() -> bool:
    for pat in (['-x', 'steam'], ['-f', 'steamwebhelper']):
        try:
            if subprocess.run(['pgrep', *pat], capture_output=True).returncode == 0:
                return True
        except OSError:
            return False
    return False


def _load_vdf_text(path: Path) -> dict:
    """Parse a text-mode .vdf. Returns {} on any failure (missing lib/file)."""
    from nexus import steamlib
    return steamlib._load_vdf(path)


def _steam_login_users() -> dict[int, dict]:
    """config/loginusers.vdf keyed by Steam3 accountid. Carries AccountName,
    PersonaName, AutoLogin and the last-login Timestamp for each account."""
    users = _load_vdf_text(STEAM_ROOT / 'config' / 'loginusers.vdf').get('users', {})
    out: dict[int, dict] = {}
    for steamid64, entry in users.items():
        if not isinstance(entry, dict):
            continue
        try:
            accountid = int(steamid64) & 0xFFFFFFFF
        except (TypeError, ValueError):
            continue
        try:
            ts = int(entry.get('Timestamp', 0))
        except (TypeError, ValueError):
            ts = 0
        out[accountid] = {
            'steamid64': str(steamid64),
            'account_name': entry.get('AccountName', ''),
            'persona': entry.get('PersonaName', ''),
            'autologin': entry.get('AutoLogin') == '1',
            # Steam sets MostRecent on the account it last signed in as.
            'most_recent': entry.get('MostRecent') == '1',
            'timestamp': ts,
        }
    return out


def _steam_last_logged_in() -> int | None:
    """The accountid Steam most recently *signed in as*, from its connection
    log. This is the only signal that survives a manual account switch --
    loginusers' AutoLogin flag keeps pointing at the auto-login account even
    after you switch away from it."""
    path = STEAM_ROOT / 'logs' / 'connection_log.txt'
    try:
        with open(path, 'rb') as f:
            # Tail only; this log grows without bound.
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode('utf-8', errors='replace')
    except OSError:
        return None
    hit = None
    for m in re.finditer(r'\[Logged On,[^\]]*\]\s*\[U:1:(\d+)\]', tail):
        hit = m.group(1)
    try:
        return int(hit) if hit else None
    except ValueError:
        return None


def _count_shortcuts(path: Path) -> int:
    """How many non-Steam shortcuts a profile actually holds. An emptied
    shortcuts.vdf still exists on disk, so presence of the file says nothing."""
    try:
        import vdf
        with open(path, 'rb') as f:
            return len(vdf.binary_load(f).get('shortcuts', {}))
    except VDF_ERRORS:
        return 0


def steam_accounts() -> list[dict]:
    """Every Steam userdata profile on disk, newest-login first. Each dict has
    accountid/account_name/persona/autologin/logged_in plus whether the profile
    already holds shortcuts."""
    base = STEAM_ROOT / 'userdata'
    if not base.is_dir():
        return []
    logins = _steam_login_users()
    last = _steam_last_logged_in()
    out = []
    for d in base.iterdir():
        # '0' is Steam's anonymous/offline stub, never a real account.
        if not d.is_dir() or d.name == '0':
            continue
        try:
            accountid = int(d.name)
        except ValueError:
            continue
        cfg = d / 'config'
        if not cfg.is_dir():
            continue
        info = dict(logins.get(accountid, {}))
        localconfig = cfg / 'localconfig.vdf'
        try:
            mtime = localconfig.stat().st_mtime
        except OSError:
            mtime = 0.0
        info.update({
            'accountid': accountid,
            'config_dir': cfg,
            'shortcut_count': _count_shortcuts(cfg / 'shortcuts.vdf'),
            'has_localconfig': localconfig.exists(),
            'mtime': mtime,
            'logged_in': accountid == last,
        })
        info.setdefault('account_name', '')
        info.setdefault('persona', '')
        info.setdefault('autologin', False)
        info.setdefault('timestamp', 0)
        out.append(info)
    out.sort(key=lambda a: (a['timestamp'], a['mtime']), reverse=True)
    return out


def match_steam_account(want: str, accounts: list[dict]) -> dict | None:
    """Resolve a user-supplied account selector against `accounts`. Accepts a
    Steam3 accountid, a steamid64, a login name or a persona name (the last two
    case-insensitively)."""
    want = str(want).strip()
    if not want:
        return None
    for a in accounts:
        if want == str(a['accountid']) or want == a.get('steamid64'):
            return a
    low = want.lower()
    for key in ('account_name', 'persona'):
        for a in accounts:
            if (a.get(key) or '').lower() == low:
                return a
    return None


# Set once in main() from --steam-account or config [integrations] steam_account.
STEAM_ACCOUNT_PIN: str | None = None
_STEAM_PROFILE: tuple[Path | None, str, str] | None = None
_STEAM_ANNOUNCED = False


def resolve_steam_account() -> tuple[dict | None, str]:
    """Pick the Steam account to write shortcuts/artwork into, and say why.

    Precedence, most to least authoritative:
      1. an explicit pin (--steam-account / config [integrations] steam_account)
      2. the account Steam last signed in as (connection log)
      3. loginusers MostRecent, then AutoLogin=1
      4. the most recent loginusers Timestamp
      5. the profile whose localconfig.vdf was touched most recently

    Never falls back to sorting by directory name: with two profiles that both
    look "used", a name sort silently picks whichever id happens to be larger.
    """
    accounts = steam_accounts()
    if not accounts:
        return None, 'no userdata profile on disk'

    if STEAM_ACCOUNT_PIN:
        hit = match_steam_account(STEAM_ACCOUNT_PIN, accounts)
        if hit:
            return hit, f'pinned to {STEAM_ACCOUNT_PIN!r}'
        known = ', '.join(steam_account_label(a) for a in accounts) or 'none'
        print(f"  [!] Steam: pinned account {STEAM_ACCOUNT_PIN!r} not found "
              f"(have: {known}) — falling back to auto-detect.", file=sys.stderr)

    for a in accounts:
        if a.get('logged_in'):
            return a, 'last signed in to Steam'
    for a in accounts:
        if a.get('most_recent'):
            return a, 'most recent in loginusers.vdf'
    for a in accounts:
        if a.get('autologin'):
            return a, 'Steam auto-login account'
    dated = [a for a in accounts if a.get('timestamp')]
    if dated:
        return dated[0], 'newest login timestamp'
    return max(accounts, key=lambda a: a['mtime']), 'most recently used profile'


def steam_account_label(account: dict) -> str:
    """'12345678 (mylogin / My Persona)' — for logs and --doctor."""
    names = [n for n in (account.get('account_name'), account.get('persona')) if n]
    seen, uniq = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    suffix = f" ({' / '.join(uniq)})" if uniq else ''
    return f"{account['accountid']}{suffix}"


def find_steam_userdata(announce: bool = False) -> Path | None:
    """The userdata/<id>/config dir shortcuts and grid art belong in. Decided
    once per run by resolve_steam_account().

    Pass announce=True from paths that *write* to Steam, so a wrong account is
    visible instead of silent. Read-only callers stay quiet -- `--list --json`
    parses stdout, so a stray line there breaks the GUI."""
    global _STEAM_PROFILE, _STEAM_ANNOUNCED
    if _STEAM_PROFILE is None:
        account, why = resolve_steam_account()
        _STEAM_PROFILE = ((account['config_dir'] if account else None), why,
                          (steam_account_label(account) if account else ''))
    cfg_dir, why, label = _STEAM_PROFILE
    if announce and cfg_dir and not _STEAM_ANNOUNCED:
        _STEAM_ANNOUNCED = True
        log.print(f"  Steam account: {label} — {why}.")
    return cfg_dir


def _wait_for_steam_closed() -> bool:
    """If Steam is running, prompt to close it. Returns True if OK to write."""
    if not steam_is_running():
        return True
    log.print("\n  [!] Steam is running. It rewrites shortcuts.vdf on exit, so a")
    log.print("      shortcut written now would be lost. Fully quit Steam first.")
    while True:
        try:
            ans = input("  [Enter] once Steam is closed, or 's' to skip Steam: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans == 's':
            return False
        if not steam_is_running():
            return True
        log.print("  Steam still appears to be running...")


def _copy_grid_art(grid_dir: Path, unsigned_appid: int, artwork: dict) -> None:
    """Copy fetched artwork into Steam's grid/ dir using appid-based names."""
    grid_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        'cover': f"{unsigned_appid}p",       # vertical capsule
        'hero':  f"{unsigned_appid}_hero",
        'logo':  f"{unsigned_appid}_logo",
        'icon':  f"{unsigned_appid}_icon",
    }
    for kind, stem in mapping.items():
        src = artwork.get(kind)
        if not src:
            continue
        ext = src.suffix or '.png'
        try:
            shutil.copyfile(src, grid_dir / f"{stem}{ext}")
        except OSError:
            pass


def _ci_key(node, key: str):
    """Case-insensitive dict key lookup (Steam's VDF casing varies)."""
    if not isinstance(node, dict):
        return None
    for k in node:
        if k.lower() == key.lower():
            return k
    return None


def _write_compat_mapping(unsigned_appid: int, proton_name: str | None) -> bool:
    """Set (proton_name given) or clear (None) the Steam Play compat tool for a
    non-Steam appid in config.vdf. Steam must be closed (it rewrites config.vdf
    on exit). Best-effort, never raises."""
    try:
        import vdf
    except ImportError:
        return False
    if not STEAM_CONFIG_VDF.exists():
        return False
    try:
        with open(STEAM_CONFIG_VDF, encoding='utf-8', errors='replace') as f:
            data = vdf.load(f)
    except VDF_ERRORS:
        return False
    node = data
    for key in ('InstallConfigStore', 'Software', 'Valve', 'Steam'):
        k = _ci_key(node, key)
        if k is None:
            if proton_name is None:
                return False
            node[key] = {}          # build the path when forcing a tool
            k = key
        node = node[k]
    mk = _ci_key(node, 'CompatToolMapping')
    if mk is None:
        if proton_name is None:
            return False
        node['CompatToolMapping'] = {}
        mk = 'CompatToolMapping'
    mapping = node[mk]
    aid = str(unsigned_appid)
    if proton_name is None and aid not in mapping:
        return False  # nothing to clear
    if proton_name is None:
        del mapping[aid]
    else:
        cur = mapping.get(aid)
        if isinstance(cur, dict) and cur.get('name') == proton_name:
            return True     # already set — don't churn config.vdf + a backup
        mapping[aid] = {"name": proton_name, "config": "", "priority": "250"}
    try:
        payload = vdf.dumps(data, pretty=True)
        backup_file(STEAM_CONFIG_VDF)
        atomic_write(STEAM_CONFIG_VDF, payload)
    except (OSError, ValueError):
        return False
    return True


def set_steam_compat_tool(unsigned_appid: int, proton_name: str) -> bool:
    """Force a Steam Play compat tool for a non-Steam appid (native mode: Steam
    runs the game .exe through this Proton)."""
    return _write_compat_mapping(unsigned_appid, proton_name)


def _read_compat_mapping() -> dict[str, str]:
    """Read-only: {appid_str: proton_name} from Steam's CompatToolMapping in
    config.vdf. Never writes; returns {} on any failure."""
    try:
        import vdf
    except ImportError:
        return {}
    if not STEAM_CONFIG_VDF.exists():
        return {}
    try:
        with open(STEAM_CONFIG_VDF, encoding='utf-8', errors='replace') as f:
            data = vdf.load(f)
    except VDF_ERRORS:
        return {}
    node = data
    for key in ('InstallConfigStore', 'Software', 'Valve', 'Steam'):
        k = _ci_key(node, key)
        if k is None:
            return {}
        node = node[k]
    mk = _ci_key(node, 'CompatToolMapping')
    if mk is None:
        return {}
    mapping = node[mk]
    return {aid: e.get('name', '') for aid, e in mapping.items() if isinstance(e, dict)}


# Where Windows games (and the Steam-emu shims FitGirl repacks ship) put saves.
_SAVE_DIRS = (
    'drive_c/users/Public/Documents',
    'drive_c/users/steamuser/Documents',
    'drive_c/users/steamuser/AppData/Roaming',
    'drive_c/users/steamuser/AppData/Local',
    'drive_c/users/steamuser/Saved Games',
)


def find_prefix_savedata(prefix_root: Path) -> list[Path]:
    """Files under a prefix that look like save data. `prefix_root` is the
    compat-data dir (the one containing pfx/). Ignores the empty skeleton Wine
    creates in every prefix."""
    hits: list[Path] = []
    pfx = prefix_root / 'pfx'
    base = pfx if pfx.is_dir() else prefix_root
    for f in _iter_savedata(base):
        hits.append(f)
        if len(hits) >= 50:              # enough to prove the point
            return hits
    return hits


# Folders that hold many games' data (look one level deeper) or none at all.
# Steam-emulator folders (Steam/RUNE/<appid>) and engine folders
# (Godot/app_userdata/<game>) are containers too.
_SAVE_CONTAINERS = {'my games', 'saved games', 'savegames', 'documents', 'steam', 'rune',
                    'codex', 'cpy', 'empress', 'onlinefix', 'goldberg steamemu saves',
                    'godot', 'app_userdata'}
_SAVE_NOISE = {'microsoft', 'temp', 'd3dscache', 'nvidia', 'amd', 'mesa_shader_cache',
               'crashdumps', 'packages', 'connecteddevicesplatform', 'unrealengine',
               'cef', 'unity', 'openal', 'fontconfig', 'dxvk', 'vkd3d', 'mangohud'}
# Below a game's own folder: logs and crash reports, not progress.
_SAVE_NOISE_ANY = {'logs', 'crashes', 'crashreports', 'crash reports', 'temp', 'tmp'}
_SAVE_NOISE_EXT = {'.lnk', '.url', '.log', '.dmp', '.tmp'}


def _is_save_noise(rel: Path) -> bool:
    """`rel` is relative to one of _SAVE_DIRS. Caches, shader caches, Start
    Menu shortcuts, logs and crash dumps live there too, but aren't saves —
    counting them made every prefix "have save data" and swelled backups."""
    if rel.name.lower() == 'desktop.ini' or rel.suffix.lower() in _SAVE_NOISE_EXT:
        return True
    dirs = [p.lower() for p in rel.parts[:-1]]
    if dirs and dirs[0] in _SAVE_NOISE:
        return True
    return any('cache' in d or d in _SAVE_NOISE_ANY for d in dirs)


def _iter_savedata(base: Path):
    """Save-looking files under a prefix's drive (`base` holds drive_c)."""
    for rel in _SAVE_DIRS:
        root = base / rel
        if not root.is_dir():
            continue
        for f in root.rglob('*'):
            if f.is_file() and not f.is_symlink() and not _is_save_noise(f.relative_to(root)):
                yield f


def savedata_owners(prefix_root: Path, hits: list[Path], limit: int = 3) -> list[str]:
    """Best-effort "whose saves are these?" — the folder names right under the
    save locations (e.g. "Larian Studios", "Remedy"), so an unnamed prefix
    can be recognised before it is restored or trashed."""
    pfx = prefix_root / 'pfx'
    base = pfx if pfx.is_dir() else prefix_root
    owners: list[str] = []
    for f in hits:
        for rel in _SAVE_DIRS:
            try:
                parts = f.relative_to(base / rel).parts[:-1]
            except ValueError:
                continue
            while parts and parts[0].lower() in _SAVE_CONTAINERS:
                parts = parts[1:]
            if parts and parts[0].lower() not in _SAVE_NOISE:
                owner = f"Steam app {parts[0]}" if parts[0].isdigit() else parts[0]
                if owner not in owners:
                    owners.append(owner)
            break
        if len(owners) >= limit:
            break
    return owners


# A copy of the saves taken just before a restore overwrote them: kept (so
# the restore can be undone by hand) but never itself offered for restore.
PRE_RESTORE = '-before-restore'


def _unique_dir(parent: Path, name: str, suffix: str = '') -> Path:
    """parent/name+suffix, or name-2+suffix, -3… if that already exists (two
    backups in the same second must not merge into one)."""
    d, n = parent / (name + suffix), 2
    while d.exists():
        d, n = parent / f"{name}-{n}{suffix}", n + 1
    return d


def backup_prefix_saves(slug: str, game_dir: Path,
                        suffix: str = '') -> tuple[Path, int, list[Path]] | None:
    """Copy known save-data directories out of game_dir/pfx into a timestamped
    backup under SAVES_DIR/<slug>/, so an uninstalled game's saves survive and
    can be reinjected into a future reinstall (see restore_prefix_saves).
    Returns (backup_dir, file_count, files_that_failed), or None if no save
    data was found."""
    pfx = game_dir / 'pfx'
    base = pfx if pfx.is_dir() else game_dir
    if not base.is_dir():
        return None
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest_root = _unique_dir(SAVES_DIR / slug, ts, suffix)
    count, failed = 0, []
    for f in _iter_savedata(base):
        dest = dest_root / f.relative_to(base)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            count += 1
        except OSError:
            failed.append(f)
    if count == 0 and not failed:
        shutil.rmtree(dest_root, ignore_errors=True)
        return None
    return dest_root, count, failed


def latest_save_backup(slug: str) -> Path | None:
    """Most recent save backup dir for `slug`, if any."""
    slug_dir = SAVES_DIR / slug
    if not slug_dir.is_dir():
        return None
    backups = sorted((d for d in slug_dir.iterdir()
                      if d.is_dir() and not d.name.endswith(PRE_RESTORE)), reverse=True)
    return backups[0] if backups else None


def restore_prefix_saves(slug: str, wine_prefix: Path) -> tuple[int, list[Path]] | None:
    """Copy the most recent save backup for `slug` back into wine_prefix
    (game_dir/pfx), so a reinstall picks up where the old one left off.
    Returns (files_restored, files_that_failed), or None if there's no backup."""
    latest = latest_save_backup(slug)
    if not latest:
        return None
    count, failed = 0, []
    for f in latest.rglob('*'):
        if not f.is_file():
            continue
        dest = wine_prefix / f.relative_to(latest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            count += 1
        except OSError:
            failed.append(f.relative_to(latest))
    return count, failed


def _report_failed_copies(failed: list[Path], what: str) -> None:
    print(f"  [!] {len(failed)} {what} could not be copied, e.g.:", file=sys.stderr)
    for f in failed[:5]:
        print(f"      {f}", file=sys.stderr)


INTEGRITY_DIR = NX.INTEGRITY_DIR

# Everything under pfx/drive_c EXCEPT these two top-level dirs is treated as
# "game content" worth snapshotting: `users` holds saves/config (_SAVE_DIRS)
# and other per-user state that's supposed to change, and `windows` is
# Wine/Proton's own runtime, churned by every Proton update — neither is
# useful (or stable enough) to check for corruption.
_INTEGRITY_SKIP_TOPLEVEL = {'users', 'windows'}


def _integrity_files(game_dir: Path) -> list[Path]:
    """Installed game files worth an integrity snapshot (see
    _INTEGRITY_SKIP_TOPLEVEL for what's excluded and why)."""
    drive_c = game_dir / 'pfx' / 'drive_c'
    if not drive_c.is_dir():
        return []
    files = []
    for top in drive_c.iterdir():
        if top.name.lower() in _INTEGRITY_SKIP_TOPLEVEL:
            continue
        if top.is_file():
            files.append(top)
        elif top.is_dir():
            files.extend(f for f in top.rglob('*') if f.is_file())
    return files


def integrity_manifest_path(slug: str) -> Path:
    return INTEGRITY_DIR / f"{slug}.json"


def load_integrity_manifest(slug: str) -> dict | None:
    try:
        return json.loads(integrity_manifest_path(slug).read_text())
    except (OSError, ValueError):
        return None


def save_integrity_manifest(slug: str, game_dir: Path, files: list[Path]) -> None:
    hashes = dict(zip(files, _parallel_md5(files)))
    manifest = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'files': {
            str(f.relative_to(game_dir)): {'size': f.stat().st_size, 'md5': h}
            for f, h in hashes.items()
        },
    }
    atomic_write(integrity_manifest_path(slug), json.dumps(manifest, indent=2))


def cmd_check_integrity(slug: str, rebaseline: bool = False) -> None:
    """Compare installed game files against a saved MD5 manifest (or create
    one if this is the first run) to catch silent corruption — relevant here
    because the game dir has btrfs checksums disabled (see disable_cow)."""
    entry = registry_get(slug)
    if not entry or not entry.get('wine_prefix'):
        print(f"'{slug}' is not a managed game with a prefix", file=sys.stderr)
        sys.exit(1)
    game_dir = Path(entry['wine_prefix']).parent
    if not game_dir.is_dir():
        print(f"Install directory for '{slug}' is missing: {game_dir}", file=sys.stderr)
        sys.exit(1)

    files = _integrity_files(game_dir)
    if not files:
        print("No installed game files found under pfx/drive_c to check "
              "(everything is under users/ or windows/, which are skipped).")
        return

    existing = None if rebaseline else load_integrity_manifest(slug)
    if existing is None:
        total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
        print(f"{'Rebaselining' if rebaseline else 'No baseline yet — creating one'}: "
              f"hashing {len(files)} file(s), {total_mb:.0f} MB...")
        save_integrity_manifest(slug, game_dir, files)
        print(f"Baseline saved ({len(files)} files). Run this again later to "
              f"check for changes.")
        return

    current_rel = {str(f.relative_to(game_dir)) for f in files}
    known_rel = set(existing.get('files', {}))
    missing = sorted(known_rel - current_rel)
    added = sorted(current_rel - known_rel)
    common = [f for f in files if str(f.relative_to(game_dir)) in known_rel]

    total_mb = sum(f.stat().st_size for f in common) / 1024 / 1024
    print(f"Checking against baseline from {existing.get('created_at', 'unknown')}: "
          f"hashing {len(common)} known file(s), {total_mb:.0f} MB...")
    changed = []
    for f, actual in zip(common, _parallel_md5(common)):
        rel = str(f.relative_to(game_dir))
        if actual != existing['files'][rel]['md5']:
            changed.append(rel)
    changed.sort()

    def _list(label, items):
        print(f"  [!] {len(items)} file(s) {label}:")
        for rel in items[:20]:
            print(f"        {rel}")
        if len(items) > 20:
            print(f"        ... and {len(items) - 20} more")

    print()
    if not missing and not changed:
        print(f"  OK — all {len(common)} file(s) match the baseline.")
    else:
        if changed:
            _list("CHANGED since the baseline (possible corruption)", changed)
        if missing:
            _list("MISSING since the baseline", missing)
    if added:
        print(f"  [i] {len(added)} file(s) not in the baseline (new — not flagged; "
              f"likely a game update or a config/settings write).")

    if changed or missing:
        print(f"\n  If this is expected (e.g. you updated or modded the game), accept")
        print(f"  the current files as the new baseline with:")
        print(f"    nexus-cli --check-integrity {slug} --rebaseline")
        sys.exit(1)


def _ensure_tracked_files(game_dir: Path) -> None:
    """Create an empty <game_dir>/tracked_files if it doesn't exist.

    Real Proton only creates this bookkeeping file via its own first-run
    copy_pfx() ('w' mode), gated on game_dir/pfx/creation_sync_guard being
    absent. umu-run's bootstrap already drops that guard file (and
    .update-timestamp) in every prefix this tool creates, so real Proton
    permanently skips copy_pfx() here and jumps straight to
    update_builtin_libs(), which unconditionally opens tracked_files for
    reading and crashes with FileNotFoundError before the game starts (seen
    live in Steam's own logs, identically, for both GE-Proton and
    proton-cachyos-slr — this is why Steam-launched shortcuts crash while
    Heroic/launch.sh, which never go through this Proton code path, don't).

    An empty file is exactly what a genuinely-fresh Proton-managed prefix
    looks like right before its own first update_builtin_libs() call, so this
    is always safe. Never touch creation_sync_guard/.update-timestamp/version/
    config_info here — synthesizing those would re-enable copy_pfx() and have
    Proton copy its own prefix skeleton over the umu-managed one. Never
    truncate an existing (non-empty) tracked_files."""
    tf = game_dir / 'tracked_files'
    try:
        if not tf.exists():
            game_dir.mkdir(parents=True, exist_ok=True)
            tf.touch()
    except OSError as e:
        log.print(f"  [!] Steam: could not create {tf} ({e}) — Proton may still crash on launch.")


def _redirect_compatdata(unsigned_appid: int, game_dir: Path) -> None:
    """Make Steam's per-game prefix dir a symlink to our install's game_dir, so
    Steam's forced Proton uses the prefix the game was installed into
    (game_dir/pfx) instead of a fresh empty one."""
    cd = STEAM_ROOT / "steamapps" / "compatdata"
    cd.mkdir(parents=True, exist_ok=True)
    link = cd / str(unsigned_appid)
    try:
        if link.is_symlink():
            if link.resolve() == game_dir.resolve():
                _ensure_tracked_files(game_dir)
                return
            link.unlink()
        elif link.is_dir():
            # A throwaway prefix Steam made earlier. It is usually empty — but
            # if the game was ever launched against it, this is where its saves
            # went, and renaming it silently would look exactly like data loss.
            saves = find_prefix_savedata(link)
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            dest = cd / f"{unsigned_appid}.bak-{ts}"
            link.rename(dest)
            if saves:
                log.print(f"\n  [!] Steam had its own prefix at compatdata/{unsigned_appid}")
                log.print(f"      and it contains {len(saves)} file(s) that look like SAVE DATA.")
                log.print(f"      Moved to: {dest}")
                log.print(f"      Your install prefix ({game_dir}/pfx) is used from now on.")
                log.print( "      If the game has lost progress, copy the saves across:")
                # `saves` was collected before the rename, so rebase onto dest.
                sub = 'pfx/drive_c/users' if (dest / 'pfx').is_dir() else 'drive_c/users'
                log.print(f"        cp -a \"{dest}/{sub}/.\" \\")
                log.print(f"              \"{game_dir}/pfx/drive_c/users/\"")
                log.print()
        elif link.exists():
            link.unlink()
        link.symlink_to(game_dir)
        _ensure_tracked_files(game_dir)
        log.print(f"  Steam: prefix redirected to {game_dir}/pfx (compatdata symlink).")
    except OSError as e:
        log.print(f"  [!] Steam: could not redirect compatdata ({e}).")


def _shortcut_uid(entry: dict) -> int:
    """A shortcuts.vdf entry's appid as unsigned (the file stores int32)."""
    try:
        return int(entry.get('appid', 0)) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def load_shortcuts(path: Path) -> dict | None:
    """Parse a binary shortcuts.vdf. A missing file is an empty library; a file
    that exists but can't be parsed returns None — callers must then leave it
    alone, because writing back an empty dict would delete every other
    non-Steam shortcut the user has."""
    try:
        import vdf
    except ImportError:
        return None
    if not path.exists():
        return {'shortcuts': {}}
    try:
        with open(path, 'rb') as f:
            data = vdf.binary_load(f)
    except VDF_ERRORS as e:
        # stderr: this also runs under --list --json, whose stdout is parsed.
        print(f"  [!] Steam: could not parse {path} ({e}) — leaving it untouched.",
              file=sys.stderr)
        return None
    if not isinstance(data.get('shortcuts'), dict):
        data['shortcuts'] = {}
    return data


def save_shortcuts(path: Path, data: dict) -> bool:
    """Back up then atomically rewrite shortcuts.vdf. Returns success."""
    import vdf
    try:
        payload = vdf.binary_dumps(data)
        backup_file(path)
        atomic_write(path, payload)
    except (OSError, ValueError, TypeError) as e:
        log.print(f"  [!] Steam: failed to write {path.name} ({e}).")
        return False
    return True


def split_game_shortcuts(shortcuts: dict, slug: str) -> tuple[list[dict], list[dict]]:
    """(others, ours): the shortcut(s) belonging to this game, and the rest.
    Matched by the appid recorded in games.json when that entry still exists
    (survives a rename inside Steam, and never catches an unrelated shortcut
    that merely has the same name); otherwise by AppName slug."""
    entries = [e for e in shortcuts.values() if isinstance(e, dict)]
    stored = _stored_appid(slug)
    ours = [e for e in entries if stored and _shortcut_uid(e) == stored]
    if not ours:
        ours = [e for e in entries if make_slug(e.get('AppName', '')) == slug]
    others = [e for e in entries if not any(e is o for o in ours)]
    return others, ours


def add_steam_shortcut_native(display_name: str, slug: str, exe: Path,
                              game_dir: Path, proton: Path,
                              artwork: dict | None,
                              launch_options: str | None = None) -> int | None:
    """Add a non-Steam shortcut that Steam runs the *native* way: the raw game
    .exe through a forced Proton, with the prefix redirected to our installed
    game_dir. This matches how Heroic runs it. Returns the appid or None.

    Re-adding an existing shortcut updates it in place: the user's collections,
    hidden flag, play time, custom icon and launch options are kept.
    `launch_options` (from the game's launch profile) replaces the launch
    options when given; None leaves whatever is there."""
    try:
        import vdf  # noqa: F401
    except ImportError:
        log.print("  [i] Steam: python 'vdf' library not available — skipping.")
        return None
    if not (exe and proton):
        log.print("  [i] Steam: need a game exe and a Proton for native mode — skipping.")
        return None
    cfg_dir = find_steam_userdata(announce=True)
    if not cfg_dir:
        log.print("  [i] Steam: no userdata profile found — skipping.")
        return None
    if not _wait_for_steam_closed():
        log.print("  [i] Steam: skipped (Steam running / user opted out).")
        return None

    exe_str = str(exe)
    # Reuse the appid this game already has, if any. The appid is
    # crc32(exe + display_name), so recomputing it after a rename or an
    # exe-path change produces a *different* id — which orphans the game's
    # compatdata prefix (and every save in it), its grid art and its forced
    # Proton mapping, while looking like nothing happened.
    unsigned = _stored_appid(slug) or shortcut_appid(exe_str, display_name)
    icon = str(artwork['icon']) if artwork and artwork.get('icon') else ''

    shortcuts_path = cfg_dir / 'shortcuts.vdf'
    data = load_shortcuts(shortcuts_path)
    if data is None:
        log.print("  [!] Steam: shortcut NOT added — fix or remove the damaged "
                  "shortcuts.vdf first (Steam recreates it).")
        return None
    others, ours = split_game_shortcuts(data['shortcuts'], slug)
    prev = next((e for e in ours if _shortcut_uid(e) == unsigned), ours[0] if ours else {})
    old_appids = [_shortcut_uid(e) for e in ours]

    entry = {
        'IsHidden': 0, 'AllowDesktopConfig': 1, 'AllowOverlay': 1, 'OpenVR': 0,
        'Devkit': 0, 'DevkitGameID': '', 'DevkitOverrideAppID': 0,
        'LastPlayTime': 0, 'FlatpakAppID': '', 'tags': {},
        'ShortcutPath': '', 'LaunchOptions': '', 'icon': '',
    }
    entry.update(prev)                     # keep everything the user set
    entry.update({                         # ...but these are ours
        'appid': _appid_signed(unsigned),
        'AppName': display_name,
        'Exe': f'"{exe_str}"',
        'StartDir': str(exe.parent),
    })
    if icon and not prev.get('icon'):
        entry['icon'] = icon
    if launch_options is not None:
        entry['LaunchOptions'] = launch_options
    data['shortcuts'] = {str(i): e for i, e in enumerate(others + [entry])}
    if not save_shortcuts(shortcuts_path, data):
        return None

    # Force the chosen Proton, redirect the prefix, and clean up the old entry's
    # forced-tool mapping so it doesn't dangle.
    for au in old_appids:
        if au != unsigned:
            _write_compat_mapping(au, None)
    tool = proton_tool_name(proton)
    if set_steam_compat_tool(unsigned, tool):
        log.print(f"  Steam: forced compat tool {tool}.")
    _redirect_compatdata(unsigned, game_dir)
    if artwork:
        _copy_grid_art(cfg_dir / 'grid', unsigned, artwork)

    verb = 'updated' if ours else 'added'
    log.print(f"  Steam: {verb} '{display_name}' natively (appid {unsigned}).")
    return unsigned


# ---------------------------------------------------------------------------
# KDE / freedesktop desktop entry
# ---------------------------------------------------------------------------

def _desktop_path(slug: str) -> Path:
    return APPLICATIONS_DIR / f"fitgirl-{slug}.desktop"


def write_desktop_entry(display_name: str, slug: str, launch: Path,
                        artwork: dict | None) -> Path | None:
    """Write a .desktop launcher so the game appears in the app menu."""
    icon = 'applications-games'
    if artwork:
        icon_src = artwork.get('icon') or artwork.get('cover')
        if icon_src:
            icon = str(icon_src)

    # Desktop Entry spec: string values escape backslash and line breaks;
    # Exec args are double-quoted with " ` $ \ backslash-escaped inside the
    # quotes, then the whole value gets the string escaping, and a literal %
    # must be written %%.
    def esc(s: str) -> str:
        return (s.replace('\\', '\\\\').replace('\n', '\\n')
                 .replace('\r', '\\r'))

    def exec_arg(a: str) -> str:
        for ch in ('\\', '"', '`', '$'):
            a = a.replace(ch, '\\' + ch)
        return f'"{a}"'

    exec_line = esc(f"bash {exec_arg(str(launch))}").replace('%', '%%')
    path = _desktop_path(slug)
    atomic_write(path,
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={esc(display_name)}\n"
        f"Comment=Installed by Nexus\n"
        f"Exec={exec_line}\n"
        f"Icon={esc(icon)}\n"
        "Categories=Game;\n"
        "Terminal=false\n"
        f"X-FitGirl-Slug={slug}\n"
    )
    path.chmod(0o644)
    if shutil.which('update-desktop-database'):
        try:
            subprocess.run(['update-desktop-database', str(APPLICATIONS_DIR)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    log.print(f"  Desktop entry: {path}")
    return path


# ---------------------------------------------------------------------------
# Heroic Games Launcher (sideloaded app)
# ---------------------------------------------------------------------------

_NANOID_ALPHABET = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "abcdefghijklmnopqrstuvwxyz-_")  # nanoid default (64 chars)


def find_heroic_config() -> Path | None:
    """Heroic's config dir (native install preferred, then Flatpak)."""
    from nexus import steamlib
    return steamlib.find_heroic_config()


def _heroic_app_name(slug: str) -> str:
    """A stable 22-char nanoid-style id derived from the slug, so re-adding the
    same game reuses the same id (and its GamesConfig) instead of duplicating."""
    digest = hashlib.sha256(f"fitgirl:{slug}".encode()).digest()
    return ''.join(_NANOID_ALPHABET[b & 63] for b in digest[:22])


def _heroic_game_config(app_name: str, wine_prefix: Path | None,
                        proton: Path | None) -> dict:
    """A sensible default per-game config: reuse the installed prefix and the
    tool's Proton, let Heroic auto-manage DXVK/VKD3D. Written only when no
    config exists yet, so user tuning is never overwritten."""
    inner = {
        "autoInstallDxvk": True, "autoInstallDxvkNvapi": True, "autoInstallVkd3d": True,
        "preferSystemLibs": False,
        "enableEsync": True, "enableFsync": True, "enableMsync": False,
        "enableWineWayland": False, "enableHDR": False, "enableWoW64": False,
        "nvidiaPrime": False, "enviromentOptions": [], "wrapperOptions": [],
        "showFps": False, "showMangohud": False, "useGameMode": True,
        "battlEyeRuntime": True, "eacRuntime": True, "language": "",
        "beforeLaunchScriptPath": "", "afterLaunchScriptPath": "",
        "verboseLogs": False, "advertiseAvxForRosetta": False,
        "enableQuickSavesMenu": False, "disableUMU": False,
    }
    if wine_prefix:
        inner["winePrefix"] = str(wine_prefix)
    if proton:
        inner["wineVersion"] = {"bin": str(proton), "name": proton.parent.name,
                                "type": "proton"}
    return {app_name: inner, "version": "v0", "explicit": True}


def _heroic_lib_path(cfg_dir: Path) -> Path:
    return cfg_dir / "sideload_apps" / "library.json"


def _load_heroic_library(lib_path: Path) -> dict | None:
    """Heroic's sideload library. Missing file -> empty library; a file that
    exists but can't be parsed -> None, and callers must not write it back
    (that would wipe every other sideloaded game)."""
    if not lib_path.exists():
        return {"games": []}
    try:
        data = json.loads(lib_path.read_text())
        if isinstance(data, dict) and isinstance(data.get('games'), list):
            return data
        if isinstance(data, dict) and 'games' not in data:
            data['games'] = []
            return data
    except (OSError, ValueError):
        pass
    print(f"  [!] Heroic: could not parse {lib_path} — leaving it untouched.",
          file=sys.stderr)
    return None


def heroic_is_running() -> bool:
    """Heroic keeps its sideload library in memory and writes it back, so an
    external edit made while it runs can be silently undone."""
    try:
        for d in Path('/proc').iterdir():
            if not d.name.isdigit():
                continue
            try:
                comm = (d / 'comm').read_text().strip().lower()
            except OSError:
                continue
            if comm.startswith('heroic'):
                return True
    except OSError:
        pass
    return False


def _heroic_wine_version(proton: Path) -> dict:
    return {"bin": str(proton), "name": proton.parent.name, "type": "proton"}


def add_heroic_entry(display_name: str, slug: str, exe: Path | None,
                     wine_prefix: Path | None, proton: Path | None,
                     artwork: dict | None, force_proton: bool = False,
                     launch: dict | None = None) -> str | None:
    """Add (or update) a Heroic sideloaded-game entry pointing at the game exe.
    Idempotent: an existing entry with the same executable is updated in place
    (its app_name and GamesConfig are kept). When `force_proton` is set and a
    `proton` is given, an existing GamesConfig's wineVersion is re-pointed to it.
    When `launch` (the game's launch profile) is given, its env / wrappers /
    args replace the GamesConfig keys a profile owns (heroic_launch_fields);
    every other per-game setting is preserved. Returns the Heroic app_name, or
    None. Best-effort; never raises out."""
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        log.print("  [i] Heroic: not installed (no config dir) — skipping.")
        return None
    if not exe:
        log.print("  [i] Heroic: no game exe to add — skipping.")
        return None
    if heroic_is_running():
        log.print("  [!] Heroic is running — it may overwrite this change when it "
                  "exits. Quit Heroic and re-run if the game doesn't show up.")

    lib_path = _heroic_lib_path(cfg_dir)
    lib = _load_heroic_library(lib_path)
    if lib is None:
        return None
    games = lib.setdefault("games", [])

    exe_str = str(exe)
    existing = next((g for g in games
                     if g.get("install", {}).get("executable") == exe_str
                     or g.get("app_name") == _heroic_app_name(slug)), None)
    app_name = existing.get("app_name") if existing else _heroic_app_name(slug)

    art = ""
    if artwork:
        art = artwork.get('cover_url') or artwork.get('hero_url') or ""
        if not art:  # fall back to a local cover file
            cover = artwork.get('cover') or artwork.get('hero')
            art = str(cover) if cover else ""

    entry = {
        "runner": "sideload",
        "app_name": app_name,
        "title": display_name,
        "install": {"executable": exe_str, "platform": "Windows", "is_dlc": False},
        "folder_name": str(Path(exe_str).parent),
        "art_cover": art,
        "is_installed": True,
        "art_square": art,
        "canRunOffline": True,
        "browserUrl": "",
        "customUserAgent": "",
        "launchFullScreen": False,
    }
    if existing:
        # Don't clobber art the entry already has (e.g. a nicer remote URL).
        if existing.get('art_cover'):
            entry['art_cover'] = existing['art_cover']
            entry['art_square'] = existing.get('art_square') or existing['art_cover']
        changed = any(existing.get(k) != v for k, v in entry.items())
        existing.update(entry)
    else:
        games.append(entry)
        changed = True

    if changed:
        try:
            payload = json.dumps(lib, indent=4)
            backup_file(lib_path)
            atomic_write(lib_path, payload)
        except (OSError, ValueError) as e:
            log.print(f"  [!] Heroic: failed to write library.json ({e}) — skipping.")
            return None

    # Per-game config: create defaults if absent. If it exists, leave the user's
    # tuning alone — except the wineVersion (force_proton) and the keys a
    # launch profile owns.
    gc_path = cfg_dir / "GamesConfig" / f"{app_name}.json"
    try:
        if not gc_path.exists():
            gc = _heroic_game_config(app_name, wine_prefix, proton)
            if launch is not None:
                gc[app_name].update(heroic_launch_fields(launch))
            atomic_write(gc_path, json.dumps(gc, indent=4))
        elif (force_proton and proton) or launch is not None:
            gc = json.loads(gc_path.read_text())
            inner = gc.get(app_name)
            if isinstance(inner, dict):
                before = json.dumps(inner, sort_keys=True)
                if force_proton and proton:
                    inner['wineVersion'] = _heroic_wine_version(proton)
                if launch is not None:
                    inner.update(heroic_launch_fields(launch))
                if json.dumps(inner, sort_keys=True) != before:
                    payload = json.dumps(gc, indent=4)
                    backup_file(gc_path)
                    atomic_write(gc_path, payload)
                    what = []
                    if force_proton and proton:
                        what.append(f"Proton {proton.parent.name}")
                    if launch is not None:
                        what.append("launch profile")
                    log.print(f"  Heroic: applied {' + '.join(what)}.")
    except (OSError, ValueError) as e:
        log.print(f"  [!] Heroic: could not update {gc_path.name} ({e}).")

    verb = "updated" if existing else "added"
    log.print(f"  Heroic: {verb} '{display_name}' (restart Heroic to see it).")
    return app_name


def remove_heroic_entry(slug: str, exe: str | None = None) -> bool:
    """Remove a Heroic sideload entry (matched by our app_name for the slug, or
    by executable) and its GamesConfig. Best-effort."""
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        return False
    lib_path = _heroic_lib_path(cfg_dir)
    if not lib_path.exists():
        return False
    lib = _load_heroic_library(lib_path)
    if lib is None:
        return False
    want_id = _heroic_app_name(slug)
    kept, removed = [], []
    for g in lib.get("games", []):
        if g.get("app_name") == want_id or (exe and
                g.get("install", {}).get("executable") == exe):
            removed.append(g)
        else:
            kept.append(g)
    if not removed:
        return False
    if heroic_is_running():
        log.print("  [!] Heroic is running — quit it, or it may re-add the entry on exit.")
    lib["games"] = kept
    try:
        payload = json.dumps(lib, indent=4)
        backup_file(lib_path)
        atomic_write(lib_path, payload)
    except (OSError, ValueError) as e:
        log.print(f"  [!] Heroic: failed to write library.json ({e}).")
        return False
    for g in removed:
        gc = cfg_dir / "GamesConfig" / f"{g.get('app_name')}.json"
        try:
            backup_file(gc)
            gc.unlink()
        except OSError:
            pass
    log.print(f"  Heroic: removed '{slug}'.")
    return True


# ---------------------------------------------------------------------------
# Management: --list / --uninstall
# ---------------------------------------------------------------------------

def _read_shortcuts(announce: bool = False) -> tuple[object, Path | None]:
    """Return (vdf_module_or_None, shortcuts_path_or_None)."""
    try:
        import vdf
    except ImportError:
        return None, None
    cfg_dir = find_steam_userdata(announce=announce)
    if not cfg_dir:
        return vdf, None
    return vdf, cfg_dir / 'shortcuts.vdf'


def _installed_games(install_base: Path) -> list[tuple[str, Path]]:
    """(slug, game_dir) for every game_dir that has a pfx/ or launch.sh."""
    out = []
    if not install_base.is_dir():
        return out
    for d in sorted(install_base.iterdir()):
        if d.is_dir() and ((d / 'pfx').is_dir() or (d / 'launch.sh').exists()):
            out.append((d.name, d))
    return out


def _steam_appnames() -> set[str]:
    """AppNames currently present in shortcuts.vdf (for steam-status checks)."""
    names: set[str] = set()
    vdf, sc_path = _read_shortcuts()
    if vdf and sc_path and sc_path.exists():
        try:
            with open(sc_path, 'rb') as f:
                sc = vdf.binary_load(f).get('shortcuts', {})
            for e in sc.values():
                if isinstance(e, dict) and e.get('AppName'):
                    names.add(e['AppName'])
        except VDF_ERRORS:
            pass
    return names


def _cover_path(artwork_dir) -> str | None:
    if not artwork_dir:
        return None
    hits = sorted(Path(artwork_dir).glob('cover.*'))
    return str(hits[0]) if hits else None


def _registry_or_scan(install_base: Path) -> list[dict]:
    """Managed games as dicts. Prefer the registry; fall back to a scan."""
    reg = load_registry()
    if reg['games']:
        return list(reg['games'].values())
    games = []
    for slug, game_dir in _installed_games(install_base):
        launch = game_dir / 'launch.sh'
        name = _launcher_display_name(launch, slug)
        games.append({
            'slug': slug, 'display_name': name, 'exe': None,
            'compatdata': None, 'wine_prefix': str(game_dir),
            'launcher': str(launch), 'source': 'scan',
            'install_base': str(install_base), 'steam_appid': None,
            'has_desktop': _desktop_path(slug).exists(),
            'artwork_dir': str(ARTWORK_DIR / slug), 'added_at': None,
            'disk_bytes': None, 'disk_bytes_at': None, 'last_played': None,
        })
    return games


def _heroic_execs() -> set[str]:
    """Executable paths currently present in Heroic's sideload library."""
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        return set()
    lib = _load_heroic_library(_heroic_lib_path(cfg_dir)) or {}
    return {g.get('install', {}).get('executable')
            for g in lib.get('games', []) if isinstance(g, dict)}


def heroic_play_stats() -> dict[str, dict]:
    """Heroic's own play tracking, {app_name: {'minutes', 'last'}} with `last`
    as unix seconds, from store/timestamp.json. Read-only; {} on any failure."""
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        return {}
    try:
        data = json.loads((cfg_dir / 'store' / 'timestamp.json').read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for app, v in data.items() if isinstance(data, dict) else ():
        if not isinstance(v, dict):
            continue
        last = None
        try:
            last = datetime.fromisoformat(str(v.get('lastPlayed')).replace('Z', '+00:00')
                                          ).timestamp()
        except (TypeError, ValueError):
            pass
        try:
            minutes = int(v.get('totalPlayed') or 0)
        except (TypeError, ValueError):
            minutes = 0
        out[app] = {'minutes': minutes, 'last': last}
    return out


def steam_shortcut_last_played() -> dict[int, int]:
    """{appid: unix seconds} from the chosen account's shortcuts.vdf
    LastPlayTime (Steam doesn't record minutes played for non-Steam games)."""
    vdf, sc_path = _read_shortcuts()
    if not (vdf and sc_path and sc_path.exists()):
        return {}
    try:
        with open(sc_path, 'rb') as f:
            shortcuts = vdf.binary_load(f).get('shortcuts', {})
    except VDF_ERRORS:
        return {}
    out = {}
    for e in shortcuts.values():
        if isinstance(e, dict):
            try:
                t = int(e.get('LastPlayTime') or 0)
            except (TypeError, ValueError):
                t = 0
            if t > 0:
                out[_shortcut_uid(e)] = t
    return out


def _iso_to_ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def managed_entries(install_base: Path, compute_sizes: bool = False,
                    verbose: bool = False) -> list[dict]:
    """The games this tool installed/registered, enriched with launcher
    status, play stats and size (what --list and --library report)."""
    games = _registry_or_scan(install_base)
    steam_names = _steam_appnames()
    heroic_execs = _heroic_execs()
    heroic_stats = heroic_play_stats()
    steam_last = steam_shortcut_last_played()
    heroic_by_exe = {}
    cfg_dir = find_heroic_config()
    if cfg_dir:
        for hg in (_load_heroic_library(_heroic_lib_path(cfg_dir)) or {}).get('games', []):
            if isinstance(hg, dict) and hg.get('app_name'):
                heroic_by_exe[hg.get('install', {}).get('executable')] = hg['app_name']

    def owned_dir(g):
        d = _owned_game_dir(g, install_base, g['slug'])
        return d if d and d.is_dir() else None

    if compute_sizes and verbose:
        owned = [g for g in games if g.get('source') != 'scan' and owned_dir(g)]
        print(f"Computing on-disk size for {len(owned)} game(s) (stats only, no "
              f"file contents read)...")

    enriched = []
    for g in games:
        slug = g['slug']
        desktop = _desktop_path(slug).exists()
        steam = (g.get('display_name') in steam_names
                 or any(slug == make_slug(n) for n in steam_names))
        exe = g.get('exe')
        if not exe and g.get('wine_prefix') and g.get('source') != 'scan':
            found = _entry_paths(g)[0]
            exe = str(found) if found else None
        heroic = bool(exe and exe in heroic_execs)
        wp = g.get('wine_prefix')
        # Play time: what this tool timed (--launch) plus what Heroic timed;
        # last played: the latest of this tool, Steam and Heroic.
        happ = g.get('heroic_app_name') or heroic_by_exe.get(exe)
        hst = heroic_stats.get(happ, {}) if happ else {}
        minutes = int((g.get('playtime_seconds') or 0) // 60) + int(hst.get('minutes') or 0)
        lasts = [t for t in (_iso_to_ts(g.get('last_played')), hst.get('last'),
                             steam_last.get(int(g.get('steam_appid') or 0))) if t]
        last_played = (datetime.fromtimestamp(max(lasts)).isoformat(timespec='seconds')
                       if lasts else None)
        disk_bytes, disk_bytes_at = g.get('disk_bytes'), g.get('disk_bytes_at')
        if compute_sizes and g.get('source') != 'scan':
            od = owned_dir(g)
            if od:
                disk_bytes = dir_size_bytes(od)
                disk_bytes_at = datetime.now().isoformat(timespec='seconds')
                update_registry_size(slug, disk_bytes)
                if verbose:
                    print(f"  {slug}: {disk_bytes / 1e9:.1f} GB")
        prof = g.get('launch') or {}
        enriched.append({
            **g,
            'launch_options': format_launch_options(prof) if profile_owns_options(prof) else None,
            'steam': steam,
            'desktop': desktop,
            'heroic': heroic,
            'cover': _cover_path(g.get('artwork_dir')),
            'prefix_exists': bool(wp and Path(wp).is_dir()),
            'disk_bytes': disk_bytes,
            'disk_bytes_at': disk_bytes_at,
            'last_played': last_played,
            'playtime_minutes': minutes,
            'exe': exe,
            'origin': g.get('source'),
            'kind': 'nexus',
        })
    return enriched


def _disk_info(path: Path) -> dict | None:
    try:
        du = shutil.disk_usage(existing_ancestor(path))
    except OSError:
        return None
    return {'path': str(path), 'total': du.total, 'used': du.used, 'free': du.free}


def cmd_list(install_base: Path, as_json: bool = False, compute_sizes: bool = False) -> None:
    enriched = managed_entries(install_base, compute_sizes, verbose=not as_json)
    if as_json:
        print(json.dumps({'install_base': str(install_base),
                          'disk': _disk_info(install_base), 'games': enriched}))
        return

    if not enriched:
        print(f"No managed games found (install_base {install_base})")
        return
    print(f"\nManaged games ({install_base}):\n")
    for g in enriched:
        print(f"  {g['slug']}  —  {g['display_name']}")
        print(f"    launcher: {g.get('launcher')}")
        print(f"    steam:   {'✓' if g['steam'] else '✗'}    "
              f"desktop: {'✓' if g['desktop'] else '✗'}    "
              f"heroic: {'✓' if g['heroic'] else '✗'}")
        if g.get('disk_bytes') is not None:
            print(f"    disk:    {g['disk_bytes'] / 1e9:.1f} GB "
                  f"(as of {g.get('disk_bytes_at', 'unknown')})")
        print(f"    last played: {g.get('last_played') or 'never'}"
              + (f"    played: {g['playtime_minutes'] / 60:.1f} h"
                 if g.get('playtime_minutes') else ''))
    print()


def _parse_size(text) -> int | None:
    """'1003.83 MiB' / '12.4 GB' -> bytes (Heroic reports sizes as text)."""
    m = re.match(r'\s*([\d.]+)\s*([KMGT]i?B)', str(text or ''), re.IGNORECASE)
    if not m:
        return None
    unit = m.group(2).upper()
    mult = {'K': 1, 'M': 2, 'G': 3, 'T': 4}[unit[0]]
    base = 1024 if 'I' in unit else 1000
    return int(float(m.group(1)) * base ** mult)


def owned_games(steam: list[dict], heroic_cfg) -> list[dict]:
    """Games the user owns but hasn't installed: Steam's from Game-db's
    cached library (Steam keeps no readable ownership list), Epic/GOG/Amazon
    from Heroic's store caches. Never touches the network."""
    from nexus import steamlib, gamedb
    have = {g['appid'] for g in steam}
    out = [steamlib.steam_owned_entry(STEAM_ROOT, item) for item in gamedb.owned_library()
           if item['appid'] not in have and not steamlib.is_tool(item['appid'],
                                                                 item.get('title') or '')]
    out += [h for h in steamlib.heroic_store_games(heroic_cfg, owned=True) if not h['installed']]
    return out


def _add_achievement_counts(games: list[dict]) -> None:
    """`achievements: [unlocked, total]` on Steam games that have any, from
    Steam's stats cache (a disk cache keeps this to a stat per file)."""
    try:
        from nexus import emuach, compat
        for g in games:                            # repacks: cached schema only, no network
            if g.get('source') == 'nexus' and g.get('wine_prefix'):
                r = emuach.status(Path(g['wine_prefix']), STEAM_ROOT, cache_only=True,
                                  appid=compat.store_appid(g))
                if r and r['total']:
                    g['achievements'] = [r['unlocked'], r['total']]
    except Exception as e:                       # noqa: BLE001
        print(f"  [i] repack achievements skipped: {e}", file=sys.stderr)
    try:
        from nexus import achievements as A
        account = steam_account_id()
        steam = {g['appid']: g for g in games if g.get('source') == 'steam' and g.get('appid')}
        if account is None or not steam:
            return
        for appid, c in A.counts(STEAM_ROOT, account, steam, ACHIEVEMENT_COUNTS).items():
            steam[appid]['achievements'] = c
    except Exception as e:                       # never worth failing the library over
        print(f"  [i] achievements skipped: {e}", file=sys.stderr)


def cmd_library(install_base: Path, as_json: bool = True, owned: bool = False) -> None:
    """Everything playable in one list: games installed by Nexus
    (kind 'nexus'), installed Steam games ('steam') and installed Heroic store
    games ('heroic'), plus disk usage for every drive they live on. With
    `owned`, also games owned but not installed (installed=False) and ones
    Steam/Heroic are downloading (installing={done, total})."""
    from nexus import steamlib
    from nexus import repackfeed
    games = managed_entries(install_base)
    online = repackfeed.updates(games)            # the stored feed only, no network
    for g in games:
        g['source'] = 'nexus'
        g['installed'] = True
        g['repack_online'] = online.get(g['slug'])
    games += incomplete_installs(install_base)
    heroic_cfg = find_heroic_config()
    steam = steamlib.scan(STEAM_ROOT, _read_compat_mapping(), installing=owned)
    heroic = steamlib.heroic_store_games(heroic_cfg)
    extra = owned_games(steam, heroic_cfg) if owned else []
    for h in heroic + [e for e in extra if e['source'] == 'heroic']:
        size = _parse_size(h.pop('install_size', None))
        h['disk_bytes'] = size if h['installed'] else None
        if not h['installed']:
            h['download_bytes'] = size
    games += steam + heroic + extra
    _add_achievement_counts(games)
    try:
        sun_apps = json.loads(SUNSHINE_APPS.read_text()).get('apps') or []
    except (OSError, ValueError, AttributeError):
        sun_apps = []
    for g in games:
        overlay_art(g)
        if g.get('source') == 'nexus' or g.get('launcher'):
            g['sunshine'] = bool(sun_apps) and _sunshine_find(sun_apps, g) is not None
    # The id protongen knows each game by (its `--game` argument and its
    # game_memory key): Steam appid, else the FNV hash of the Heroic app name.
    for g in games:
        if g['source'] == 'steam':
            g['protongen_id'] = g['appid']
        elif g['source'] == 'heroic':
            g['protongen_id'] = protongen_heroic_id(g['app_name'])
        elif g.get('steam_appid'):
            g['protongen_id'] = int(g['steam_appid']) & 0xFFFFFFFF
        elif g.get('heroic_app_name'):
            g['protongen_id'] = protongen_heroic_id(g['heroic_app_name'])

    disks, seen = [], set()
    for p in [install_base, *(Path(g['library']) for g in steam if g.get('library')),
              *(Path(g['install_dir']).parent for g in heroic if g.get('install_dir'))]:
        try:
            dev = os.stat(existing_ancestor(p)).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        info = _disk_info(p)
        if info:
            info['dev'] = dev
            disks.append(info)

    def dev_of(g):
        path = g.get('install_dir') or g.get('compatdata') or g.get('wine_prefix')
        try:
            return os.stat(existing_ancestor(Path(path))).st_dev if path else None
        except OSError:
            return None
    for g in games:
        g['dev'] = dev_of(g)

    if as_json:
        print(json.dumps({'install_base': str(install_base), 'disks': disks,
                          'disk': disks[0] if disks else None, 'games': games}))
        return
    for kind in ('nexus', 'steam', 'heroic'):
        rows = [g for g in games if g['source'] == kind]
        print(f"\n{kind.capitalize()} ({len(rows)}):")
        for g in rows:
            size = f"{g['disk_bytes'] / 1e9:.1f} GB" if g.get('disk_bytes') else '?'
            print(f"  {g['display_name']:<48} {size:>9}   last played "
                  f"{g.get('last_played') or 'never'}")


def cmd_running(install_base: Path, as_json: bool) -> None:
    """Slugs of the library games running right now (see steamlib.running)."""
    from nexus import steamlib
    r = steamlib.running()
    playing = []
    for g in managed_entries(install_base):
        exe = (g.get('exe') or '').replace('\\', '/').rsplit('/', 1)[-1].lower()
        if (g['slug'] in r['slugs'] or int(g.get('steam_appid') or 0) in r['appids']
                or (exe and exe in r['exes'])):
            playing.append(g['slug'])
    playing += [f"steam-{a}" for a in r['appids'] if a < 0x80000000]
    for h in steamlib.heroic_store_games(find_heroic_config()):
        d = h.get('install_dir')
        if d and steamlib.in_dir_cmdline(d, r['paths']):
            playing.append(h['slug'])
    if as_json:
        print(json.dumps({'playing': sorted(set(playing))}))
    else:
        print('\n'.join(sorted(set(playing))) or 'Nothing running.')


# ---------------------------------------------------------------------------
# Pausing a running repack, and benchmark runs
# ---------------------------------------------------------------------------

def _proc_state(pid: int) -> str | None:
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        return stat.rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def running_repacks() -> dict[str, list[int]]:
    """{slug: pids} for Nexus games with processes in their prefix now."""
    out = {}
    for slug, e in load_registry()['games'].items():
        pids = prefix_processes(_prefix_markers(e))
        if pids:
            out[slug] = pids
    return out


def cmd_pause(slug: str | None, mode: str) -> None:
    """Freeze (SIGSTOP) or thaw (SIGCONT) everything running in a repack's
    prefix; mode 'toggle' picks by the processes' state. Without a slug: the
    one repack that's running."""
    running = running_repacks()
    if slug is None:
        if len(running) != 1:
            print("No repack is running." if not running else
                  f"Several are running ({', '.join(sorted(running))}) — name one.",
                  file=sys.stderr)
            sys.exit(1)
        slug = next(iter(running))
    pids = running.get(slug) or prefix_processes(_prefix_markers(_require_entry(slug)))
    if not pids:
        print(f"'{slug}' isn't running.", file=sys.stderr)
        sys.exit(1)
    if mode == 'toggle':
        stopped = sum(_proc_state(p) == 'T' for p in pids)
        mode = 'resume' if stopped * 2 >= len(pids) else 'pause'
    sig = signal.SIGSTOP if mode == 'pause' else signal.SIGCONT
    n = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            n += 1
        except OSError:
            pass
    print(f"{'Paused' if mode == 'pause' else 'Resumed'} '{slug}' ({n} processes).")


BENCH_DIR = NX.DATA_DIR / 'benchmarks'


def cmd_benchmark(install_base: Path, slug: str, seconds: int, delay: int) -> None:
    """Launch a repack with MangoHud logging `seconds` long, starting `delay`
    seconds in (Shift+F2 also starts/stops it by hand). Results:
    --bench-results."""
    entry = _require_entry(slug)
    sh = Path(entry.get('launcher') or '')
    try:
        hooked = BENCH_HOOK in sh.read_text()
    except OSError:
        hooked = False
    if not hooked:
        print(f"The launcher for '{slug}' predates benchmarks. Apply its launch settings once "
              f"(Game settings → Apply, or nexus-cli --apply-launch {slug}), then retry.",
              file=sys.stderr)
        sys.exit(1)
    if not shutil.which('mangohud'):
        print("Benchmarks need MangoHud.", file=sys.stderr)
        sys.exit(1)
    out = BENCH_DIR / slug
    out.mkdir(parents=True, exist_ok=True)
    extra = [f"output_folder={out}", f"autostart_log={max(0, delay)}",
             f"log_duration={max(5, seconds)}", "log_interval=0"]
    if not perf_state(entry.get('launch'))['mangohud']:
        extra.append('no_display')          # log without showing an overlay you didn't ask for
    os.environ['NEXUS_MANGOHUD_EXTRA'] = ','.join(extra)
    print(f"Benchmark: logging {seconds} s, starting {delay} s after launch "
          f"(Shift+F2 toggles it by hand). Results: nexus-cli --bench-results {slug}")
    cmd_launch(install_base, slug)


def parse_mangohud_csv(text: str) -> dict | None:
    """avg / 1% low / 0.1% low fps, samples and seconds from a MangoHud log."""
    lines = text.splitlines()
    try:
        i = next(n for n, l in enumerate(lines) if l.startswith('fps,frametime'))
    except StopIteration:
        return None
    cols = lines[i].split(',')
    ft_col = cols.index('frametime')
    el_col = cols.index('elapsed') if 'elapsed' in cols else None
    frametimes, elapsed = [], []
    for l in lines[i + 1:]:
        parts = l.split(',')
        try:
            ft = float(parts[ft_col])
        except (IndexError, ValueError):
            continue
        if ft > 0:
            frametimes.append(ft)
            if el_col is not None:
                try:
                    elapsed.append(float(parts[el_col]))
                except (IndexError, ValueError):
                    pass
    if len(frametimes) < 10:
        return None
    srt = sorted(frametimes)

    def low(pct):                    # fps at the pct-slowest frame time
        k = min(len(srt) - 1, int(len(srt) * (1 - pct / 100)))
        return round(1000 / srt[k], 1)
    span = (elapsed[-1] - elapsed[0]) / 1e9 if len(elapsed) > 1 else sum(frametimes) / 1000
    return {'avg_fps': round(1000 * len(frametimes) / sum(frametimes), 1),
            'low_1': low(1), 'low_01': low(0.1), 'frames': len(frametimes),
            'seconds': round(span if span > 0 else sum(frametimes) / 1000, 1)}


def cmd_bench_results(slug: str, as_json: bool) -> None:
    rows = []
    d = BENCH_DIR / slug
    for f in sorted(d.glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True) \
            if d.is_dir() else []:
        if f.name.endswith('_summary.csv'):
            continue
        try:
            r = parse_mangohud_csv(f.read_text(errors='replace'))
        except OSError:
            r = None
        if r:
            rows.append({'file': str(f), 'at': datetime.fromtimestamp(f.stat().st_mtime)
                         .isoformat(timespec='seconds'), **r})
    if as_json:
        print(json.dumps({'slug': slug, 'runs': rows[:20]}))
        return
    if not rows:
        print(f"No benchmark runs for '{slug}' yet (nexus-cli --benchmark {slug}).")
    for r in rows[:20]:
        print(f"  {r['at']}  avg {r['avg_fps']} fps · 1% low {r['low_1']} · 0.1% low "
              f"{r['low_01']} · {r['seconds']} s")


# The steam:// and heroic:// URLs Nexus itself builds: a known verb and an id,
# nothing a remote value could turn into another Steam command.
_OPEN_URL_RE = re.compile(
    r'^steam://(?:(?:rungameid|install|uninstall|validate|gameproperties|store'
    r'|nav/games/details)/\d+|open/keyboard)$'
    r'|^heroic://launch/\w[\w.-]*/\w[\w.-]*$')


def cmd_open_url(url: str) -> None:
    """Launch a Steam or Heroic game (or open a Steam page) by URL. Only the
    steam:// and heroic:// forms in _OPEN_URL_RE are accepted."""
    from nexus import steamlib
    if not _OPEN_URL_RE.match(url):
        print(f"Refusing to open {url!r}: only steam:// and heroic:// URLs", file=sys.stderr)
        sys.exit(2)
    if not steamlib.open_url(url):
        print("Neither `steam` nor `xdg-open` is available", file=sys.stderr)
        sys.exit(1)
    print(f"Opened {url}")


# ---------------------------------------------------------------------------
# Sunshine (game streaming): one apps.json entry per game
# ---------------------------------------------------------------------------

SUNSHINE_APPS = Path.home() / '.config/sunshine/apps.json'
SUNSHINE_ART = NX.DATA_DIR / 'sunshine'


def _cli_command() -> str:
    """How Sunshine should call us: the nexus-cli on PATH, else this repo's."""
    return shutil.which('nexus-cli') or str(CLI_BIN)


def _sunshine_marker(g: dict) -> tuple[str, str]:
    """The option and value that identify our entry for a game inside its
    command line."""
    if g.get('source', 'nexus') == 'nexus':
        return '--launch', g['slug']
    return '--open-url', g['launcher']


def _has_option(cmd, opt: str, value: str) -> bool:
    """`opt value` as two whole words of a shell command line ('--launch foo'
    must not match '--launch foo-bar')."""
    try:
        toks = shlex.split(cmd) if isinstance(cmd, str) else []
    except ValueError:
        return False
    return any(a == opt and b == value for a, b in zip(toks, toks[1:]))


def _sunshine_png(g: dict) -> str | None:
    """Sunshine's box art must be a PNG: convert the game's cover once (Qt's
    image reader, else ImageMagick). None when there is no local cover."""
    src = g.get('cover') or _cover_path(g.get('artwork_dir'))
    if not src or not Path(src).is_file():
        return None
    dest = SUNSHINE_ART / f"{make_slug(g['slug'])}.png"
    try:
        if dest.exists() and dest.stat().st_mtime >= Path(src).stat().st_mtime:
            return str(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if src.lower().endswith('.png'):
        try:
            shutil.copyfile(src, dest)
            return str(dest)
        except OSError:
            return None
    try:
        from PyQt6.QtGui import QImage
        img = QImage(src)
        if not img.isNull() and img.save(str(dest), 'PNG'):
            return str(dest)
    except ImportError:
        pass
    if shutil.which('magick'):
        r = subprocess.run(['magick', src, str(dest)], capture_output=True, timeout=30)
        if r.returncode == 0 and dest.exists():
            return str(dest)
    return None


def sunshine_entry(g: dict) -> dict:
    """The apps.json entry for a library game. A repack runs in the
    foreground (`--launch … --wait`), so the stream ends when the game does;
    Steam and Heroic games are handed to their launcher (detached)."""
    entry = {'name': g.get('display_name') or g.get('name') or g['slug'],
             'auto-detach': True, 'wait-all': True, 'exit-timeout': 5,
             'exclude-global-prep-cmd': False}
    cli = shlex.quote(_cli_command())
    if g.get('source', 'nexus') == 'nexus':
        entry['cmd'] = f"{cli} --launch {shlex.quote(g['slug'])} --wait"
    else:
        entry['detached'] = [f"{cli} --open-url {shlex.quote(g['launcher'])}"]
    png = _sunshine_png(g)
    if png:
        entry['image-path'] = png
    return entry


def _sunshine_find(apps: list, g: dict) -> int | None:
    opt, value = _sunshine_marker(g)
    for i, a in enumerate(apps):
        cmds = [a.get('cmd') or ''] + list(a.get('detached') or [])
        if any(_has_option(c, opt, value) for c in cmds):
            return i
    return None


def sunshine_has(g: dict) -> bool:
    try:
        data = json.loads(SUNSHINE_APPS.read_text())
        return _sunshine_find(data.get('apps') or [], g) is not None
    except (OSError, ValueError, AttributeError):
        return False


def cmd_sunshine(ident: str, remove: bool = False) -> None:
    """Add (or update) / remove a game's Sunshine entry. apps.json is backed
    up first and never rewritten when it can't be parsed."""
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if g.get('source', 'nexus') != 'nexus' and not re.match(
            r'^(steam|heroic)://[\w/.?=&-]+$', g.get('launcher') or ''):
        print(f"{ident!r} has no launch URL Sunshine could use", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(SUNSHINE_APPS.read_text())
        apps = data['apps']
        if not isinstance(apps, list):
            raise ValueError("apps is not a list")
    except FileNotFoundError:
        if remove:
            print("Sunshine has no apps.json — nothing to remove.")
            return
        if not SUNSHINE_APPS.parent.is_dir():
            print("Sunshine isn't set up (no ~/.config/sunshine).", file=sys.stderr)
            sys.exit(1)
        data, apps = {'env': {}, 'apps': []}, None
        data['apps'] = apps = []
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Not touching {SUNSHINE_APPS}: it can't be read ({e}).", file=sys.stderr)
        sys.exit(1)
    i = _sunshine_find(apps, g)
    name = g.get('display_name') or g.get('name') or g['slug']
    if remove:
        if i is None:
            print(f"{name} isn't in Sunshine.")
            return
        apps.pop(i)
        verb = "Removed"
    else:
        new = sunshine_entry(g)
        if i is None:
            apps.append(new)
        else:
            # Keep anything the user added in Sunshine's UI (prep commands,
            # its uuid…); refresh only what Nexus owns.
            apps[i] = {**apps[i], **new}
        verb = "Updated" if i is not None else "Added"
    backup_file(SUNSHINE_APPS)
    atomic_write(SUNSHINE_APPS, json.dumps(data, indent=4) + '\n')
    prep = {'Added': 'to', 'Updated': 'in', 'Removed': 'from'}[verb]
    print(f"{verb} {name} {prep} Sunshine. Restart Sunshine "
          f"(or save any app in its web UI) for Moonlight to see the change.")


def game_has_steam_shortcut(slug: str) -> bool:
    """Read-only: does the chosen Steam profile hold a shortcut for this game?"""
    vdf, sc_path = _read_shortcuts()
    if not (vdf and sc_path and sc_path.exists()):
        return False
    data = load_shortcuts(sc_path)
    return bool(data and split_game_shortcuts(data['shortcuts'], slug)[1])


def remove_steam_shortcut(slug: str, require_steam_closed: bool = True) -> bool:
    """Remove this game's Steam shortcut(s) (see split_game_shortcuts), their
    grid art, forced compat tool and compatdata symlink. Returns True if the
    file was rewritten. Best-effort."""
    vdf, sc_path = _read_shortcuts(announce=True)
    if not (vdf and sc_path and sc_path.exists()):
        return False
    if require_steam_closed and not _wait_for_steam_closed():
        print("  [i] Skipped Steam shortcut removal (Steam running / opted out).")
        return False
    data = load_shortcuts(sc_path)
    if data is None:
        return False
    others, ours = split_game_shortcuts(data['shortcuts'], slug)
    if not ours:
        return False
    data['shortcuts'] = {str(i): e for i, e in enumerate(others)}
    if not save_shortcuts(sc_path, data):
        return False
    grid = sc_path.parent / 'grid'
    for au in (_shortcut_uid(e) for e in ours):
        for p in grid.glob(f"{au}*"):
            try:
                p.unlink()
            except OSError:
                pass
        # Native-mode cleanup: drop the forced compat tool and the
        # compatdata symlink we created for this appid.
        _write_compat_mapping(au, None)
        link = STEAM_ROOT / "steamapps" / "compatdata" / str(au)
        if link.is_symlink():
            try:
                link.unlink()
            except OSError:
                pass
    set_registry_steam_appid(slug, None)
    print(f"  Removed Steam shortcut(s) for '{slug}'.")
    return True


def _rmtree_collect(path: Path) -> list[str]:
    """rmtree that keeps going past errors and returns the paths it couldn't
    remove (rmtree(ignore_errors=True) hides a partial delete)."""
    failed: list[str] = []
    def on_err(_func, p, _exc):
        failed.append(str(p))
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_err)
    else:
        shutil.rmtree(path, onerror=on_err)
    return failed


def _owned_game_dir(entry: dict, install_base: Path, slug: str) -> Path | None:
    """The install directory this tool created for a game, i.e. the one it may
    delete: <install_base recorded at install time>/<slug>. An --add-steam game
    lives wherever the user put it, so nothing is owned."""
    if entry.get('source') == 'add-steam' or not valid_slug(slug):
        return None
    base = Path(entry.get('install_base') or install_base)
    d = base / slug
    # Never anything but a direct child of the base: a symlinked game folder
    # pointing elsewhere is left alone.
    try:
        if d.is_symlink() or d.resolve().parent != base.resolve():
            return None
    except OSError:
        return None
    return d


def _owned_artwork_dir(entry: dict, slug: str) -> Path | None:
    """The artwork folder uninstall may delete: only one directly inside
    ARTWORK_DIR, whatever the registry entry says."""
    d = Path(entry.get('artwork_dir') or (ARTWORK_DIR / slug))
    try:
        if d.is_symlink() or d.resolve().parent != ARTWORK_DIR.resolve():
            return None
    except OSError:
        return None
    return d


def cmd_uninstall(install_base: Path, slug: str, yes: bool = False,
                  save_backup: bool = False) -> None:
    entry = registry_get(slug) or {}
    owned_dir = _owned_game_dir(entry, install_base, slug)
    if owned_dir is not None and not owned_dir.exists():
        owned_dir = None
    desktop = _desktop_path(slug)
    artwork_dir = _owned_artwork_dir(entry, slug)
    launcher = Path(entry['launcher']) if entry.get('launcher') else None

    if not entry and not owned_dir and not desktop.exists():
        print(f"Nothing found for slug '{slug}'")
        sys.exit(1)

    # Steam must be closed to remove the shortcut. Check this BEFORE deleting
    # anything: deleting the prefix and registry entry while leaving the
    # shortcut, its compatdata symlink and forced-tool mapping behind strands
    # all three with nothing left that knows about them.
    has_shortcut = game_has_steam_shortcut(slug)
    if has_shortcut and steam_is_running():
        if yes:
            print("Steam is running, and this game has a Steam shortcut that can only be "
                  "removed while Steam is closed. Quit Steam and try again.",
                  file=sys.stderr)
            sys.exit(2)
        if not _wait_for_steam_closed():
            print("Aborted — nothing was removed (Steam is still running).")
            sys.exit(2)

    save_hits = find_prefix_savedata(owned_dir) if owned_dir else []

    print(f"About to remove '{slug}':")
    if owned_dir:
        print(f"  - install: {owned_dir}")
    if launcher and launcher.exists() and launcher.parent == LAUNCHERS_DIR:
        print(f"  - launcher: {launcher}")
    if desktop.exists():
        print(f"  - desktop: {desktop}")
    if artwork_dir and artwork_dir.exists():
        print(f"  - artwork: {artwork_dir}")
    print("  - matching Steam shortcut + Heroic entry (if any)")
    print("  (install logs are kept)")

    do_backup = save_backup
    if save_hits:
        if save_backup:
            print(f"  Found what looks like save data ({len(save_hits)}+ file(s)) — "
                  f"it will be backed up to ~/.local/share/nexus/saves/{slug}/ "
                  f"and offered back if you reinstall this game.")
        elif yes:
            print(f"  Found what looks like save data ({len(save_hits)}+ file(s)) — "
                  f"NOT backing it up (pass --save-backup to keep it). It will be deleted "
                  f"with the rest of the prefix.")
        else:
            print(f"  Found what looks like save data ({len(save_hits)}+ file(s)).")
            try:
                ans = input("  Back it up before removing? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
            do_backup = ans in ('y', 'yes')

    if not yes:
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans not in ('y', 'yes'):
            print("Aborted.")
            return

    if save_hits and do_backup:
        result = backup_prefix_saves(slug, owned_dir)
        if result and result[2]:
            _report_failed_copies(result[2], "save file(s)")
            print(f"  Aborting before deleting anything; the partial backup is in "
                  f"{result[0]}.", file=sys.stderr)
            sys.exit(1)
        if result:
            backup_dir, count, _ = result
            print(f"  Backed up {count} save file(s) to {backup_dir}")
        elif save_backup:
            print("  [!] Save backup found nothing to copy — aborting before deleting "
                  "anything.", file=sys.stderr)
            sys.exit(1)

    if has_shortcut and not remove_steam_shortcut(slug, require_steam_closed=False):
        print("  [!] Could not remove the Steam shortcut — aborting before deleting "
              "anything else.", file=sys.stderr)
        sys.exit(1)
    remove_heroic_entry(slug, exe=entry.get('exe'))

    if desktop.exists():
        try:
            desktop.unlink()
        except OSError:
            pass
    # Only delete an external launcher (in LAUNCHERS_DIR); a game_dir/launch.sh
    # is removed with the prefix below.
    if launcher and launcher.exists() and launcher.parent == LAUNCHERS_DIR:
        try:
            launcher.unlink()
        except OSError:
            pass
    if artwork_dir and artwork_dir.exists():
        shutil.rmtree(artwork_dir, ignore_errors=True)
    failed = _rmtree_collect(owned_dir) if owned_dir else []
    if shutil.which('update-desktop-database'):
        try:
            subprocess.run(['update-desktop-database', str(APPLICATIONS_DIR)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if failed:
        # Keep the registry entry so the game stays visible and can be retried.
        print(f"  [!] Could not delete {len(failed)} path(s) under {owned_dir}, e.g. "
              f"{failed[0]}", file=sys.stderr)
        print(f"Partially removed '{slug}' — fix the permissions and run --uninstall "
              f"again.", file=sys.stderr)
        sys.exit(1)
    unregister_game(slug)
    print(f"Removed '{slug}'.")


def launch_log_path(slug: str) -> Path:
    return LOG_DIR / f"{slug}-last-launch.log"


def _prefix_markers(entry: dict) -> set[str]:
    """The real paths a game's Wine processes carry as WINEPREFIX (umu, wine)
    or STEAM_COMPAT_DATA_PATH (proton)."""
    out = set()
    for k in ('wine_prefix', 'compatdata'):
        if entry.get(k):
            out.add(os.path.realpath(entry[k]).rstrip('/'))
    return out


def prefix_processes(markers: set[str]) -> list[int]:
    """Processes running in one of these prefixes, found by the environment
    every Wine process inherits — whatever the game's exe ends up being
    called (a launcher that starts the real game and exits included)."""
    pids, me = [], os.getpid()
    if not markers:
        return pids
    for p in Path('/proc').iterdir():
        if not p.name.isdigit() or int(p.name) == me:
            continue
        try:
            env = (p / 'environ').read_bytes()
        except OSError:
            continue
        for var in env.split(b'\0'):
            k, _, v = var.partition(b'=')
            if k in (b'WINEPREFIX', b'STEAM_COMPAT_DATA_PATH') and \
                    os.path.realpath(v.decode('utf-8', 'replace')).rstrip('/') in markers:
                pids.append(int(p.name))
                break
    return pids


def steam_shortcut_url(entry: dict) -> str | None:
    """steam://rungameid/ for a game's non-Steam shortcut (its 64-bit game
    id: the shortcut appid in the high half, 0x02000000 marking a shortcut)."""
    try:
        appid = int(entry.get('steam_appid') or 0)
    except (TypeError, ValueError):
        return None
    if appid < 0x80000000:
        return None
    return f"steam://rungameid/{(appid << 32) | 0x02000000}"


def cmd_launch_via(slug: str, via: str) -> None:
    """Whether Play starts a repack itself (launch.sh, Nexus's tracker) or
    through its Steam shortcut (Steam Input, overlay, recording, presence)."""
    entry = _require_entry(slug)
    if via == 'steam' and not steam_shortcut_url(entry):
        print(f"'{slug}' isn't in Steam yet — add it first (nexus-cli --steam {slug}).",
              file=sys.stderr)
        sys.exit(1)
    update_registry_entry(slug, launch_via='steam' if via == 'steam' else None)
    print(f"'{slug}' now launches {'through Steam' if via == 'steam' else 'from Nexus'}.")


def _launch_through_steam(slug: str, entry: dict, wait: bool) -> None:
    from nexus import steamlib
    url = steam_shortcut_url(entry)
    if not steamlib.open_url(url):
        print("Neither `steam` nor `xdg-open` is available", file=sys.stderr)
        sys.exit(1)
    touch_last_played(slug)
    print(f"Launched '{slug}' through Steam ({url}).")
    if wait:                     # Sunshine: wait for the game to show up, then to end
        markers = _prefix_markers(entry)
        deadline = time.monotonic() + 120
        while not prefix_processes(markers) and time.monotonic() < deadline:
            time.sleep(2)
        while prefix_processes(markers):
            time.sleep(5)


def cmd_launch(install_base: Path, slug: str, force: bool = False, wait: bool = False) -> None:
    entry = registry_get(slug) or {}
    if not force:
        from nexus import steamlib
        if slug in steamlib.running()['slugs'] or prefix_processes(_prefix_markers(entry)):
            print(f"'{slug}' is already running (pass --force to start it again).",
                  file=sys.stderr)
            sys.exit(3)
    if entry.get('launch_via') == 'steam' and steam_shortcut_url(entry) \
            and not os.environ.get('NEXUS_MANGOHUD_EXTRA'):      # a benchmark runs here
        _launch_through_steam(slug, entry, wait)
        return
    launcher = entry.get('launcher')
    if not launcher:
        cand = install_base / slug / 'launch.sh'
        launcher = str(cand) if cand.exists() else None
    if not launcher or not Path(launcher).exists():
        print(f"No launcher found for '{slug}'", file=sys.stderr)
        sys.exit(1)
    # A detached tracker runs the game and times the session (play time), and
    # keeps its output where the GUI's log viewer finds it.
    out = launch_log_path(slug)
    tracker = subprocess.Popen(
        [sys.executable, str(CLI_BIN), '--track-session', slug, launcher],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        rc = tracker.wait(timeout=3)
    except subprocess.TimeoutExpired:
        rc = None      # still running — the normal case for a game
    if rc not in (None, 0):
        print(f"'{slug}' exited immediately with code {rc}. Output: {out}", file=sys.stderr)
        try:
            for line in out.read_text(errors='replace').strip().splitlines()[-10:]:
                print(f"  {line}", file=sys.stderr)
        except OSError:
            pass
        sys.exit(1)
    print(f"Launched '{slug}' (output: {out}).")
    if wait and rc is None:             # Sunshine: the stream lasts as long as the game
        sys.stdout.flush()
        sys.exit(tracker.wait())


def cmd_track_session(slug: str, launcher: str) -> None:
    """Internal (spawned by --launch): run the game, wait for it, and add the
    session's length to the registry's play time. Exits with the game's code."""
    started = datetime.now()
    touch_last_played(slug)
    out = launch_log_path(slug)
    out.parent.mkdir(parents=True, exist_ok=True)
    entry = registry_get(slug) or {}
    config = load_config()
    from nexus import presence, compat, session
    name = entry.get('display_name') or slug
    awake = presence.inhibit(f"Playing {name}") if config['keep_awake'] else None
    discord = presence.discord_presence(config['discord_client_id'], name,
                                        compat.store_appid(entry))
    notes: list[str] = []
    undo = session.enter(say=notes.append)
    watcher = _achievement_watcher(entry, name)
    try:
        rc, elapsed = _run_session(slug, launcher, out, started, notes)
    finally:
        if watcher:
            watcher.stop()
        notes = []
        session.leave(undo, say=notes.append)
        if notes:
            try:
                with open(out, 'a') as fh:
                    fh.write('\n'.join(notes) + '\n')
            except OSError:
                pass
        if discord:
            discord.close()
        presence.release(awake)
    if elapsed >= 30:           # an immediate crash isn't play time
        from nexus import history
        history.record(slug, 'nexus', started, datetime.now(), name)
        with registry_lock():
            reg = load_registry()
            e = reg['games'].get(slug)
            if e is not None:
                e['playtime_seconds'] = int(e.get('playtime_seconds') or 0) + int(elapsed)
                e['last_played'] = datetime.now().isoformat(timespec='seconds')
                save_registry(reg)
    sys.exit(rc)


def _achievement_watcher(entry: dict, game_name: str):
    """While a repack plays: a desktop notification for every achievement
    its Steam emulator records (with the achievement's icon)."""
    from nexus import emuach, compat
    if not entry.get('wine_prefix') or not shutil.which('notify-send'):
        return None
    prefix = Path(entry['wine_prefix'])
    icons = NX.CACHE_DIR / 'achicons'

    def unlocked(a: dict):
        args = ['notify-send', '-a', 'Nexus', '-u', 'normal']
        url = a.get('icon_url') or ''
        if url.startswith('https://'):
            dest = icons / (hashlib.sha1(url.encode()).hexdigest()[:20] + '.jpg')
            try:
                if not dest.exists():
                    from nexus import net
                    net.download(url, dest, timeout=10, limit=1 << 20)
                args += ['-i', str(dest)]
            except Exception:                    # noqa: BLE001 — the text is enough
                pass
        subprocess.run(args + ['--', f"Achievement unlocked — {game_name}",
                               f"{a.get('title') or a.get('name')}"
                               + (f"\n{a['desc']}" if a.get('desc') else '')],
                       capture_output=True, timeout=10)
    w = emuach.Watcher(prefix, unlocked, STEAM_ROOT)
    appid = compat.store_appid(entry)
    if appid:                                    # warm the schema cache off the clock
        threading.Thread(target=emuach.schema, args=(appid, STEAM_ROOT), daemon=True).start()
    w.start()
    return w


def _run_session(slug: str, launcher: str, out: Path, started: datetime,
                 notes: list[str] = ()) -> tuple[int, float]:
    """Run launch.sh, then wait for anything left in the game's prefix.
    Returns (exit code, seconds played)."""
    with open(out, 'w') as fh:
        fh.write(f"# {slug} launched {started.isoformat(timespec='seconds')}\n")
        for n in notes:
            fh.write(n + '\n')
        fh.flush()
        rc = subprocess.run(['bash', launcher], stdout=fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL).returncode
        # A launcher stub can start the real game and exit; the session lasts
        # as long as anything still runs in the game's prefix.
        markers = _prefix_markers(registry_get(slug) or {})
        lingering = False
        while prefix_processes(markers):
            lingering = True
            time.sleep(5)
        if lingering:
            fh.write("\n# the launcher exited first; waited for the game's processes\n")
        elapsed = (datetime.now() - started).total_seconds()
        fh.write(f"\n# exited {rc} after {elapsed / 60:.1f} min\n")
    return rc, elapsed


# ---------------------------------------------------------------------------
# Applying a game's settings to its launchers (launch.sh, Steam, Heroic)
# ---------------------------------------------------------------------------

def profile_owns_options(prof: dict | None) -> bool:
    """A launch profile owns the launch options once env/wrappers/args have
    been set (even to empty). One holding only a per-game `proton` doesn't,
    so the user's own Steam launch options / Heroic tuning are left alone."""
    return bool(prof) and 'env' in prof


def _entry_paths(entry: dict) -> tuple[Path | None, Path | None, Path | None]:
    """(exe, game_dir/compatdata, wine_prefix) for a registry entry."""
    wine_prefix = Path(entry['wine_prefix']) if entry.get('wine_prefix') else None
    exe = Path(entry['exe']) if entry.get('exe') else None
    if not exe and wine_prefix:
        exe = find_game_exe(wine_prefix)
    game_dir = (Path(entry['compatdata']) if entry.get('compatdata')
                else (wine_prefix.parent if wine_prefix else None))
    return exe, game_dir, wine_prefix


def _proton_is_explicit(entry: dict, config: dict) -> bool:
    return bool((entry.get('launch') or {}).get('proton')
                or config.get('proton') or config.get('heroic_proton'))


def regen_launch_sh(entry: dict, config: dict, proton: Path | None) -> Path | None:
    """Rewrite a game's launch.sh from its registry entry + launch profile."""
    launcher = entry.get('launcher')
    if not launcher:
        return None
    wine_prefix = Path(entry['wine_prefix']) if entry.get('wine_prefix') else None
    compatdata = Path(entry['compatdata']) if entry.get('compatdata') else None
    game_exe = _entry_paths(entry)[0]
    if game_exe and not entry.get('exe'):
        # Older entries never recorded the exe; without this a regenerate would
        # replace a working launcher with the "no executable" stub.
        update_registry_entry(entry['slug'], exe=str(game_exe))
    runner = select_runner(config.get('runner', 'auto'), proton)
    # A raw-wine / add-steam launcher has no compatdata: fall back to wine.
    if runner != 'wine' and not compatdata:
        runner = 'wine'
    gameid = entry.get('umu_gameid')
    if runner == 'umu' and gameid is None and entry.get('display_name'):
        gameid = resolve_umu_gameid(entry['display_name'])
        if gameid:
            update_registry_entry(entry['slug'], umu_gameid=gameid)
    return write_launch_script(Path(launcher), compatdata,
                               wine_prefix or Path(launcher).parent, game_exe,
                               entry['slug'], proton, entry.get('display_name'),
                               runner=runner, launch=entry.get('launch'), gameid=gameid)


def sync_steam(entry: dict, config: dict, proton: Path | None,
               artwork: dict | None = None) -> int | None:
    """Add or update the game's Steam shortcut from its registry entry."""
    exe, game_dir, _ = _entry_paths(entry)
    if not (exe and game_dir):
        log.print(f"  [i] Steam: '{entry['slug']}' needs a detected exe + prefix — skipping.")
        return None
    prof = entry.get('launch')
    return add_steam_shortcut_native(
        entry['display_name'], entry['slug'], exe, game_dir, proton, artwork,
        launch_options=format_launch_options(prof) if profile_owns_options(prof) else None)


def sync_heroic(entry: dict, config: dict, proton: Path | None,
                artwork: dict | None = None) -> str | None:
    """Add or update the game's Heroic sideload entry from its registry entry."""
    exe, _, wine_prefix = _entry_paths(entry)
    prof = entry.get('launch')
    return add_heroic_entry(entry['display_name'], entry['slug'], exe, wine_prefix,
                            proton, artwork,
                            force_proton=_proton_is_explicit(entry, config),
                            launch=prof if profile_owns_options(prof) else None)


def _require_entry(slug: str) -> dict:
    entry = registry_get(slug)
    if not entry:
        print(f"'{slug}' is not a managed game (see --list)", file=sys.stderr)
        sys.exit(1)
    return entry


def cmd_apply(slug: str, config: dict, steam: bool | None = None,
              heroic: bool | None = None) -> None:
    """Re-apply a game's registry entry (exe, name, Proton, launch profile) to
    every launcher it has: launch.sh, the desktop entry, and — when the game is
    already in them (or `steam`/`heroic` is True) — Steam and Heroic."""
    global log
    log = Logger()
    entry = _require_entry(slug)
    proton = resolve_game_proton(entry, config)
    if proton and not entry.get('proton'):
        update_registry_entry(slug, proton=proton.parent.name)
        entry['proton'] = proton.parent.name
    log.print(f"Applying settings for '{slug}' (Proton: "
              f"{proton.parent.name if proton else 'none found'})")
    sh = regen_launch_sh(entry, config, proton)
    if sh:
        log.print(f"  launch.sh: {sh}")
    if entry.get('has_desktop') and sh:
        write_desktop_entry(entry['display_name'], slug, sh, _local_artwork(slug) or None)
    if steam is None:
        steam = bool(entry.get('steam_appid')) or game_has_steam_shortcut(slug)
    if steam:
        appid = sync_steam(entry, config, proton)
        if appid:
            set_registry_steam_appid(slug, appid)
    if heroic is None:
        heroic = bool(entry.get('heroic_app_name'))
    if heroic:
        app_name = sync_heroic(entry, config, proton)
        if app_name:
            update_registry_entry(slug, heroic_app_name=app_name)


def _save_profile(slug: str, prof: dict, source: str) -> dict:
    prof = {**prof, 'source': source,
            'updated_at': datetime.now().isoformat(timespec='seconds')}
    update_registry_entry(slug, launch=prof)
    return prof


def cmd_set_launch(slug: str, text: str, config: dict) -> None:
    """Set a game's launch profile from a Steam launch-options string, a umu
    command line or a protongen preset share code, then apply it everywhere.
    An empty string clears env/wrappers/args (the per-game Proton is kept)."""
    entry = _require_entry(slug)
    old = entry.get('launch') or {}
    try:
        if text.strip().startswith(PROTONGEN_CODE_PREFIX):
            name, prof = decode_protongen_code(text)
            source = f"protongen preset '{name}'"
        else:
            prof, source = parse_launch_options(text), 'manual'
    except LaunchParseError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not prof.get('proton'):
        prof['proton'] = old.get('proton')
    if prof.get('proton') and not proton_by_name(prof['proton']):
        print(f"  [!] Proton '{prof['proton']}' is not installed — keeping the current "
              f"one. Installed: {', '.join(sorted(list_protons())) or 'none'}")
        prof['proton'] = old.get('proton')
    prof = _save_profile(slug, prof, source)
    print(f"Launch profile for '{slug}' ({source}):")
    print(f"  {format_launch_options(prof) or '(no options)'}")
    cmd_apply(slug, config)


def cmd_perf(slug: str, items: list[str], config: dict) -> None:
    """Set performance switches on a game's launch profile and apply it
    everywhere (no items: just show them)."""
    entry = _require_entry(slug)
    prof = entry.get('launch') or {}
    if not items:
        print(json.dumps(perf_state(prof), indent=1))
        return
    try:
        changes = parse_perf_args(items)
    except LaunchParseError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if changes.get('gamescope') and not shutil.which('gamescope'):
        print("gamescope isn't installed.", file=sys.stderr)
        sys.exit(1)
    if changes.get('mode') in PERF_MODES and not shutil.which(changes['mode']):
        print(f"{changes['mode']} isn't installed.", file=sys.stderr)
        sys.exit(1)
    if changes.get('mangohud') and not shutil.which('mangohud'):
        print("mangohud isn't installed.", file=sys.stderr)
        sys.exit(1)
    if 'lsfg' in changes:
        mult = changes.pop('lsfg')
        cur = lsfg_state(entry)
        if cur is None:
            if mult:
                print("lsfg-vk isn't set up (its Vulkan layer and ~/.config/lsfg-vk/conf.toml).",
                      file=sys.stderr)
                sys.exit(1)
        elif (cur.get('multiplier') or None) != mult:
            try:
                lsfg_set(entry, mult)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                print(f"lsfg-vk: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"lsfg-vk: {f'{mult}× frame generation' if mult else 'off'} for '{slug}'.")
        if not changes:
            return
    new = apply_perf(prof, changes)
    src = prof.get('source') or 'manual'
    if 'performance' not in src:
        src = f"{src} + performance settings"
    new = _save_profile(slug, new, src)
    print(f"Launch options for '{slug}':")
    print(f"  {format_launch_options(new) or '(no options)'}")
    cmd_apply(slug, config)


def cmd_set_proton(slug: str, name: str, config: dict) -> None:
    """Pin (or with '' / 'default', unpin) the Proton a game is played with."""
    entry = _require_entry(slug)
    prof = dict(entry.get('launch') or {})
    if name.strip().lower() in ('', 'default', 'none', 'auto'):
        prof.pop('proton', None)
    else:
        hit = proton_by_name(name)
        if not hit:
            print(f"Proton '{name}' is not installed. Installed: "
                  f"{', '.join(sorted(list_protons())) or 'none'}", file=sys.stderr)
            sys.exit(1)
        prof['proton'] = hit.parent.name
    update_registry_entry(slug, launch=prof or None)
    cmd_apply(slug, config)


def cmd_from_protongen(slug: str, config: dict) -> None:
    """Import protongen's remembered tuning for this game (read-only from its
    state.toml) as the launch profile, then apply it everywhere."""
    entry = _require_entry(slug)
    hit = protongen_memory_for(entry)
    if not hit:
        print(f"protongen has no saved tuning for '{slug}'. Tune it in protongen "
              f"first (it lists the game via its Steam shortcut or Heroic entry).",
              file=sys.stderr)
        sys.exit(1)
    which, cfg = hit
    prof = profile_from_protongen_config(cfg)
    if prof.get('proton') and not proton_by_name(prof['proton']):
        print(f"  [!] protongen's Proton '{prof['proton']}' is not installed — ignoring it.")
        prof['proton'] = (entry.get('launch') or {}).get('proton')
    prof = _save_profile(slug, prof, f"protongen ({which} entry)")
    print(f"Imported protongen tuning for '{slug}' from its {which} entry:")
    print(f"  {format_launch_options(prof) or '(no options)'}")
    if prof.get('proton'):
        print(f"  Proton: {prof['proton']}")
    cmd_apply(slug, config)


def cmd_protongen_default(target: str, config: dict) -> None:
    """Apply protongen's default profile to one game (replacing its launch
    options), or with 'all' to every game that has no launch options of its
    own yet — tuned games are left alone."""
    if protongen_default_config() is None:
        print("protongen has no default profile. Set one in protongen (build a "
              "command, then Settings → Save as default profile).", file=sys.stderr)
        sys.exit(1)
    if target != 'all':
        slugs = [target]
    else:
        games = load_registry()['games']
        slugs = sorted(s for s, e in games.items()
                       if not profile_owns_options(e.get('launch')))
        tuned = len(games) - len(slugs)
        if tuned:
            print(f"Leaving {tuned} game(s) that already have launch options alone.")
        if not slugs:
            print("Every game already has launch options — nothing to do.")
            return
    for slug in slugs:
        entry = _require_entry(slug)
        prof = _save_profile(slug, protongen_default_profile(entry),
                             "protongen default profile")
        print(f"Applied protongen's default profile to '{slug}':")
        print(f"  {format_launch_options(prof) or '(no options)'}")
        if prof.get('proton'):
            print(f"  Proton: {prof['proton']}")
        cmd_apply(slug, config)


def cmd_show_launch(slug: str, config: dict, as_json: bool = False) -> None:
    entry = _require_entry(slug)
    globals()['log'] = Logger()
    prof = entry.get('launch') or {}
    proton = resolve_game_proton(entry, config, quiet=True)
    pg = protongen_memory_for(entry)
    pg_prof = profile_from_protongen_config(pg[1]) if pg else None
    info = {
        'slug': slug,
        'profile': prof or None,
        'owns_options': profile_owns_options(prof),
        'launch_options': format_launch_options(prof),
        'proton': proton.parent.name if proton else None,
        'proton_pinned': prof.get('proton'),
        'protons': sorted(list_protons()),
        'protongen': ({'source': pg[0], 'launch_options': format_launch_options(pg_prof),
                       'proton': pg_prof.get('proton'),
                       'differs': [pg_prof.get(k) for k in ('env', 'wrappers', 'args', 'proton')]
                                  != [prof.get(k) for k in ('env', 'wrappers', 'args', 'proton')]}
                      if pg else None),
        'protongen_installed': bool(protongen_bin()),
        'protongen_default': ({'launch_options': format_launch_options(pd),
                               'proton': pd.get('proton'),
                               'applied': [pd.get(k) for k in ('env', 'wrappers', 'args')]
                                          == [prof.get(k) for k in ('env', 'wrappers', 'args')]}
                              if (pd := protongen_default_profile(entry)) else None),
        'perf': perf_state(prof),
        'launch_via': entry.get('launch_via') or 'nexus',
        'in_steam': bool(steam_shortcut_url(entry)),
        'lsfg': lsfg_state(entry),
        'perf_tools': {t: bool(shutil.which(t)) for t in
                       ('gamescope', 'mangohud', *PERF_MODES)},
        'niri': bool(os.environ.get('NIRI_SOCKET')),
        'display_name': entry.get('display_name'),
        'umu_gameid': entry.get('umu_gameid'),
        'exe': str(_entry_paths(entry)[0] or '') or None,
        'install_log': str(latest_install_log(slug) or '') or None,
        'launch_log': str(launch_log_path(slug)) if launch_log_path(slug).exists() else None,
    }
    if as_json:
        print(json.dumps(info))
        return
    print(f"{slug}:")
    print(f"  Proton:          {info['proton'] or 'none found'}"
          f"{'  (pinned for this game)' if prof.get('proton') else ''}")
    print(f"  umu game id:     {info['umu_gameid'] or 'umu-0 (no protonfixes match)'}")
    if info['owns_options']:
        print(f"  Launch options:  {info['launch_options'] or '(none — cleared)'}")
        print(f"  Source:          {prof.get('source', '?')}  ({prof.get('updated_at', '?')})")
    else:
        print("  Launch options:  not managed (Steam/Heroic keep their own)")
    if pg:
        print(f"  protongen:       has tuning via its {pg[0]} entry"
              f"{' — differs from this profile' if info['protongen']['differs'] else ' — in sync'}")
        print(f"                   {info['protongen']['launch_options'] or '(no options)'}")
        print(f"                   import with: nexus-cli --from-protongen {slug}")
    if info['protongen_default'] and not info['protongen_default']['applied']:
        print(f"  protongen default profile: {info['protongen_default']['launch_options']}")
        print(f"                   apply with: nexus-cli --protongen-default {slug}")


def latest_install_log(slug: str) -> Path | None:
    logs = sorted(LOG_DIR.glob(f"{glob_escape(slug)}-[0-9]*.log")) if LOG_DIR.is_dir() else []
    return logs[-1] if logs else None


def cmd_list_protons(config: dict, as_json: bool = False) -> None:
    protons = list_protons()
    default = resolve_game_proton(None, config, quiet=True)
    rows = [{'name': n, 'tool_name': proton_tool_name(p), 'path': str(p.parent),
             'default': bool(default and p == default)} for n, p in sorted(protons.items())]
    if as_json:
        print(json.dumps({'protons': rows, 'default': default.parent.name if default else None}))
        return
    for r in rows:
        print(f"  {'*' if r['default'] else ' '} {r['name']}  ({r['path']})")


def _heroic_proton_bins() -> dict[str, list[str]]:
    """{proton binary path: [Heroic game titles]} from every GamesConfig."""
    out: dict[str, list[str]] = {}
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        return out
    titles = {}
    for name in ('sideload_apps/library.json', 'store_cache/legendary_library.json',
                 'store_cache/gog_library.json'):
        try:
            data = json.loads((cfg_dir / name).read_text())
        except (OSError, ValueError):
            continue
        lib = data.get('games', data.get('library', [])) if isinstance(data, dict) else data
        for g in lib if isinstance(lib, list) else ():
            if isinstance(g, dict) and g.get('app_name'):
                titles[g['app_name']] = g.get('title') or g['app_name']
    for gc in (cfg_dir / 'GamesConfig').glob('*.json'):
        try:
            data = json.loads(gc.read_text())
        except (OSError, ValueError):
            continue
        inner = data.get(gc.stem)
        wv = inner.get('wineVersion') if isinstance(inner, dict) else None
        if isinstance(wv, dict) and wv.get('type') == 'proton' and wv.get('bin'):
            out.setdefault(wv['bin'], []).append(titles.get(gc.stem, gc.stem))
    return out


def proton_usage(config: dict) -> dict[str, list[str]]:
    """{build dir name: ['Nexus: 007 First Light', 'Steam: PEAK', ...]} —
    who plays with each installed build, across Nexus, Steam and Heroic."""
    from nexus import steamlib
    protons = list_protons()
    by_path = {str(p): n for n, p in protons.items()}
    use: dict[str, list[str]] = {n: [] for n in protons}
    for e in load_registry()['games'].values():
        p = resolve_game_proton(e, config, quiet=True)
        if p and p.parent.name in use:
            use[p.parent.name].append(f"Nexus: {e.get('display_name') or e['slug']}")
    names = {g['appid']: g['display_name'] for g in steamlib.scan(STEAM_ROOT)}
    ours = {int(e.get('steam_appid') or 0) for e in load_registry()['games'].values()}
    shortcut_names = _all_shortcut_names()
    for appid, tool in _read_compat_mapping().items():
        p = proton_by_name(tool, protons)
        if not p:
            continue
        aid = int(appid) if appid.isdigit() else 0
        if aid in names:
            use[p.parent.name].append(f"Steam: {names[aid]}")
        elif aid in shortcut_names and aid not in ours:
            use[p.parent.name].append(f"Steam shortcut: {shortcut_names[aid]}")
    for binpath, titles in _heroic_proton_bins().items():
        n = by_path.get(binpath) or by_path.get(str(Path(binpath)))
        if n:
            use[n] += [f"Heroic: {t}" for t in titles if f"Nexus: {t}" not in use[n]]
    default = resolve_game_proton(None, config, quiet=True)
    if default and default.parent.name in use:
        use[default.parent.name].insert(0, 'Default for new games')
    return use


def _valve_protons() -> list[dict]:
    from nexus import steamlib
    out = []
    for lib in steamlib.library_folders(STEAM_ROOT):
        for p in sorted((lib / 'steamapps' / 'common').glob('Proton*/proton')):
            out.append({'name': p.parent.name, 'path': str(p.parent), 'source': 'valve'})
    return out


def install_hint(arch: str, other: str) -> str:
    """How to install a package here: the pacman line on Arch-based systems,
    the generic package name elsewhere."""
    if shutil.which('pacman'):
        return f"sudo pacman -S --needed {arch}"
    return f"install {other} with your distro's package manager"


def _pacman_version(pkg: str) -> str | None:
    if not shutil.which('pacman'):
        return None
    try:
        r = subprocess.run(['pacman', '-Q', pkg], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.split()[1] if r.returncode == 0 and len(r.stdout.split()) > 1 else None


def cmd_protons(config: dict, as_json: bool, refresh: bool = False) -> None:
    """Installed builds (with who uses them) plus upstream release status."""
    from nexus import protons as PR, net
    usage = proton_usage(config)
    rows = []
    for name, binp in sorted(list_protons().items(), key=lambda kv: PR.version_key(kv[0]),
                             reverse=True):
        d = binp.parent
        rows.append({
            'name': name, 'tool_name': proton_tool_name(binp), 'path': str(d),
            'family': PR.family_of(name),
            'source': 'user' if PR.is_user_build(d) else 'system',
            'used_by': usage.get(name, []),
            'removable': PR.is_user_build(d),
        })
    families = {}
    for fam in ('ge', 'cachyos'):
        try:
            rel = PR.releases(fam, refresh)
            latest = rel['releases'][0] if rel['releases'] else None
            families[fam] = {'latest': latest and latest['tag'], 'stale': rel['stale'],
                             'fetched_at': rel['fetched_at'], 'error': None}
        except net.NetError as e:
            families[fam] = {'latest': None, 'error': str(e)}
    ge_have = [r['name'] for r in rows if r['family'] == 'ge']
    newest_ge = max(ge_have, key=PR.version_key, default=None)
    latest_ge = families['ge'].get('latest')
    families['ge']['update'] = bool(latest_ge and (
        not newest_ge or PR.version_key(latest_ge) > PR.version_key(newest_ge)[:2]))
    families['cachyos']['installed'] = _pacman_version('proton-cachyos-slr') \
        or _pacman_version('proton-cachyos')
    try:
        dismissed = __import__('tomllib').loads(
            (NX.PROTONGEN_CONFIG / 'state.toml').read_text()).get('dismissed_runtime_updates', [])
    except (OSError, ValueError):
        dismissed = []
    info = {'installed': rows, 'valve': _valve_protons(), 'families': families,
            'protongen_dismissed': dismissed,
            'default': (resolve_game_proton(None, config, quiet=True) or Path('')).parent.name
                       or None}
    if as_json:
        print(json.dumps(info))
        return
    print("Installed Proton builds:")
    for r in rows:
        print(f"  {r['name']:<28} {r['source']:<7} used by: "
              f"{', '.join(r['used_by']) or 'nothing'}")
    for v in info['valve']:
        print(f"  {v['name']:<28} valve   (managed by Steam)")
    ge = families['ge']
    if ge.get('latest'):
        print(f"\nGE-Proton latest: {ge['latest']}"
              + ("  — update available: nexus-cli --proton-install latest" if ge['update'] else
                 "  (installed)"))
    cy = families['cachyos']
    if cy.get('installed'):
        print(f"proton-cachyos (pacman): {cy['installed']}; upstream: {cy.get('latest')}. "
              f"Update with: sudo pacman -Syu")


def cmd_proton_releases(refresh: bool, as_json: bool) -> None:
    from nexus import protons as PR, net
    try:
        rel = PR.releases('ge', refresh)
    except net.NetError as e:
        print(f"Couldn't reach GitHub: {e}", file=sys.stderr)
        sys.exit(1)
    have = set(list_protons())
    for r in rel['releases']:
        r['installed'] = any(n == r['tag'] or n.startswith(f"{r['tag']}-") for n in have)
    if as_json:
        print(json.dumps(rel))
        return
    for r in rel['releases']:
        print(f"  {r['tag']:<16} {r['published_at'][:10]}  "
              f"{(r['tar_size'] or 0) / 1e6:6.0f} MB{'  (installed)' if r['installed'] else ''}")


def cmd_proton_install(tag: str, force: bool = False) -> None:
    """Install a GE-Proton release ('latest' or a tag) into Steam's
    compatibilitytools.d. Steam picks it up on its next start. With force,
    an installed copy is replaced: the new one is downloaded, verified and
    extracted first, then the old one goes to the Trash."""
    from nexus import protons as PR, net, cleanup as C
    try:
        rel = PR.releases('ge')['releases']
    except net.NetError as e:
        print(f"Couldn't reach GitHub: {e}", file=sys.stderr)
        sys.exit(1)
    pick = rel[0] if tag == 'latest' and rel else next((r for r in rel if r['tag'] == tag), None)
    if not pick:
        print(f"No GE-Proton release {tag!r} (see --proton-releases)", file=sys.stderr)
        sys.exit(1)
    have = [n for n in list_protons() if n == pick['tag'] or n.startswith(f"{pick['tag']}-")]
    if have and not force:
        broken = damaged_protons()
        print(f"{pick['tag']} is already installed."
              + (f" It's damaged ({broken[have[0]]}): reinstall it with --force."
                 if have[0] in broken else ''))
        return
    if have and not all(PR.is_user_build(list_protons()[n].parent) for n in have):
        print(f"{have[0]} is a system package — reinstall it with your package manager.",
              file=sys.stderr)
        sys.exit(1)
    try:
        final = PR.install(pick, say=lambda m: print(m, flush=True),
                           replace=C.trash if have else None)
    except PR.InstallError as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)
    if have:
        print(f"Reinstalled {final.name} → {final} (the old copy is in the Trash)")
        return
    print(f"Installed {final.name} → {final}")
    print("Restart Steam to see it in Steam's compatibility list.")


def cmd_proton_remove(name: str, force: bool, config: dict) -> None:
    from nexus import protons as PR, cleanup as C
    binp = list_protons().get(name)
    if not binp:
        print(f"No installed Proton named {name!r}", file=sys.stderr)
        sys.exit(1)
    if not PR.is_user_build(binp.parent):
        print(f"{name} is a system package — remove it with your package manager.",
              file=sys.stderr)
        sys.exit(1)
    users = [u for u in proton_usage(config).get(name, []) if u != 'Default for new games']
    if users and not force:
        print(f"{name} is still used by: {', '.join(users)}. Move them to another build "
              f"first (--proton-migrate {name} <other>), or pass --force.", file=sys.stderr)
        sys.exit(1)
    C.trash(binp.parent)
    print(f"Moved {name} to the Trash.")


def cmd_proton_migrate(old: str, new: str, config: dict) -> None:
    """Point everything using build `old` at `new`: Nexus games (their pin or
    recorded build), Steam games' forced compat tool, and Heroic entries."""
    protons = list_protons()
    if old not in protons or new not in protons:
        print(f"Both builds must be installed (have: {', '.join(sorted(protons))})",
              file=sys.stderr)
        sys.exit(1)
    newp = protons[new]
    globals()['log'] = Logger()
    changed = []
    # Nexus games
    for slug, e in list(load_registry()['games'].items()):
        prof = dict(e.get('launch') or {})
        touched = False
        if prof.get('proton') == old:
            prof['proton'] = new
            update_registry_entry(slug, launch=prof)
            touched = True
        if e.get('proton') == old:
            update_registry_entry(slug, proton=new)
            touched = True
        if touched:
            changed.append(slug)
    for slug in changed:
        cmd_apply(slug, config)
    # Steam games
    old_tool, new_tool = proton_tool_name(protons[old]), proton_tool_name(newp)
    steam_ids = [aid for aid, tool in _read_compat_mapping().items() if tool == old_tool]
    if steam_ids:
        if steam_is_running():
            print(f"  [!] Steam is running — skipped re-pointing {len(steam_ids)} Steam "
                  f"game(s). Quit Steam and run this again.")
        else:
            for aid in steam_ids:
                set_steam_compat_tool(int(aid), new_tool)
            print(f"  Steam: {len(steam_ids)} game(s) now use {new_tool}.")
    # Heroic entries not owned by Nexus (Nexus's were re-applied above)
    cfg_dir = find_heroic_config()
    if cfg_dir:
        n = 0
        for gc in (cfg_dir / 'GamesConfig').glob('*.json'):
            try:
                data = json.loads(gc.read_text())
            except (OSError, ValueError):
                continue
            inner = data.get(gc.stem)
            wv = inner.get('wineVersion') if isinstance(inner, dict) else None
            if isinstance(wv, dict) and wv.get('bin') == str(protons[old]):
                inner['wineVersion'] = _heroic_wine_version(newp)
                backup_file(gc)
                atomic_write(gc, json.dumps(data, indent=4))
                n += 1
        if n:
            print(f"  Heroic: {n} game(s) now use {new}.")
    print(f"Moved {len(changed)} Nexus game(s) from {old} to {new}.")


def cmd_protondb(appids: list[str], refresh: bool, as_json: bool) -> None:
    from nexus import protondb
    tiers = protondb.tiers([int(a) for a in appids if a.isdigit()], refresh)
    if as_json:
        print(json.dumps({str(k): v for k, v in tiers.items()}))
        return
    for appid, t in tiers.items():
        print(f"  {appid}: {t.get('tier') or 'no reports'}"
              + (f" (trending {t['trending']}, {t.get('total')} reports)" if t.get('tier') else ''))


def _game_by_id(ident: str, light: bool = False) -> dict | None:
    """A library game by Nexus slug, `steam-<appid>`/appid, or Heroic slug.
    `light`: for a Steam game just its id (no library scan) — enough for
    callers that only need the appid."""
    reg = load_registry()['games']
    if ident in reg:
        return {**reg[ident], 'source': 'nexus'}
    from nexus import steamlib, gamedb
    appid = ident.removeprefix('steam-')
    if appid.isdigit() and light:
        return {'slug': f'steam-{appid}', 'source': 'steam', 'appid': int(appid)}
    if appid.isdigit():
        for g in steamlib.scan(STEAM_ROOT, _read_compat_mapping(), installing=True):
            if g['appid'] == int(appid):
                return g
        for item in gamedb.owned_library():        # owned, not installed
            if item['appid'] == int(appid):
                return steamlib.steam_owned_entry(STEAM_ROOT, item)
    for g in steamlib.heroic_store_games(find_heroic_config(), owned=True):
        if g['slug'] == ident or g['app_name'] == ident:
            return g
    return None


def _protongen_id(g: dict) -> int | None:
    if g.get('source') == 'steam':
        return g['appid']
    if g.get('source') == 'heroic':
        return protongen_heroic_id(g['app_name'])
    if g.get('steam_appid'):
        return int(g['steam_appid']) & 0xFFFFFFFF
    if g.get('heroic_app_name'):
        return protongen_heroic_id(g['heroic_app_name'])
    return None


def cmd_repack_feed(refresh: bool, as_json: bool) -> None:
    from nexus import repackfeed
    data = repackfeed.refresh(force=refresh)
    found = repackfeed.updates(load_registry()['games'].values(), data)
    if as_json:
        print(json.dumps({'fetched_at': data.get('fetched_at'), 'updates': found}))
        return
    if not found:
        print(f"No newer repacks posted (feed checked {data.get('fetched_at') or 'never'}).")
    for slug, post in sorted(found.items()):
        what = "update" if post['kind'] == 'update' else "new repack"
        print(f"  {slug}: {what} posted {post['date'][:10]} — {post['title']}\n    {post['url']}")


def _cached_wishlist() -> list | None:
    """The Game-db wishlist from its cache (no network), or None when
    Game-db isn't set up."""
    from nexus import gamedb
    if not gamedb.configured():
        return None
    try:
        res = gamedb.summary(cache_only=True)
    except gamedb.GameDbError:
        return None
    return ((res or {}).get('data') or {}).get('wishlist') or []


def cmd_repack_index(refresh: bool, as_json: bool) -> None:
    """Sync FitGirl's catalogue, then say which wishlist games have a
    repack — pushing the new ones to the phone (config [notify])."""
    from nexus import net, notify, repackindex as RI

    def progress(page, pages):
        print(f"[{page}/{pages or '?'}] Reading FitGirl's catalogue", flush=True)
    error = None
    try:
        data = RI.sync(force=refresh, progress=progress if not as_json else None)
    except net.NetError as e:
        data, error = RI.load(), str(e)
    wishlist = _cached_wishlist()
    matches = RI.wishlist_matches(wishlist, data) if wishlist else {}
    fresh, first = RI.new_matches(matches, data) if wishlist is not None else ([], False)
    if wishlist is not None and data['posts']:
        RI.save(data)
    titles = {str(g.get('appid')): g.get('title') for g in wishlist or []}
    news = [{'appid': a, 'title': titles.get(a), **matches[a]} for a in fresh]
    if news and not error:
        s = notify.settings()
        if notify.configured(s):
            if first:
                title = f"{len(news)} wishlist games are on FitGirl"
                body = ", ".join(n['title'] or '' for n in news[:6]) + ("…" if len(news) > 6 else "")
            else:
                title = ("On FitGirl: " + (news[0]['title'] or news[0]['slug'])
                         if len(news) == 1 else f"{len(news)} wishlist games are on FitGirl")
                body = ", ".join(n['title'] or '' for n in news[:6])
            for err in notify.push(title, body, True, s):
                print(f"  [!] push notification: {err}", file=sys.stderr)
    if as_json:
        print(json.dumps({'count': len(data['posts']), 'synced_at': data.get('synced_at'),
                          'built_at': data.get('built_at'), 'error': error,
                          'wishlist': wishlist is not None, 'matches': len(matches),
                          'new_matches': news, 'first': first}))
        return
    if error:
        print(f"  [!] FitGirl's site: {error}", file=sys.stderr)
    print(f"FitGirl catalogue: {len(data['posts'])} repacks "
          f"(synced {data.get('synced_at') or 'never'}).")
    if wishlist is None:
        print("Game-db isn't set up, so there's no wishlist to compare.")
        return
    print(f"{len(matches)} of {len(wishlist)} wishlist games have a repack:")
    for a, m in sorted(matches.items(), key=lambda kv: (titles.get(kv[0]) or '').lower()):
        likely = '' if m['confidence'] == 'exact' else '  (likely)'
        print(f"  {titles.get(a) or a}: {m['title']}{likely}\n    {m['url']}")


def _size_bytes(text: str | None) -> int | None:
    """"from 47.4 GB [Selective Download]" -> bytes (the largest figure)."""
    units = {'kb': 1 << 10, 'mb': 1 << 20, 'gb': 1 << 30, 'tb': 1 << 40}
    found = [float(n.replace(',', '.')) * units[u.lower()]
             for n, u in re.findall(r'(\d+(?:[.,]\d+)?)\s*([KMGT]B)', text or '', re.I)]
    return int(max(found)) if found else None


def _torrent_dest(s: dict) -> str | None:
    from nexus import torrent as T
    if s.get('download_dir'):
        return s['download_dir']
    try:
        return T.client(s).info().get('download_dir')
    except T.TorrentError:
        return None


def cmd_repack_page(slug: str, as_json: bool) -> None:
    """What a grab would download: sizes, where it goes and the free space
    there. The magnet links themselves aren't printed."""
    from nexus import net, repackindex as RI, torrent as T
    try:
        page = RI.fetch_page(slug)
    except net.NetError as e:
        print(json.dumps({'error': str(e)}) if as_json else f"FitGirl's site: {e}")
        sys.exit(1)
    s = T.settings()
    dest = _torrent_dest(s) if s['client'] else None
    free = None
    if dest:
        try:
            free = shutil.disk_usage(Path(dest).expanduser()).free
        except OSError:
            free = None
    out = {'slug': slug, 'title': page['title'], 'url': page['url'],
           'repack_size': page['repack_size'], 'original_size': page['original_size'],
           'size_bytes': _size_bytes(page['repack_size']), 'magnets': len(page['magnets']),
           'client': s['client'], 'dest': dest, 'free_bytes': free}
    if as_json:
        print(json.dumps(out))
        return
    print(f"{page['title']}\n  {page['url']}\n  repack {page['repack_size'] or '?'}, "
          f"installed {page['original_size'] or '?'}; {len(page['magnets'])} magnet link(s)")
    if dest:
        print(f"  downloads to {dest}" + (f" ({free / (1 << 30):.1f} GB free)" if free else ''))


def cmd_repack_grab(slug: str, appid: str | None, auto_install: bool, as_json: bool) -> None:
    """Read the repack page's magnet link and hand it to the torrent client."""
    from nexus import net, repackindex as RI, torrent as T
    try:
        page = RI.fetch_page(slug)
        if not page['magnets']:
            raise T.TorrentError(f"the repack page has no magnet link: {page['url']}")
        meta = {'source': 'fitgirl', 'slug': slug, 'title': page['title']}
        if appid:
            meta['appid'] = appid
        got = T.add(page['magnets'][0]['uri'], auto_install=auto_install, meta=meta)
    except (net.NetError, T.TorrentError) as e:
        if as_json:
            print(json.dumps({'error': {'kind': 'torrent', 'message': str(e)}}))
        print(f"Couldn't grab it: {e}", file=sys.stderr)
        sys.exit(1)
    got['repack_size'] = page['repack_size']
    if as_json:
        print(json.dumps(got))
        return
    print(f"Downloading {got['name'] or page['title']} ({page['repack_size'] or '?'})"
          + (" — Nexus installs it when it finishes." if auto_install else "."))


def cmd_repack_reject(argv: list[str]) -> None:
    from nexus import repackindex as RI
    appid, slug = argv[0], (argv[1] if len(argv) > 1 else None)
    if not appid.isdigit() or (slug and not RI._SLUG_RE.match(slug)):
        print("usage: --repack-reject APPID [SLUG]  (no SLUG: forget the rejection)",
              file=sys.stderr)
        sys.exit(2)
    RI.reject(appid, slug)
    print(f"Won't match {appid} with {slug}." if slug else f"Matching {appid} again.")


def _owned_store_ids() -> dict[int, str]:
    """{store appid: library slug} for everything you have: installed Steam
    games, Steam games Game-db knows you own, and repacks (by umu GAMEID)."""
    from nexus import steamlib, gamedb, compat
    out = {}
    try:
        for item in gamedb.owned_library():               # cache only
            out[int(item['appid'])] = f"steam-{item['appid']}"
    except Exception:                                     # noqa: BLE001 — optional
        pass
    for g in steamlib.scan(STEAM_ROOT, _read_compat_mapping()):
        out[int(g['appid'])] = g['slug']
    for slug, e in load_registry()['games'].items():
        a = compat.store_appid(e)
        if a:
            out[a] = slug
    return out


def _torrent_mark(h: str, **fields) -> None:
    """Record an install's progress on the download it came from: started
    when the install begins, installed only once it has succeeded."""
    from nexus import torrent as T
    try:
        h = T.check_hash(h)
    except T.TorrentError:
        return
    tracked = T.load_tracked()
    if h in tracked:
        tracked[h].update(fields)
        try:
            T.save_tracked(tracked)
        except OSError:
            pass


def cmd_torrent(args) -> None:
    """--torrent-add / --torrents [--all] / --torrent-pause|resume|remove /
    --torrent-test / --torrent-done / --torrent-forget."""
    from nexus import torrent as T
    try:
        if args.torrent_test or args.torrent_detect:
            if args.torrent_detect:
                found = T.detect()
                print(json.dumps({'found': found}) if args.json else
                      (f"{found['client']} answers at {found['url']}" if found
                       else "No torrent client answers on its default port."))
                return
            info = T.client().info()
            print(json.dumps({'ok': True, **info}) if args.json else
                  f"{info['client']} {info['version']} answers"
                  + (f"; it downloads to {info['download_dir']}" if info['download_dir'] else ''))
            return
        for action in ('pause', 'resume', 'remove'):
            hashes = getattr(args, f'torrent_{action}')
            if hashes:
                T.control(action, hashes, delete_data=args.delete_data)
                done = {'pause': 'Paused', 'resume': 'Resumed', 'remove': 'Removed'}[action]
                print(json.dumps({'ok': True}) if args.json else
                      f"{done} {len(hashes)} torrent(s).")
                return
        if args.torrent_add:
            got = T.add(args.torrent_add, auto_install=args.auto_install)
            if args.json:
                print(json.dumps(got))
            else:
                print(f"Added {got['name'] or got['hash']} to your torrent client"
                      + (" — Nexus installs it when it finishes." if args.auto_install else "."))
            return
        if args.torrent_done or args.torrent_forget:
            h = (args.torrent_done or args.torrent_forget).lower()
            tracked = T.load_tracked()
            if h not in tracked:
                print(f"Not a torrent Nexus added: {h}", file=sys.stderr)
                sys.exit(1)
            if args.torrent_forget:
                tracked.pop(h)
            else:
                tracked[h]['installed'] = True
            T.save_tracked(tracked)
            return
        if args.all:
            s = T.settings()
            c = T.client(s)
            rows, info = T.all_status(s), c.info()
            if args.json:
                print(json.dumps({'torrents': rows, 'client': info}))
                return
        else:
            rows = T.status()
    except T.TorrentError as e:
        if args.json:
            print(json.dumps({'error': {'kind': 'torrent', 'message': str(e)}, 'torrents': [],
                              'configured': T.settings()['client'] is not None}))
        print(f"Torrent client: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps({'torrents': rows}))
        return
    if not rows:
        print("No downloads added from Nexus." if not args.all else "No torrents.")
    for r in rows:
        state = ("gone from the client" if r.get('gone') else "installed" if r.get('installed')
                 else "done" if r.get('done') else f"{r['percent']:.0f}%")
        print(f"  {r['name'] or r['hash']}: {state}")


def cmd_check_repack(path: str, as_json: bool, clamav: bool) -> None:
    from nexus import safety
    d = Path(path).expanduser()
    if not d.is_dir():
        print(f"Not a folder: {d}", file=sys.stderr)
        sys.exit(1)
    r = safety.check(d, clamav=clamav)
    if as_json:
        print(json.dumps(r))
        return
    print({'ok': "Looks like a normal FitGirl repack.",
           'warn': "Some things look unusual — check them before installing.",
           'danger': "This repack looks tampered with. Don't install it."}[r['verdict']])
    for f in r['findings']:
        print(f"  [{f['level']}] {f['file']}: {f['why']}")
    if clamav and not (r['clamav'] or {}).get('available'):
        print(f"  ClamAV isn't installed ({install_hint('clamav', 'clamav')}, then sudo freshclam).")
    sys.exit(2 if r['verdict'] == 'danger' else 0)


def cmd_history(as_json: bool) -> None:
    from nexus import history
    sm = history.summary()
    if as_json:
        print(json.dumps(sm))
        return
    y = sm['year']
    if not y['sessions']:
        print("No play sessions recorded yet (they're recorded from now on).")
        return
    print(f"{y['year']}: {y['minutes'] / 60:.1f} h over {y['sessions']} sessions, "
          f"{y['games']} games, {y['days']} days (best streak {y['best_streak']} days)")
    if y['longest']:
        print(f"  Longest session: {y['longest']['name']}, {y['longest']['minutes'] / 60:.1f} h "
              f"on {y['longest']['date']}")
    for t in y['top']:
        print(f"  {t['minutes'] / 60:6.1f} h  {t['name']}")


def cmd_store_info(ident: str, as_json: bool, refresh: bool = False) -> None:
    """Store page facts for any game: trailers, reviews, metacritic, genres
    and similar games (owned ones marked)."""
    from nexus import storeinfo, compat
    g = _game_by_id(ident, light=True)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    info = storeinfo.info(compat.store_appid(g), refresh)
    if info['similar']:
        owned = _owned_store_ids()
        for s_ in info['similar']:
            s_['owned_slug'] = owned.get(s_['appid'])
    if as_json:
        print(json.dumps(info))
        return
    st, rv = info['store'] or {}, info['reviews'] or {}
    if not st and not rv:
        print("The Steam store has nothing on this game.")
        return
    print(f"{st.get('name') or ident}  {st.get('url') or ''}")
    if st.get('blurb'):
        print(f"  {st['blurb']}")
    if rv:
        print(f"  Reviews: {rv['desc']} ({rv.get('percent')}% of {rv['total']})")
    if st.get('metacritic'):
        print(f"  Metacritic: {st['metacritic']['score']}")
    if st.get('genres'):
        print(f"  {', '.join(st['genres'])} · {st.get('release') or ''}")
    for t in st.get('trailers') or []:
        print(f"  Trailer: {t['name']}  {t['url']}")
    if info['similar']:
        print("  More like this: " + ", ".join(
            s_['name'] + (' (yours)' if s_.get('owned_slug') else '') for s_ in info['similar'][:8]))


def compat_lines(info: dict) -> list[str]:
    """Plain-text anti-cheat / Steam Deck / PCGamingWiki lines for --game-tips."""
    out = []
    ac = info.get('anticheat')
    if ac:
        out.append(f"Anti-cheat: {ac['status']} ({', '.join(ac['anticheats']) or '?'})"
                   f"  {ac['url']}")
    dk = info.get('deck')
    if dk:
        out.append("Steam Deck: " + (dk.get('deck') or 'unknown')
                   + (f", SteamOS {dk['steamos']}" if dk.get('steamos') else ''))
    pw = info.get('pcgw')
    if pw:
        sup = ', '.join(f"{k} {v}" for k, v in pw['support'].items())
        out.append(f"PCGamingWiki: {sup or 'no support table'}  {pw['url']}")
        for f in pw['fixes']:
            out.append(f"  fix: {f}")
    return out


def cmd_game_tips(ident: str, as_json: bool) -> None:
    """Everything the details panel shows beyond the basics, for any game:
    protongen's saved tuning, ProtonDB tier (Steam games), and the protongen
    recipes that fit this machine."""
    from nexus import tips, protondb
    g = _game_by_id(ident, light=True)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    from nexus import compat
    hw = tips.hardware()
    tier = None
    pdb = None
    store = compat.store_appid(g)            # a repack's comes from its umu GAMEID
    if store:
        pdb = protondb.tiers([store]).get(store)
        tier = (pdb or {}).get('tier')
    pg = None
    if g.get('source') == 'nexus':
        hit = protongen_memory_for(g)
    else:
        pid = _protongen_id(g)
        hit = None
        try:
            mem = __import__('tomllib').loads(PROTONGEN_STATE.read_text()).get('game_memory', {})
            cfg = mem.get(str(pid)) if pid else None
            hit = ('protongen', cfg) if isinstance(cfg, dict) else None
        except (OSError, ValueError):
            pass
    if hit:
        prof = profile_from_protongen_config(hit[1])
        pg = {'source': hit[0], 'launch_options': format_launch_options(prof),
              'proton': prof.get('proton')}
    cur = format_launch_options(g.get('launch') or {}) if g.get('source') == 'nexus' else None
    recipes = tips.recipes_for(hw, tier)
    info = {'id': ident, 'protondb': pdb, 'protongen': pg, 'protongen_id': _protongen_id(g),
            'recipes': recipes, 'current': cur,
            'gpu': next((f"{x['name']} ({x['gen'] or x['vendor']})" for x in hw['gpus']
                         if not x['integrated']), None),
            'llm': _llm_config() is not None, 'gamedb': None,
            'store_appid': store, **compat.facts(store)}
    try:
        from nexus import gamedb
        appid = g['appid'] if g.get('source') == 'steam' else g.get('steam_appid')
        if appid and str(appid).isdigit():
            info['gamedb'] = gamedb.lookup([int(appid)]).get(int(appid))   # cache only
    except Exception:
        pass
    if not ((info.get('gamedb') or {}).get('hltb') or {}).get('main'):
        from nexus import hltb
        name = g.get('display_name') or g.get('name')
        if g.get('source') == 'steam' and not name:
            full = _game_by_id(ident)
            name = (full or {}).get('display_name') or (full or {}).get('name')
        info['hltb'] = hltb.lookup(name) if name else None
    if as_json:
        print(json.dumps(info))
        return
    if pdb:
        print(f"ProtonDB: {pdb.get('tier') or 'no reports'}  {pdb['url']}")
    if info.get('hltb'):
        h = info['hltb']
        print(f"HowLongToBeat: main {h['main'] or '?'} h · +extras {h['plus'] or '?'} h · "
              f"100% {h['complete'] or '?'} h")
    for line in compat_lines(info):
        print(line)
    if pg:
        print(f"protongen ({pg['source']}): {pg['launch_options']}")
    print(f"Suggestions for {info['gpu']}:")
    for r in recipes:
        print(f"  - {r['name']}: {r['launch_options']}")


def cmd_apply_recipe(slug: str, name: str, config: dict) -> None:
    """Merge a protongen recipe into a Nexus game's launch options (its env
    vars override same-named ones; its wrappers are added if missing)."""
    from nexus import tips
    entry = _require_entry(slug)
    rec = next((r for r in tips.recipes_for(tips.hardware(), None, ('profile', 'fix'))
                if r['name'] == name), None) or next(
        (r for r in tips.load_recipes()[0] if r.get('name') == name), None)
    if not rec:
        print(f"No recipe named {name!r}", file=sys.stderr)
        sys.exit(1)
    add = parse_launch_options(tips.recipe_launch_string(rec))
    prof = {**empty_profile(), **(entry.get('launch') or {})}
    env = {k: v for k, v in prof.get('env', [])}
    env.update({k: v for k, v in add['env']})
    prof['env'] = [[k, v] for k, v in env.items()]
    wr = list(prof.get('wrappers') or [])
    for seg in split_wrappers(add['wrappers']):
        if seg[0] not in wr:
            wr = seg + wr if seg[0] == 'gamescope' else wr + seg
    prof['wrappers'] = wr
    prof = _save_profile(slug, prof, f"recipe '{name}'")
    print(f"Applied '{name}' to {slug}: {format_launch_options(prof)}")
    cmd_apply(slug, config)


def _llm_config() -> dict | None:
    """protongen's local-LLM settings, only when enabled and pointing at this
    machine (log text never leaves it)."""
    try:
        st = __import__('tomllib').loads(PROTONGEN_STATE.read_text())
    except (OSError, ValueError):
        return None
    ep = st.get('llm_endpoint') or 'http://127.0.0.1:1234/v1'
    if not st.get('llm_enabled'):
        return None
    if not re.match(r'^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)', ep):
        return None
    return {'endpoint': ep.rstrip('/'), 'model': st.get('llm_model') or 'gpt-oss-20b'}


def cmd_explain_log(ident: str) -> None:
    """Ask the local LLM protongen is configured with to explain a game's
    latest launch/install log (error lines + tail). Loopback endpoints only."""
    import urllib.request
    cfg = _llm_config()
    if not cfg:
        print("Local AI is off. Enable it in protongen (Settings → AI log coach) with a "
              "local endpoint such as LM Studio.", file=sys.stderr)
        sys.exit(1)
    path = launch_log_path(ident)
    if not path.exists():
        path = latest_install_log(ident) or path
    try:
        text = path.read_text(errors='replace')[-64_000:]
    except OSError:
        print(f"No log for {ident!r} yet", file=sys.stderr)
        sys.exit(1)
    errors = [l for l in text.splitlines()
              if re.search(r'\b(err|error|fail|fatal|exception|crash|unhandled)\b', l, re.I)][-80:]
    prompt = ("You help Linux gamers fix games running under Proton/Wine. Explain in plain "
              "words what went wrong and suggest concrete fixes (Proton env vars, a different "
              "Proton build, missing dependencies). Be brief.\n\nError lines:\n"
              + "\n".join(errors) + "\n\nLog tail:\n" + text[-12_000:])
    body = json.dumps({'model': cfg['model'], 'messages': [{'role': 'user', 'content': prompt}],
                       'temperature': 0.2}).encode()
    req = urllib.request.Request(cfg['endpoint'] + '/chat/completions', data=body,
                                 headers={'Content-Type': 'application/json'})
    print(f"Asking {cfg['model']} about {path.name}...", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            answer = json.loads(r.read())['choices'][0]['message']['content']
    except (OSError, ValueError, KeyError, IndexError) as e:
        print(f"The local model didn't answer: {e}", file=sys.stderr)
        sys.exit(1)
    print()
    print(answer.strip())


def cmd_news(refresh: bool, as_json: bool) -> None:
    from nexus import news, steamlib, protons as PR, net
    games = [{'appid': g['appid'], 'name': g['display_name']}
             for g in steamlib.scan(STEAM_ROOT)]
    rel = []
    for fam in ('ge', 'cachyos'):
        try:
            for r in PR.releases(fam)['releases'][:3]:
                rel.append({**r, 'family_label': PR.FAMILIES[fam]['label']})
        except net.NetError:
            pass
    data = news.collect(games, rel, refresh)
    data['feeds'] = news.feeds()
    if as_json:
        print(json.dumps(data))
        return
    for i in data['items'][:40]:
        print(f"{(i.get('date') or '')[:10]}  [{i['source']}] {i['title']}")
        if i.get('link'):
            print(f"            {i['link']}")
    bad = [s for s in data['sources'] if s['error']]
    for b in bad:
        print(f"  [!] {b['name']}: {b['error']}", file=sys.stderr)


def cmd_news_feed(action: str, url: str, name: str | None) -> None:
    from nexus import news
    # Feeds are fetched over https only (nexus/net.py); removal accepts any
    # URL so an old http:// entry can still be taken out.
    if action == 'add' and not re.match(r'^https://', url):
        print("Feed URL must start with https://", file=sys.stderr)
        sys.exit(2)
    user = [f for f in news.feeds() if f.get('user')]
    if action == 'add':
        if any(f['url'] == url for f in user):
            print("Already added.")
            return
        user.append({'name': name or re.sub(r'^https?://(www\.)?', '', url).split('/')[0],
                     'url': url})
    else:
        user = [f for f in user if f['url'] != url]
    news.save_user_feeds(user)
    print(f"{'Added' if action == 'add' else 'Removed'} feed {url}")


def _ago(ts: float) -> str:
    mins = int((datetime.now().timestamp() - ts) // 60)
    if mins < 1:
        return 'just now'
    if mins < 60:
        return f'{mins} min ago'
    if mins < 48 * 60:
        return f'{mins // 60} h ago'
    return f'{mins // 1440} days ago'


def cmd_gamedb(section: str, refresh: bool, as_json: bool, time_budget: str = '1to3') -> None:
    """Wishlist deals, upcoming releases and backlog picks from Game-db
    (cached 45 min; --refresh to bypass). section: all|deals|upcoming|backlog."""
    from nexus import gamedb, steamlib
    s = gamedb.settings()
    if not gamedb.configured(s):
        url = s['url'] or gamedb.DEFAULT_URL
        why = s['url_error'] or ("Game-db isn't set up — create a token on "
                                 f"{url}/account and add it in Settings "
                                 "([gamedb] token / NEXUS_GAMEDB_TOKEN, [gamedb] enabled = true)")
        if as_json:                   # the GUI renders the error itself
            print(json.dumps({'configured': False, 'error': {'kind': 'config', 'message': why},
                              'url': s['url']}))
            return
        print(why, file=sys.stderr)
        sys.exit(2)
    try:
        res = gamedb.summary(refresh=refresh)
    except gamedb.GameDbError as e:
        if as_json:
            print(json.dumps({'configured': True, 'url': s['url'],
                              'error': {'kind': e.kind, 'message': str(e)}}))
            return
        print(f"Game-db: {e}", file=sys.stderr)
        sys.exit(1)
    data = res['data']
    if as_json:
        installed = sorted({int(g['appid']) for g in steamlib.scan(STEAM_ROOT)
                            if str(g.get('appid', '')).isdigit()})
        out = {'configured': True, 'url': s['url'], 'fetched_at': res['fetched_at'],
               'stale': res['stale'], 'error': res['error'], 'installed_appids': installed}
        try:
            out['owned_appids'] = sorted(_owned_store_ids())
        except Exception:                                 # noqa: BLE001 — optional
            out['owned_appids'] = []
        if section in ('deals', 'all'):
            from nexus import repackindex
            out['repacks'] = repackindex.wishlist_matches(data.get('wishlist') or [])
        if section == 'deals':
            out['wishlist'] = data.get('wishlist', [])
            out['radar'] = data.get('radar')
        elif section == 'upcoming':
            out['upcoming'] = data.get('upcoming', [])
        elif section == 'backlog':
            out['tonight'] = (data.get('tonight') or {}).get(time_budget, [])
            out['backlog'] = data.get('backlog')
        else:
            out['summary'] = data
        print(json.dumps(out))
        return
    if res['error']:
        print(f"  [!] {res['error']['message']} — showing data from {_ago(res['fetched_at'])}",
              file=sys.stderr)
    cur = data.get('currency') or ''
    radar = data.get('radar') or {}
    if section in ('all', 'deals'):
        sale = [g for g in data.get('wishlist', []) if (g.get('price') or {}).get('onSale')]
        print(f"Wishlist deals — {len(sale)} on sale"
              + (f", {radar['totalNow']:.2f} {cur} for all of them" if radar.get('totalNow') else '')
              + f" (region {data.get('region')}, synced {_ago(res['fetched_at'])})")
        for g in sale:
            p = g['price']
            best = p.get('best') or {}
            shop = best.get('shop') or ('keyshop' if best.get('category') == 'keyshop' else '')
            cut = f"-{p['cutPct']}%" if p.get('cutPct') else ''
            atl = 'ATL' if p.get('atHistoricalLow') else ''
            print(f"  {p['currentStr']:>10} {cut:>5} {atl:3}  {g['title'][:44]:44}  "
                  f"low {p.get('historicalLowStr') or '—':>9}  {shop}")
        if not sale:
            print("  nothing on sale right now")
    if section in ('all', 'upcoming'):
        up = data.get('upcoming', [])
        print(f"\nUpcoming — {len(up)} games")
        for g in up:
            print(f"  {gamedb.release_label(g):>14}  {g['title']}"
                  + ('  (owned)' if g.get('owned') else ''))
    if section in ('all', 'backlog'):
        b = data.get('backlog') or {}
        print(f"\nBacklog — {b.get('beaten', 0)} beaten, {b.get('inProgress', 0)} in progress, "
              f"{b.get('notStarted', 0)} not started, ~{b.get('backlogHours', 0)} h left")
        labels = {'lt1h': 'under an hour', '1to3': '1–3 hours', '3plus': '3+ hours',
                  'long': 'a long session'}
        print(f"Tonight ({labels.get(time_budget, time_budget)}):")
        for pick in (data.get('tonight') or {}).get(time_budget, []):
            print(f"  {pick['title']}  — {'; '.join(pick.get('reasons') or [])}")


# config.toml keys that hold credentials: (section, key, what it is)
CONFIG_SECRETS = (('steamgriddb', 'api_key', 'SteamGridDB key'),
                  ('gamedb', 'token', 'Game-db token'),
                  ('torrent', 'password', 'torrent client password'),
                  ('notify', 'ntfy_token', 'ntfy token'))


def config_secrets_doctor(add, group: str) -> None:
    """--doctor: warn when config.toml holds a credential and others can read it
    (the GUI writes it 0600; a hand-made one is usually 0644)."""
    try:
        import tomllib
        data = tomllib.loads(CONFIG_PATH.read_text())
        mode = CONFIG_PATH.stat().st_mode & 0o777
    except (OSError, ValueError, ImportError):
        return
    held = [what for sec, key, what in CONFIG_SECRETS
            if isinstance(data.get(sec), dict) and data[sec].get(key)]
    if held and mode & 0o077:
        add(group, 'warn', 'Config file permissions',
            f"{CONFIG_PATH} is {oct(mode)} and holds your {', '.join(held)}",
            f"chmod 600 {CONFIG_PATH} {CONFIG_PATH}.bak*")


def gamedb_doctor(add, group: str) -> None:
    """Game-db checks for --doctor; cache-only, never touches the network."""
    from nexus import gamedb
    s = gamedb.settings()
    if not s['enabled'] and not s['token']:
        add(group, 'info', 'Game-db', 'not set up (optional) — wishlist deals, releases, backlog',
            'Create a token on Game-db (/account) and add it in Settings')
        return
    if s['url_error']:
        add(group, 'warn', 'Game-db', s['url_error'], 'Fix [gamedb] url in Settings')
        return
    if not s['token']:
        add(group, 'warn', 'Game-db', 'enabled but no token',
            f"Create one on {s['url']}/account and add it in Settings")
        return
    st = gamedb.last_state()
    try:
        res = gamedb.summary(cache_only=True)
    except gamedb.GameDbError:
        res = None
    err = (st.get('error') or {})
    if err.get('kind') == 'auth':
        add(group, 'warn', 'Game-db', err.get('message', 'token rejected'),
            f"Create a new token on {s['url']}/account")
    elif res:
        d = res['data']
        on_sale = (d.get('radar') or {}).get('onSaleCount', 0)
        add(group, 'ok', 'Game-db', f"{s['url']} — synced {_ago(res['fetched_at'])} · "
                                    f"{on_sale} on sale"
            + (f" · last error: {err['message']}" if err else ''))
        mine = (resolve_steam_account()[0] or {}).get('accountid')
        theirs = (d.get('user') or {}).get('steamId64')
        if mine and theirs and gamedb.steamid64(mine) != str(theirs):
            add(group, 'info', 'Game-db Steam account',
                f"Game-db tracks Steam {theirs}; Nexus is using {gamedb.steamid64(mine)}")
    else:
        add(group, 'ok' if not err else 'warn', 'Game-db',
            f"{s['url']} — not fetched yet" + (f" ({err['message']})" if err else ''),
            'Open the Deals page or run nexus-cli --deals')


PREFIX_TOOLS = ('winecfg', 'regedit', 'winetricks', 'kill')


def cmd_prefix_tool(slug: str, tool: str, config: dict) -> None:
    """Run a Wine tool against a game's prefix with the same runner + Proton the
    game plays with: winecfg, regedit, winetricks (GUI), or kill (stop every
    process in the prefix via wineserver -k)."""
    entry = _require_entry(slug)
    if tool not in PREFIX_TOOLS:
        print(f"Unknown tool '{tool}' (choose: {', '.join(PREFIX_TOOLS)})", file=sys.stderr)
        sys.exit(1)
    exe, game_dir, wine_prefix = _entry_paths(entry)
    if not wine_prefix or not wine_prefix.is_dir():
        print(f"'{slug}' has no prefix on disk", file=sys.stderr)
        sys.exit(1)
    globals()['log'] = Logger()
    proton = resolve_game_proton(entry, config)
    runner = select_runner(config.get('runner', 'auto'), proton)
    if runner != 'wine' and not game_dir:
        runner = 'wine'
    env = {**wine_env(), 'WINEPREFIX': str(wine_prefix), 'WINEDEBUG': '-all'}
    if tool == 'kill':
        bins = _proton_wine_bins(proton) if proton and runner != 'wine' else {}
        server = bins.get('WINESERVER') or shutil.which('wineserver') or 'wineserver'
        try:
            rc = subprocess.run([server, '-k'], env=env, timeout=30).returncode
        except subprocess.TimeoutExpired:
            rc = -1
        print(f"Stopped processes in {wine_prefix}" if rc == 0
              else f"wineserver -k exited {rc} (nothing running?)")
        return
    if runner == 'umu':
        env.update(_umu_env({}, wine_prefix, proton))
        cmd = ['umu-run', tool]
    elif runner == 'proton':
        env.update({'STEAM_COMPAT_DATA_PATH': str(game_dir),
                    'STEAM_COMPAT_CLIENT_INSTALL_PATH': str(STEAM_ROOT)})
        if tool == 'winetricks':
            env.update(_proton_wine_bins(proton))
            cmd = ['winetricks']
        else:
            cmd = [str(proton), 'run', tool]
    else:
        cmd = [tool] if tool == 'winetricks' else ['wine', tool]
    print(f"Running {tool} for '{slug}' ({' '.join(cmd)})...")
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_set_exe(slug: str, exe_path: str, config: dict) -> None:
    """Point a game at a different executable (the largest-exe guess was
    wrong), then re-apply. The Steam appid is kept, so saves/art survive."""
    entry = _require_entry(slug)
    exe = Path(exe_path).expanduser().resolve()
    if not exe.is_file() or exe.suffix.lower() != '.exe':
        print(f"Error: '{exe}' is not an .exe file", file=sys.stderr)
        sys.exit(1)
    update_registry_entry(slug, exe=str(exe))
    print(f"'{slug}' now launches {exe}")
    cmd_apply(slug, config)


def cmd_rename(slug: str, name: str, config: dict) -> None:
    """Change a game's display name everywhere (the slug, and so the install
    dir and Steam appid, stay the same)."""
    _require_entry(slug)
    name = ' '.join(name.split())
    if not name:
        print("Error: empty name", file=sys.stderr)
        sys.exit(1)
    update_registry_entry(slug, display_name=name)
    print(f"'{slug}' renamed to {name!r}")
    cmd_apply(slug, config)


def _local_artwork(slug: str) -> dict:
    """Build an artwork dict from already-downloaded files in ARTWORK_DIR/<slug>."""
    out: dict[str, Path] = {}
    d = ARTWORK_DIR / slug
    if d.is_dir():
        for kind in ('cover', 'hero', 'logo', 'icon'):
            hits = sorted(d.glob(f'{kind}.*'))
            if hits:
                out[kind] = hits[0]
    return out


def cmd_add_steam(slug: str, config: dict) -> None:
    """Add a Steam shortcut for an already-managed game (by slug). Steam runs
    the game .exe through a forced Proton, with the prefix redirected to the
    install."""
    entry = registry_get(slug)
    if not entry or not entry.get('launcher'):
        print(f"'{slug}' is not a managed game with a launcher", file=sys.stderr)
        sys.exit(1)
    globals()['log'] = Logger()
    artwork = _local_artwork(slug)
    if not artwork and config.get('api_key'):
        artwork = fetch_artwork(entry['display_name'], slug, config) or {}
    appid = sync_steam(entry, config, resolve_game_proton(entry, config), artwork or None)
    if not appid:
        sys.exit(1)
    set_registry_steam_appid(slug, appid)


def cmd_add_heroic(slug: str, config: dict) -> None:
    """Add an already-managed game (by slug) to Heroic as a sideloaded app,
    using its registered exe + prefix + cached artwork."""
    entry = registry_get(slug)
    if not entry:
        print(f"'{slug}' is not a managed game", file=sys.stderr)
        sys.exit(1)
    globals()['log'] = Logger()
    artwork = _local_artwork(slug)
    if not artwork and config.get('api_key'):
        artwork = fetch_artwork(entry['display_name'], slug, config) or {}
    app_name = sync_heroic(entry, config, resolve_game_proton(entry, config), artwork or None)
    if app_name:
        update_registry_entry(slug, heroic_app_name=app_name)


def cmd_refetch_art(slug: str, config: dict) -> None:
    entry = registry_get(slug)
    if not entry:
        print(f"'{slug}' is not a managed game", file=sys.stderr)
        sys.exit(1)
    log_local = Logger()
    globals()['log'] = log_local
    art = fetch_artwork(entry['display_name'], slug, config)
    if not art:
        print("No artwork (missing API key, no match, or offline).")
        return
    print(f"Fetched: {', '.join(sorted(k for k in art if not k.endswith('_url')))}")
    appid = entry.get('steam_appid')
    if appid:
        cfg_dir = find_steam_userdata(announce=True)
        if cfg_dir:
            _copy_grid_art(cfg_dir / 'grid', appid, art)
            print("Updated Steam grid artwork.")


def _launcher_display_name(launch_path: Path, slug: str,
                           steam_names: set[str] | None = None) -> str:
    """Best display name for a game: prefer the launcher's '# Game: <name>'
    comment, then a matching Steam shortcut AppName, then a prettified slug."""
    try:
        for line in launch_path.read_text().splitlines():
            if line.startswith('# Game: '):
                return line[len('# Game: '):].strip() or slug
    except OSError:
        pass
    for n in (steam_names or ()):
        if make_slug(n) == slug:
            return n
    return slug.replace('-', ' ').title()


def cmd_reindex(install_base: Path) -> None:
    """Backfill the registry from a filesystem scan of managed launchers."""
    steam_names = _steam_appnames()
    added = 0
    for slug, game_dir in _installed_games(install_base):
        if registry_get(slug):
            continue
        launch = game_dir / 'launch.sh'
        exe = find_game_exe(game_dir / 'pfx') or find_game_exe(game_dir)
        register_game(
            slug=slug, display_name=_launcher_display_name(launch, slug, steam_names),
            exe=exe, compatdata=game_dir, wine_prefix=game_dir / 'pfx', launcher=launch,
            source='install', install_base=install_base, steam_appid=None,
            has_desktop=_desktop_path(slug).exists(), artwork_dir=ARTWORK_DIR / slug)
        added += 1
    # Also register any external launchers (from --add-steam) not under install_base
    for sh in sorted(LAUNCHERS_DIR.glob('*.sh')) if LAUNCHERS_DIR.is_dir() else []:
        slug = sh.stem
        if registry_get(slug):
            continue
        register_game(
            slug=slug, display_name=_launcher_display_name(sh, slug, steam_names),
            exe=None, compatdata=None, wine_prefix=None, launcher=sh, source='add-steam',
            install_base=install_base, steam_appid=None,
            has_desktop=_desktop_path(slug).exists(), artwork_dir=ARTWORK_DIR / slug)
        added += 1
    print(f"Reindexed: {added} game(s) added to the registry.")


def scan_repacks(scan_dir: Path) -> list[dict]:
    """Look for extracted FitGirl repack folders directly under scan_dir (e.g.
    ~/Downloads) — a folder counts as a candidate if it has a setup.exe, the
    same signal find_setup_exe uses to run the installer. Cheap: one dir
    listing, plus a size scan only for folders that pass that check.

    `complete` is False while a download is still running; `update_for`
    names the installed game this is a different release of (its
    fingerprint differs from the one recorded at install)."""
    if not scan_dir.is_dir():
        return []
    known = load_registry()['games']
    candidates = []
    try:
        entries = sorted(scan_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or not find_setup_exe(entry):
            continue
        info = repack_info(entry)
        slug = info['slug']
        fp = repack_fingerprint(entry)
        recorded = (known.get(slug) or {}).get('repack_fingerprint')
        try:
            mtime = int(entry.stat().st_mtime)
        except OSError:
            mtime = None
        candidates.append({
            'path': str(entry),
            'name': info['name'],
            'slug': slug,
            'version': info['version'],
            'size_bytes': dir_size_bytes(entry),
            'installed': slug in known,
            'fingerprint': fp,
            'mtime': mtime,
            'complete': repack_complete(entry),
            'update_for': slug if (slug in known and fp and recorded and fp != recorded)
                          else None,
            'same_release': bool(fp and recorded and fp == recorded),
        })
    return candidates


def cmd_scan(scan_dirs: list[Path], as_json: bool = False) -> None:
    candidates = [c for d in scan_dirs for c in scan_repacks(d)]
    if as_json:
        print(json.dumps({'scan_dir': str(scan_dirs[0]),
                          'scan_dirs': [str(d) for d in scan_dirs],
                          'candidates': candidates}))
        return
    where = ', '.join(str(d) for d in scan_dirs)
    if not candidates:
        print(f"No repack folders found in {where}")
        return
    print(f"Repack folders found in {where}:\n")
    for c in candidates:
        tag = ("  (update for the installed game)" if c['update_for'] else
               "  (already installed)" if c['installed'] else "")
        if not c['complete']:
            tag += "  (still downloading)"
        print(f"  {c['name']}  —  {c['size_bytes'] / 1e9:.1f} GB{tag}")
        print(f"    {c['path']}")
    print(f"\nInstall one with:  nexus-cli \"<path>\"")


def steam_account_id() -> int | None:
    """The Steam account Nexus works with (see resolve_steam_account)."""
    cfg = find_steam_userdata()
    try:
        return int(cfg.parent.name) if cfg else None
    except ValueError:
        return None


ACHIEVEMENT_COUNTS = NX.CACHE_DIR / 'achievements.json'


def cmd_achievements(ident: str, as_json: bool) -> int:
    """Achievements from Steam's own stats cache: one game's list, or the
    newest unlocks across every game the account has run."""
    from nexus import achievements as A
    entry = registry_get(ident) if valid_slug(ident) else None
    if entry is not None:                      # a repack: its Steam emulator's files
        from nexus import emuach, compat
        r = (emuach.status(Path(entry['wine_prefix']), STEAM_ROOT,
                           appid=compat.store_appid(entry)) if entry.get('wine_prefix') else None) \
            or {'appid': None, 'total': 0, 'unlocked': 0, 'items': []}
        if as_json:
            print(json.dumps(r))
            return 0
        if not r['total']:
            print("No achievement file from the game's Steam emulator yet.")
            return 0
        print(f"{r['unlocked']} of {r['total']} achievements ({r.get('emulator')})")
        for a in r['items']:
            mark = '✓' if a['unlocked'] else ' '
            print(f"  [{mark}] {a['title'] if a['unlocked'] or not a['hidden'] else 'Hidden'}")
        return 0
    account = steam_account_id()
    if account is None:
        print("No Steam account found.", file=sys.stderr)
        return 1
    if ident == 'recent':
        stats = STEAM_ROOT / 'appcache' / 'stats'
        appids = [int(p.stem.split('_')[1]) for p in stats.glob('UserGameStatsSchema_*.bin')
                  if p.stem.split('_')[1].isdigit()]
        out = {'recent': A.recent(STEAM_ROOT, account, appids, n=12)}
        if as_json:
            print(json.dumps(out))
        else:
            for a in out['recent']:
                when = datetime.fromtimestamp(a['time']).strftime('%Y-%m-%d')
                print(f"  {when}  {a['title']}  ({a['appid']})")
        return 0
    appid = ident.removeprefix('steam-')
    if not appid.isdigit():
        print(f"Not a Steam game: {ident!r} (achievements come from Steam)", file=sys.stderr)
        return 1
    r = A.achievements(STEAM_ROOT, account, int(appid)) or \
        {'appid': int(appid), 'total': 0, 'unlocked': 0, 'items': []}
    if as_json:
        print(json.dumps(r))
        return 0
    print(f"{r['unlocked']} of {r['total']} achievements")
    for a in r['items']:
        mark = '✓' if a['unlocked'] else ' '
        title = a['title'] if a['unlocked'] or not a['hidden'] else "Hidden achievement"
        print(f"  [{mark}] {title}")
    return 0


def cmd_trash_repack(path: Path) -> int:
    """Move a repack folder to the Trash. Only a folder that looks like one
    (it has a setup.exe) — this is reachable from a GUI button."""
    from nexus import cleanup as C
    if not path.is_dir() or not find_setup_exe(path):
        print(f"Not a repack folder (no setup.exe): {path}", file=sys.stderr)
        return 1
    try:
        C.trash(path)
    except OSError as e:
        print(f"Could not move {path} to the Trash: {e}", file=sys.stderr)
        return 1
    print(f"Moved {path.name} to the Trash.")
    return 0


def cmd_discard_incomplete(install_base: Path, slug: str) -> int:
    """Trash an unfinished install's folder — only one that has the marker and
    isn't a library game, so a working install can never go this way."""
    from nexus import cleanup as C
    game_dir = install_base / slug
    if slug in load_registry()['games']:
        print(f"'{slug}' is in the library; uninstall it instead.", file=sys.stderr)
        return 1
    if read_incomplete_marker(game_dir) is None:
        print(f"No unfinished install at {game_dir}", file=sys.stderr)
        return 1
    try:
        C.trash(game_dir)
    except OSError as e:
        print(f"Could not move {game_dir} to the Trash: {e}", file=sys.stderr)
        return 1
    print(f"Moved the unfinished install of {slug} to the Trash.")
    return 0


def cmd_detect(path: Path, name_override: str | None) -> None:
    """Print a repack folder's detected name/slug and whether it's already a
    managed install, as JSON. Read-only; used by the GUI to warn before a
    duplicate install starts."""
    if name_override:
        name, slug = name_override, make_slug(name_override)
    elif path.is_dir():
        name, slug = detect_game_name(path)
    else:
        name, slug = path.name, make_slug(path.name)
    entry = load_registry()['games'].get(slug)
    fp = repack_fingerprint(path) if path.is_dir() else None
    recorded = (entry or {}).get('repack_fingerprint')
    print(json.dumps({'path': str(path), 'name': name, 'slug': slug,
                      'installed': entry is not None,
                      'update': bool(fp and recorded and fp != recorded)}))


def cmd_repair_launchers(install_base: Path, config: dict, slug: str | None = None) -> None:
    """Regenerate launch.sh for managed games from their registry entry, launch
    profile and Proton (resolve_game_proton — the same build Steam and Heroic
    use, so a repair never silently moves a game to a different Proton). Does
    not touch Steam/Heroic — use --apply-launch <slug> for that."""
    global log
    log = Logger()
    reg = load_registry()
    if slug and slug not in reg['games']:
        print(f"'{slug}' is not a managed game", file=sys.stderr)
        sys.exit(1)
    entries = [reg['games'][slug]] if slug else list(reg['games'].values())
    n = 0
    for e in entries:
        proton = resolve_game_proton(e, config)
        if proton and not e.get('proton'):
            update_registry_entry(e['slug'], proton=proton.parent.name)
        path = regen_launch_sh(e, config, proton)
        if path:
            print(f"  regenerated {path}  (Proton: {proton.parent.name if proton else 'none'})")
            n += 1
    print(f"Repaired {n} launcher(s).")


def add_existing_to_steam(path: Path, name: str | None, args, config: dict) -> None:
    """Register an already-installed game (by dir or exe) into Steam + desktop
    menu, generating a launcher for its current prefix. No files are moved."""
    global log
    log = Logger()  # plain console logger for this standalone command

    path = path.expanduser().resolve()
    if path.is_dir():
        exe = find_exe_in_dir(path)
        if not exe:
            print(f"Error: no game .exe found under {path}", file=sys.stderr)
            sys.exit(1)
    elif path.suffix.lower() == '.exe' and path.exists():
        exe = path
    else:
        print(f"Error: '{path}' is not a game folder or .exe", file=sys.stderr)
        sys.exit(1)

    display_name = name or exe.stem
    slug = make_slug(display_name)
    clash = registry_get(slug)
    if clash and clash.get('exe') and Path(clash['exe']) != exe:
        print(f"Error: '{slug}' is already a managed game ({clash['exe']}). "
              f"Pick a different --name.", file=sys.stderr)
        sys.exit(1)
    compatdata, wine_prefix = detect_compatdata(exe)
    proton = resolve_game_proton(clash, config)
    runner = select_runner(config.get('runner', 'auto'), proton)
    # umu/proton launchers only make sense for an exe inside a Proton-style
    # prefix (pfx/ ancestor); a raw-wine prefix falls back to plain wine.
    if runner in ('umu', 'proton') and not compatdata:
        runner = 'wine'
    runner_label = (f"umu-run + {proton.parent.name}" if runner == 'umu'
                    else proton.parent.name if runner == 'proton'
                    else 'Wine (system)')

    log.section("Add to Steam")
    log.print(f"  Game:        {display_name}")
    log.print(f"  Slug:        {slug}")
    log.print(f"  Exe:         {exe}")
    log.print(f"  Runner:      {runner_label}")
    if compatdata:
        log.print(f"  Compat data: {compatdata}")
    log.print(f"  Wine prefix: {wine_prefix}")

    if proton and not compatdata:
        log.print("\n  [!] Proton is available but this exe isn't inside a Proton")
        log.print("      prefix (no pfx/ ancestor) — the launcher will use raw wine.")

    launch = write_launch_script(LAUNCHERS_DIR / f"{slug}.sh", compatdata,
                                 wine_prefix, exe, slug, proton, display_name,
                                 runner=runner, launch=(clash or {}).get('launch'))
    log.print(f"\n  Launcher:    {launch}")

    artwork = None
    if not args.no_artwork:
        log.print("\nFetching artwork from SteamGridDB...")
        artwork = _best_effort("Artwork", fetch_artwork, display_name, slug, config)
        if artwork:
            log.print(f"  Got: {', '.join(sorted(k for k in artwork if not k.endswith('_url')))}")
        else:
            log.print("  No artwork (missing API key, no match, or offline) — skipping.")

    steam_appid_val = None
    if not args.no_steam and config.get('steam', True):
        if exe and compatdata:
            prof = (clash or {}).get('launch')
            steam_appid_val = add_steam_shortcut_native(
                display_name, slug, exe, compatdata, proton, artwork,
                launch_options=(format_launch_options(prof)
                                if profile_owns_options(prof) else None))
        else:
            log.print("  [i] Steam: need a game exe inside a Proton prefix — skipping.")
    desktop_path = None
    if not args.no_desktop and config.get('desktop', True):
        desktop_path = write_desktop_entry(display_name, slug, launch, artwork)
    heroic_app_name = None
    if not args.no_heroic and config.get('heroic', True):
        prof = (clash or {}).get('launch')
        heroic_app_name = add_heroic_entry(
            display_name, slug, exe, wine_prefix, proton, artwork,
            force_proton=_proton_is_explicit(clash or {}, config),
            launch=prof if profile_owns_options(prof) else None)

    register_game(
        slug=slug, display_name=display_name, exe=exe, compatdata=compatdata,
        wine_prefix=wine_prefix, launcher=launch, source='add-steam',
        install_base=None, steam_appid=steam_appid_val,
        has_desktop=bool(desktop_path), artwork_dir=ARTWORK_DIR / slug,
        heroic_app_name=heroic_app_name,
        proton=proton.parent.name if proton else None)

    log.print("\nDone. Restart Steam/Heroic to see the game in your library.")


# ---------------------------------------------------------------------------
# Doctor — environment self-check
# ---------------------------------------------------------------------------

def cmd_list_steam_accounts() -> None:
    """Show every Steam account on this machine and which one gets shortcuts."""
    accounts = steam_accounts()
    if not accounts:
        print("No Steam userdata profiles found under", STEAM_ROOT / 'userdata')
        return
    chosen, why = resolve_steam_account()
    chosen_id = chosen['accountid'] if chosen else None
    print(f"Steam accounts under {STEAM_ROOT / 'userdata'}:\n")
    for a in accounts:
        mark = '->' if a['accountid'] == chosen_id else '  '
        flags = []
        if a.get('logged_in'):
            flags.append('signed in')
        if a.get('autologin'):
            flags.append('auto-login')
        if a.get('shortcut_count'):
            flags.append(f"{a['shortcut_count']} shortcut"
                         f"{'s' if a['shortcut_count'] != 1 else ''}")
        print(f"  {mark} {steam_account_label(a)}"
              + (f"  [{', '.join(flags)}]" if flags else ''))
    if chosen:
        print(f"\nShortcuts go to {steam_account_label(chosen)} — {why}.")
    print("\nTo pin a different one, either:")
    print("  nexus-cli --steam-account <accountid|login name> ...")
    print(f"or add to {CONFIG_PATH} under [integrations]:")
    print('  steam_account = "<accountid or login name>"')


ORPHAN_HOLDING_DIR = NX.ORPHAN_HOLDING_DIR

# Renamed-aside by _redirect_compatdata when it found something already
# sitting where a redirect symlink needed to go (name: "<appid>.bak-<ts>").
_ORPHAN_BAK_RE = re.compile(r'^(\d+)\.bak-\d{8}-\d{6}$')


def _all_shortcut_names() -> dict[int, str]:
    """{unsigned appid: AppName} for every non-Steam shortcut on every account."""
    try:
        import vdf
    except ImportError:
        return {}
    out: dict[int, str] = {}
    for acct in steam_accounts():
        path = acct['config_dir'] / 'shortcuts.vdf'
        try:
            with open(path, 'rb') as f:
                shortcuts = vdf.binary_load(f).get('shortcuts', {})
        except VDF_ERRORS:
            continue
        for e in shortcuts.values():
            if isinstance(e, dict) and 'appid' in e:
                out[_shortcut_uid(e)] = e.get('AppName') or str(_shortcut_uid(e))
    return out


def _all_shortcut_appids() -> set[int] | None:
    """Unsigned appids of every non-Steam shortcut across every local Steam
    account — compatdata is shared at the steamapps level regardless of which
    account's shortcuts.vdf references a given id. None when any account's
    file couldn't be read (or vdf is missing): then nobody can say a
    shortcut prefix is unused."""
    try:
        import vdf
    except ImportError:
        return None
    out: set[int] = set()
    for acct in steam_accounts():
        path = acct['config_dir'] / 'shortcuts.vdf'
        if not path.exists():
            continue
        try:
            with open(path, 'rb') as f:
                shortcuts = vdf.binary_load(f).get('shortcuts', {})
            shortcuts = dict(shortcuts)
        except VDF_ERRORS:
            return None
        for e in shortcuts.values():
            if isinstance(e, dict) and 'appid' in e:
                try:
                    out.add(int(e['appid']) & 0xFFFFFFFF)
                except (TypeError, ValueError):
                    pass
    return out


def _steam_libraries() -> list[Path]:
    from nexus import steamlib
    return steamlib.library_folders(STEAM_ROOT)


def _quarantine_dir(lib: Path) -> Path:
    """Where a stray prefix from library `lib` is quarantined: the Nexus data
    dir for the main Steam library, else a folder inside that same library —
    so quarantining is an instant rename, never a multi-GB copy."""
    if lib.resolve() == STEAM_ROOT.resolve():
        return ORPHAN_HOLDING_DIR
    return lib / 'steamapps' / 'nexus-quarantine'


def find_orphan_compatdata(install_base: Path) -> list[dict]:
    """compatdata/<id> entries, in every Steam library, that nothing uses:
      - a non-Steam-shortcut id (>= 0x80000000) no shortcut on any account
        and no managed game points at,
      - a Steam appid whose game (or tool) is no longer installed anywhere,
      - one of _redirect_compatdata's "<appid>.bak-<timestamp>" safety renames.
    Read-only; nothing here is deleted. `has_savedata` flags prefixes that
    look like they still hold saves (a stranded prefix is exactly how 007
    First Light's saves were once lost track of)."""
    from nexus import steamlib
    shortcut_ids = _all_shortcut_appids()
    referenced = set(shortcut_ids or ())
    for entry in load_registry()['games'].values():
        try:
            referenced.add(int(entry.get('steam_appid') or 0))
        except (TypeError, ValueError):
            pass
    installed = steamlib.manifest_appids(STEAM_ROOT) | steamlib.TOOL_APPIDS

    orphans = []
    for lib in _steam_libraries():
        cd_root = lib / "steamapps" / "compatdata"
        if not cd_root.is_dir():
            continue
        for d in sorted(cd_root.iterdir()):
            if not d.is_dir() or d.is_symlink():
                continue
            m = _ORPHAN_BAK_RE.match(d.name)
            if m:
                appid, reason = int(m.group(1)), 'renamed aside by a previous redirect'
            else:
                try:
                    appid = int(d.name)
                except ValueError:
                    continue
                if appid == 0 or appid in referenced:
                    continue
                if appid >= 0x80000000:
                    if shortcut_ids is None:
                        continue             # a shortcuts.vdf we couldn't read: can't tell
                    reason = 'no current shortcut references this id'
                elif appid in installed:
                    continue
                else:
                    reason = 'the game is no longer installed'
            orphans.append({
                'path': d, 'appid': appid, 'reason': reason, 'library': str(lib),
                'size_bytes': dir_size_bytes(d),
                'has_savedata': bool(find_prefix_savedata(d)),
            })
    return orphans


def quarantine_path(path: Path, lib: Path) -> Path:
    """Move a stray prefix into quarantine and remember where it came from."""
    qdir = _quarantine_dir(lib)
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / path.name
    if dest.exists():
        dest = qdir / f"{path.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(path), str(dest))
    atomic_write(dest.with_name(dest.name + '.origin'), json.dumps(
        {'origin': str(path), 'at': datetime.now().isoformat(timespec='seconds')}))
    return dest


def quarantined() -> list[dict]:
    """Everything currently in quarantine, with where it would be restored to."""
    out = []
    for lib in _steam_libraries():
        qdir = _quarantine_dir(lib)
        if not qdir.is_dir():
            continue
        for d in sorted(qdir.iterdir()):
            if not d.is_dir():
                continue
            try:
                meta = json.loads(d.with_name(d.name + '.origin').read_text())
            except (OSError, ValueError):
                meta = {'origin': str(lib / 'steamapps' / 'compatdata' /
                                      re.sub(r'-\d{8}-\d{6}$', '', d.name))}
            saves = find_prefix_savedata(d)
            out.append({'name': d.name, 'path': str(d), 'origin': meta.get('origin'),
                        'at': meta.get('at'), 'size': dir_size_bytes(d),
                        'has_savedata': bool(saves),
                        'save_hint': savedata_owners(d, saves)})
    return out


def cmd_quarantine_restore(name: str) -> None:
    hit = next((q for q in quarantined() if q['name'] == name), None)
    if not hit:
        print(f"'{name}' is not in quarantine", file=sys.stderr)
        sys.exit(1)
    origin = Path(hit['origin'])
    if origin.exists():
        print(f"Can't restore: {origin} exists again (Steam recreated it?). Move it "
              f"away first.", file=sys.stderr)
        sys.exit(1)
    shutil.move(hit['path'], str(origin))
    Path(hit['path'] + '.origin').unlink(missing_ok=True)
    print(f"Restored {origin}")


def cmd_clean_orphan_compatdata(install_base: Path, target: str, yes: bool = False) -> None:
    """Quarantine a stray compatdata dir (see find_orphan_compatdata). Never
    deletes — `--quarantine-restore` puts it back; the cleanup page can
    trash it later."""
    by_name = {o['path'].name: o for o in find_orphan_compatdata(install_base)}
    match = by_name.get(target) or next(
        (o for name, o in by_name.items()
         if name == target or name.startswith(f"{target}.")), None)
    if not match:
        print(f"'{target}' is not a currently-detected orphan compatdata dir "
              f"— run --doctor to see the current list.", file=sys.stderr)
        sys.exit(1)

    path = match['path']
    print(f"About to quarantine: {path}  ({match['size_bytes'] / 1e9:.2f} GB)")
    print(f"  Reason: {match['reason']}")
    if match['has_savedata']:
        print("  [!] This looks like it contains SAVE DATA. Check it before deleting it.")
    if not yes:
        try:
            ans = input("  Move it to quarantine? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans not in ('y', 'yes'):
            print("Aborted.")
            return
    dest = quarantine_path(path, Path(match['library']))
    print(f"Quarantined to {dest}")
    print(f"Undo with:  nexus-cli --quarantine-restore {shlex.quote(dest.name)}")


def cleanup_candidates(install_base: Path, config: dict) -> list[dict]:
    """Everything the Storage page can offer to clean up (see nexus/cleanup)."""
    from nexus import cleanup as C, steamlib
    reg = load_registry()['games']
    out: list[dict] = []
    for o in find_orphan_compatdata(install_base):
        out.append(C.candidate('orphan-prefix', o['path'], title=f"compatdata/{o['path'].name}",
                               reason=o['reason'], action='quarantine', size=o['size_bytes'],
                               saves=o['has_savedata'], extra={'library': o['library']}))
    shortcut_ids = _all_shortcut_appids()
    keep_ids = (steamlib.manifest_appids(STEAM_ROOT) | set(shortcut_ids or ())
                | {int(e.get('steam_appid') or 0) for e in reg.values()})
    caches = C.shader_caches(_steam_libraries(), keep_ids)
    if shortcut_ids is None:        # unreadable shortcuts: leave shortcut ids alone
        caches = [c for c in caches if int(Path(c['path']).name) < 0x80000000]
    out += caches
    out += C.mesa_caches(Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache'))
    seen: set[str] = set()
    for c in (c for d in resolve_scan_dirs(None) for c in scan_repacks(d)):
        if c['path'] in seen:
            continue
        seen.add(c['path'])
        if c.get('installed'):
            out.append(C.candidate('installed-repack', c['path'], title=c['name'],
                                   reason=f"installed as '{c['slug']}'", preselect=False,
                                   size=c.get('size_bytes')))
    out += C.force_leftovers(install_base, find_prefix_savedata)
    out += C.old_logs(LOG_DIR)
    out += C.old_save_backups(SAVES_DIR)
    out += C.stale_data(set(reg), ARTWORK_DIR, INTEGRITY_DIR)
    return out


def cmd_cleanup_scan(install_base: Path, as_json: bool) -> None:
    from nexus import cleanup as C
    cands = cleanup_candidates(install_base, load_config())
    quar = quarantined()
    if as_json:
        print(json.dumps({'candidates': cands, 'quarantine': quar,
                          'categories': {k: {'title': t, 'about': a}
                                         for k, (t, a) in C.CATEGORIES.items()},
                          'trash_bytes': C.trash_size()}))
        return
    total = sum(c['size'] for c in cands)
    print(f"Cleanup candidates ({total / 1e9:.1f} GB):")
    for c in cands:
        mark = '*' if c['preselect'] else ' '
        print(f"  {mark} {c['size'] / 1e9:7.2f} GB  {C.CATEGORIES[c['category']][0]}: "
              f"{c['title']}  ({c['reason']}){'  [SAVES?]' if c['saves'] else ''}")
        print(f"      id: {c['id']}")
    if quar:
        print("\nIn quarantine:")
        for q in quar:
            print(f"    {q['size'] / 1e9:7.2f} GB  {q['name']}  (from {q['origin']})")
    print("\nRemove with:  nexus-cli --cleanup <id> [...] --yes   (to Trash; add --forever to "
          "delete)")


def cmd_cleanup(install_base: Path, ids: list[str], forever: bool, yes: bool) -> None:
    """Remove the given cleanup candidates (re-scanned now, so a stale id
    can't target anything else). Trash by default."""
    from nexus import cleanup as C
    cands = {c['id']: c for c in cleanup_candidates(install_base, load_config())}
    quar = {f"quarantine:{q['path']}": q for q in quarantined()}
    unknown = [i for i in ids if i not in cands and i not in quar]
    if unknown:
        print(f"Not a current cleanup candidate: {', '.join(unknown)} — re-scan first.",
              file=sys.stderr)
        sys.exit(1)
    total = sum((cands.get(i) or quar.get(i)).get('size') or 0 for i in ids)
    verb = 'Deleting' if forever else 'Moving to Trash'
    if not yes:
        try:
            ans = input(f"{verb} {len(ids)} item(s), {total / 1e9:.2f} GB. Proceed? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans.strip().lower() not in ('y', 'yes'):
            print("Aborted.")
            return
    freed, failed = 0, 0
    for n, i in enumerate(ids, 1):
        c = cands.get(i) or quar[i]
        path = Path(c['path'])
        try:
            if c.get('action') == 'quarantine' and not forever:
                dest = quarantine_path(path, Path(c['library']))
                print(f"[{n}/{len(ids)}] quarantined {path} -> {dest}")
            elif forever or c.get('action') == 'delete':
                C.delete(path)
                Path(str(path) + '.origin').unlink(missing_ok=True)
                freed += c.get('size') or 0
                print(f"[{n}/{len(ids)}] deleted {path}")
            else:
                C.trash(path)
                Path(str(path) + '.origin').unlink(missing_ok=True)
                print(f"[{n}/{len(ids)}] moved to Trash: {path}")
        except OSError as e:
            failed += 1
            print(f"[{n}/{len(ids)}] [!] {path}: {e}", file=sys.stderr)
    if freed:
        print(f"Freed {freed / 1e9:.2f} GB.")
    if not forever:
        print("Trashed items still use space until you empty the Trash.")
    if failed:
        sys.exit(1)


def _launch_sh_proton_ok(entry: dict, proton: Path | None) -> bool:
    """Does the game's launch.sh run with `proton` (the play Proton)?"""
    launcher = entry.get('launcher')
    if not (launcher and proton):
        return True
    try:
        text = Path(launcher).read_text(errors='replace')
    except OSError:
        return True
    if 'umu-run' not in text and ' run ' not in text:
        return True                     # raw-wine launcher: no Proton involved
    return str(proton.parent) in text or str(proton) in text


def doctor_checks(install_base: Path) -> list[dict]:
    """Every environment/game check as data: {group, status (ok|warn|fail|info),
    title, detail, fix, actions:[{label, args, steam}]}. Read-only. The text
    report (--doctor) and the GUI's Health page both render this."""
    out: list[dict] = []

    def add(group, status, title, detail='', fix='', actions=None):
        out.append({'group': group, 'status': status, 'title': title,
                    'detail': detail, 'fix': fix, 'actions': actions or []})

    cfg = load_config()
    R = 'Requirements'
    wv = shutil.which('wine')
    try:
        wver = (subprocess.run(['wine', '--version'], capture_output=True, text=True,
                               timeout=15).stdout.strip() if wv else '')
    except (OSError, subprocess.TimeoutExpired):
        wver = ''
    add(R, 'ok' if wv else 'fail', 'Wine', wver or 'not found',
        '' if wv else f"Install it: {install_hint('wine', 'wine')}")
    umu = find_umu()
    add(R, 'ok' if umu else 'warn', 'umu-run', umu or 'not found',
        '' if umu else 'Install umu-launcher (AUR), or set [runner] backend = "proton"')
    proton = find_proton()
    add(R, 'ok' if proton else 'fail', 'Proton',
        ', '.join(sorted(list_protons())) or 'none installed',
        '' if proton else 'Install GE-Proton with ProtonPlus into compatibilitytools.d')
    for name, why in sorted(damaged_protons().items()):
        tag = proton_repair_tag(name)
        add(R, 'fail', f'{name} is damaged',
            f"{why}. Every game or installer run with it aborts at once "
            "(\"unimplemented function win32u.…\").",
            "Reinstall it (downloaded and verified again; the damaged copy goes to "
            "the Trash)" if tag else "Reinstall it with your package manager",
            [{'label': 'Reinstall', 'args': ['--proton-install', tag, '--force'],
              'steam': False}] if tag else None)
    add(R, 'ok' if shutil.which('winetricks') else 'info', 'winetricks',
        'found' if shutil.which('winetricks') else 'not found (only needed for --deps)')
    try:
        import vdf  # noqa: F401
        has_vdf = True
    except ImportError:
        has_vdf = False
    add(R, 'ok' if has_vdf else 'warn', 'python-vdf', 'found' if has_vdf else 'missing',
        '' if has_vdf else ("Steam shortcuts are disabled — "
                         + install_hint('python-vdf', 'python3-vdf (or: pip install --user vdf)')))
    base_ok = False
    try:
        install_base.mkdir(parents=True, exist_ok=True)
        base_ok = os.access(install_base, os.W_OK)
    except OSError:
        pass
    add(R, 'ok' if base_ok else 'fail', 'Install folder', str(install_base),
        '' if base_ok else f'Not writable — set [install] base in {CONFIG_PATH}')

    I = 'Integrations'
    account, why = resolve_steam_account()
    others = [steam_account_label(a) for a in steam_accounts()
              if not account or a['accountid'] != account['accountid']]
    add(I, 'ok' if account else 'info', 'Steam account',
        f"{steam_account_label(account)} — {why}" if account else 'no Steam profile found',
        (f"Other accounts on this machine: {', '.join(others)}. Pin one with "
         f"[integrations] steam_account.") if others else '')
    heroic_dir = find_heroic_config()
    add(I, 'ok' if heroic_dir else 'info', 'Heroic',
        str(heroic_dir) if heroic_dir else 'not installed (optional)')
    add(I, 'ok' if cfg.get('api_key') else 'info', 'SteamGridDB artwork',
        'API key set' if cfg.get('api_key') else 'no API key — games get placeholder art',
        '' if cfg.get('api_key') else 'Add an API key in Settings')
    hp = resolve_game_proton(None, cfg, quiet=True)
    src = ('[runner] proton' if cfg.get('proton') else
           '[heroic] proton' if cfg.get('heroic_proton') else 'newest installed')
    add(I, 'ok' if hp else 'warn', 'Play Proton',
        f"{hp.parent.name if hp else 'none'} ({src})")
    pg = protongen_bin()
    add(I, 'ok' if pg else 'info', 'protongen', pg or 'not installed (optional)')
    try:
        gamedb_doctor(add, I)
    except Exception:
        pass
    config_secrets_doctor(add, I)
    try:
        from nexus import protons as PR
        latest = (PR.releases('ge')['releases'] or [{}])[0].get('tag')
        have = [n for n in list_protons() if PR.family_of(n) == 'ge']
        newest = max(have, key=PR.version_key, default=None)
        if latest and (not newest or PR.version_key(latest) > PR.version_key(newest)[:2]):
            add(I, 'info', f'GE-Proton {latest} is available',
                f"Installed: {newest or 'none'}",
                actions=[{'label': 'Install', 'args': ['--proton-install', 'latest'],
                          'steam': False}])
    except Exception:
        pass                      # offline: the Proton page reports it

    G = 'Games'
    reg = load_registry()
    cd_root = STEAM_ROOT / "steamapps" / "compatdata"
    compat_map = _read_compat_mapping()
    protons = list_protons()
    for slug, entry in sorted(reg['games'].items()):
        name = entry.get('display_name') or slug
        gp = resolve_game_proton(entry, cfg, quiet=True)
        if not _launch_sh_proton_ok(entry, gp):
            add(G, 'warn', f'{name}: launcher uses a different Proton',
                f"launch.sh doesn't run {gp.parent.name}, but Steam/Heroic do — the prefix "
                f"gets switched between builds.",
                actions=[{'label': 'Re-apply settings', 'args': ['--apply-launch', slug],
                          'steam': bool(entry.get('steam_appid'))}])
        appid, compatdata = entry.get('steam_appid'), entry.get('compatdata')
        if appid and compatdata:
            link = cd_root / str(appid)
            try:
                redirected = link.is_symlink() and link.resolve() == Path(compatdata).resolve()
            except OSError:
                redirected = False
            if redirected and not (Path(compatdata) / 'tracked_files').exists():
                add(G, 'fail', f'{name}: will crash when launched from Steam',
                    "The prefix is missing Proton's tracked_files bookkeeping file.",
                    actions=[{'label': 'Repair Steam shortcut', 'args': ['--steam', slug],
                              'steam': True}])
            tool = compat_map.get(str(appid))
            path = proton_by_name(tool, protons) if tool else None
            if path and str(path).startswith('/usr/'):
                add(G, 'info', f'{name}: Steam uses a Proton under /usr',
                    f"{tool} lives in /usr, which the Steam Linux Runtime container can't "
                    f"always see.",
                    f"Optional: symlink it into {STEAM_ROOT}/compatibilitytools.d/{tool}")
        wp = entry.get('wine_prefix')
        if wp:
            wt = Path(wp) / 'winetricks.log'
            try:
                if wt.is_file() and 'dxvk' in wt.read_text(errors='replace').split():
                    add(G, 'warn', f'{name}: winetricks DXVK overrides Proton\'s',
                        'The prefix pins native d3d11/dxgi overrides, so Proton no longer '
                        'manages DXVK.',
                        'Remove the overrides in winecfg → Libraries, or reinstall with --force.',
                        actions=[{'label': 'Open winecfg',
                                  'args': ['--prefix-tool', slug, 'winecfg'], 'steam': False}])
            except OSError:
                pass
            if not Path(wp).is_dir():
                add(G, 'fail', f'{name}: install folder is missing', wp,
                    'Reinstall it, or prune the stale entry.',
                    actions=[{'label': 'Prune missing games', 'args': ['--prune', '--yes'],
                              'steam': True}])
        if pg:
            hit = protongen_memory_for(entry)
            if hit:
                cur = entry.get('launch') or {}
                pprof = profile_from_protongen_config(hit[1])
                if [pprof.get(k) for k in ('env', 'wrappers', 'args')] != \
                        [cur.get(k) for k in ('env', 'wrappers', 'args')]:
                    add(G, 'info', f'{name}: protongen has different tuning',
                        format_launch_options(pprof) or '(no options)',
                        actions=[{'label': 'Pull from protongen',
                                  'args': ['--from-protongen', slug],
                                  'steam': bool(entry.get('steam_appid'))}])

    try:
        from nexus import tips
        out += tips.system_checks()
    except Exception as e:                       # never let advice break the doctor
        add('Performance', 'info', 'Performance checks unavailable', str(e))

    S = 'Storage'
    for o in find_orphan_compatdata(install_base):
        add(S, 'warn', f"Stray Steam prefix compatdata/{o['path'].name}",
            f"{o['size_bytes'] / 1e9:.2f} GB · {o['reason']}"
            + (" · looks like it has SAVE DATA — check before cleaning" if o['has_savedata']
               else ''),
            actions=[{'label': 'Quarantine', 'steam': False,
                      'args': ['--clean-orphan-compatdata', o['path'].name, '--yes']}])
    return out


def _looks_unmounted(path: Path, entry: dict) -> bool:
    """A missing game folder whose drive is probably just not mounted: its
    install base is gone too, or nothing exists below a mount point."""
    if entry.get('install_base') and not Path(entry['install_base']).is_dir():
        return True
    a = path
    while not a.exists() and a != a.parent:
        a = a.parent
    mounts = (Path('/mnt'), Path('/media'), Path('/run/media'))
    return a == Path('/') or a in mounts or a.parent in mounts \
        or a.parent.parent == Path('/run/media')


def stale_registry_entries() -> list[dict]:
    """Registry entries whose game is gone from disk (deleted by hand,
    outside Nexus): the prefix, or for an --add-steam game the exe."""
    out = []
    for slug, e in sorted(load_registry()['games'].items()):
        p = Path(e['wine_prefix']) if e.get('wine_prefix') else \
            Path(e['exe']) if e.get('exe') else None
        if p is None or p.exists():
            continue
        out.append({'slug': slug, 'name': e.get('display_name') or slug, 'path': str(p),
                    'unmounted': _looks_unmounted(p, e), 'entry': e})
    return out


def cmd_prune(yes: bool = False, as_json: bool = False) -> int:
    """Drop registry entries for games that were deleted outside Nexus, with
    their Steam shortcut, Heroic entry, app-menu entry and launcher. Lists
    them only, unless --yes. A game on a drive that isn't mounted is never
    pruned."""
    stale = stale_registry_entries()
    removed, skipped = [], []
    steam_up = None
    for s in stale:
        slug, e = s['slug'], s['entry']
        if s['unmounted']:
            skipped.append({'slug': slug, 'reason': 'its drive looks unmounted'})
            continue
        if not yes:
            continue
        if game_has_steam_shortcut(slug):
            steam_up = steam_is_running() if steam_up is None else steam_up
            if steam_up:
                skipped.append({'slug': slug, 'reason': 'Steam is running (it has a shortcut)'})
                continue
            if not remove_steam_shortcut(slug, require_steam_closed=False):
                skipped.append({'slug': slug, 'reason': "the Steam shortcut couldn't be removed"})
                continue
        remove_heroic_entry(slug, exe=e.get('exe'))
        _desktop_path(slug).unlink(missing_ok=True)
        launcher = Path(e['launcher']) if e.get('launcher') else None
        if launcher and launcher.parent == LAUNCHERS_DIR:
            launcher.unlink(missing_ok=True)
        unregister_game(slug)
        removed.append(slug)
    if as_json:
        print(json.dumps({'stale': [{k: v for k, v in s.items() if k != 'entry'}
                                    for s in stale],
                          'removed': removed, 'skipped': skipped}))
        return 0
    if not stale:
        print("Nothing to prune: every game in the library is still on disk.")
        return 0
    for s in stale:
        mark = 'removed' if s['slug'] in removed else \
            next((f"kept — {k['reason']}" for k in skipped if k['slug'] == s['slug']),
                 'missing')
        print(f"  {s['name']} ({s['slug']}): {s['path']}  [{mark}]")
    if not yes:
        print("Run again with --yes to remove the entries marked 'missing' "
              "(and their Steam/Heroic/app-menu entries).")
    return 0


def cmd_doctor(install_base: Path, as_json: bool = False) -> None:
    """Readiness report: every requirement, integration and per-game problem,
    each with a fix. Read-only; never changes anything."""
    checks = doctor_checks(install_base)
    if as_json:
        print(json.dumps({'checks': checks}))
        return
    sym = {'ok': '\033[32m✓\033[0m', 'fail': '\033[31m✗\033[0m',
           'warn': '\033[33m!\033[0m', 'info': '\033[36mi\033[0m'}
    if not sys.stdout.isatty():
        sym = {'ok': '✓', 'fail': '✗', 'warn': '!', 'info': 'i'}
    print("Nexus — environment check")
    group = None
    for c in checks:
        if c['group'] != group:
            group = c['group']
            print(f"\n{group}")
        print(f"  {sym[c['status']]}  {c['title']}" + (f" — {c['detail']}" if c['detail'] else ''))
        if c['fix']:
            print(f"       {c['fix']}")
        for a in c['actions']:
            print(f"       fix: nexus-cli {' '.join(shlex.quote(x) for x in a['args'])}")
    fails = [c for c in checks if c['status'] == 'fail' and c['group'] == 'Requirements']
    print("\n  " + ("Ready to install." if not fails else
                    "Not ready — resolve the ✗ items above."))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """The CLI, with a last line of defence: an unexpected exception still
    ends with one readable line on stderr and, for --json, a JSON error on
    stdout (the GUI parses stdout — a bare traceback there reads as "bad
    JSON")."""
    try:
        _main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:                 # noqa: BLE001 — reported, then exit 1
        import traceback
        traceback.print_exc(file=sys.stderr)
        msg = f"{type(e).__name__}: {e}"
        if '--json' in sys.argv[1:]:
            print(json.dumps({'error': {'kind': 'internal', 'message': msg}}))
        print(f"nexus-cli: internal error — {msg}", file=sys.stderr)
        sys.exit(1)


def _best_effort(what: str, fn, *a, **kw):
    """An integration after the game is installed (artwork, Steam, Heroic,
    the app menu): a failure there is reported, never fatal — the install
    still gets registered."""
    try:
        return fn(*a, **kw)
    except Exception as e:                 # noqa: BLE001
        log.print(f"  [!] {what} failed ({type(e).__name__}: {e}). The game is installed; "
                  f"add it again later from Nexus.")
        return None


# Options whose value (or first value) names a managed game by slug. They
# become paths (install dir, artwork, saves), so they are checked up front:
# `--uninstall ..` must not reach the folder above the install base.
_SLUG_OPTS = {
    'uninstall': '--uninstall', 'discard_incomplete': '--discard-incomplete',
    'restore_saves': '--restore-saves', 'check_integrity': '--check-integrity',
    'launch': '--launch', 'steam': '--steam', 'remove_steam': '--remove-steam',
    'heroic': '--heroic', 'remove_heroic': '--remove-heroic',
    'refetch_art': '--refetch-art', 'repair_launchers': '--repair-launchers',
    'show_launch': '--show-launch', 'set_launch': '--set-launch',
    'set_proton': '--proton', 'from_protongen': '--from-protongen',
    'protongen_default': '--protongen-default', 'apply_launch': '--apply-launch',
    'set_exe': '--set-exe', 'rename': '--rename', 'prefix_tool': '--prefix-tool',
    'track_session': '--track-session', 'apply_recipe': '--apply-recipe',
    'explain_log': '--explain-log',
}
_SLUG_OPTS_ALL = {'protongen_default', 'apply_launch'}   # also take 'all'


def _check_slug_args(args) -> None:
    for dest, flag in _SLUG_OPTS.items():
        v = getattr(args, dest, None)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if not v or (v == 'all' and dest in _SLUG_OPTS_ALL):
            continue
        require_slug(v, flag)


def _main() -> None:
    global log

    parser = argparse.ArgumentParser(
        prog='nexus-cli',
        description='Nexus backend: install FitGirl repacks, manage Steam/Heroic entries, Proton builds and cleanup.',
    )
    parser.add_argument('repack_dir', nargs='?', default='.',
                        help='Path to the FitGirl repack folder (default: current dir)')
    parser.add_argument('--name', help='Override auto-detected game name')
    parser.add_argument('--verify', action='store_true',
                        help='Verify MD5 checksums before installing (slow for large files)')
    parser.add_argument('--no-deps', action='store_true',
                        help='Force-skip winetricks deps even if --deps was given '
                             '(deps are already off by default)')
    parser.add_argument('--force', action='store_true',
                        help='Start from a fresh prefix: an existing install of this game is '
                             'moved aside to <slug>.old-<time> (not deleted)')
    parser.add_argument('--install-dir', default=None,
                        help='Base install directory. Overrides config [install] base; '
                             f'default {INSTALL_BASE}')
    parser.add_argument('--log', action='store_true',
                        help='Save full log to ~/.local/share/nexus/logs/<game>-<time>.log')
    parser.add_argument('--debug', action='store_true',
                        help='Enable verbose Wine/Proton debug output (implies --log)')
    parser.add_argument('--skip-mem-check', action='store_true',
                        help='Bypass the pre-install memory hard-stop (use if you know '
                             'your system can handle it; not recommended)')
    parser.add_argument('--skip-disk-check', action='store_true',
                        help='Bypass the pre-install disk-space hard-stop (use if you '
                             'know your system can handle it; not recommended)')
    parser.add_argument('--no-steam', action='store_true',
                        help='Do not add the installed game to Steam')
    parser.add_argument('--no-desktop', action='store_true',
                        help='Do not create a desktop-menu launcher entry')
    parser.add_argument('--no-heroic', action='store_true',
                        help='Do not add the installed game to Heroic (sideloaded)')
    parser.add_argument('--no-artwork', action='store_true',
                        help='Do not fetch cover/hero/logo/icon art from SteamGridDB')
    parser.add_argument('--deps', metavar='SPEC',
                        help="Install winetricks verbs into the prefix: 'all' or a "
                             "comma list (e.g. vcrun2019,d3dcompiler_47). Off by "
                             "default — Proton already ships the usual runtimes")
    parser.add_argument('-y', '--yes', action='store_true',
                        help='Auto-confirm proceed/continue prompts (non-interactive)')
    parser.add_argument('--list', action='store_true',
                        help='List managed games (with Steam/desktop status) and exit')
    parser.add_argument('--json', action='store_true',
                        help='With --list or --scan, emit machine-readable JSON')
    parser.add_argument('--sizes', action='store_true',
                        help='With --list, (re)compute and cache each managed game\'s '
                             'on-disk size (stats only, but slow for a large library)')
    parser.add_argument('--uninstall', metavar='SLUG',
                        help='Remove a game (prefix + Steam shortcut + desktop entry) and exit')
    parser.add_argument('--save-backup', action='store_true', dest='save_backup',
                        help='With --uninstall, back up detected save-game files before '
                             'removing the prefix (default: off — asks interactively unless '
                             '--yes is also given, in which case saves are NOT backed up)')
    parser.add_argument('--import-save', action='store_true', dest='import_save',
                        help='On install, restore a previous save backup into a fresh prefix '
                             'if one exists (default: off — asks interactively unless --yes '
                             'is also given, in which case no backup is imported)')
    parser.add_argument('--use-protongen-default', action='store_true',
                        dest='use_protongen_default',
                        help="On install, set the game up with protongen's default profile "
                             "(launch options + Proton) unless it already has its own")
    parser.add_argument('--interactive', action='store_true',
                        help="Show FitGirl's setup window and click through it yourself "
                             "(default: install silently into the prefix's C:\\Games\\<slug>; "
                             "config [install] unattended = false makes this the default)")
    parser.add_argument('--unattended', action='store_true',
                        help='Install silently even if config [install] unattended = false')
    parser.add_argument('--from-torrent', metavar='HASH', dest='from_torrent',
                        help=argparse.SUPPRESS)       # the download this install is from
    parser.add_argument('--defer-steam', action='store_true', dest='defer_steam',
                        help="If Steam is running when the install finishes, don't wait for "
                             "it: print a steam-pending event and leave the shortcut for a "
                             "later `--steam <slug>` (the GUI does this once Steam is closed)")
    parser.add_argument('--trash-repack', action='store_true', dest='trash_repack',
                        help='After a successful install, move the repack folder to the '
                             'Trash (config [install] trash_repack = true does it always)')
    parser.add_argument('--trash-repack-dir', metavar='PATH', dest='trash_repack_dir',
                        help='Move a repack folder (one with a setup.exe) to the Trash and exit')
    parser.add_argument('--discard-incomplete', metavar='SLUG', dest='discard_incomplete',
                        help='Move an unfinished install (cancelled or failed, not in the '
                             'library) to the Trash and exit')
    parser.add_argument('--achievements', metavar='GAME',
                        help="A Steam game's achievements (appid or steam-<appid>), or "
                             "'recent' for the newest unlocks across games; from Steam's "
                             "local cache, read-only (use --json)")
    parser.add_argument('--restore-saves', metavar='SLUG', dest='restore_saves',
                        help="Re-copy a managed game's most recent save backup into its "
                             "current prefix and exit")
    parser.add_argument('--check-integrity', metavar='SLUG', dest='check_integrity',
                        help="Hash a managed game's installed files against a saved "
                             "baseline (creating one on first run) to catch corruption, "
                             "then exit")
    parser.add_argument('--rebaseline', action='store_true',
                        help='With --check-integrity, accept the current files as the '
                             'new baseline instead of comparing against the old one')
    parser.add_argument('--clean-orphan-compatdata', metavar='APPID', dest='clean_orphan',
                        help="Move a stray compatdata prefix (see --doctor) out of "
                             "Steam's way and exit. Never deletes it outright.")
    parser.add_argument('--add-steam', metavar='PATH', dest='add_steam',
                        help='Add an already-installed game (folder or .exe) to Steam '
                             '+ desktop menu and exit (use --name to set its title)')
    parser.add_argument('--launch', metavar='SLUG',
                        help='Launch a managed game by slug and exit')
    parser.add_argument('--pause', metavar='SLUG',
                        help='Freeze a running repack (SIGSTOP everything in its prefix)')
    parser.add_argument('--resume', metavar='SLUG', help='Thaw a paused repack')
    parser.add_argument('--pause-toggle', nargs='?', const='', metavar='SLUG',
                        dest='pause_toggle',
                        help='Pause or resume (no slug: the one repack running) — '
                             'handy as a compositor key binding')
    parser.add_argument('--benchmark', metavar='SLUG',
                        help='Launch a repack with MangoHud frame-time logging')
    parser.add_argument('--bench-seconds', type=int, default=60, dest='bench_seconds',
                        help='With --benchmark: how long to log (default 60)')
    parser.add_argument('--bench-delay', type=int, default=30, dest='bench_delay',
                        help='With --benchmark: seconds after launch before logging '
                             '(default 30; Shift+F2 starts it by hand)')
    parser.add_argument('--bench-results', metavar='SLUG', dest='bench_results',
                        help="A repack's benchmark runs (avg / 1%% / 0.1%% low fps)")
    parser.add_argument('--launch-via', nargs=2, metavar=('SLUG', 'steam|nexus'),
                        dest='launch_via',
                        help='Play a repack through its Steam shortcut (Steam Input, overlay, '
                             'recording) or straight from Nexus (the default)')
    parser.add_argument('--wait', action='store_true',
                        help='With --launch: stay until the game exits (for Sunshine)')
    parser.add_argument('--sunshine', metavar='GAME',
                        help='Add or update a game in Sunshine (slug, steam-<appid> or Heroic slug)')
    parser.add_argument('--remove-sunshine', metavar='GAME', dest='remove_sunshine',
                        help="Remove a game's Sunshine entry")
    parser.add_argument('--steam', metavar='SLUG', dest='steam',
                        help='Add a Steam shortcut for a managed game (by slug) and exit')
    parser.add_argument('--remove-steam', metavar='SLUG', dest='remove_steam',
                        help='Remove just the Steam shortcut for a game and exit')
    parser.add_argument('--heroic', metavar='SLUG', dest='heroic',
                        help='Add a managed game (by slug) to Heroic as a sideloaded app and exit')
    parser.add_argument('--remove-heroic', metavar='SLUG', dest='remove_heroic',
                        help='Remove just the Heroic sideload entry for a game and exit')
    parser.add_argument('--art-variants', metavar='GAME', dest='art_variants',
                        help='SteamGridDB images to choose from for a game (any source); '
                             'use with --kind and --page')
    parser.add_argument('--kind', choices=sorted(SGDB_KINDS), default='cover',
                        help='Art kind for --art-variants / --set-art')
    parser.add_argument('--page', type=int, default=0, help='Result page for --art-variants')
    parser.add_argument('--set-art', metavar='GAME', dest='set_art',
                        help='Use a SteamGridDB image (--url) for a game, or --reset it')
    parser.add_argument('--url', help='Image URL for --set-art (SteamGridDB only)')
    parser.add_argument('--reset', action='store_true', help='With --set-art or --set-sgdb: back to the default')
    parser.add_argument('--sgdb-search', metavar='NAME', dest='sgdb_search',
                        help='Search SteamGridDB games by name (to pick the right match)')
    parser.add_argument('--set-sgdb', metavar='GAME', dest='set_sgdb',
                        help='Pin the SteamGridDB game (--id, --name) a game fetches art '
                             'from, or --reset to automatic matching')
    parser.add_argument('--id', type=int, dest='sgdb_gid', help='SteamGridDB game id for --set-sgdb')
    parser.add_argument('--sgdb-name', dest='sgdb_name', help='Its name, for display (--set-sgdb)')
    parser.add_argument('--fill-art', action='store_true', dest='fill_art',
                        help='Fetch missing hero/logo/banner art from SteamGridDB for every game')
    parser.add_argument('--if-stale', action='store_true', dest='if_stale',
                        help='With --fill-art: only if it has not run in the last day')
    parser.add_argument('--refetch-art', metavar='SLUG', dest='refetch_art',
                        help='Re-download SteamGridDB artwork for a game and exit')
    parser.add_argument('--reindex', action='store_true',
                        help='Backfill the game registry from a filesystem scan and exit')
    parser.add_argument('--scan', action='store_true',
                        help='Look for extracted repack folders (default: ~/Downloads) '
                             'and suggest them for install, then exit')
    parser.add_argument('--scan-dir', metavar='DIR', dest='scan_dir',
                        help='Folder to search with --scan. Overrides config '
                             '[install] scan_dir; default ~/Downloads')
    parser.add_argument('--detect', metavar='PATH',
                        help="Print a repack folder's detected name/slug and whether "
                             "it's already installed, as JSON, then exit (used by the GUI)")
    parser.add_argument('--steam-account', metavar='ACCOUNT', dest='steam_account',
                        help='Steam account to add shortcuts to (accountid, steamid64, login '
                             'name or persona name). Overrides config [integrations] '
                             'steam_account; see --list-steam-accounts')
    parser.add_argument('--list-steam-accounts', action='store_true',
                        dest='list_steam_accounts',
                        help='List the Steam accounts on this machine, show which one '
                             'shortcuts would go to, and exit')
    parser.add_argument('--doctor', action='store_true',
                        help='Check that wine/umu/proton/steam/deps are all present and exit')
    parser.add_argument('--prune', action='store_true',
                        help='List games deleted outside Nexus; with --yes, drop their '
                             'registry, Steam, Heroic and app-menu entries')
    parser.add_argument('--repair-launchers', metavar='SLUG', nargs='?', const='',
                        dest='repair_launchers',
                        help='Regenerate launch.sh for all managed games (or one SLUG) with '
                             'the current runner/template, then exit')
    g = parser.add_argument_group('per-game settings (launch profile)')
    g.add_argument('--show-launch', metavar='SLUG', dest='show_launch',
                   help="Show a game's Proton, launch options and protongen status "
                        "(add --json for machine-readable output)")
    g.add_argument('--set-launch', nargs=2, metavar=('SLUG', 'OPTIONS'), dest='set_launch',
                   help='Set launch options for a game and apply them to launch.sh, Steam '
                        'and Heroic. OPTIONS is a Steam launch-options string '
                        '(e.g. "PROTON_ENABLE_WAYLAND=1 mangohud %%command%%"), a umu '
                        'command line, or a protongen preset code (protongen:v1:...). '
                        'An empty string clears them.')
    g.add_argument('--proton', nargs=2, metavar=('SLUG', 'BUILD'), dest='set_proton',
                   help="Play a game with a specific installed Proton build ('default' to "
                        "unpin), applied to launch.sh, Steam and Heroic")
    g.add_argument('--from-protongen', metavar='SLUG', dest='from_protongen',
                   help="Import protongen's saved tuning for a game (read-only from "
                        "~/.config/protongen/state.toml) and apply it everywhere")
    g.add_argument('--protongen-default', metavar='SLUG', dest='protongen_default',
                   help="Apply protongen's default profile (its Settings → Save as default "
                        "profile) to a game and apply it everywhere ('all' for every game "
                        "without launch options of its own)")
    g.add_argument('--apply-launch', metavar='SLUG', dest='apply_launch',
                   help="Re-apply a game's settings to launch.sh, its desktop entry, and "
                        "its Steam shortcut / Heroic entry if it has them ('all' for "
                        "every game)")
    g.add_argument('--set-exe', nargs=2, metavar=('SLUG', 'EXE'), dest='set_exe',
                   help='Point a game at a different .exe (when auto-detection picked the '
                        'wrong one) and re-apply')
    g.add_argument('--rename', nargs=2, metavar=('SLUG', 'NAME'), dest='rename',
                   help="Change a game's display name everywhere (slug stays the same)")
    g.add_argument('--list-protons', action='store_true', dest='list_protons',
                   help='List installed Proton builds (with --json for the GUI)')
    g.add_argument('--prefix-tool', nargs=2, metavar=('SLUG', 'TOOL'), dest='prefix_tool',
                   help="Run a tool in a game's prefix: winecfg, regedit, winetricks, or "
                        "kill (stop everything running in it)")
    parser.add_argument('--track-session', nargs=2, metavar=('SLUG', 'LAUNCHER'),
                        dest='track_session', help=argparse.SUPPRESS)
    parser.add_argument('--orphans', action='store_true',
                        help='List stray Steam compatdata prefixes (with --json for the GUI)')
    g2 = parser.add_argument_group('library')
    g2.add_argument('--library', action='store_true',
                    help='List every game: Nexus installs, Steam and Heroic store games '
                         '(with --json for the GUI)')
    g2.add_argument('--owned', action='store_true',
                    help='With --library: also games you own but have not installed '
                         '(Steam via Game-db, Epic/GOG via Heroic)')
    g2.add_argument('--running', action='store_true',
                    help='Which library games are running right now')
    g2.add_argument('--open-url', metavar='URL', dest='open_url',
                    help='Launch/open a steam:// or heroic:// URL (e.g. steam://rungameid/APPID)')
    g3 = parser.add_argument_group('cleanup')
    g3.add_argument('--cleanup-scan', action='store_true', dest='cleanup_scan',
                    help='List what can be cleaned up (stray prefixes, caches, old logs, '
                         'installed repacks, ...) — add --json for the GUI')
    g3.add_argument('--cleanup', nargs='+', metavar='ID',
                    help='Clean up the given candidate ids (from --cleanup-scan): moved to '
                         'the Trash, or quarantined for stray prefixes')
    g3.add_argument('--forever', action='store_true',
                    help='With --cleanup, delete permanently instead of using the Trash')
    g3.add_argument('--quarantine-restore', metavar='NAME', dest='quarantine_restore',
                    help='Put a quarantined prefix back where it came from')
    g4 = parser.add_argument_group('Proton builds')
    g4.add_argument('--protons', action='store_true',
                    help='Installed Proton builds, who uses each, and available updates')
    g4.add_argument('--proton-releases', action='store_true', dest='proton_releases',
                    help='List recent GE-Proton releases')
    g4.add_argument('--refresh', action='store_true',
                    help='Bypass the cache for --protons, --proton-releases, --news, '
                         '--protondb and the Game-db flags')
    g4.add_argument('--proton-install', metavar='TAG', dest='proton_install',
                    help="Download, verify and install a GE-Proton release ('latest' or a tag)")
    g4.add_argument('--proton-remove', metavar='NAME', dest='proton_remove',
                    help='Move a user-installed Proton build to the Trash (refuses if in use)')
    g4.add_argument('--proton-migrate', nargs=2, metavar=('OLD', 'NEW'), dest='proton_migrate',
                    help='Point every Nexus, Steam and Heroic game using OLD at NEW')
    g5 = parser.add_argument_group('tips and protongen')
    g5.add_argument('--protondb', nargs='+', metavar='APPID',
                    help='ProtonDB tiers for Steam appids (cached a day)')
    g5.add_argument('--game-tips', metavar='GAME', dest='game_tips',
                    help='protongen tuning, ProtonDB tier, Steam Deck / anti-cheat / '
                         'PCGamingWiki facts and fitting recipes for a game '
                         '(slug, steam-<appid> or Heroic slug)')
    g5.add_argument('--perf', nargs='+', metavar=('SLUG', 'KEY=VALUE'),
                    help='Performance switches for a game: fps=60 mode=game-performance|'
                         'gamemoderun|off mangohud=on|off wayland=on|off hidraw=on|off '
                         'upscaler=off|fsr4|optiscaler gamescope=WxH[@R][,fsr][,fullscreen]'
                         '[,render=WxH]|off (just the slug: show them)')
    g5.add_argument('--repack-feed', action='store_true', dest='repack_feed',
                    help="Check FitGirl's feed for newer repacks of installed games "
                         "(fetched at most every 6 h; --refresh forces it)")
    g5.add_argument('--repack-index', action='store_true', dest='repack_index',
                    help="Sync FitGirl's whole catalogue and show which wishlist games have "
                         "a repack (incremental, at most every 6 h; --refresh forces it)")
    g5.add_argument('--repack-page', metavar='SLUG', dest='repack_page',
                    help="A repack's sizes and where a grab would download it")
    g5.add_argument('--repack-grab', metavar='SLUG', dest='repack_grab',
                    help="Send a FitGirl repack's magnet link to your torrent client "
                         "(--auto-install installs it when done; --appid tags the wishlist game)")
    g5.add_argument('--appid', metavar='APPID', help='With --repack-grab: the wishlist game')
    g5.add_argument('--repack-reject', nargs='+', metavar=('APPID', 'SLUG'), dest='repack_reject',
                    help="That repack isn't this wishlist game (no SLUG: undo)")
    g5.add_argument('--store-info', metavar='GAME', dest='store_info',
                    help='Steam store facts for a game: trailers, reviews, metacritic, '
                         'similar games (cached)')
    g5.add_argument('--torrent-add', metavar='MAGNET|FILE', dest='torrent_add',
                    help='Hand a magnet link or .torrent to your torrent client '
                         '(config [torrent]); --auto-install installs it once downloaded')
    g5.add_argument('--auto-install', action='store_true', dest='auto_install',
                    help='With --torrent-add: install the repack when the download finishes')
    g5.add_argument('--torrents', action='store_true',
                    help='Downloads added from Nexus, as your torrent client sees them')
    g5.add_argument('--all', action='store_true',
                    help='With --torrents: every torrent in the client, not just Nexus\'s')
    g5.add_argument('--torrent-pause', nargs='+', metavar='HASH', dest='torrent_pause',
                    help='Pause torrents in your client')
    g5.add_argument('--torrent-resume', nargs='+', metavar='HASH', dest='torrent_resume',
                    help='Resume torrents in your client')
    g5.add_argument('--torrent-remove', nargs='+', metavar='HASH', dest='torrent_remove',
                    help='Remove torrents from your client (--delete-data also deletes the files)')
    g5.add_argument('--delete-data', action='store_true', dest='delete_data',
                    help='With --torrent-remove: delete the downloaded files too')
    g5.add_argument('--torrent-test', action='store_true', dest='torrent_test',
                    help='Check that Nexus can talk to your torrent client')
    g5.add_argument('--torrent-detect', action='store_true', dest='torrent_detect',
                    help=argparse.SUPPRESS)
    g5.add_argument('--torrent-done', metavar='HASH', dest='torrent_done',
                    help=argparse.SUPPRESS)
    g5.add_argument('--torrent-forget', metavar='HASH', dest='torrent_forget',
                    help="Stop following a download (the client keeps it)")
    g5.add_argument('--check-repack', metavar='DIR', dest='check_repack',
                    help="Look a repack folder over before installing it (extra programs, "
                         "disguised names, what the installer runs); --clamav also scans it")
    g5.add_argument('--ignore-safety', action='store_true', dest='ignore_safety',
                    help="Install a repack even when --check-repack's look says it's tampered")
    g5.add_argument('--clamav', action='store_true',
                    help='With --check-repack: scan the small files with ClamAV')
    g5.add_argument('--history', action='store_true',
                    help='Play sessions: the last 12 weeks and this year in review')
    g5.add_argument('--notify-test', action='store_true', dest='notify_test',
                    help='Send a test push to the phone targets in config [notify]')
    g5.add_argument('--apply-recipe', nargs=2, metavar=('SLUG', 'RECIPE'), dest='apply_recipe',
                    help="Merge one of protongen's recipes into a game's launch options")
    g5.add_argument('--explain-log', metavar='SLUG', dest='explain_log',
                    help="Explain a game's latest log with protongen's local AI (if enabled)")
    g6 = parser.add_argument_group('news (experimental)')
    g6.add_argument('--news', action='store_true',
                    help='Gaming news: Linux gaming feeds, Proton releases, Steam news for '
                         'your installed games (cached an hour; --refresh to bypass)')
    g6.add_argument('--news-add-feed', nargs='+', metavar=('URL', 'NAME'), dest='news_add',
                    help='Add an RSS/Atom feed to the News page')
    g6.add_argument('--news-remove-feed', metavar='URL', dest='news_remove',
                    help='Remove a feed you added')
    g7 = parser.add_argument_group('Game-db (wishlist deals, releases, backlog)')
    g7.add_argument('--gamedb', action='store_true',
                    help='Everything from Game-db: deals, upcoming releases, backlog '
                         '(cached 45 min; --refresh to bypass)')
    g7.add_argument('--deals', action='store_true',
                    help='Wishlist games on sale, closest to their all-time low first')
    g7.add_argument('--upcoming', action='store_true',
                    help='Unreleased wishlist games with a release countdown')
    g7.add_argument('--backlog', action='store_true',
                    help="Backlog progress and tonight's picks from your owned games")
    g7.add_argument('--time', choices=('lt1h', '1to3', '3plus', 'long'), default='1to3',
                    help='With --backlog: how long you have to play (default 1to3)')
    g7.add_argument('--gamedb-lookup', nargs='+', metavar='APPID', dest='gamedb_lookup',
                    help='Cached Game-db info (price, HLTB, release) for Steam appids; '
                         'never hits the network')
    args = parser.parse_args()
    _check_slug_args(args)

    # A GUI front-end's Cancel button sends SIGTERM to this process; make sure
    # that also stops whatever subprocess run() is currently waiting on
    # (umu-run/wine/winetricks), instead of just orphaning it.
    signal.signal(signal.SIGTERM, _handle_sigterm)
    for line in NX.migrate():
        print(f"  Moved {line} (old path kept as a symlink).", file=sys.stderr)
    # Under the GUI stdout is a pipe (block-buffered by default), which held
    # status lines like "[3/4] Running setup.exe" back for the whole install.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # Which Steam account gets the shortcuts. CLI beats config; both beat
    # auto-detection. Must be set before any find_steam_userdata() call.
    globals()['STEAM_ACCOUNT_PIN'] = (args.steam_account
                                      or load_config().get('steam_account'))

    install_base = resolve_install_base(args.install_dir)

    # Management commands short-circuit the install flow
    if args.list_steam_accounts:
        cmd_list_steam_accounts()
        sys.exit(0)
    if args.doctor:
        cmd_doctor(install_base, as_json=args.json)
        sys.exit(0)
    if args.prune:
        sys.exit(cmd_prune(yes=args.yes, as_json=args.json))
    if args.repair_launchers is not None:
        cmd_repair_launchers(install_base, load_config(), args.repair_launchers or None)
        sys.exit(0)
    if args.list:
        cmd_list(install_base, as_json=args.json, compute_sizes=args.sizes)
        sys.exit(0)
    if args.reindex:
        cmd_reindex(install_base)
        sys.exit(0)
    if args.scan:
        cmd_scan(resolve_scan_dirs(args.scan_dir), as_json=args.json)
        sys.exit(0)
    if args.detect:
        cmd_detect(Path(args.detect).expanduser(), args.name)
        sys.exit(0)
    if args.achievements:
        sys.exit(cmd_achievements(args.achievements, args.json))
    if args.trash_repack_dir:
        sys.exit(cmd_trash_repack(Path(args.trash_repack_dir).expanduser()))
    if args.discard_incomplete:
        sys.exit(cmd_discard_incomplete(install_base, args.discard_incomplete))
    if args.uninstall:
        cmd_uninstall(install_base, args.uninstall, yes=args.yes,
                      save_backup=args.save_backup)
        sys.exit(0)
    if args.restore_saves:
        entry = registry_get(args.restore_saves)
        if not entry or not entry.get('wine_prefix'):
            print(f"'{args.restore_saves}' is not a managed game with a prefix",
                  file=sys.stderr)
            sys.exit(1)
        latest = latest_save_backup(args.restore_saves)
        if latest is None:
            print(f"No save backup found for '{args.restore_saves}'")
            sys.exit(1)
        # What's there now is kept first: a restore never loses progress.
        before = backup_prefix_saves(args.restore_saves, Path(entry['wine_prefix']),
                                     suffix=PRE_RESTORE)
        if before and before[2]:
            _report_failed_copies(before[2], "current save file(s)")
            print("Nothing was restored, so nothing was overwritten.", file=sys.stderr)
            sys.exit(1)
        if before:
            print(f"Kept the current saves ({before[1]} file(s)) in {before[0]}")
        n, failed = restore_prefix_saves(args.restore_saves, Path(entry['wine_prefix']))
        print(f"Restored {n} save file(s) from {latest.name} into {entry['wine_prefix']}")
        if failed:
            _report_failed_copies(failed, "save file(s)")
            sys.exit(1)
        sys.exit(0)
    if args.check_integrity:
        cmd_check_integrity(args.check_integrity, rebaseline=args.rebaseline)
        sys.exit(0)
    if args.clean_orphan:
        cmd_clean_orphan_compatdata(install_base, args.clean_orphan, yes=args.yes)
        sys.exit(0)
    if args.add_steam:
        add_existing_to_steam(Path(args.add_steam), args.name, args, load_config())
        sys.exit(0)
    if args.launch_via:
        slug, via = args.launch_via
        if via not in ('steam', 'nexus'):
            print("--launch-via takes steam or nexus", file=sys.stderr)
            sys.exit(2)
        cmd_launch_via(require_slug(slug, '--launch-via'), via)
        sys.exit(0)
    if args.pause or args.resume or args.pause_toggle is not None:
        target = args.pause or args.resume or args.pause_toggle or None
        cmd_pause(require_slug(target, '--pause') if target else None,
                  'pause' if args.pause else 'resume' if args.resume else 'toggle')
        sys.exit(0)
    if args.benchmark:
        cmd_benchmark(install_base, require_slug(args.benchmark, '--benchmark'),
                      args.bench_seconds, args.bench_delay)
        sys.exit(0)
    if args.bench_results:
        cmd_bench_results(require_slug(args.bench_results, '--bench-results'), args.json)
        sys.exit(0)
    if args.sunshine or args.remove_sunshine:
        cmd_sunshine(args.sunshine or args.remove_sunshine, remove=bool(args.remove_sunshine))
        sys.exit(0)
    if args.launch:
        cmd_launch(install_base, args.launch, force=args.force, wait=args.wait)
        sys.exit(0)
    if args.steam:
        cmd_add_steam(args.steam, load_config())
        sys.exit(0)
    if args.remove_steam:
        remove_steam_shortcut(args.remove_steam)
        sys.exit(0)
    if args.heroic:
        cmd_add_heroic(args.heroic, load_config())
        sys.exit(0)
    if args.remove_heroic:
        globals()['log'] = Logger()
        remove_heroic_entry(args.remove_heroic,
                            exe=(registry_get(args.remove_heroic) or {}).get('exe'))
        sys.exit(0)
    if args.refetch_art:
        cmd_refetch_art(args.refetch_art, load_config())
        sys.exit(0)
    if args.art_variants:
        cmd_art_variants(args.art_variants, args.kind, max(0, args.page), args.json)
        sys.exit(0)
    if args.sgdb_search:
        cmd_sgdb_search(args.sgdb_search, args.json)
        return
    if args.set_sgdb:
        cmd_set_sgdb(args.set_sgdb, args.sgdb_gid, args.sgdb_name, args.reset)
        return
    if args.set_art:
        cmd_set_art(args.set_art, args.kind, args.url, args.reset)
        sys.exit(0)
    if args.fill_art:
        cmd_fill_art(install_base, args.json, args.if_stale)
        sys.exit(0)
    if args.news:
        cmd_news(args.refresh, args.json)
        sys.exit(0)
    if args.news_add:
        cmd_news_feed('add', args.news_add[0], ' '.join(args.news_add[1:]) or None)
        sys.exit(0)
    if args.news_remove:
        cmd_news_feed('remove', args.news_remove, None)
        sys.exit(0)
    for flag, section in (('gamedb', 'all'), ('deals', 'deals'), ('upcoming', 'upcoming'),
                          ('backlog', 'backlog')):
        if getattr(args, flag):
            cmd_gamedb(section, args.refresh, args.json, args.time)
            sys.exit(0)
    if args.gamedb_lookup:
        from nexus import gamedb
        ids = [int(a) for a in args.gamedb_lookup if a.isdigit()]
        print(json.dumps({str(k): v for k, v in gamedb.lookup(ids).items()}))
        sys.exit(0)
    if args.protondb:
        cmd_protondb(args.protondb, args.refresh, args.json)
        sys.exit(0)
    if args.game_tips:
        cmd_game_tips(args.game_tips, args.json)
        sys.exit(0)
    if args.perf:
        cmd_perf(require_slug(args.perf[0], '--perf'), args.perf[1:], load_config())
        sys.exit(0)
    if args.repack_feed:
        cmd_repack_feed(args.refresh, args.json)
        sys.exit(0)
    if args.repack_index:
        cmd_repack_index(args.refresh, args.json)
        sys.exit(0)
    if args.repack_page:
        cmd_repack_page(args.repack_page, args.json)
        sys.exit(0)
    if args.repack_grab:
        cmd_repack_grab(args.repack_grab, args.appid, args.auto_install, args.json)
        sys.exit(0)
    if args.repack_reject:
        cmd_repack_reject(args.repack_reject)
        sys.exit(0)
    if (args.torrent_add or args.torrents or args.torrent_done or args.torrent_forget
            or args.torrent_pause or args.torrent_resume or args.torrent_remove
            or args.torrent_test or args.torrent_detect):
        cmd_torrent(args)
        sys.exit(0)
    if args.check_repack:
        cmd_check_repack(args.check_repack, args.json, args.clamav)
        sys.exit(0)
    if args.history:
        cmd_history(args.json)
        sys.exit(0)
    if args.store_info:
        cmd_store_info(args.store_info, args.json, args.refresh)
        sys.exit(0)
    if args.notify_test:
        from nexus import notify
        if not notify.configured():
            print("Nothing to send to: set [notify] ntfy = \"https://…\" and/or "
                  "kdeconnect = true in config.toml.", file=sys.stderr)
            sys.exit(1)
        errs = notify.push("Nexus", "Test notification — pushes work.")
        for e in errs:
            print(f"  [!] {e}", file=sys.stderr)
        print("Sent." if not errs else "Some targets failed.")
        sys.exit(1 if errs else 0)
    if args.apply_recipe:
        cmd_apply_recipe(*args.apply_recipe, load_config())
        sys.exit(0)
    if args.explain_log:
        cmd_explain_log(args.explain_log)
        sys.exit(0)
    if args.protons:
        cmd_protons(load_config(), args.json, args.refresh)
        sys.exit(0)
    if args.proton_releases:
        cmd_proton_releases(args.refresh, args.json)
        sys.exit(0)
    if args.proton_install:
        cmd_proton_install(args.proton_install, args.force)
        sys.exit(0)
    if args.proton_remove:
        cmd_proton_remove(args.proton_remove, args.force, load_config())
        sys.exit(0)
    if args.proton_migrate:
        cmd_proton_migrate(*args.proton_migrate, load_config())
        sys.exit(0)
    if args.cleanup_scan:
        cmd_cleanup_scan(install_base, args.json)
        sys.exit(0)
    if args.cleanup:
        cmd_cleanup(install_base, args.cleanup, args.forever, args.yes)
        sys.exit(0)
    if args.quarantine_restore:
        cmd_quarantine_restore(args.quarantine_restore)
        sys.exit(0)
    if args.library:
        cmd_library(install_base, as_json=args.json, owned=args.owned)
        sys.exit(0)
    if args.running:
        cmd_running(install_base, args.json)
        sys.exit(0)
    if args.open_url:
        cmd_open_url(args.open_url)
        sys.exit(0)
    if args.track_session:
        cmd_track_session(*args.track_session)
    if args.orphans:
        orphans = find_orphan_compatdata(install_base)
        rows = [{**o, 'path': str(o['path'])} for o in orphans]
        if args.json:
            print(json.dumps({'orphans': rows}))
        else:
            for o in rows:
                print(f"  {o['path']}  {o['size_bytes'] / 1e9:.2f} GB  ({o['reason']})"
                      + ("  SAVE DATA?" if o['has_savedata'] else ''))
        sys.exit(0)
    if args.list_protons:
        cmd_list_protons(load_config(), as_json=args.json)
        sys.exit(0)
    if args.prefix_tool:
        cmd_prefix_tool(args.prefix_tool[0], args.prefix_tool[1], load_config())
        sys.exit(0)
    if args.show_launch:
        cmd_show_launch(args.show_launch, load_config(), as_json=args.json)
        sys.exit(0)
    if args.set_launch:
        cmd_set_launch(args.set_launch[0], args.set_launch[1], load_config())
        sys.exit(0)
    if args.set_proton:
        cmd_set_proton(args.set_proton[0], args.set_proton[1], load_config())
        sys.exit(0)
    if args.from_protongen:
        cmd_from_protongen(args.from_protongen, load_config())
        sys.exit(0)
    if args.protongen_default:
        cmd_protongen_default(args.protongen_default, load_config())
        sys.exit(0)
    if args.apply_launch:
        config = load_config()
        slugs = (sorted(load_registry()['games']) if args.apply_launch == 'all'
                 else [args.apply_launch])
        for slug in slugs:
            cmd_apply(slug, config)
        sys.exit(0)
    if args.set_exe:
        cmd_set_exe(args.set_exe[0], args.set_exe[1], load_config())
        sys.exit(0)
    if args.rename:
        cmd_rename(args.rename[0], args.rename[1], load_config())
        sys.exit(0)

    repack_dir = Path(args.repack_dir).resolve()

    if not repack_dir.is_dir():
        print(f"Error: '{repack_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)
    if not args.ignore_safety:
        from nexus import safety
        verdict = safety.check(repack_dir)
        if verdict['verdict'] == 'danger':
            print("  [!] This repack looks tampered with:", file=sys.stderr)
            for f in verdict['findings']:
                if f['level'] == 'danger':
                    print(f"      {f['file']}: {f['why']}", file=sys.stderr)
            print("      Not installing it. If you're sure it's fine, pass --ignore-safety.",
                  file=sys.stderr)
            sys.exit(1)

    if args.from_torrent:
        _torrent_mark(args.from_torrent, install_started=time.strftime('%Y-%m-%dT%H:%M:%S'))

    # Warn about characters that break Wine's internal shell path handling
    BAD_CHARS = set('&|;<>()^%!')
    bad = [c for c in BAD_CHARS if c in str(repack_dir)]
    if bad:
        chars = ' '.join(f'"{c}"' for c in bad)
        print(f"\n  [!] WARNING: The repack folder path contains {chars}")
        print(f"      Wine's installer shell interprets these as special characters")
        print(f"      and will likely crash at low progress %.")
        print(f"      Rename the folder to remove them before continuing.")
        print(f"      e.g.  mv \"{repack_dir}\" \"{str(repack_dir).translate(str.maketrans({'&': 'and', '|': '-', ';': '-'}))}\"")
        print()
        if not args.yes:
            try:
                ans = input("  Continue anyway? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                sys.exit(0)
            if ans not in ('y', 'yes'):
                sys.exit(0)

    # Early soft warning about low RAM (a hard, re-checked gate runs again
    # right before setup.exe — memory can shift during verify/prefix-init)
    meminfo = read_meminfo()
    if meminfo:
        avail_mb = meminfo.get('MemAvailable', 0) // 1024
        if avail_mb < 3000:
            print(f"\n  [!] WARNING: Only {avail_mb} MB RAM available.")
            print(f"      FitGirl's decompressor needs ~3 GB free to avoid crashing.")
            print(f"      Close other applications (browser, Heroic, etc.) first.")
            print()

    # Same shape as the RAM warning above: an early soft heads-up here, and a
    # hard, re-checked gate right before setup.exe runs.
    repack_size = dir_size_bytes(repack_dir)
    if repack_size:
        try:
            free = shutil.disk_usage(existing_ancestor(install_base)).free
        except OSError:
            free = None
        if free is not None and free < repack_size * DISK_SPACE_WARN_MULTIPLIER:
            print(f"\n  [!] WARNING: Only {free / 1e9:.1f} GB free at {install_base}.")
            print(f"      This repack is {repack_size / 1e9:.1f} GB — installs often need "
                  f"as much again to extract, so this could run tight or fail partway through.")
            print()

    info = repack_info(repack_dir)
    if args.name:
        display_name = args.name
        slug = make_slug(args.name)
    else:
        display_name, slug = info['name'], info['slug']
    # Silent by default: FitGirl's Inno installer runs with no window into a
    # known folder (see inno_args). --interactive shows the setup window.
    unattended = args.unattended or (not args.interactive and
                                     load_config().get('unattended', True))

    # Prefix model: game_dir is the Proton compat-data dir; the actual Wine
    # prefix is game_dir/pfx (Proton always uses STEAM_COMPAT_DATA_PATH/pfx and
    # ignores a separately-set WINEPREFIX). Setting WINEPREFIX=game_dir/pfx keeps
    # winetricks pointed at the SAME prefix the game installs into.
    game_dir = install_base / slug
    wine_prefix = game_dir / 'pfx'
    proton = find_proton()
    runner = select_runner(load_config().get('runner', 'auto'), proton)
    if runner == 'umu':
        runner_label = f"umu-run + {proton.parent.name}"
    elif runner == 'proton':
        runner_label = proton.parent.name
    else:
        runner_label = "Wine (system)"

    base_env: dict[str, str] = {'WINEPREFIX': str(wine_prefix)}
    if runner in ('umu', 'proton'):
        base_env['STEAM_COMPAT_CLIENT_INSTALL_PATH'] = str(STEAM_ROOT)
        # umu derives its own compat-data path from WINEPREFIX; only the raw
        # `proton run` path needs STEAM_COMPAT_DATA_PATH set explicitly.
        if runner == 'proton':
            base_env['STEAM_COMPAT_DATA_PATH'] = str(game_dir)
    if args.debug:
        base_env['WINEDEBUG'] = 'warn,err,fixme'
        base_env['PROTON_LOG'] = '1'
        base_env['PROTON_LOG_DIR'] = str(LOG_DIR)
    else:
        # FitGirl installers spam Wine's default err+fixme channels, and we
        # capture that output — silencing it is a real speedup on a long install.
        base_env['WINEDEBUG'] = '-all'

    # Set up logger
    log_path: Path | None = None
    if args.log or args.debug:
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        log_path = LOG_DIR / f"{slug}-{ts}.log"
    log = Logger(log_path)

    log.section("FitGirl Installer")
    log.print(f"  Game:        {display_name}")
    log.print(f"  Slug:        {slug}")
    log.print(f"  Source:      {repack_dir}")
    log.print(f"  Runner:      {runner_label}")
    log.print(f"  Wine prefix: {wine_prefix}")
    if args.force and game_dir.exists():
        log.print(f"  --force:     the existing install will be moved aside, not deleted")
    if log.log_path:
        log.print(f"  Log file:    {log.log_path}")
    if args.debug:
        log.print(f"  Debug:       ON (WINEDEBUG=warn,err,fixme  PROTON_LOG=1)")
    log.print(f"  Installer:   {'silent (no setup window)' if unattended else 'setup window'}")
    log.print()

    # Write system info to log before anything else
    log.system_info(repack_dir, wine_prefix, runner_label, base_env)

    if not args.yes:
        try:
            ans = input("Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            log.print("\nAborted.")
            sys.exit(0)
        if ans in ('n', 'no'):
            log.print("Aborted.")
            sys.exit(0)

    # 1. Verify
    if args.verify:
        log.print("\n[1/4] Verifying files...")
        if not verify_bins(repack_dir):
            log.print("\nVerification FAILED. Re-download the corrupted files.")
            sys.exit(1)
        log.print("  All checked files OK.")
    else:
        log.print("\n[1/4] Skipping file verification (use --verify to enable).")

    # 2. Find installer
    setup_exe = find_setup_exe(repack_dir)
    if not setup_exe:
        log.print("Error: No setup.exe found in the repack directory.", flush=True)
        sys.exit(1)

    # 3. Prefix
    log.print("\n[2/4] Setting up prefix...")
    if args.force and game_dir.exists():
        # "Wipe" without destroying anything irrecoverable: saves live in the
        # old prefix, so move it aside for the user to delete once the fresh
        # install works (same rename-not-delete rule as compatdata cleanup).
        aside = game_dir.with_name(f"{game_dir.name}.old-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            game_dir.rename(aside)
        except OSError as e:
            log.print(f"  [!] --force: could not move {game_dir} aside ({e}).")
            sys.exit(1)
        log.print(f"  --force: moved the old install to {aside}")
        log.print(f"           (saves are still in there; delete it once the reinstall works)")
    # Created only now, after every "Proceed?" — aborting earlier leaves nothing
    # behind. Proton/umu's file-lock code expects the dir to exist, and +C
    # (no CoW/compression) only takes effect while it is still empty.
    game_dir.mkdir(parents=True, exist_ok=True)
    if disable_cow(game_dir):
        log.print(f"  Filesystem: CoW + compression disabled for {game_dir}")
    # Until the game is registered, the folder is an unfinished install that
    # the library can resume (same arguments, minus --force: resuming reuses
    # what's there) or discard.
    resume = [str(repack_dir) if a == args.repack_dir else a
              for a in sys.argv[1:] if a != '--force']
    if args.repack_dir not in sys.argv[1:]:
        resume.insert(0, str(repack_dir))
    write_incomplete_marker(game_dir, {
        'name': display_name, 'source': str(repack_dir), 'argv': resume,
        'started': datetime.now().isoformat(timespec='seconds')})
    _install_session(slug, display_name, game_dir)
    already_installed = (wine_prefix / 'drive_c').exists()
    if already_installed:
        log.print(f"  [!] '{slug}' already appears to be installed at {wine_prefix}.")
        log.print(f"      Continuing reuses the existing prefix (e.g. to install an update")
        log.print(f"      or DLC repack on top). Pass --force for a fresh prefix instead.")
        if not args.yes:
            try:
                ans = input("  Continue with the existing installation? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
            if ans not in ('y', 'yes'):
                log.print("Aborted.")
                sys.exit(0)
        fresh_prefix = False
    else:
        fresh_prefix = True
        init_prefix(game_dir, base_env, proton, runner, wine_prefix)

    # A fresh prefix (new slug, or --force reinit) may be standing in for one
    # that was uninstalled earlier — offer to reinject its saved backup.
    if fresh_prefix:
        backup = latest_save_backup(slug)
        if backup:
            if args.import_save:
                do_restore = True
            elif args.yes:
                do_restore = False
                log.print(f"  Found a save backup from a previous install "
                          f"({backup.name}) — NOT importing it (pass --import-save "
                          f"to opt in).")
            else:
                try:
                    ans = input(f"  Found a save backup from a previous install "
                                f"({backup.name}). Import it into the new "
                                f"prefix? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = 'n'
                do_restore = ans in ('y', 'yes')
            if do_restore:
                n, failed = restore_prefix_saves(slug, wine_prefix) or (0, [])
                if n:
                    log.print(f"  Restored {n} save file(s) from {backup.name}.")
                if failed:
                    log.print(f"  [!] {len(failed)} save file(s) from {backup.name} could "
                              f"not be restored, e.g. {failed[0]}")

    # Where a silent install goes: over the existing game when installing on
    # top (an update), else C:\\Games\\<slug>.
    target = None
    if unattended:
        target = (existing_install_root((registry_get(slug) or {}).get('exe'), wine_prefix)
                  if already_installed else None) or silent_target(wine_prefix, slug)
    setup_extra = inno_args(win_path(target, wine_prefix)) if target else []
    # Silent installs also skip the VC++/DirectX redists and the installer's
    # music; every install keeps setup from opening FitGirl's site at the end.
    # The user's own WINEDLLOVERRIDES (from the environment) is kept.
    skipped = setup_redists(setup_exe) if target else set()
    overrides = silent_setup_overrides(setup_exe, skipped) if target else NO_BROWSER
    setup_env = {**base_env, 'WINEDLLOVERRIDES': ';'.join(
        o for o in (os.environ.get('WINEDLLOVERRIDES'),
                    base_env.get('WINEDLLOVERRIDES'), overrides) if o)}

    # 4. Installer (bounded retry: a signal-killed attempt can retry in-place;
    #    a normal exit — success or the installer's own nonzero code — does not)
    for attempt in range(MAX_SETUP_RETRIES + 1):
        if not preflight_hard_memory_check(skip=args.skip_mem_check):
            log.print("\nAborted due to low memory.")
            sys.exit(1)
        if not preflight_hard_disk_check(install_base, repack_size, skip=args.skip_disk_check):
            log.print("\nAborted due to insufficient disk space.")
            sys.exit(1)

        suffix = f" (attempt {attempt + 1}/{MAX_SETUP_RETRIES + 1})" if attempt else ""
        log.print(f"\n[3/4] Running {setup_exe.name} via {runner_label}...{suffix}")
        if target:
            log.print(f"  Installing silently into {win_path(target, wine_prefix)} — no setup "
                      f"window (--interactive shows it).")
            log.print("  Skipping its VC++/DirectX redists (Proton ships them) and its music:")
            log.print(f"    {', '.join(sorted(skipped))}\n")
        else:
            log.print("  The installer GUI will open. Follow the on-screen steps.")
            log.print("  You can install to any path — it stays inside this prefix.\n")

        progress = InstallProgress(target) if target else None
        if progress:
            progress.start()
        try:
            if runner == 'umu':
                rc = run(['umu-run', str(setup_exe), *setup_extra],
                          extra_env=_umu_env(setup_env, wine_prefix, proton),
                          capture=True, diagnose=True, label="setup.exe (via umu-run)")
            elif runner == 'proton':
                rc = run([str(proton), 'run', str(setup_exe), *setup_extra], extra_env=setup_env,
                          capture=True, diagnose=True, label="setup.exe (via Proton)")
            else:
                rc = run(['wine', str(setup_exe), *setup_extra], extra_env=setup_env,
                          capture=True, diagnose=True, label="setup.exe (via Wine)")
        finally:
            if progress:
                progress.stop()

        if rc >= 0:
            if rc == 0:
                log.print("\n  Installer closed.")
            else:
                log.print(f"\n  [!] Installer exited with code {rc} — it may not have "
                          f"finished. Checking for the game anyway...")
            break

        if attempt == MAX_SETUP_RETRIES:
            log.print("\n  [!] Max retries reached — giving up on the installer step.")
            log.print(f"      Prefix retained at {wine_prefix} for manual retry:")
            if runner == 'umu':
                log.print(f"        WINEPREFIX=\"{wine_prefix}\" GAMEID=umu-0 "
                          f"PROTONPATH=\"{proton.parent}\" umu-run \"{setup_exe}\"")
            else:
                log.print(f"        WINEPREFIX=\"{wine_prefix}\" wine \"{setup_exe}\"")
            sys.exit(1)

        if args.yes:
            ans = 'y'
            log.print("  Retrying in the same prefix (--yes)...")
        else:
            try:
                ans = input("  Retry running the installer in the same prefix? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
        if ans in ('n', 'no'):
            log.print("Aborted.")
            sys.exit(1)

    # 5. Deps — opt-in. Proton bundles the VC++ runtimes, d3dcompiler_47 and
    #    DXVK, so the winetricks pass is dead weight (and harmful for dxvk) on
    #    every modern build. --deps is the escape hatch for the rare game that
    #    genuinely needs a verb.
    if args.deps and not args.no_deps:
        log.print(f"\n[4/4] Installing requested dependencies ({args.deps})...")
        install_deps(wine_prefix, base_env, spec=args.deps, runner=runner, proton=proton)
    else:
        log.print("\n[4/4] Skipping winetricks deps — Proton ships the runtimes.")
        log.print("      Need one anyway?  --deps vcrun2019[,other_verb]")

    # 6. Launch script
    log.print("\nSearching for game executable...")
    cands = exe_candidates(target) if target else []
    if not cands:
        cands = exe_candidates(wine_prefix / 'drive_c')
    game_exe = cands[0][1] if cands else None
    if game_exe:
        log.print(f"  Found: {game_exe.relative_to(wine_prefix)}")
        # The pick is the largest exe; the GUI shows the runners-up so a
        # wrong guess (a launcher, a bundled tool) is one click to fix.
        emit_event('exe-candidates', {'slug': slug, 'chosen': str(game_exe), 'candidates': [
            {'path': str(p), 'size': n} for n, p in cands[:3]]})
    else:
        # No real game exe (Windows/Steam stubs are skipped) usually means the
        # installer never actually ran — FitGirl's setup.exe often crashes on
        # launch under a non-GE Proton. Warn loudly and DON'T create phantom
        # Steam/desktop entries for a game that isn't there.
        log.print("\n  [!] NO GAME EXECUTABLE FOUND — the install did not complete.")
        build = proton.parent.name if proton else None
        if WINE_ABORTS:
            fn = re.search(r'unimplemented function (\S+?),', WINE_ABORTS[0])
            log.print(f"      Wine aborted before setup.exe could run: it called "
                      f"{fn.group(1) if fn else 'a function'},")
            log.print(f"      which {build or 'this Proton'} doesn't have. That means the "
                      f"Proton build is")
            log.print("      damaged (its Windows DLLs don't match its Linux side) — "
                      "not the repack.")
            why = damaged_protons().get(build or '')
            if why:
                log.print(f"      {build}: {why}.")
            tag = proton_repair_tag(build) if build else None
            log.print(f"      Fix: nexus-cli --proton-install {tag} --force, then retry."
                      if tag else "      Fix: reinstall that Proton build, then retry.")
            log.print(f"      Prefix kept for retry: {wine_prefix}")
            log.print("\n  Skipping Steam/desktop/artwork (nothing to launch).")
            log.close()
            sys.exit(1)
        log.print("      The prefix contains only Windows/Steam stubs, so setup.exe")
        log.print("      likely crashed on launch (a quick exit + no extraction).")
        log.print("      FitGirl installers need a working x86-64 GE-Proton; the")
        log.print(f"      current runner is '{runner_label}'.")
        log.print("      Fix: check the Proton build (nexus-cli --doctor), or install a")
        log.print(f"      GE-Proton release (--proton-install latest), then retry.")
        log.print(f"      Prefix kept for retry: {wine_prefix}")
        if target:
            log.print("      The silent installer may not suit this repack: try again with")
            log.print("      the setup window (--interactive).")
            emit_event('retry-interactive', {'slug': slug})
        log.print("\n  Skipping Steam/desktop/artwork (nothing to launch).")
        log.close()
        sys.exit(1)

    # The Proton the game is *played* with (may differ from the installer's:
    # see resolve_game_proton). A reinstall keeps an existing launch profile.
    config = load_config()
    prev = registry_get(slug) or {}
    prof = prev.get('launch')
    pg_default = None
    if args.use_protongen_default and not profile_owns_options(prof):
        pg_default = protongen_default_profile(prev)
        if pg_default:
            prof = pg_default
            prev = {**prev, 'launch': prof}
            log.print(f"\nUsing protongen's default profile: "
                      f"{format_launch_options(prof) or '(no options)'}")
        else:
            log.print("\n  [!] protongen has no default profile — launch options left empty.")
    play_proton = resolve_game_proton(prev, config) or proton
    umu_gameid = prev.get('umu_gameid')
    if runner == 'umu' and not umu_gameid:
        umu_gameid = resolve_umu_gameid(display_name)
        if umu_gameid:
            log.print(f"  umu-protonfixes id: {umu_gameid}")
    launch = write_launch_script(game_dir / 'launch.sh', game_dir, wine_prefix,
                                 game_exe, slug, play_proton, display_name, runner=runner,
                                 launch=prof, gameid=umu_gameid)

    # 7. Integrations (Steam shortcut, desktop entry) — best-effort, never fatal
    artwork = None
    if not args.no_artwork:
        log.print("\nFetching artwork from SteamGridDB...")
        artwork = _best_effort("Artwork", fetch_artwork, display_name, slug, config)
        if artwork:
            log.print(f"  Got: {', '.join(sorted(k for k in artwork if not k.endswith('_url')))}")
        else:
            log.print("  No artwork (missing API key, no match, or offline) — skipping.")

    steam_appid_val = None
    steam_pending = False
    if not args.no_steam and config.get('steam', True) and args.defer_steam \
            and steam_is_running():
        # Steam would overwrite the shortcut on exit; the GUI adds it with
        # `--steam <slug>` once Steam is closed, instead of the whole install
        # waiting for that.
        steam_pending = True
        log.print("\nSteam is running — the Steam shortcut is added once Steam is closed.")
        emit_event('steam-pending', {'slug': slug})
    elif not args.no_steam and config.get('steam', True):
        steam_appid_val = _best_effort(
            "The Steam shortcut", add_steam_shortcut_native,
            display_name, slug, game_exe, game_dir, play_proton, artwork,
            launch_options=format_launch_options(prof) if profile_owns_options(prof) else None)

    desktop_path = None
    if not args.no_desktop and config.get('desktop', True):
        desktop_path = _best_effort("The app-menu entry", write_desktop_entry,
                                    display_name, slug, launch, artwork)

    heroic_app_name = None
    if not args.no_heroic and config.get('heroic', True):
        heroic_app_name = _best_effort(
            "The Heroic entry", add_heroic_entry,
            display_name, slug, game_exe, wine_prefix, play_proton, artwork,
            force_proton=_proton_is_explicit(prev, config),
            launch=prof if profile_owns_options(prof) else None)

    register_game(
        slug=slug, display_name=display_name, exe=game_exe, compatdata=game_dir,
        wine_prefix=wine_prefix, launcher=launch, source='install',
        install_base=install_base, steam_appid=steam_appid_val or prev.get('steam_appid'),
        has_desktop=bool(desktop_path), artwork_dir=ARTWORK_DIR / slug,
        heroic_app_name=heroic_app_name,
        proton=play_proton.parent.name if play_proton else None,
        umu_gameid=umu_gameid,
        repack={'repack_fingerprint': repack_fingerprint(repack_dir),
                'repack_version': info['version'], 'repack_source': str(repack_dir),
                'repack_installed_at': datetime.now().isoformat(timespec='seconds')})
    clear_incomplete_marker(game_dir)
    if pg_default:
        _save_profile(slug, pg_default, "protongen default profile")
    trashed = False
    if args.trash_repack or config.get('trash_repack'):
        from nexus import cleanup as C
        try:
            C.trash(repack_dir)
            trashed = True
            log.print(f"\nMoved the repack folder to the Trash: {repack_dir}")
        except OSError as e:
            log.print(f"\n  [!] Could not move the repack to the Trash ({e}).")
    if args.from_torrent:
        _torrent_mark(args.from_torrent, installed=True, slug=slug)
    emit_event('installed', {'slug': slug, 'name': display_name, 'source': str(repack_dir),
                             'repack_trashed': trashed, 'steam_pending': steam_pending})

    log.section("Done!")
    log.print(f"  Wine prefix:   {wine_prefix}")
    log.print(f"  Launch script: {launch}")
    if desktop_path:
        log.print(f"  Desktop entry: {desktop_path}")
    if steam_appid_val:
        log.print(f"  Steam:         added to library (restart Steam to see it)")
    elif steam_pending:
        log.print(f"  Steam:         waiting for Steam to close (nexus-cli --steam {slug})")
    if heroic_app_name:
        log.print(f"  Heroic:        added as a sideloaded game (restart Heroic to see it)")
    if log.log_path:
        log.print(f"  Log file:      {log.log_path}")
    if game_exe:
        log.print(f"\n  To play:")
        log.print(f"    bash \"{launch}\"")
    else:
        log.print(f"\n  Edit {launch} to set the game .exe, then:")
        log.print(f"    bash \"{launch}\"")
    log.print()

    log.close()


if __name__ == '__main__':
    main()
