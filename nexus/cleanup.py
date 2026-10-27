"""Reclaiming disk space safely.

Candidates are described as plain dicts ({id, category, title, path, size,
reason, action, preselect, saves}); the CLI gathers the facts (registry,
Steam libraries, shortcut ids) and these helpers apply the rules and do the
removal. Default removal is the Trash (Recycle Bin on Windows), so a mistake can be undone
from the file manager; "forever" is a separate, explicit choice."""

import os
import re
import shutil
import time
from pathlib import Path

from . import platform

CATEGORIES = {
    'orphan-prefix':  ("Stray Steam prefixes",
                       "Wine prefixes in steamapps/compatdata that no installed game or "
                       "shortcut uses. Moved to quarantine first — you can restore them."),
    'shadercache':    ("Shader caches of removed games",
                       "Steam's per-game shader cache for games that aren't installed."),
    'mesa-cache':     ("Mesa shader cache",
                       "Rebuilt automatically; games may stutter for a while after clearing."),
    'installed-repack': ("Repacks you've already installed",
                         "The downloaded repack folders. Keep one if you might reinstall."),
    'force-leftover': ("Old installs replaced by a reinstall",
                       "Left by a fresh reinstall (--force). May still hold your saves."),
    'old-logs':       ("Old logs", "Install and launch logs older than 30 days."),
    'old-save-backups': ("Older save backups", "Beyond the newest 3 backups per game."),
    'stale-data':     ("Leftovers of removed games", "Artwork and integrity data."),
    'game-snapshot':  ("Older game snapshots",
                       "btrfs snapshots behind the newest one per game (and all of a removed "
                       "game's). They share space with the game: each frees only what changed "
                       "since."),
}


def dir_size(path: Path) -> int:
    """Bytes under path (stats only, symlinks not followed)."""
    total = 0
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        for root, dirs, files in os.walk(path, followlinks=False):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def candidate(category, path, *, title=None, reason='', action='trash',
              preselect=True, saves=False, size=None, extra=None) -> dict:
    path = Path(path)
    return {
        'id': f"{category}:{path}",
        'category': category,
        'title': title or path.name,
        'path': str(path),
        'size': dir_size(path) if size is None else size,
        'reason': reason,
        'action': action,             # trash | quarantine | delete
        'preselect': preselect and not saves,
        'saves': saves,
        **(extra or {}),
    }


# ---- rules (pure: they only look at the paths they're given) --------------

def old_logs(log_dir: Path, days: int = 30, now: float | None = None) -> list[dict]:
    """Logs older than `days`, always keeping the newest log per game."""
    if not log_dir.is_dir():
        return []
    now = now or time.time()
    newest: dict[str, Path] = {}
    logs = sorted(log_dir.glob('*.log'), key=lambda p: p.stat().st_mtime)
    for p in logs:
        key = re.sub(r'-(\d{8}-\d{6}|last-launch)$', '', p.stem)
        newest[key] = p
    keep = set(newest.values())
    return [candidate('old-logs', p, reason=f"{int((now - p.stat().st_mtime) // 86400)} days old",
                      action='delete')
            for p in logs if p not in keep and now - p.stat().st_mtime > days * 86400]


def old_save_backups(saves_dir: Path, keep: int = 3) -> list[dict]:
    out = []
    if not saves_dir.is_dir():
        return out
    for game in sorted(d for d in saves_dir.iterdir() if d.is_dir()):
        backups = sorted((d for d in game.iterdir() if d.is_dir()), reverse=True)
        for d in backups[keep:]:
            out.append(candidate('old-save-backups', d, title=f"{game.name} · {d.name}",
                                 reason=f"{len(backups)} backups; the newest {keep} are kept",
                                 preselect=False))
    return out


def stale_data(slugs: set[str], artwork_dir: Path, integrity_dir: Path) -> list[dict]:
    out = []
    if artwork_dir.is_dir():
        out += [candidate('stale-data', d, title=f"Artwork · {d.name}",
                          reason="game no longer installed", action='delete')
                for d in sorted(artwork_dir.iterdir()) if d.is_dir() and d.name not in slugs]
    if integrity_dir.is_dir():
        out += [candidate('stale-data', f, title=f"Integrity baseline · {f.stem}",
                          reason="game no longer installed", action='delete')
                for f in sorted(integrity_dir.glob('*.json')) if f.stem not in slugs]
    return out


def force_leftovers(install_base: Path, has_saves) -> list[dict]:
    if not install_base.is_dir():
        return []
    return [candidate('force-leftover', d, reason="replaced by a fresh reinstall",
                      saves=bool(has_saves(d)))
            for d in sorted(install_base.glob('*.old-*')) if d.is_dir()]


def shader_caches(libraries: list[Path], keep_ids: set[int]) -> list[dict]:
    out = []
    for lib in libraries:
        sc = lib / 'steamapps' / 'shadercache'
        if not sc.is_dir():
            continue
        for d in sorted(sc.iterdir()):
            if d.is_dir() and d.name.isdigit() and int(d.name) not in keep_ids:
                out.append(candidate('shadercache', d, title=f"shadercache/{d.name}",
                                     reason=f"app {d.name} isn't installed"))
    return out


def mesa_caches(cache_home: Path) -> list[dict]:
    return [candidate('mesa-cache', d, reason="rebuilt on demand", preselect=False)
            for d in sorted(cache_home.glob('mesa_shader_cache*')) if d.is_dir()]


# ---- removal -------------------------------------------------------------

def trash(path: Path) -> None:
    """Move to the Trash / Recycle Bin (recoverable): see platform.trash."""
    platform.trash(path)


def delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def trash_size() -> int:
    return platform.trash_size()
