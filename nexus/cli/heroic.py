"""Heroic Games Launcher: sideloaded entries and their per-game config."""

import hashlib
import json
import sys
from pathlib import Path

from .common import atomic_write, backup_file, log


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


def _heroic_exe(g) -> str | None:
    """The executable of a Heroic sideload entry, or None for a malformed one
    (Heroic's own file can hold nulls or non-dict values where we expect dicts)."""
    inst = g.get('install') if isinstance(g, dict) else None
    exe = inst.get('executable') if isinstance(inst, dict) else None
    return exe if isinstance(exe, str) else None


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
    existing = next((g for g in games if isinstance(g, dict)
                     and (_heroic_exe(g) == exe_str
                          or g.get("app_name") == _heroic_app_name(slug))), None)
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
            inner = gc.get(app_name) if isinstance(gc, dict) else None
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
        if isinstance(g, dict) and (g.get("app_name") == want_id
                                    or (exe and _heroic_exe(g) == exe)):
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


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .launchprofile import heroic_launch_fields  # noqa: E402
