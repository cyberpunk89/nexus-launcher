"""Save games: find them in a Wine prefix (or where the Ludusavi manifest
says, on Windows), back them up on uninstall and put them back."""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .common import atomic_write, SAVES_DIR


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


# A manifest backup's map of each copied file to where it came from.
SAVE_FILES_MAP = 'files.json'


def backup_manifest_saves(slug: str, game_dir: Path, entry: list,
                          folders: dict[str, str] | None = None,
                          suffix: str = '') -> tuple[Path, int, list[Path]] | None:
    """backup_prefix_saves for a game with no prefix (a native Windows
    install): copy the files its Ludusavi manifest `entry` names (see
    nexus.saves) into SAVES_DIR/<slug>/<timestamp>/, with files.json mapping
    each copy to its original path so restore_manifest_saves can put it back.
    Same return as backup_prefix_saves."""
    from nexus import saves
    hits = saves.resolve(entry, game_dir, folders, noise=_is_save_noise)
    if not hits:
        return None
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest_root = _unique_dir(SAVES_DIR / slug, ts, suffix)
    # One numbered folder per save location keeps two locations' same-named files apart.
    roots = list(dict.fromkeys(root for _f, root in hits))
    mapping, failed = {}, []
    for f, root in hits:
        rel = Path(str(roots.index(root))) / f.relative_to(root)
        try:
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            mapping[rel.as_posix()] = str(f)
        except OSError:
            failed.append(f)
    dest_root.mkdir(parents=True, exist_ok=True)
    atomic_write(dest_root / SAVE_FILES_MAP, json.dumps(mapping, indent=1))
    return dest_root, len(mapping), failed


def restore_manifest_saves(slug: str) -> tuple[int, list[Path]] | None:
    """Put the latest backup_manifest_saves backup for `slug` back where its
    files.json says each file came from. Same return as restore_prefix_saves;
    None also when the latest backup isn't a manifest one."""
    latest = latest_save_backup(slug)
    if not latest:
        return None
    try:
        mapping = json.loads((latest / SAVE_FILES_MAP).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    count, failed = 0, []
    for rel, original in mapping.items():
        try:
            dest = Path(original)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(latest / rel, dest)
            count += 1
        except OSError:
            failed.append(Path(rel))
    return count, failed


# What a game outside the manifest gets, rather than a guess at its saves.
NO_SAVE_PATHS = "Nexus doesn't know where this game keeps its saves"


def manifest_save_entry(entry: dict) -> tuple[list | None, str]:
    """The Ludusavi manifest's save paths for a registry entry (a native
    Windows install), matched by store appid, else by name; (None, why) when
    there are none to use."""
    from nexus import compat, saves
    idx = saves.load_index()
    if idx is None:
        return None, "Nexus couldn't load its list of save locations (offline?)"
    appid = compat.store_appid(entry)
    sid = entry.get('steam_appid')               # a shortcut's own id is never a store's
    if appid is None and str(sid or '').isdigit() and 0 < int(sid) < 0x80000000:
        appid = int(sid)
    title = saves.find_game(idx, appid, entry.get('display_name') or entry.get('slug') or '')
    files = (idx.get('games') or {}).get(title) if title else None
    return (files, '') if files else (None, NO_SAVE_PATHS)


def is_manifest_backup(backup: Path) -> bool:
    """True for a backup_manifest_saves backup (restore_manifest_saves can put
    it back), False for one copied out of a Wine prefix."""
    return (backup / SAVE_FILES_MAP).is_file()


def _report_failed_copies(failed: list[Path], what: str) -> None:
    print(f"  [!] {len(failed)} {what} could not be copied, e.g.:", file=sys.stderr)
    for f in failed[:5]:
        print(f"      {f}", file=sys.stderr)
