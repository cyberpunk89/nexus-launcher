"""Running Windows games: Proton discovery, umu, Wine prefixes, launch.sh."""

import hashlib
import io
import platform
import re
import shlex
import shutil
import sys
from pathlib import Path

from .common import _cached_read, atomic_write, log, run, SECRET_ENV, STEAM_ROOT, WINE_ENV_DROP


PROTON_SEARCH_DIRS = list(dict.fromkeys([
    STEAM_ROOT / "compatibilitytools.d",
    Path.home() / ".local/share/Steam/compatibilitytools.d",
    Path.home() / ".steam/root/compatibilitytools.d",
    Path("/usr/share/steam/compatibilitytools.d"),
]))


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


# Proton discovery globs every compat-tool dir and reads an ELF header per
# build, and damage checks can hash core DLLs; one command asked for it a
# dozen times (once per game, again in find_proton, ...). Cached until a
# compat-tool dir changes (adding, removing or replacing a build touches it).
_PROTON_SCAN: tuple | None = None


def _proton_dirs_sig() -> tuple:
    out = []
    for d in PROTON_SEARCH_DIRS:
        try:
            out.append((str(d), d.stat().st_mtime_ns))
        except OSError:
            out.append((str(d), None))
    return tuple(out)


def _scan_protons() -> dict:
    """{'found': {name: proton bin}, 'skipped': [wrong-arch names],
    'damaged': {name: why} | None (computed on first use)} — cached."""
    global _PROTON_SCAN
    sig = _proton_dirs_sig()
    if _PROTON_SCAN is not None and _PROTON_SCAN[0] == sig:
        return _PROTON_SCAN[1]
    found: dict[str, Path] = {}
    skipped: list[str] = []
    for search_dir in PROTON_SEARCH_DIRS:
        if not search_dir.is_dir():
            continue
        for proton_bin in search_dir.glob("*/proton"):
            if not proton_bin.is_file():
                continue
            if _proton_wine_is_native(proton_bin.parent):
                found.setdefault(proton_bin.parent.name, proton_bin)
            else:
                skipped.append(proton_bin.parent.name)
    scan = {'found': found, 'skipped': skipped, 'damaged': None}
    _PROTON_SCAN = (sig, scan)
    return scan


def damaged_protons() -> dict[str, str]:
    """{build folder name: why} for installed builds whose files were changed
    after they were installed (see protons.damage) — they can't run anything."""
    scan = _scan_protons()
    if scan['damaged'] is None:
        from nexus import protons as PR
        builds = [b.parent for b in scan['found'].values()]
        scan['damaged'] = {b.name: why for b in builds if (why := PR.damage(b, builds))}
    return dict(scan['damaged'])


def find_proton() -> Path | None:
    candidates: list[tuple[str, Path]] = []
    scan = _scan_protons()
    skipped = scan['skipped']
    broken = damaged_protons()
    for name, proton_bin in scan['found'].items():
        if name in broken:
            if name not in _WARNED_DAMAGE:          # stderr, once: --json stdout is parsed
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
    return dict(_scan_protons()['found'])


def proton_tool_name(proton: Path) -> str:
    """The name Steam keys this compat tool by in config.vdf's CompatToolMapping:
    the internal name inside its compatibilitytool.vdf. That usually equals the
    directory name, but packaged/renamed builds can differ — and a mapping that
    names a tool Steam doesn't know is silently ignored."""
    manifest = proton.parent / 'compatibilitytool.vdf'
    tools = _cached_read(manifest, _load_vdf_text).get('compatibilitytools', {}) \
        .get('compat_tools', {})
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


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .repack import COMMON_DEPS  # noqa: E402
from .steam import _load_vdf_text  # noqa: E402
