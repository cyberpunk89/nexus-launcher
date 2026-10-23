"""Adding any Windows game, not only a FitGirl repack (`--import PATH`): a
GOG-style offline installer (run silently when it's an Inno Setup one, else
with its own window) or a folder that already holds the game (copied in —
reflinked where the filesystem can). Either way the game gets the same
place as a repack: its own folder and prefix under the install base,
launch.sh, Steam / Heroic / app-menu entries, the registry (local = true,
so the repack-only features leave it alone) — and it can be uninstalled.

None of FitGirl's steps apply: no MD5 check, no repack safety allowlist
(its scan only knows FitGirl's layout), no redist skipping."""

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .common import log, LOG_DIR, Logger, make_slug, run, STEAM_ROOT, use_logger, win_path

_GOG_NAME = re.compile(r'^setup_(.+?)(?:_v?\d[\w.]*)?(?:_\(\d+bit\))?(?:_\(\d+\))*$',
                       re.IGNORECASE)


def guess_name(path: Path) -> str:
    """A display name from an installer or folder: the installer's own
    ProductName when it has one, else the file/folder name tidied up
    (GOG's setup_<name>_<version>_(64bit)_(<build>).exe included)."""
    if path.is_file():
        name = setup_product_name(path)
        if name and name.strip() and 'setup' not in name.lower():
            return name.strip()
        stem = path.stem
        m = _GOG_NAME.match(stem)
        if m:
            stem = m.group(1)
    else:
        stem = path.name
    stem = re.sub(r'[_.]+', ' ', stem).strip()
    stem = clean_repack_name(stem)[0] or stem
    return stem[:1].upper() + stem[1:] if stem.islower() else stem


def _runner_env(game_dir: Path, wine_prefix: Path, runner: str, debug: bool) -> dict:
    env = {'WINEPREFIX': str(wine_prefix),
           'WINEDEBUG': 'warn,err,fixme' if debug else '-all'}
    if runner in ('umu', 'proton'):
        env['STEAM_COMPAT_CLIENT_INSTALL_PATH'] = str(STEAM_ROOT)
        if runner == 'proton':
            env['STEAM_COMPAT_DATA_PATH'] = str(game_dir)
    return env


def run_windows_exe(exe: Path, extra: list[str], runner: str, proton: Path | None,
                    env: dict, wine_prefix: Path) -> int:
    if runner == 'umu':
        return run(['umu-run', str(exe), *extra], extra_env=_umu_env(env, wine_prefix, proton),
                   capture=True, diagnose=True, label=f"{exe.name} (via umu-run)")
    if runner == 'proton':
        return run([str(proton), 'run', str(exe), *extra], extra_env=env, capture=True,
                   diagnose=True, label=f"{exe.name} (via Proton)")
    return run(['wine', str(exe), *extra], extra_env=env, capture=True, diagnose=True,
               label=f"{exe.name} (via Wine)")


