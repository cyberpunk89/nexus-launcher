"""Steam achievements, read from the Steam client's own cache — no Steam
Web API key, read-only. The network is used only for the global unlock
percentages behind "rarest" (Steam's public, keyless endpoint, cached).

Steam keeps two binary-VDF files per game under appcache/stats/:
  UserGameStatsSchema_<appid>.bin        every achievement: stat id + bit,
                                         API name, title/description per
                                         language, icons, hidden flag
  UserGameStats_<accountid>_<appid>.bin  this account's progress: per stat
                                         a bitfield (`data`) of unlocked
                                         bits and `AchievementTimes`
Both only exist for games the account has run with Steam at least once."""

import json
import os
import time
from pathlib import Path

ICON_URL = ("https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/"
            "{appid}/{icon}")

PERCENT_URL = ("https://api.steampowered.com/ISteamUserStats/"
               "GetGlobalAchievementPercentagesForApp/v2/?gameid={appid}")
PERCENT_AGE = 30 * 86400
EMPTY_PERCENT_AGE = 6 * 3600     # empty may be an error page as much as "no data"

_CACHE: dict[tuple, tuple[float, object]] = {}      # (kind, path) -> (mtime, parsed)


def _stats_dir(steam_root: Path) -> Path:
    return steam_root / 'appcache' / 'stats'


def _load(path: Path):
    """A parsed binary VDF, memoised by mtime (None if missing/unreadable)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _CACHE.get(('vdf', path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        import vdf
        data = vdf.binary_loads(path.read_bytes())
    except Exception:              # python-vdf missing, or a file Steam is rewriting
        return None
    _CACHE[('vdf', path)] = (mtime, data)
    return data


def _text(display: dict, key: str, lang: str) -> str:
    v = display.get(key)
    if isinstance(v, dict):
        return str(v.get(lang) or v.get('english') or next(iter(v.values()), '') or '')
    return str(v or '')


def schema(steam_root: Path, appid: int, lang: str = 'english') -> list[dict]:
    """Every achievement of a game, in Steam's order."""
    data = _load(_stats_dir(steam_root) / f'UserGameStatsSchema_{appid}.bin')
    if not isinstance(data, dict) or not data:
        return []
    root = data.get(str(appid)) or next(iter(data.values()), {})
    out = []
    for stat_id, stat in (root.get('stats') or {}).items():
        if not isinstance(stat, dict) or not isinstance(stat.get('bits'), dict):
            continue
        for bit, item in stat['bits'].items():
            if not isinstance(item, dict):
                continue
            d = item.get('display') or {}
            out.append({
                'stat': str(stat_id), 'bit': int(bit), 'name': item.get('name') or '',
                'title': _text(d, 'name', lang) or item.get('name') or '',
                'desc': _text(d, 'desc', lang),
                'hidden': str(d.get('hidden', 0)) not in ('0', '', 'None'),
                'icon': d.get('icon') or None, 'icon_gray': d.get('icon_gray') or None,
            })
    return out


def unlocked(steam_root: Path, account_id: int | str, appid: int) -> dict[tuple, int]:
    """(stat id, bit) -> unlock time (0 if Steam didn't record one)."""
    data = _load(_stats_dir(steam_root) / f'UserGameStats_{account_id}_{appid}.bin')
    cache = (data or {}).get('cache') if isinstance(data, dict) else None
    out: dict[tuple, int] = {}
    for stat_id, stat in (cache or {}).items():
        if not isinstance(stat, dict) or 'data' not in stat:
            continue
        try:
            bits = int(stat['data']) & 0xFFFFFFFF          # stored as a signed int32
        except (TypeError, ValueError):
            continue
        times = stat.get('AchievementTimes') or {}
        for bit in range(32):
            if bits >> bit & 1:
                try:
                    t = int(times.get(str(bit)) or times.get(bit) or 0)
                except (TypeError, ValueError):
                    t = 0
                out[(str(stat_id), bit)] = t
    return out


def achievements(steam_root: Path, account_id: int | str, appid: int,
                 lang: str = 'english') -> dict | None:
    """{total, unlocked, items}: items unlocked first (newest first), then
    the rest in Steam's order. None when the game has no achievements here."""
    items = schema(steam_root, appid, lang)
    if not items:
        return None
    got = unlocked(steam_root, account_id, appid)
    for a in items:
        t = got.get((a['stat'], a['bit']))
        a['unlocked'] = t is not None
        a['time'] = t or None
        icon = a['icon'] if a['unlocked'] else (a['icon_gray'] or a['icon'])
        a['icon_url'] = ICON_URL.format(appid=appid, icon=icon) if icon else None
    order = {id(a): i for i, a in enumerate(items)}
    items.sort(key=lambda a: (not a['unlocked'], -(a['time'] or 0), order[id(a)]))
    return {'appid': appid, 'total': len(items),
            'unlocked': sum(a['unlocked'] for a in items), 'items': items}


