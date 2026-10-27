"""Game snapshots on btrfs — "Undo update". Each game Nexus installs on a
btrfs volume gets its own subvolume, which can be snapshotted in an instant
(the snapshot shares every byte until something changes). Before a repack
update, a Proton change, winetricks or a reinstall, Nexus takes one; Roll
back puts it back in place.

Everything here runs as the user, no root: creating subvolumes and
snapshots is allowed for their owner, and a writable snapshot is removed by
emptying it and `rmdir` (no `user_subvol_rm_allowed` needed). Snapshots
live next to the games, in `<install base>/.nexus-snapshots/<slug>/
<time>-<reason>`, so they stay on the same volume. Games that aren't
subvolumes yet (installed before this) are converted with --snapshots-enable:
a reflink copy into a new subvolume, instant and without extra space."""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from nexus import features

from .common import emit_event, log, Logger, use_logger

SNAP_DIR = '.nexus-snapshots'
REASON_RE = re.compile(r'^[a-z][a-z0-9-]{0,30}$')
NAME_RE = re.compile(r'^(\d{8}-\d{6})-([a-z][a-z0-9-]{0,30})$')
REASONS = {'manual': "Snapshot", 'update': "Before an update", 'proton': "Before a Proton change",
           'winetricks': "Before winetricks", 'reinstall': "Before a reinstall",
           'before-rollback': "Before a roll back", 'deps': "Before installing extras"}


class SnapshotError(Exception):
    pass


# ── the filesystem ───────────────────────────────────────────────────────

def fs_type(path: Path) -> str | None:
    """The filesystem type `path` (or its nearest existing ancestor) is on,
    from /proc/self/mountinfo."""
    p = existing_ancestor(Path(path))
    try:
        real = os.path.realpath(p)
        lines = Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    best, kind = '', None
    for line in lines:
        left, _, right = line.partition(' - ')
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        mnt = fields[4].replace('\\040', ' ')
        if (real == mnt or real.startswith(mnt.rstrip('/') + '/')) and len(mnt) >= len(best):
            best, kind = mnt, right.split()[0]
    return kind


def btrfs_ready(path: Path) -> bool:
    return (features.has('btrfs') and fs_type(path) == 'btrfs'
            and shutil.which('btrfs') is not None)


def is_subvolume(path: Path) -> bool:
    """A btrfs subvolume's root directory is always inode 256."""
    try:
        return os.stat(path).st_ino == 256 and fs_type(path) == 'btrfs'
    except OSError:
        return False


