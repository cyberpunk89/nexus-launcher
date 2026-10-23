"""Steam achievements, read from the Steam client's own cache — no network,
no Steam Web API key, read-only.

Steam keeps two binary-VDF files per game under appcache/stats/:
  UserGameStatsSchema_<appid>.bin        every achievement: stat id + bit,
                                         API name, title/description per
                                         language, icons, hidden flag
  UserGameStats_<accountid>_<appid>.bin  this account's progress: per stat
                                         a bitfield (`data`) of unlocked
                                         bits and `AchievementTimes`
Both only exist for games the account has run with Steam at least once."""

from pathlib import Path

ICON_URL = ("https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/"
            "{appid}/{icon}")

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