def cmd_import(args, install_base: Path) -> None:
    src = Path(args.import_path).expanduser()
    try:
        src = src.resolve()
    except OSError:
        pass
    is_installer = src.is_file() and src.suffix.lower() in ('.exe', '.msi')
    if not (is_installer or src.is_dir()):
        print(f"Error: {src} is neither a Windows installer (.exe/.msi) nor a folder",
              file=sys.stderr)
        sys.exit(1)
    display_name = args.name or guess_name(src)
    slug = make_slug(display_name)
    game_dir = install_base / slug
    wine_prefix = game_dir / 'pfx'
    if game_dir.exists() or registry_get(slug):
        print(f"Error: '{slug}' already exists ({game_dir}). Pick another name with --name.",
              file=sys.stderr)
        sys.exit(1)
    if src.is_dir():
        try:
            inside = src.resolve().is_relative_to(install_base.resolve())
        except OSError:
            inside = False
        if inside:
            print("Error: that folder is inside the install folder already — use "
                  "--reindex / Add existing games instead.", file=sys.stderr)
            sys.exit(1)

    proton = find_proton()
    runner = select_runner(load_config().get('runner', 'auto'), proton)
    log_path = (LOG_DIR / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}.log"
                if args.log or args.debug else None)
    use_logger(Logger(log_path))
    log.section("Adding a game")
    log.print(f"  Game:        {display_name}")
    log.print(f"  Slug:        {slug}")
    log.print(f"  From:        {src} ({'installer' if is_installer else 'folder'})")
    log.print(f"  Wine prefix: {wine_prefix}")
    if args.clamav:
        from nexus import safety
        # Programs only, and a bounded number: a scan, not a full audit.
        files = [src] if src.is_file() else [
            p for p in src.rglob('*') if p.suffix.lower() in ('.exe', '.dll', '.msi')
            and p.is_file() and p.stat().st_size < 64 << 20][:2000]
        r = safety.clamscan(files)
        if r.get('infected'):
            log.print(f"  [!] ClamAV: {', '.join(map(str, r['infected'][:3]))} — not adding it.")
            sys.exit(2)
        log.print("  ClamAV: " + ("nothing found" if r.get('available', True) else
                                  "not installed — not scanned"))

    if make_game_dir(game_dir):
        log.print("  Filesystem: a btrfs subvolume (snapshots can undo updates)")
    write_incomplete_marker(game_dir, {'name': display_name, 'slug': slug,
                                       'import': str(src),
                                       'started': datetime.now().isoformat(timespec='seconds')})
    env = _runner_env(game_dir, wine_prefix, runner, args.debug)
    log.print("\n[1/3] Setting up the prefix...")
    init_prefix(game_dir, env, proton, runner, wine_prefix)
    target = silent_target(wine_prefix, slug)

    if is_installer:
        silent = (not args.interactive and src.suffix.lower() == '.exe'
                  and inno_header(src) is not None)
        extra = inno_args(win_path(target, wine_prefix)) if silent else []
        log.print(f"\n[2/3] Running {src.name}"
                  + (" silently (Inno Setup)" if silent else " — follow its window") + "...")
        if src.suffix.lower() == '.msi':
            rc = run_windows_exe(Path('msiexec'), ['/i', str(src)], runner, proton, env,
                                 wine_prefix)
        else:
            rc = run_windows_exe(src, extra, runner, proton, env, wine_prefix)
        log.print("  Installer closed." if rc == 0 else
                  f"  [!] The installer exited with code {rc}. Checking for the game anyway...")
    else:
        log.print(f"\n[2/3] Copying {src.name} into the prefix...")
        target.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(['cp', '-a', '--reflink=auto', f"{src}/.", f"{target}/"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.print(f"  [!] Copying failed: {r.stderr.strip()}")
            log.print(f"      Discard it with: nexus-cli --discard-incomplete {slug}")
            sys.exit(1)

    log.print("\n[3/3] Looking for the game's .exe...")
    cands = exe_candidates(target) or exe_candidates(wine_prefix / 'drive_c')
    if not cands:
        log.print("  [!] No game .exe found — nothing was added.")
        log.print(f"      Discard it with: nexus-cli --discard-incomplete {slug}")
        log.close()
        sys.exit(1)
    game_exe = cands[0][1]
    log.print(f"  Found: {game_exe.relative_to(wine_prefix)}")
    emit_event('exe-candidates', {'slug': slug, 'chosen': str(game_exe), 'candidates': [
        {'path': str(p), 'size': n} for n, p in cands[:3]]})
    done = _finish_install(args, slug=slug, display_name=display_name, game_dir=game_dir,
                           wine_prefix=wine_prefix, game_exe=game_exe,
                           install_base=install_base, runner=runner, proton=proton,
                           extra={'local': True, 'import_source': str(src)})
    emit_event('installed', {'slug': slug, 'name': display_name, 'source': str(src),
                             'repack_trashed': False, 'steam_pending': done['steam_pending']})
    log.section("Done!")
    log.print(f"  {display_name} is in your library. Launch script: {done['launch']}")
    log.close()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from nexus.inno import inno_header  # noqa: E402
from .common import emit_event  # noqa: E402
from .config import load_config  # noqa: E402
from .install import _finish_install  # noqa: E402
from .registry import registry_get  # noqa: E402
from .repack import (clean_repack_name, exe_candidates, inno_args, setup_product_name,
    silent_target, write_incomplete_marker)  # noqa: E402
from .runner import _umu_env, find_proton, init_prefix, select_runner  # noqa: E402
from .snapshots import make_game_dir  # noqa: E402