def _btrfs(*args: str) -> None:
    try:
        r = subprocess.run(['btrfs', *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SnapshotError(f"btrfs {args[0]} {args[1]}: {e}") from None
    if r.returncode != 0:
        raise SnapshotError((r.stderr or r.stdout).strip() or f"btrfs exited {r.returncode}")


def make_game_dir(path: Path, config: dict | None = None) -> bool:
    """Create a game's (empty) folder: a subvolume on btrfs unless config
    [install] subvolumes = false, else a plain folder; NOCOW either way
    (disable_cow). True if it's a subvolume."""
    config = config if config is not None else load_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    sub = False
    if config.get('subvolumes', True) and btrfs_ready(path.parent):
        try:
            _btrfs('subvolume', 'create', str(path))
            sub = True
        except SnapshotError as e:
            log.print(f"  [i] No subvolume for this game ({e}); snapshots won't be "
                      f"available for it.")
    if not sub:
        path.mkdir(parents=True, exist_ok=False)
    disable_cow(path)
    return sub


def remove_game_dir(path: Path) -> list[str]:
    """Delete a game folder, a subvolume or a snapshot: empty it, then rmdir
    (allowed for its owner on a writable subvolume). The paths that couldn't
    be removed."""
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        path.unlink()
        return []
    return _rmtree_collect(path)


def _nocow(path: Path) -> bool:
    try:
        r = subprocess.run(['lsattr', '-d', str(path)], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and 'C' in r.stdout.split(' ', 1)[0]


# ── snapshots of one game ────────────────────────────────────────────────

def snap_root(entry: dict, slug: str) -> Path | None:
    base = entry.get('install_base') or (Path(entry['compatdata']).parent
                                         if entry.get('compatdata') else None)
    return Path(base) / SNAP_DIR / slug if base else None


def snapshot_dirs(entry: dict, slug: str) -> list[Path]:
    root = snap_root(entry, slug)
    if root is None or not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and NAME_RE.match(d.name))


def list_snapshots(entry: dict, slug: str) -> list[dict]:
    """Newest first: {name, path, created (ISO), reason, label}."""
    out = []
    for d in reversed(snapshot_dirs(entry, slug)):
        ts, reason = NAME_RE.match(d.name).groups()
        created = datetime.strptime(ts, '%Y%m%d-%H%M%S').isoformat(timespec='seconds')
        out.append({'name': d.name, 'path': str(d), 'created': created, 'reason': reason,
                    'label': REASONS.get(reason, reason.replace('-', ' ').capitalize())})
    return out


def _game(slug: str, install_base: Path) -> tuple[dict, Path]:
    entry = registry_get(slug)
    if not entry:
        raise SnapshotError(f"'{slug}' is not a managed game (see --list)")
    d = _owned_game_dir(entry, install_base, slug)
    if d is None or not d.is_dir():
        raise SnapshotError("only games Nexus installed can have snapshots")
    return entry, d


def status(slug: str, install_base: Path) -> dict:
    try:
        entry, d = _game(slug, install_base)
    except SnapshotError as e:
        return {'available': False, 'why': str(e), 'snapshots': []}
    if fs_type(d) != 'btrfs':
        return {'available': False, 'why': "the game isn't on a btrfs drive",
                'snapshots': []}
    if not shutil.which('btrfs'):
        return {'available': False, 'why': "btrfs-progs isn't installed", 'snapshots': []}
    sub = is_subvolume(d)
    return {'available': sub, 'convertible': not sub,
            'why': None if sub else "set up before snapshots existed — enable them first",
            'snapshots': list_snapshots(entry, slug)}


def take(slug: str, reason: str, install_base: Path, keep: int | None = None) -> Path | None:
    """Snapshot a game now (None when it can't have snapshots — not an
    error: the step it guards just goes ahead). Keeps the newest `keep`."""
    if not REASON_RE.match(reason):
        raise SnapshotError(f"not a snapshot reason: {reason!r}")
    try:
        entry, d = _game(slug, install_base)
    except SnapshotError:
        return None
    if not is_subvolume(d) or not shutil.which('btrfs'):
        return None
    root = snap_root(entry, slug)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{datetime.now():%Y%m%d-%H%M%S}-{reason}"
    if dest.exists():
        raise SnapshotError(f"{dest.name} already exists — try again in a second")
    _btrfs('subvolume', 'snapshot', str(d), str(dest))
    keep = keep or load_config().get('snapshots_keep', 1)
    for old in snapshot_dirs(entry, slug)[:-keep]:
        remove_game_dir(old)
    return dest


def auto_snapshot(slug: str, reason: str, install_base: Path) -> None:
    """take() before a risky step, never stopping it: says what happened."""
    try:
        snap = take(slug, reason, install_base)
    except SnapshotError as e:
        log.print(f"  [!] Couldn't take a snapshot first ({e}) — going ahead anyway.")
        return
    if snap:
        log.print(f"  Snapshot taken ({snap.name}) — Roll back undoes this.")


def rollback(slug: str, name: str, install_base: Path) -> None:
    entry, d = _game(slug, install_base)
    if not NAME_RE.match(name or ''):
        raise SnapshotError(f"no snapshot called {name!r}")
    snap = snap_root(entry, slug) / name
    if not snap.is_dir():
        raise SnapshotError(f"no snapshot called {name!r}")
    if prefix_processes(_prefix_markers(entry)):
        raise SnapshotError(f"'{entry['display_name']}' is running — quit it first")
    take(slug, 'before-rollback', install_base, keep=99)    # this, too, can be undone
    aside = d.with_name(f"{d.name}.rollback-{datetime.now():%Y%m%d-%H%M%S}")
    os.rename(d, aside)
    try:
        _btrfs('subvolume', 'snapshot', str(snap), str(d))
    except SnapshotError:
        os.rename(aside, d)
        raise
    # The state we replaced is the before-rollback snapshot now: the aside
    # copy holds nothing more (and moving a subvolume to the Trash would copy it).
    failed = remove_game_dir(aside)
    if failed:
        log.print(f"  [!] Couldn't remove {aside} completely; delete it yourself.")
    keep = load_config().get('snapshots_keep', 1)
    for old in snapshot_dirs(entry, slug)[:-(keep + 1)]:
        remove_game_dir(old)


def delete(slug: str, name: str, install_base: Path) -> None:
    entry, _d = _game(slug, install_base)
    if not NAME_RE.match(name or ''):
        raise SnapshotError(f"no snapshot called {name!r}")
    snap = snap_root(entry, slug) / name
    if not snap.is_dir():
        raise SnapshotError(f"no snapshot called {name!r}")
    failed = remove_game_dir(snap)
    if failed:
        raise SnapshotError(f"couldn't remove {len(failed)} files, e.g. {failed[0]}")


def convert(slug: str, install_base: Path) -> None:
    """Make an existing plain game folder a subvolume: a reflink copy into a
    new subvolume (instant, no extra space), checked, then swapped in."""
    entry, d = _game(slug, install_base)
    if is_subvolume(d):
        return
    if not btrfs_ready(d):
        raise SnapshotError("the game isn't on a btrfs drive (or btrfs-progs is missing)")
    if prefix_processes(_prefix_markers(entry)):
        raise SnapshotError(f"'{entry['display_name']}' is running — quit it first")
    new = d.with_name(f"{d.name}.nexus-new")
    if new.exists():
        remove_game_dir(new)                     # a conversion that was interrupted
    _btrfs('subvolume', 'create', str(new))
    if _nocow(d):                                # reflinks need both sides alike
        subprocess.run(['chattr', '+C', str(new)], capture_output=True)
    r = subprocess.run(['cp', '-a', '--reflink=always', f"{d}/.", f"{new}/"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        remove_game_dir(new)
        raise SnapshotError("couldn't copy it without using extra space "
                            f"({r.stderr.strip() or 'cp failed'}); nothing changed")
    if file_map(d) != file_map(new):
        remove_game_dir(new)
        raise SnapshotError("the copy didn't match the original; nothing changed")
    aside = d.with_name(f"{d.name}.nexus-old")
    os.rename(d, aside)
    os.rename(new, d)
    failed = remove_game_dir(aside)                # shares its data with the new one
    if failed:
        log.print(f"  [!] Couldn't remove {aside} completely; delete it yourself.")


# ── the command ──────────────────────────────────────────────────────────

def cmd_snapshots(install_base: Path, args) -> int:
    """--snapshots SLUG [--json] | --snapshot SLUG | --rollback SLUG NAME |
    --delete-snapshot SLUG NAME | --snapshots-enable SLUG."""
    use_logger(Logger())
    as_json = args.json
    try:
        if args.snapshots:
            s = status(args.snapshots, install_base)
            if as_json:
                print(json.dumps(s))
            elif not s['snapshots']:
                print("No snapshots." + (f" ({s['why']})" if s.get('why') else ''))
            else:
                for x in s['snapshots']:
                    print(f"  {x['name']}  {x['label']}")
            return 0
        if args.snapshot:
            snap = take(args.snapshot, 'manual', install_base)
            if snap is None:
                raise SnapshotError(status(args.snapshot, install_base).get('why')
                                    or "this game can't have snapshots")
            msg, extra = f"Snapshot taken: {snap.name}", {'name': snap.name}
        elif args.rollback:
            slug, name = args.rollback
            rollback(slug, name, install_base)
            msg, extra = f"Rolled {slug} back to {name}.", {}
        elif args.delete_snapshot:
            slug, name = args.delete_snapshot
            delete(slug, name, install_base)
            msg, extra = f"Deleted snapshot {name}.", {}
        else:
            convert(args.snapshots_enable, install_base)
            msg, extra = "Snapshots are on for this game.", {}
    except (SnapshotError, OSError) as e:
        if as_json:
            print(json.dumps({'ok': False, 'error': str(e)}))
        print(f"  [!] {e}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({'ok': True, **extra}))
    else:
        log.print(msg)
    emit_event('snapshots', {'ok': True})
    return 0


def snapshot_doctor(install_base: Path) -> list[tuple]:
    """(status, title, detail, fix, actions) rows for --doctor's Storage group."""
    if not btrfs_ready(install_base):
        return []
    plain = []
    for slug, e in load_registry()['games'].items():
        d = _owned_game_dir(e, install_base, slug)
        if d is not None and d.is_dir() and not is_subvolume(d):
            plain.append(slug)
    if not plain:
        return [('ok', 'Game snapshots', 'every game can be snapshotted (btrfs)', '', [])]
    return [('info', 'Game snapshots',
             f"{len(plain)} game(s) installed before snapshots existed: "
             + ", ".join(plain[:4]) + ("…" if len(plain) > 4 else ""),
             "Enable them per game (game page → Snapshots), or here",
             [{'label': f"Enable for {s}", 'args': ['--snapshots-enable', s], 'steam': False}
              for s in plain[:3]])]


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .common import disable_cow, existing_ancestor  # noqa: E402
from .config import load_config  # noqa: E402
from .move import file_map  # noqa: E402
from .registry import load_registry, registry_get  # noqa: E402
from .sunshine import _prefix_markers, prefix_processes  # noqa: E402
from .uninstall import _owned_game_dir, _rmtree_collect  # noqa: E402