def counts(steam_root: Path, account_id: int | str, appids,
           cache_file: Path | None = None) -> dict[int, list[int]]:
    """appid -> [unlocked, total], for the games that have achievements.
    With `cache_file`, results are kept on disk keyed by the two files'
    mtimes, so a library load only parses games whose stats changed."""
    import json
    import os
    from . import net
    cached: dict = {}
    if cache_file:
        try:
            cached = json.loads(cache_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cached = {}
    stats = _stats_dir(steam_root)
    out, fresh, changed = {}, {}, False
    for appid in appids:
        stamp = []
        for name in (f'UserGameStatsSchema_{appid}.bin', f'UserGameStats_{account_id}_{appid}.bin'):
            try:
                stamp.append(os.stat(stats / name).st_mtime)
            except OSError:
                stamp.append(0)
        if not stamp[0]:
            continue                                   # no schema: no achievements known
        key = f"{account_id}:{appid}"
        hit = cached.get(key)
        if hit and hit.get('stamp') == stamp:
            val = hit['counts']
        else:
            items = schema(steam_root, appid)
            got = unlocked(steam_root, account_id, appid)    # may hold plain stats' bits too
            val = [sum((a['stat'], a['bit']) in got for a in items), len(items)] \
                if items else None
            changed = True
        fresh[key] = {'stamp': stamp, 'counts': val}
        if val:
            out[appid] = val
    if cache_file and (changed or len(fresh) != len(cached)):
        net.save_json(cache_file, fresh)
    return out


def recent(steam_root: Path, account_id: int | str, appids, n: int = 8) -> list[dict]:
    """The newest unlocks across games: {appid, title, desc, time, icon_url}."""
    out = []
    for appid in appids:
        got = unlocked(steam_root, account_id, appid)
        if not got:
            continue
        for a in schema(steam_root, appid):
            t = got.get((a['stat'], a['bit']))
            if t:
                out.append({'appid': appid, 'title': a['title'], 'desc': a['desc'], 'time': t,
                            'icon_url': ICON_URL.format(appid=appid, icon=a['icon'])
                            if a['icon'] else None})
    out.sort(key=lambda a: -a['time'])
    return out[:n]


def closest(counts: dict, n: int = 4) -> list:
    """The keys of `counts` ({key: [unlocked, total]}) started but not
    finished, nearest to 100% first."""
    left = [(k, v) for k, v in counts.items() if v and 0 < v[0] < v[1]]
    left.sort(key=lambda kv: (-kv[1][0] / kv[1][1], str(kv[0])))
    return [k for k, _v in left[:n]]


def parse_percentages(data: bytes) -> dict[str, float]:
    """API name -> global unlock percentage, from the
    GetGlobalAchievementPercentagesForApp answer (percent is a string in v2)."""
    try:
        root = json.loads(data)
        items = root['achievementpercentages']['achievements']
    except (ValueError, KeyError, TypeError):
        return {}
    out = {}
    for a in items if isinstance(items, list) else []:
        try:
            out[str(a['name'])] = float(a['percent'])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _percent_path(cache_dir: Path, appid: int) -> Path:
    return Path(cache_dir) / f"{appid}.json"


def needs_fetch(appid: int, cache_dir: Path) -> bool:
    """True when the cached percentages are missing or too old."""
    from . import net
    path = _percent_path(cache_dir, appid)
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        return net._age(path) >= (PERCENT_AGE if cached else EMPTY_PERCENT_AGE)
    except (OSError, ValueError):
        return True


def global_percentages(appid: int, cache_dir: Path, allow_fetch: bool = True,
                       fetch=None) -> dict[str, float]:
    """{api name: percent} for a game, cached per appid like the community
    schema: 30 days, an empty answer only 6 h, and a good copy is never
    replaced by an empty one. A failed fetch leaves an `.offline` marker, so
    for net.FAIL_TTL every later lookup is cache-only."""
    from . import net
    cache_dir = Path(cache_dir)
    path = _percent_path(cache_dir, appid)
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cached = None
    if not allow_fetch or not needs_fetch(appid, cache_dir):
        return cached or {}
    marker = cache_dir / '.offline'
    try:
        if net._age(marker) < net.FAIL_TTL:
            return cached or {}
    except OSError:
        pass
    fetch = fetch or (lambda: net.get(PERCENT_URL.format(appid=appid), timeout=8,
                                      limit=4 << 20))
    try:
        got = parse_percentages(fetch())
    except net.NetError:
        try:
            net.write_atomic(marker, str(int(time.time())))
        except OSError:
            pass
        return cached or {}
    if not got and cached:
        try:
            os.utime(path)                  # keep the good copy, don't re-ask each call
        except OSError:
            pass
        return cached
    net.save_json(path, got)
    return got


def rarest(steam_root: Path, account_id: int | str, percents: dict, n: int = 6) -> list[dict]:
    """Your rarest unlocks: {appid, title, desc, time, percent, icon_url},
    lowest global percentage first (newest first on a tie). `percents` is
    {appid: {api name: percent}}; games without it are left out."""
    out = []
    for appid, pct in percents.items():
        if not pct:
            continue
        got = unlocked(steam_root, account_id, appid)
        if not got:
            continue
        for a in schema(steam_root, appid):
            t = got.get((a['stat'], a['bit']))
            if t is None or a['name'] not in pct:
                continue
            out.append({'appid': appid, 'title': a['title'], 'desc': a['desc'], 'time': t,
                        'percent': pct[a['name']],
                        'icon_url': ICON_URL.format(appid=appid, icon=a['icon'])
                        if a['icon'] else None})
    out.sort(key=lambda a: (a['percent'], -(a['time'] or 0)))
    return out[:n]
