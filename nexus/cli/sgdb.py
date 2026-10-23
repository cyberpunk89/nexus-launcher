"""SteamGridDB artwork: the install-time fetch, variants, picks, gap filling."""

import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from nexus import paths as NX

from .common import ARTWORK_DIR, atomic_write, SGDB_API, STEAM_ROOT


def _sgdb_get(path: str, api_key: str) -> dict | None:
    """GET a SteamGridDB API endpoint, return parsed JSON 'data' or None."""
    data, err = _sgdb_json(path, api_key)
    return None if err else data


ART_MAX_BYTES = 32 << 20


def _download(url: str, dest: Path) -> Path | None:
    """An image download: https only, size-capped, through the shared client."""
    from nexus import net
    try:
        net.download(url, dest, timeout=30, limit=ART_MAX_BYTES)
    except net.NetError:
        return None
    return dest


def fetch_artwork(display_name: str, slug: str, config: dict) -> dict | None:
    """Best-effort SteamGridDB fetch. Returns {kind: Path} for the assets it
    could download (cover/hero/logo/icon), or None. Never raises."""
    api_key = config.get('api_key')
    if not api_key:
        return None

    import urllib.parse
    forced = read_sgdb_match(ARTWORK_DIR / slug)
    if forced:
        game_id = forced['id']
    else:
        term = urllib.parse.quote(display_name, safe='')
        results = _sgdb_get(f"/search/autocomplete/{term}", api_key)
        if not results:
            return None
        game_id = results[0].get('id')
    if not game_id:
        return None

    # (kind, endpoint, extra query) — first asset of each is used
    wanted = [
        ('cover', f"/grids/game/{game_id}?dimensions=600x900,660x930&types=static"),
        ('hero',  f"/heroes/game/{game_id}?types=static"),
        ('logo',  f"/logos/game/{game_id}?types=static"),
        ('icon',  f"/icons/game/{game_id}"),
    ]
    dest_dir = ARTWORK_DIR / slug

    def fetch_one(item):
        """Metadata lookup + download for one art kind. Returns (kind, path,
        url) or None. Runs off-thread, so it must not touch shared state."""
        kind, endpoint = item
        data = _sgdb_get(endpoint, api_key)
        if not data:
            return None
        url = data[0].get('url')
        if not url:
            return None
        saved = save_art(dest_dir, kind, url)
        return (kind, saved, url) if saved else None

    # Four independent lookup+download pairs; running them serially made this
    # eight round trips back to back. These are network-bound, so threads are
    # the right tool.
    out: dict[str, Path] = {}
    from concurrent.futures import ThreadPoolExecutor   # lazy: ~10 ms import
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        for res in pool.map(fetch_one, wanted):
            if not res:
                continue
            kind, saved, url = res
            out[kind] = saved
            # Stash the source URL too (Heroic prefers a remote art URL). These
            # str values sit alongside the Path values; callers read art by the
            # bare kind keys, so the extra '<kind>_url' keys are harmless.
            out[f'{kind}_url'] = url
    return out or None


# ---------------------------------------------------------------------------
# SteamGridDB for every game: variants to choose from, picks, gap filling
# ---------------------------------------------------------------------------
#
# Art lives in ARTWORK_DIR/<slug>/<kind>.<ext> for every game (repacks have
# always used it; Steam/Heroic games get one when art is picked or filled).
# picks.json there records what the user chose, so `--library` can put a
# pick ahead of Steam's own library-cache art. Steam's files are never touched.

SGDB_CACHE = NX.CACHE_DIR / 'sgdb'
SGDB_KINDS = {                     # kind -> (endpoint, query)
    'cover': ('grids', 'dimensions=600x900,660x930&types=static'),
    'wide':  ('grids', 'dimensions=920x430,460x215&types=static'),
    'hero':  ('heroes', 'types=static'),
    'logo':  ('logos', 'types=static'),
    'icon':  ('icons', 'types=static'),
}
_SGDB_CDN = re.compile(r'^https://cdn2?\.steamgriddb\.com/[A-Za-z0-9_./-]+$')
VARIANTS_TTL = 24 * 3600
MISS_TTL = 7 * 24 * 3600


def _sgdb_json(path: str, api_key: str) -> tuple[object, str | None]:
    """GET an SGDB endpoint: (data, None) or (None, 'auth'|'rate'|'none'|'net').
    Through net.get: the key never follows a redirect to another host, and the
    reply is size-capped."""
    from nexus import net
    try:
        payload = json.loads(net.get(f"{SGDB_API}{path}", timeout=15, limit=8 << 20,
                                     headers={'Authorization': f'Bearer {api_key}'}))
    except net.NetError as e:
        return None, {401: 'auth', 403: 'auth', 404: 'none', 429: 'rate'}.get(e.status, 'net')
    except ValueError:
        return None, 'net'
    if not isinstance(payload, dict) or not payload.get('success'):
        return None, 'none'
    return payload.get('data'), None


