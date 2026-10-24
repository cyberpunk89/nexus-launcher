"""A FitGirl repack on disk: names, fingerprints, Inno Setup options, MD5
checks, install progress and the memory/disk pre-flight checks."""

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .common import atomic_write, emit_event, existing_ancestor, log, make_slug
from nexus import platform
from nexus.inno import inno_header


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
# Wine's console window. A FitGirl setup's last [Run] step is `cmd.exe /c
# host.cmd` ("Redirect fake FitGirl sites to the real one"), started from the
# installer's GUI, so Wine opens a console window for it and a silent install
# ends with "Applying redirection rules for fake FitGirl sites..." on screen.
# With conhost.exe disabled a console program runs without a console: the
# script and everything it starts still run (the prefix's hosts file is still
# written), just nowhere to be seen. cmd.exe itself can't be the one disabled:
# FitGirl's unpacking runs {cmd} lines too.
CONSOLE_HOST = 'conhost.exe'


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
    redists; the disabled audio drivers mute its music, the disabled console
    host its console windows (CONSOLE_HOST). Only setup.exe (and what it
    spawns) gets these; the game itself is untouched."""
    names = (sorted(setup_redists(setup_exe) if skip is None else skip)
             + list(AUDIO_DRIVERS) + [CONSOLE_HOST])
    return ';'.join(f'{n}=d' for n in names)


def silent_target(wine_prefix: Path, slug: str) -> Path:
    """Where a new silent install puts the game: C:\\Games\\<slug>."""
    return wine_prefix / 'drive_c' / 'Games' / slug


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
        data = json.loads((game_dir / INCOMPLETE_MARKER).read_text(encoding='utf-8'))
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
    for line in md5_path.read_text(errors='ignore', encoding='utf-8').splitlines():
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


def find_setup_exe(repack_dir: Path) -> Path | None:
    for name in ['setup.exe', 'Setup.exe', 'SETUP.EXE']:
        p = repack_dir / name
        if p.exists():
            return p
    return None


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
    if -rc == getattr(signal, 'SIGKILL', None):
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


def _beside_unity(exe: Path) -> bool:
    return any((exe.parent / dll).is_file() for dll in ('UnityPlayer.dll', 'GameAssembly.dll'))


def exe_candidates(root: Path) -> list[tuple[int, Path]]:
    """Likely game exes under `root`, largest first: every .exe over 500 KB
    that isn't an uninstaller / redist / crash handler / setup helper, and
    a smaller one sitting next to Unity's runtime (a Unity game's exe is a
    thin launcher; the game is in UnityPlayer.dll / GameAssembly.dll)."""
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
        if size < 500_000 and not (size >= 50_000 and _beside_unity(exe)):
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


def read_meminfo() -> dict[str, int]:
    """/proc/meminfo's fields in kB (MemAvailable, SwapFree, ...); {} when
    unreadable. See platform.meminfo."""
    return platform.meminfo()


def _swap_is_compressed() -> bool:
    """True if active swap is zram (compressed, RAM-backed) rather than real disk swap."""
    try:
        lines = Path('/proc/swaps').read_text(encoding='utf-8').splitlines()[1:]
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


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .config import load_config  # noqa: E402
from .registry import load_registry, registry_get  # noqa: E402
