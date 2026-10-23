"""Moving an installed game to another folder or drive (`--move SLUG DIR`):
the files first (a rename when it can be, else a copy — a reflink copy on
the same btrfs volume, so instant — checked file by file), then everything
that names the old place: the registry, launch.sh, the app-menu entry, the
Steam shortcut and its compatdata link, Heroic's entry and prefix. The old
copy is kept as `<slug>.old-<time>` until you remove it (Storage → Clean
up lists it), so a move that goes wrong loses nothing."""

import errno
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .common import emit_event, log, Logger, use_logger

HEADROOM = 2 << 30              # free space to leave on the destination


def file_map(root: Path) -> dict[str, int]:
    """{relative path: size} for every file and symlink under root (symlinks
    not followed: a prefix's dosdevices point at /)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = -1 if os.path.islink(p) else st.st_size
    return out


def rebase(entry: dict, old: Path, new: Path) -> dict:
    """The registry fields that pointed into `old`, pointed into `new`."""
    out = {}
    o = str(old).rstrip('/')
    for k in ('compatdata', 'wine_prefix', 'launcher', 'exe'):
        v = entry.get(k)
        if isinstance(v, str) and (v == o or v.startswith(o + '/')):
            out[k] = str(new) + v[len(o):]
    out['install_base'] = str(new.parent)
    return out


def _copy(src: Path, dest: Path, total: int) -> None:
    """cp -a (reflinked where the filesystem can), reporting progress as
    `@nexus move {done,total}` events while it runs."""
    stop = threading.Event()

    def report():
        while not stop.wait(2):
            emit_event('move', {'done': dir_size_bytes(dest), 'total': total})
    t = threading.Thread(target=report, daemon=True)
    t.start()
    try:
        r = subprocess.run(['cp', '-a', '--reflink=auto', f"{src}/.", f"{dest}/"],
                           capture_output=True, text=True)
    finally:
        stop.set()
        t.join()
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or f"cp exited {r.returncode}")
    emit_event('move', {'done': total, 'total': total})


def _fail(msg: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({'ok': False, 'error': msg}))
    print(f"  [!] {msg}", file=sys.stderr)
    return 1


def cmd_move(install_base: Path, slug: str, dest_base: str, as_json: bool = False) -> int:
    use_logger(Logger())
    entry = registry_get(slug)
    if not entry:
        return _fail(f"'{slug}' is not a managed game (see --list)", as_json)
    src = _owned_game_dir(entry, install_base, slug)
    if src is None or not src.is_dir():
        return _fail("only games Nexus installed can be moved (this one lives where you "
                     "put it)", as_json)
    base = Path(dest_base).expanduser()
    try:
        base = base.resolve()
    except OSError:
        pass
    dest = base / slug
    if base == src.parent.resolve():
        return _fail(f"'{slug}' is already in {base}", as_json)
    if dest.exists() or dest.is_symlink():
        return _fail(f"{dest} already exists", as_json)
    if prefix_processes(_prefix_markers(entry)):
        return _fail(f"'{entry['display_name']}' is running — quit it first", as_json)
    if entry.get('steam_appid') and steam_is_running():
        return _fail("Steam is running — quit it first (it rewrites its shortcuts on exit)",
                     as_json)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail(f"can't create {base}: {e}", as_json)
    if not os.access(base, os.W_OK):
        return _fail(f"{base} isn't writable", as_json)
    snaps = snapshot_dirs(entry, slug)

    log.print(f"Moving {entry['display_name']} to {dest}")
    moved = False
    try:
        os.rename(src, dest)                    # same filesystem (and subvolume): instant
        moved = True
        log.print("  Renamed in place.")
    except OSError as e:
        if e.errno != errno.EXDEV:
            return _fail(f"couldn't move it: {e}", as_json)
    if not moved:
        total = dir_size_bytes(src)
        free = shutil.disk_usage(base).free
        if free < total + HEADROOM:
            return _fail(f"not enough space in {base}: {fmt_gb(total)} needed, "
                         f"{fmt_gb(free)} free", as_json)
        log.print(f"  Copying {fmt_gb(total)}…")
        try:
            make_game_dir(dest)                 # a subvolume on btrfs, NOCOW (snapshots.py)
            _copy(src, dest, total)
        except (OSError, SnapshotError) as e:
            remove_game_dir(dest)
            return _fail(f"copying failed, nothing was changed: {e}", as_json)
        a, b = file_map(src), file_map(dest)
        if a != b:
            missing = sorted(set(a) - set(b))[:3] or \
                [k for k in a if a[k] != b.get(k)][:3]
            remove_game_dir(dest)
            return _fail("the copy doesn't match the original (e.g. "
                         f"{', '.join(missing)}), nothing was changed", as_json)
        log.print(f"  Copied and checked {len(a)} files.")

    update_registry_entry(slug, **rebase(entry, src, dest))
    cmd_apply(slug, load_config())
    fix_heroic_prefix(registry_get(slug))
    old = None
    if not moved:
        old = src.with_name(f"{src.name}.old-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            os.rename(src, old)
            log.print(f"  The old copy is kept as {old} — remove it in Storage → Clean up.")
        except OSError as e:
            log.print(f"  [!] Couldn't set the old copy aside ({e}); it's still at {src}.")
            old = src
    for d in snaps:                             # snapshots of the old place: of no use now
        remove_game_dir(d)
    if snaps:
        log.print(f"  Removed {len(snaps)} snapshot(s) of the old location.")
    update_registry_size(slug, dir_size_bytes(dest))
    log.print(f"Done: {entry['display_name']} is now in {dest}.")
    if as_json:
        print(json.dumps({'ok': True, 'dest': str(dest), 'old': str(old) if old else None,
                          'renamed': moved}))
    return 0


def fmt_gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def fix_heroic_prefix(entry: dict | None) -> None:
    """Point the game's Heroic GamesConfig winePrefix at its current prefix
    (written once at creation, so a move must update it itself)."""
    if not entry or not entry.get('heroic_app_name') or not entry.get('wine_prefix'):
        return
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        return
    gc_path = cfg_dir / 'GamesConfig' / f"{entry['heroic_app_name']}.json"
    try:
        gc = json.loads(gc_path.read_text())
    except (OSError, ValueError):
        return                                  # none, or unreadable: never rewritten
    inner = gc.get(entry['heroic_app_name']) if isinstance(gc, dict) else None
    if not isinstance(inner, dict) or inner.get('winePrefix') == entry['wine_prefix']:
        return
    inner['winePrefix'] = entry['wine_prefix']
    try:
        backup_file(gc_path)
        atomic_write(gc_path, json.dumps(gc, indent=4))
        log.print("  Heroic: prefix updated.")
    except OSError as e:
        log.print(f"  [!] Heroic: couldn't update {gc_path.name} ({e}).")


def move_targets(install_base: Path) -> list[dict]:
    """Where a game could go: the install base, every Steam library and the
    bases games already live in — with free space."""
    from nexus import steamlib
    seen, out = set(), []
    cands = [install_base] + [Path(e['install_base']) for e in
                              load_registry()['games'].values() if e.get('install_base')]
    try:
        cands += [Path(lib) / 'Nexus' for lib in steamlib.library_folders(STEAM_ROOT)]
    except Exception:                           # noqa: BLE001 — Steam's file is optional
        pass
    for p in cands:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            free = shutil.disk_usage(existing_ancestor(p)).free
        except OSError:
            continue
        out.append({'path': key, 'free': free, 'exists': p.is_dir()})
    return out


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import cmd_apply  # noqa: E402
from .common import (atomic_write, backup_file, dir_size_bytes, existing_ancestor,
    STEAM_ROOT)  # noqa: E402
from .config import load_config  # noqa: E402
from .heroic import find_heroic_config  # noqa: E402
from .registry import (load_registry, registry_get, update_registry_entry,
    update_registry_size)  # noqa: E402
from .snapshots import make_game_dir, remove_game_dir, snapshot_dirs, SnapshotError  # noqa: E402
from .steam import steam_is_running  # noqa: E402
from .sunshine import _owned_game_dir, _prefix_markers, prefix_processes  # noqa: E402
