"""Read-only view of the games other launchers own: installed Steam games
(library folders + app manifests + per-user play stats + cached art) and
installed Heroic store games (Epic / GOG / Amazon). Nothing here writes to
Steam's or Heroic's files."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import net, platform

# Steam "apps" that are runtimes/tools, not games.
TOOL_APPIDS = {228980, 1070560, 1391110, 1628350, 4183110, 1493710, 2180100,
               961940, 1113280, 1245040, 1420170, 1580130, 1826330, 2230260,
               2348590, 2805730, 3658110}
TOOL_NAME_RE = re.compile(r'^(Proton\b|Steam Linux Runtime|Steamworks Common|'
                          r'SteamVR|Steam Audio)', re.IGNORECASE)

# appmanifest StateFlags bits (Steam's EAppState).
STATE_UPDATE_REQUIRED = 0x2
STATE_FULLY_INSTALLED = 0x4
STATE_UPDATE_RUNNING = 0x100
STATE_UPDATE_PAUSED = 0x200

STEAM_CDN = "https://steamcdn-a.akamaihd.net/steam/apps/{appid}/{file}"


def _load_vdf(path: Path) -> dict:
    """Parse a text-mode .vdf. {} on any failure: no python-vdf, no file, or a
    truncated one (python-vdf raises SyntaxError and friends on those)."""
    try:
        import vdf
        with open(path, encoding='utf-8', errors='replace') as f:
            return vdf.load(f)
    except (OSError, ValueError, ImportError, SyntaxError, EOFError, KeyError,
            TypeError, AttributeError):
        return {}


def _ci(node, *keys):
    """Case-insensitive nested lookup; {} when any level is missing."""
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = next((v for k, v in node.items() if k.lower() == key.lower()), {})
    return node


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def library_folders(steam_root: Path) -> list[Path]:
    """Every Steam library folder (the root's own first), existing ones only."""
    out = []
    for cand in (steam_root / 'steamapps' / 'libraryfolders.vdf',
                 steam_root / 'config' / 'libraryfolders.vdf'):
        data = _ci(_load_vdf(cand), 'libraryfolders')
        for entry in data.values() if isinstance(data, dict) else ():
            p = entry.get('path') if isinstance(entry, dict) else entry
            if p and Path(p).is_dir() and Path(p) not in out:
                out.append(Path(p))
        if out:
            break
    if steam_root.is_dir() and steam_root not in out:
        out.insert(0, steam_root)
    return out


def is_tool(appid: int, name: str) -> bool:
    return appid in TOOL_APPIDS or bool(TOOL_NAME_RE.match(name or ''))


def _play_cache() -> Path:
    from .paths import CACHE_DIR
    return CACHE_DIR / 'steam-playstats.json'


def play_stats(steam_root: Path, cache_file: Path | None = None) -> dict[int, dict]:
    """{appid: {'last': unix, 'minutes': int}} from every account's
    localconfig.vdf, merged by max (last played / play time). The digest is
    kept on disk keyed by those files' size and mtime: localconfig.vdf runs
    to megabytes on a big account, and the library is read after every job."""
    out: dict[int, dict] = {}
    base = steam_root / 'userdata'
    if not base.is_dir():
        return out
    files = sorted(base.glob('*/config/localconfig.vdf'))
    try:
        key = [[str(p), p.stat().st_mtime_ns, p.stat().st_size] for p in files]
    except OSError:
        key = None
    cache_file = cache_file or _play_cache()
    if key is not None:
        try:
            hit = json.loads(cache_file.read_text(encoding='utf-8'))
            if hit.get('key') == key:
                return {int(a): v for a, v in hit['stats'].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    out = _parse_play_stats(files)
    if key is not None:
        net.save_json(cache_file, {'key': key, 'stats': out})
    return out


def _parse_play_stats(files) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for cfg in files:
        apps = _ci(_load_vdf(cfg), 'UserLocalConfigStore', 'Software', 'Valve', 'Steam', 'apps')
        for aid, v in apps.items() if isinstance(apps, dict) else ():
            if not isinstance(v, dict):
                continue
            appid = _int(aid, -1)
            if appid < 0:
                continue
            cur = out.setdefault(appid, {'last': 0, 'minutes': 0})
            cur['last'] = max(cur['last'], _int(_ci(v, 'LastPlayed') or 0))
            cur['minutes'] = max(cur['minutes'], _int(_ci(v, 'Playtime') or 0))
    return out


_libc = None                # None: not loaded yet; False: no statx here (looked once)


def birth_time(path) -> float | None:
    """When a file or directory was created (statx STATX_BTIME), which
    os.stat doesn't expose on Linux. None where the kernel or filesystem
    can't say. A game folder's birth time is its real install date; the
    appmanifest's mtime moves with every update."""
    global _libc
    if _libc is False:
        return None
    import ctypes
    import ctypes.util
    try:
        if _libc is None:
            # Asked once: where there's no libc with statx (Windows), trying
            # again for every game cost a scan of 500 games ~40 ms.
            try:
                lib = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
                lib.statx                           # noqa: B018 — AttributeError if missing
            except (OSError, AttributeError):
                _libc = False
                return None
            _libc = lib
        buf = ctypes.create_string_buffer(256)       # struct statx
        AT_FDCWD, STATX_BTIME = -100, 0x800
        if _libc.statx(AT_FDCWD, str(path).encode(), 0, STATX_BTIME, buf) != 0:
            return None
        mask = int.from_bytes(buf.raw[0:4], 'little')
        if not mask & STATX_BTIME:
            return None
        sec = int.from_bytes(buf.raw[80:88], 'little', signed=True)  # stx_btime.tv_sec
        return float(sec) if sec > 0 else None
    except (OSError, AttributeError, ValueError):
        return None


_ART_INDEX: dict[Path, tuple[float, dict[int, list[str]], set[int]]] = {}


def _art_index(cache: Path) -> tuple[dict[int, list[str]], set[int]]:
    """One listing of the library cache: old-style `<appid>_<file>` art by
    appid, and the appids that have a newer-style `<appid>/` folder. A glob
    or an is_dir() per game re-lists or stats it every time: thousands of
    files and a stat per owned game, slow on Windows. Rebuilt when the folder
    changes (a new game's folder or file changes its mtime)."""
    try:
        mtime = cache.stat().st_mtime
    except OSError:
        return {}, set()
    hit = _ART_INDEX.get(cache)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    flat: dict[int, list[str]] = {}
    dirs: set[int] = set()
    try:
        with os.scandir(cache) as it:
            for e in it:
                if e.name.isdigit():
                    if e.is_dir():
                        dirs.add(int(e.name))
                    continue
                head, sep, _rest = e.name.partition('_')
                if sep and head.isdigit() and e.is_file():
                    flat.setdefault(int(head), []).append(e.path)
    except OSError:
        return {}, set()
    _ART_INDEX[cache] = (mtime, flat, dirs)
    return flat, dirs


def _files_under(d: str) -> list[str]:
    """Every file below `d`, with os.scandir (its entries know file from
    folder without a stat on Windows; rglob stats each one)."""
    out: list[str] = []
    try:
        with os.scandir(d) as it:
            entries = list(it)
    except OSError:
        return out
    for e in entries:
        try:
            if e.is_file():
                out.append(e.path)
            elif e.is_dir():
                out += _files_under(e.path)
        except OSError:
            continue
    return out


def art(steam_root: Path, appid: int) -> dict:
    """Local art paths from Steam's library cache. Newer clients store
    `librarycache/<appid>/<hash>/<file>`, older ones `<appid>_<file>`."""
    cache = steam_root / 'appcache' / 'librarycache'
    found: dict[str, str] = {}
    wanted = {
        'cover': ('library_600x900', 'library_capsule'),
        'hero': ('library_hero',),
        'hero_blur': ('library_hero_blur',),
        'logo': ('logo',),
        'header': ('library_header', 'header'),
    }
    flat, dirs = _art_index(cache)
    files = _files_under(str(cache / str(appid))) if appid in dirs else []
    files += flat.get(appid, [])
    stems = {}
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0].removeprefix(f'{appid}_')
        stems.setdefault(stem, f)
    for kind, names in wanted.items():
        for n in names:
            if n in stems:
                found[kind] = stems[n]
                break
    return found


def _art_fields(steam_root: Path, appid: int) -> dict:
    """Local library art for an appid (installed or not — Steam caches art
    for every owned game), with Steam's CDN as the fallback."""
    a = art(steam_root, appid)

    def cdn(have, file):
        return None if have else STEAM_CDN.format(appid=appid, file=file)
    return {
        'cover': a.get('cover'), 'hero': a.get('hero'), 'hero_blur': a.get('hero_blur'),
        'logo': a.get('logo'), 'wide': a.get('header'),
        'cover_url': cdn(a.get('cover'), 'library_600x900.jpg'),
        'hero_url': cdn(a.get('hero'), 'library_hero.jpg'),
        'logo_url': cdn(a.get('logo'), 'logo.png'),
        'wide_url': cdn(a.get('header'), 'header.jpg'),
    }


def scan(steam_root: Path, compat_map: dict[str, str] | None = None,
         installing: bool = False) -> list[dict]:
    """Installed Steam games (tools filtered out), newest-played first. With
    `installing`, games Steam is still downloading come too, as
    installed=False with installing={done, total, paused}."""
    stats = play_stats(steam_root)
    compat_map = compat_map or {}
    seen: set[int] = set()
    games = []
    for lib in library_folders(steam_root):
        for acf in sorted((lib / 'steamapps').glob('appmanifest_*.acf')):
            st = _ci(_load_vdf(acf), 'AppState')
            appid = _int(_ci(st, 'appid'), -1)
            name = _ci(st, 'name') or ''
            if appid < 0 or appid in seen or is_tool(appid, name):
                continue
            flags = _int(_ci(st, 'StateFlags'))
            done = bool(flags & STATE_FULLY_INSTALLED)
            if not done and not installing:
                continue
            seen.add(appid)
            ps = stats.get(appid, {})
            last = max(_int(_ci(st, 'LastPlayed')), ps.get('last', 0))
            installdir = _ci(st, 'installdir') or ''
            # Install date: the game folder's birth time, else the manifest's.
            game_dir = lib / 'steamapps' / 'common' / installdir if installdir else None
            born = birth_time(game_dir) if game_dir else None
            try:
                born = born or acf.stat().st_mtime
                installed = datetime.fromtimestamp(born).isoformat(timespec='seconds')
            except OSError:
                installed = None
            g = {
                'slug': f'steam-{appid}',
                'source': 'steam',
                'appid': appid,
                'installed': done,
                'display_name': name or installdir or str(appid),
                'install_dir': str(lib / 'steamapps' / 'common' / installdir) if installdir else None,
                'library': str(lib),
                'disk_bytes': _int(_ci(st, 'SizeOnDisk')) or None,
                'last_played': datetime.fromtimestamp(last).isoformat(timespec='seconds')
                               if last else None,
                'playtime_minutes': ps.get('minutes', 0),
                'last_updated': _int(_ci(st, 'LastUpdated')) or None,
                'update_pending': done and bool(
                    flags & (STATE_UPDATE_REQUIRED | STATE_UPDATE_RUNNING)),
                'proton': compat_map.get(str(appid)),
                'installed_at': installed if done else None,
                'launcher': f'steam://rungameid/{appid}',
                'install_url': f'steam://install/{appid}',
                'steam': True,
                **_art_fields(steam_root, appid),
            }
            if not done:
                g['installing'] = {'done': _int(_ci(st, 'BytesDownloaded')),
                                   'total': _int(_ci(st, 'BytesToDownload')),
                                   'paused': bool(flags & STATE_UPDATE_PAUSED)}
            games.append(g)
    games.sort(key=lambda g: g['last_played'] or '', reverse=True)
    return games


def steam_owned_entry(steam_root: Path, item: dict) -> dict:
    """An owned Steam game that isn't installed, from Game-db's library
    (appid, title, playtimeMinutes, lastPlayedAt). Play opens Steam's install
    dialog."""
    appid = int(item['appid'])
    return {
        'slug': f'steam-{appid}',
        'source': 'steam',
        'appid': appid,
        'installed': False,
        'display_name': item.get('title') or str(appid),
        'install_dir': None,
        'disk_bytes': None,
        'last_played': _iso_local(item.get('lastPlayedAt')),
        'playtime_minutes': _int(item.get('playtimeMinutes')),
        'launcher': f'steam://rungameid/{appid}',
        'install_url': f'steam://install/{appid}',
        'steam': True,
        **_art_fields(steam_root, appid),
    }


def heroic_downloads(heroic_cfg: Path | None) -> set[str]:
    """App names in Heroic's download queue."""
    try:
        d = json.loads((heroic_cfg / 'store' / 'download-manager.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return set()
    q = d.get('queue') if isinstance(d, dict) else None
    return {(i.get('params') or {}).get('appName') for i in q or () if isinstance(i, dict)} - {None}


HEROIC_CONFIG_DIRS = [
    Path.home() / ".config/heroic",
    Path.home() / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
]


def find_heroic_config() -> Path | None:
    """Heroic's config dir (native install preferred, then Flatpak)."""
    for d in HEROIC_CONFIG_DIRS:
        if (d / "config.json").exists() or (d / "sideload_apps").is_dir():
            return d
    return None


def downloads(steam_root: Path, heroic_cfg: Path | None = None) -> dict:
    """What Steam and Heroic are downloading, read cheaply — app manifests
    and Heroic's queue only, no art, play stats or store caches — so the GUI
    can follow a download every few seconds without a full library scan.
    `steam` maps appid -> {done, total, paused}; `steam_installed` lists the
    fully installed appids (a finished download moves there)."""
    steam, installed = {}, []
    for lib in library_folders(steam_root):
        for acf in (lib / 'steamapps').glob('appmanifest_*.acf'):
            st = _ci(_load_vdf(acf), 'AppState')
            appid = _int(_ci(st, 'appid'), -1)
            if appid < 0:
                continue
            flags = _int(_ci(st, 'StateFlags'))
            if flags & STATE_FULLY_INSTALLED:
                installed.append(appid)
            else:
                steam[appid] = {'done': _int(_ci(st, 'BytesDownloaded')),
                                'total': _int(_ci(st, 'BytesToDownload')),
                                'paused': bool(flags & STATE_UPDATE_PAUSED)}
    return {'steam': steam, 'steam_installed': sorted(installed),
            'heroic': sorted(heroic_downloads(heroic_cfg)) if heroic_cfg else []}


def heroic_store_games(heroic_cfg: Path | None, owned: bool = False) -> list[dict]:
    """Installed Epic (legendary) / GOG / Amazon (nile) games from Heroic's
    store caches, with Heroic's own play time. Heroic runs them. With
    `owned`, the rest of the account's library too (installed=False); for
    those, Heroic's launch URL asks to install the game."""
    if not heroic_cfg:
        return []
    try:
        stamps = json.loads((heroic_cfg / 'store' / 'timestamp.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        stamps = {}
    queued = heroic_downloads(heroic_cfg) if owned else set()
    out = []
    for runner, fname in (('legendary', 'legendary_library.json'),
                          ('gog', 'gog_library.json'), ('nile', 'nile_library.json')):
        try:
            data = json.loads((heroic_cfg / 'store_cache' / fname).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        lib = data.get('library', data.get('games', [])) if isinstance(data, dict) else data
        for g in lib if isinstance(lib, list) else ():
            if not isinstance(g, dict) or not (g.get('is_installed') or owned):
                continue
            app = g.get('app_name')
            inst = g.get('install') or {}
            if not app or inst.get('is_dlc') or app.startswith('gog-redist'):
                continue
            st = stamps.get(app) or {}
            done = bool(g.get('is_installed'))
            out.append({
                'slug': f'heroic-{runner}-{app}',
                'source': 'heroic',
                'runner': runner,
                'app_name': app,
                'installed': done,
                **({} if done or app not in queued else {'installing': {'done': 0, 'total': 0}}),
                'display_name': g.get('title') or app,
                'install_dir': inst.get('install_path'),
                'disk_bytes': None,
                'install_size': inst.get('install_size'),
                'last_played': _iso_local(st.get('lastPlayed')),
                'playtime_minutes': _int(st.get('totalPlayed')),
                'cover_url': g.get('art_square') or g.get('art_cover'),
                'hero_url': g.get('art_cover'),
                'wide_url': g.get('art_cover'),
                'logo_url': g.get('art_logo'),
                'launcher': f'heroic://launch/{runner}/{app}',
                'install_url': f'heroic://launch/{runner}/{app}',
                'heroic': True,
            })
    return out


def _iso_local(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace('Z', '+00:00')).astimezone() \
            .replace(tzinfo=None).isoformat(timespec='seconds')
    except ValueError:
        return None


def open_url(url: str) -> bool:
    """Hand a steam:// or heroic:// URL to its launcher, detached. False when
    nothing here can open it. See platform.open_url."""
    return platform.open_url(url)


def manifest_appids(steam_root: Path) -> set[int]:
    """Every appid with an app manifest in any library (games and tools,
    installed or mid-download) — i.e. everything Steam still owns on disk."""
    ids: set[int] = set()
    for lib in library_folders(steam_root):
        for acf in (lib / 'steamapps').glob('appmanifest_*.acf'):
            m = re.match(r'appmanifest_(\d+)\.acf$', acf.name)
            if m:
                ids.add(int(m.group(1)))
    return ids


def running() -> dict:
    """What's being played right now, from the process list (read-only, ~ms):
    Steam's reaper carries `SteamLaunch AppId=<id>` (Steam games and non-Steam
    shortcuts alike), Nexus's own launches run under `--track-session <slug>`,
    and any .exe names / paths seen let Heroic launches be matched too."""
    appids, slugs, exes, cmdlines = set(), set(), set(), []
    for p in platform.processes():
        args = p.cmdline
        if not args:
            continue
        cmd = ' '.join(args)
        m = re.search(r'SteamLaunch AppId=(\d+)', cmd)
        if m:
            appids.add(int(m.group(1)))
        m = re.search(r'--track-session (\S+)', cmd)
        if m:
            slugs.add(m.group(1))
        for tok in re.findall(r'[^\s"\'\\/]+\.exe\b', cmd, re.IGNORECASE):
            exes.add(tok.lower())
        # Whole arguments too: "Disco Elysium.exe" has a space the token
        # pattern above would cut it at.
        for arg in args:
            a = arg.strip()
            if a.lower().endswith('.exe'):
                exes.add(re.split(r'[\\/]', a)[-1].lower())
        if '.exe' in cmd.lower() or 'wine' in cmd.lower():
            cmdlines.append(cmd)
    steam_game = platform.steam_running_appid()         # Windows: no reaper to read
    if steam_game:
        appids.add(steam_game)
    exes -= {'steam.exe', 'explorer.exe', 'services.exe', 'winedevice.exe', 'plugplay.exe',
             'svchost.exe', 'rpcss.exe', 'tabtip.exe', 'conhost.exe', 'start.exe',
             'wineboot.exe', 'steamwebhelper.exe', 'rundll32.exe'}
    return {'appids': sorted(appids), 'slugs': sorted(slugs), 'exes': sorted(exes),
            'paths': cmdlines[:200]}


def in_dir_cmdline(install_dir: str, cmdlines: list[str]) -> bool:
    """Whether a command line runs something inside install_dir: the
    folder followed by a separator (so /games/Foo doesn't match
    /games/FooBar), Wine's Z:\\ form included."""
    def norm(s: str) -> str:                     # Windows paths ignore case
        return os.path.normcase(s).replace('\\', '/')
    d = norm(install_dir).rstrip('/') + '/'
    return any(d in norm(c) for c in cmdlines)
