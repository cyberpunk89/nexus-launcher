"""The game registry (games.json): the source of truth for managed games."""

import contextlib
import fcntl
import json
import os
import sys
from datetime import datetime

from nexus import paths as NX

from .common import atomic_write, DATA_DIR


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
                 'repack_source', 'repack_installed_at', 'umu_gameid', 'local',
                 'import_source', 'launch_via')


def register_game(*, slug: str, display_name: str, exe, compatdata, wine_prefix,
                  launcher, source: str, install_base, steam_appid,
                  has_desktop: bool, artwork_dir, heroic_app_name=None,
                  proton: str | None = None, repack: dict | None = None,
                  umu_gameid: str | None = None, extra: dict | None = None) -> None:
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
        if extra:                    # e.g. local=True for a game that isn't a repack
            entry.update(extra)
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