def _cache_json(name: str) -> dict:
    try:
        return json.loads((SGDB_CACHE / name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _cache_put(name: str, data) -> None:
    try:
        atomic_write(SGDB_CACHE / name, json.dumps(data))
    except OSError:
        pass


_SGDB_LOCK = threading.Lock()


def _cache_set(name: str, key: str, value) -> None:
    """Add one key to a shared cache file. --fill-art looks games up from
    several threads: read-modify-write under a lock, or the last writer
    drops everyone else's ids."""
    with _SGDB_LOCK:
        data = _cache_json(name)
        data[key] = value
        _cache_put(name, data)


def _sgdb_key(g: dict) -> tuple[int | None, str]:
    """(Steam appid or None, the ids.json key) for a library game. A store
    source (nexus/sources) may know the Steam appid as `store_appid`."""
    appid = g.get('appid') if g.get('source') == 'steam' else g.get('store_appid')
    appid = int(appid) if str(appid or '').isdigit() and 0 < int(appid) < 0x80000000 else None
    return appid, (f"steam:{appid}" if appid
                   else f"name:{(g.get('display_name') or '').lower()}")


def cached_sgdb_id(g: dict) -> int | None:
    """The SteamGridDB game id already settled for a game (the user's match,
    else an earlier lookup), without asking SteamGridDB."""
    forced = read_sgdb_match(art_dir_for(g)) if g.get('slug') else None
    if forced:
        return forced['id']
    gid = _cache_json('ids.json').get(_sgdb_key(g)[1])
    return gid if isinstance(gid, int) and gid > 0 else None


def sgdb_game_id(g: dict, api_key: str) -> tuple[int | None, str | None]:
    """The SteamGridDB game id for a library game: by Steam appid when it has
    a real one (a repack's steam_appid is a *shortcut* id, not a store id),
    otherwise the best name match. Cached; misses are remembered a week.
    A store source's game carries the cached id as `sgdb_id` (cmd_library)."""
    forced = read_sgdb_match(art_dir_for(g)) if g.get('slug') else None
    if forced:
        return forced['id'], None
    if isinstance(g.get('sgdb_id'), int) and g['sgdb_id'] > 0:
        return g['sgdb_id'], None
    appid, key = _sgdb_key(g)
    ids, misses = _cache_json('ids.json'), _cache_json('misses.json')
    if key in ids:
        return ids[key], None
    if time.time() - misses.get(key, 0) < MISS_TTL:
        return None, 'none'
    if appid:
        data, err = _sgdb_json(f"/games/steam/{appid}", api_key)
        gid = data.get('id') if isinstance(data, dict) else None
    else:
        import urllib.parse
        name = g.get('display_name') or ''
        data, err = _sgdb_json(f"/search/autocomplete/{urllib.parse.quote(name, safe='')}", api_key)
        gid = data[0].get('id') if isinstance(data, list) and data \
            and isinstance(data[0], dict) else None
    if gid:
        _cache_set('ids.json', key, gid)
        return gid, None
    if err in (None, 'none'):
        _cache_set('misses.json', key, time.time())
    return None, err or 'none'


def _variant(item: dict) -> dict | None:
    url = item.get('url') or ''
    if not _SGDB_CDN.match(url):
        return None
    thumb = item.get('thumb') or url
    return {'id': item.get('id'), 'url': url,
            'thumb': thumb if _SGDB_CDN.match(thumb) else url,
            'width': item.get('width'), 'height': item.get('height'),
            'style': item.get('style'), 'mime': item.get('mime'),
            'score': item.get('score'),
            'author': (item.get('author') or {}).get('name')}


def art_variants(g: dict, kind: str, page: int, api_key: str) -> dict:
    gid, err = sgdb_game_id(g, api_key)
    if not gid:
        return {'game_id': None, 'kind': kind, 'items': [], 'error': err}
    name = f"{gid}-{kind}-{page}.json"
    cached = _cache_json(name)
    if cached and time.time() - cached.get('at', 0) < VARIANTS_TTL:
        return cached['result']
    endpoint, query = SGDB_KINDS[kind]
    data, err = _sgdb_json(f"/{endpoint}/game/{gid}?{query}&page={page}", api_key)
    items = [v for v in (_variant(i) for i in (data or []) if isinstance(i, dict)) if v]
    result = {'game_id': gid, 'kind': kind, 'page': page, 'items': items, 'error': err}
    if err is None:
        _cache_put(name, {'at': time.time(), 'result': result})
    return result


def art_dir_for(g: dict) -> Path:
    return Path(g.get('artwork_dir') or (ARTWORK_DIR / g['slug']))


def read_picks(d: Path) -> dict:
    try:
        data = json.loads((d / 'picks.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_sgdb_match(d: Path) -> dict | None:
    """The SteamGridDB game the user chose for this game (sgdb.json), if any."""
    try:
        data = json.loads((d / 'sgdb.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and int(data.get('id')) > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def sgdb_search(term: str, api_key: str) -> dict:
    """SteamGridDB games matching a name, for choosing the right one."""
    import urllib.parse
    data, err = _sgdb_json(f"/search/autocomplete/{urllib.parse.quote(term, safe='')}", api_key)
    items = []
    for i in (data if isinstance(data, list) else []):
        if not isinstance(i, dict) or not i.get('id'):
            continue
        ts = i.get('release_date')
        year = datetime.fromtimestamp(ts).year if isinstance(ts, (int, float)) else None
        items.append({'id': i['id'], 'name': i.get('name') or '', 'year': year,
                      'verified': bool(i.get('verified'))})
    return {'items': items, 'error': err if not items else None}


def _art_file(d: Path, kind: str) -> Path | None:
    hits = sorted(p for p in d.glob(f'{kind}.*') if p.suffix != '.part')
    return hits[0] if hits else None


def save_art(d: Path, kind: str, url: str) -> Path | None:
    """Download `url` as d/<kind>.<ext>, replacing any other <kind>.* file."""
    import urllib.parse
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() or '.png'
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        ext = '.png'
    tmp = d / f"{kind}{ext}.part"
    if not _download(url, tmp):
        return None
    for old in d.glob(f'{kind}.*'):
        if old != tmp:
            old.unlink(missing_ok=True)
    dest = d / f"{kind}{ext}"
    os.replace(tmp, dest)
    return dest


def overlay_art(g: dict) -> None:
    """Point a library game at its artwork dir, with the user's picks taking
    precedence over the source's own art (Steam library cache, Heroic)."""
    d = art_dir_for(g)
    if not d.is_dir():
        return
    g['artwork_dir'] = str(d)
    for kind in read_picks(d):
        f = _art_file(d, kind) if kind in SGDB_KINDS else None
        if f:
            g[kind] = str(f)
            g.pop(f'{kind}_url', None)


def cmd_art_variants(ident: str, kind: str, page: int, as_json: bool) -> None:
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if kind not in SGDB_KINDS:
        print(f"Unknown art kind {kind!r}", file=sys.stderr)
        sys.exit(2)
    key = load_config().get('api_key')
    res = art_variants(g, kind, page, key) if key else \
        {'game_id': None, 'kind': kind, 'items': [], 'error': 'no-key'}
    picks = read_picks(art_dir_for(g))
    res['current'] = (picks.get(kind) or {}).get('url')
    if as_json:
        print(json.dumps(res))
        return
    for v in res['items']:
        print(f"{v['id']:>10}  {v['width']}x{v['height']}  {v['style'] or '':<10} {v['url']}")
    if res.get('error'):
        print(f"({res['error']})", file=sys.stderr)


def cmd_set_art(ident: str, kind: str, url: str | None, reset: bool) -> None:
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if kind not in SGDB_KINDS:
        print(f"Unknown art kind {kind!r}", file=sys.stderr)
        sys.exit(2)
    d = art_dir_for(g)
    d.mkdir(parents=True, exist_ok=True)
    picks = read_picks(d)
    if reset:
        picks.pop(kind, None)
        if g.get('source') == 'nexus':
            # A repack's "default" is SteamGridDB's first match: fetch it again.
            key = load_config().get('api_key')
            first = (art_variants(g, kind, 0, key)['items'] or [None])[0] if key else None
            saved = save_art(d, kind, first['url']) if first else None
        else:
            for old in d.glob(f'{kind}.*'):
                old.unlink(missing_ok=True)
            saved = None
        print(f"Reset the {kind} art for {g.get('display_name')}.")
    else:
        if not url or not _SGDB_CDN.match(url):
            print("Only SteamGridDB image URLs can be set.", file=sys.stderr)
            sys.exit(2)
        saved = save_art(d, kind, url)
        if not saved:
            print("Download failed.", file=sys.stderr)
            sys.exit(1)
        picks[kind] = {'url': url, 'at': datetime.now().isoformat(timespec='seconds')}
        print(f"Set the {kind} art for {g.get('display_name')}.")
    atomic_write(d / 'picks.json', json.dumps(picks, indent=2))
    appid = g.get('steam_appid') if g.get('source') == 'nexus' else None
    if saved and appid and kind in ('cover', 'hero', 'logo', 'icon'):
        cfg_dir = find_steam_userdata(announce=False)
        if cfg_dir:
            _copy_grid_art(cfg_dir / 'grid', appid, {kind: saved})
            print("Updated the Steam shortcut's artwork too.")


def cmd_sgdb_search(term: str, as_json: bool) -> None:
    key = load_config().get('api_key')
    res = sgdb_search(term, key) if key else {'items': [], 'error': 'no-key'}
    if as_json:
        print(json.dumps(res))
        return
    for v in res['items']:
        print(f"{v['id']:>8}  {v['year'] or '----'}  {v['name']}")
    if res.get('error'):
        print(f"({res['error']})", file=sys.stderr)


def cmd_set_sgdb(ident: str, game_id: int | None, name: str | None, reset: bool) -> None:
    """Pin the SteamGridDB game used for a library game's artwork (or --reset
    to automatic matching). Art picked from the old game is dropped."""
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if not reset and not (game_id and game_id > 0):
        print("--set-sgdb needs --id (or --reset).", file=sys.stderr)
        sys.exit(2)
    d = art_dir_for(g)
    d.mkdir(parents=True, exist_ok=True)
    match = d / 'sgdb.json'
    if reset:
        match.unlink(missing_ok=True)
    else:
        atomic_write(match, json.dumps({'id': game_id, 'name': name or ''}))
    # Anything picked or auto-fetched belongs to the previous match.
    (d / 'picks.json').unlink(missing_ok=True)
    for kind in SGDB_KINDS:
        for old in d.glob(f'{kind}.*'):
            old.unlink(missing_ok=True)
    if g.get('source') == 'nexus':
        cmd_refetch_art(g['slug'], load_config())
    print(f"{'Reset' if reset else 'Set'} the SteamGridDB match for {g.get('display_name')}.")


def cmd_fill_art(install_base: Path, as_json: bool, if_stale: bool) -> None:
    """Fill missing hero / logo / wide art from SteamGridDB for every game
    (first match). Runs at most once a day with --if-stale; stops on a rate
    limit or a bad key."""
    stamp = SGDB_CACHE / 'fill-stamp'
    key = load_config().get('api_key')
    out = {'filled': 0, 'error': None, 'skipped': False}
    if not key:
        out['error'] = 'no-key'
    elif if_stale and stamp.exists() and time.time() - stamp.stat().st_mtime < 24 * 3600:
        out['skipped'] = True
    else:
        from nexus import sources, steamlib
        games = [{**g, 'source': 'nexus'} for g in managed_entries(install_base)]
        games += steamlib.scan(STEAM_ROOT, _read_compat_mapping())
        games += steamlib.heroic_store_games(find_heroic_config())
        stores = sources.installed_games()       # Epic etc.: no art of their own
        games += stores
        bare = {id(g) for g in stores}
        todo = []
        for g in games:
            overlay_art(g)
            d = art_dir_for(g)
            kinds = ('cover', 'hero', 'logo', 'wide') if id(g) in bare else ('hero', 'logo', 'wide')
            for kind in kinds:
                local = g.get(kind) and Path(g[kind]).exists()
                if local or g.get(f'{kind}_url') or (d.is_dir() and _art_file(d, kind)):
                    continue
                todo.append((g, kind))
        stop = {'err': None}

        def one(job):
            g, kind = job
            if stop['err']:
                return 0
            res = art_variants(g, kind, 0, key)
            if res.get('error') in ('rate', 'auth'):
                stop['err'] = res['error']
                return 0
            first = (res.get('items') or [None])[0]
            if not first:
                return 0
            d = art_dir_for(g)
            d.mkdir(parents=True, exist_ok=True)
            return 1 if save_art(d, kind, first['url']) else 0
        from concurrent.futures import ThreadPoolExecutor   # lazy: ~10 ms import
        with ThreadPoolExecutor(max_workers=4) as pool:
            out['filled'] = sum(pool.map(one, todo))
        out['error'] = stop['err']
        try:
            SGDB_CACHE.mkdir(parents=True, exist_ok=True)
            stamp.touch()
        except OSError:
            pass
    if as_json:
        print(json.dumps(out))
    else:
        print(f"Filled {out['filled']} image(s)." + (f" ({out['error']})" if out['error'] else ""))


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .config import load_config  # noqa: E402
from .gamecmds import cmd_refetch_art  # noqa: E402
from .heroic import find_heroic_config  # noqa: E402
from .manage import _game_by_id, managed_entries  # noqa: E402
from .steam import _copy_grid_art, _read_compat_mapping, find_steam_userdata  # noqa: E402
