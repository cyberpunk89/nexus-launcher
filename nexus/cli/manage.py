"""The library: --list / --library, finding games, uninstalling."""

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .common import (ARTWORK_DIR, dir_size_bytes, existing_ancestor, make_slug, STEAM_ROOT,
    VDF_ERRORS)


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
    hit = _art_file(Path(artwork_dir), 'cover')
    return str(hit) if hit else None


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
    return {e for g in lib.get('games', []) if (e := _heroic_exe(g))}


def heroic_play_stats() -> dict[str, dict]:
    """Heroic's own play tracking, {app_name: {'minutes', 'last'}} with `last`
    as unix seconds, from store/timestamp.json. Read-only; {} on any failure."""
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        return {}
    try:
        data = json.loads((cfg_dir / 'store' / 'timestamp.json').read_text(encoding='utf-8'))
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
                heroic_by_exe[_heroic_exe(hg)] = hg['app_name']

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
            'owned_dir': bool(owned_dir(g)),      # Nexus's own folder: can move / snapshot
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
    from nexus import sources
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
    stores = sources.installed_games()            # Epic etc.; a failing one is skipped
    games += steam + stores + heroic + extra
    _add_achievement_counts(games)
    try:
        sun_apps = json.loads(SUNSHINE_APPS.read_text(encoding='utf-8')).get('apps') or []
    except (OSError, ValueError, AttributeError):
        sun_apps = []
    for g in stores:
        # The SteamGridDB game its art came from, as --fill-art settled it:
        # later lookups (the art picker) start there instead of searching.
        gid = cached_sgdb_id(g)
        if gid:
            g['sgdb_id'] = gid
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
              *(Path(g['install_dir']).parent for g in heroic + stores if g.get('install_dir'))]:
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
                or (exe and exe in r['exes'])
                or (g.get('install_dir') and steamlib.in_dir_cmdline(g['install_dir'],
                                                                     r['paths']))):
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
    from nexus import sources
    for g in sources.installed_games():          # Epic etc.
        if g['slug'] == ident:
            gid = cached_sgdb_id(g)
            return {**g, 'sgdb_id': gid} if gid else g
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


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import _entry_paths, profile_owns_options  # noqa: E402
from .desktop import _desktop_path  # noqa: E402
from .gamecmds import _launcher_display_name, ACHIEVEMENT_COUNTS, steam_account_id  # noqa: E402
from .heroic import (_heroic_exe, _heroic_lib_path, _load_heroic_library, find_heroic_config)  # noqa: E402
from .launchprofile import format_launch_options, protongen_heroic_id  # noqa: E402
from .registry import load_registry, update_registry_size  # noqa: E402
from .repack import incomplete_installs  # noqa: E402
from .sgdb import _art_file, cached_sgdb_id, overlay_art  # noqa: E402
from .steam import _read_compat_mapping, _shortcut_uid, find_steam_userdata  # noqa: E402
from .sunshine import _owned_game_dir, _sunshine_find, SUNSHINE_APPS  # noqa: E402
