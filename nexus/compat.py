"""Compatibility facts for a game beyond ProtonDB, all keyed by Steam store
appid and cached on disk:

- anti-cheat status from Are We Anti-Cheat Yet (one public games.json);
- Valve's Steam Deck / SteamOS compatibility report;
- PCGamingWiki's support table (ultrawide, HDR, high frame rate, upscaling,
  Direct3D/Vulkan) and its "Essential improvements" headings.

PCGamingWiki no longer allows anonymous Cargo queries, so the page is found
through its appid redirect and its wikitext is read instead. Every lookup
fails soft: offline, the last good answer is served, else nothing."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote

from . import net
from .paths import CACHE_DIR

AWACY_URL = ("https://raw.githubusercontent.com/AreWeAntiCheatYet/AreWeAntiCheatYet/"
             "HEAD/games.json")
AWACY_PAGE = "https://areweanticheatyet.com/game/{slug}"
DECK_URL = ("https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport"
            "?nAppID={appid}&l=english")
PCGW_APPID = "https://www.pcgamingwiki.com/api/appid.php?appid={appid}"
PCGW_PARSE = ("https://www.pcgamingwiki.com/w/api.php?action=parse&page={page}"
              "&prop=wikitext&format=json&formatversion=2&redirects=1")
PCGW_PAGE = "https://www.pcgamingwiki.com/wiki/{page}"
DAY = 24 * 3600

DECK = {0: None, 1: 'Unsupported', 2: 'Playable', 3: 'Verified'}
# PCGamingWiki template fields shown, as (wikitext key, label).
PCGW_FIELDS = (('ultrawidescreen', 'Ultrawide'), ('4k ultra hd', '4K'), ('hdr', 'HDR'),
               ('high frame rate', 'High frame rate'), ('upscaling', 'Upscaling'),
               ('ray tracing', 'Ray tracing'), ('frame gen', 'Frame generation'))
PCGW_VALUES = {'true', 'false', 'limited', 'hackable', 'n/a', 'always on'}


def store_appid(game: dict) -> int | None:
    """The Steam store appid behind any library game: a Steam game's own,
    or for a repack the one its umu GAMEID was resolved to (umu-<appid>).
    Non-Steam shortcut ids (>= 0x80000000) are never store appids."""
    for v in (game.get('appid') if game.get('source') == 'steam' else None,
              (game.get('umu_gameid') or '').removeprefix('umu-')):
        if v is not None and str(v).isdigit() and 0 < int(v) < 0x80000000:
            return int(v)
    return None


def _cached_json(path, max_age: float, fetch, refresh: bool = False):
    """fetch() through a JSON file cache. A failed fetch serves the last
    good copy; None from fetch() is a real "nothing here" and is cached."""
    try:
        if not refresh and time.time() - path.stat().st_mtime < max_age:
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    try:
        value = fetch()
    except (net.NetError, ValueError, KeyError, TypeError):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = net._tmp_name(path)
        tmp.write_text(json.dumps(value))
        tmp.replace(path)
    except OSError:
        pass
    return value


# -- Are We Anti-Cheat Yet --------------------------------------------------

def anticheat_index(refresh: bool = False) -> dict[int, dict]:
    """{appid: {status, anticheats, name, url, note}} for every listed game."""
    try:
        data, _at, _stale = net.cached(AWACY_URL, 'awacy-games.json', DAY, refresh,
                                       timeout=20)
    except net.NetError:
        return {}
    out = {}
    for g in data if isinstance(data, list) else []:
        sid = str(((g.get('storeIds') or {}).get('steam')) or '')
        if not sid.isdigit():
            continue
        notes = [n[0] for n in g.get('notes') or [] if isinstance(n, list) and n]
        out[int(sid)] = {'status': g.get('status'), 'name': g.get('name'),
                         'anticheats': list(g.get('anticheats') or []),
                         'native': bool(g.get('native')),
                         'note': notes[0] if notes else None,
                         'url': AWACY_PAGE.format(slug=quote(g.get('slug') or ''))}
    return out


def anticheat(appids, refresh: bool = False) -> dict[int, dict]:
    idx = anticheat_index(refresh)
    return {a: idx[a] for a in appids if a in idx}


# -- Steam Deck / SteamOS ---------------------------------------------------

def _deck_one(appid: int, refresh: bool) -> dict | None:
    def fetch():
        raw = json.loads(net.get(DECK_URL.format(appid=appid), timeout=10, limit=1 << 20))
        res = raw.get('results') if isinstance(raw, dict) else None
        if not isinstance(res, dict):
            return None
        deck = DECK.get(res.get('resolved_category'))
        steamos = res.get('steamos_resolved_category')
        if not deck and not steamos:
            return None
        return {'deck': deck,
                'steamos': {1: 'Unsupported', 2: 'Compatible'}.get(steamos)}
    return _cached_json(CACHE_DIR / 'deck' / f"{appid}.json", 7 * DAY, fetch, refresh)


def deck(appids, refresh: bool = False) -> dict[int, dict]:
    ids = sorted({int(a) for a in appids})
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        got = dict(zip(ids, pool.map(lambda a: _deck_one(a, refresh), ids)))
    return {a: v for a, v in got.items() if v}


# -- PCGamingWiki -------------------------------------------------------------

def parse_pcgw(wikitext: str) -> dict:
    """The support table and fix headings from a PCGamingWiki page."""
    support = {}
    for key, label in PCGW_FIELDS:
        m = re.search(r'^\|\s*' + re.escape(key) + r'\s*=[ \t]*([^\n|]*)', wikitext,
                      re.I | re.M)
        v = (m.group(1).strip().lower() if m else '')
        if v in PCGW_VALUES:
            support[label] = v
    apis = []
    for key, name in (('direct3d versions', 'Direct3D'), ('vulkan versions', 'Vulkan'),
                      ('opengl versions', 'OpenGL')):
        m = re.search(r'^\|\s*' + re.escape(key) + r'\s*=[ \t]*([^\n|]*)', wikitext,
                      re.I | re.M)
        v = m.group(1).strip() if m else ''
        if v and v.lower() not in ('false', 'unknown'):
            apis.append(f"{name} {v}" if re.match(r'[\d.,\s]+$', v) else name)
    fixes = []
    m = re.search(r'^==\s*Essential improvements\s*==\s*$(.*?)(?=^==[^=])', wikitext,
                  re.M | re.S)
    if m:
        fixes = [h.strip() for h in re.findall(r'^===\s*([^=\n]+?)\s*===\s*$', m.group(1),
                                               re.M)]
    return {'support': support, 'apis': apis, 'fixes': fixes[:8]}


def _pcgw_one(appid: int, refresh: bool) -> dict | None:
    def fetch():
        url = net.final_url(PCGW_APPID.format(appid=appid), timeout=15)
        if '/wiki/' not in url:
            return None                            # PCGamingWiki has no page for it
        page = unquote(url.split('/wiki/', 1)[1].split('#')[0])
        raw = json.loads(net.get(PCGW_PARSE.format(page=quote(page)), timeout=15,
                                 limit=4 << 20))
        text = ((raw.get('parse') or {}).get('wikitext')) if isinstance(raw, dict) else None
        if not isinstance(text, str):
            return None
        info = parse_pcgw(text)
        info['page'] = page.replace('_', ' ')
        info['url'] = PCGW_PAGE.format(page=quote(page.replace(' ', '_'), safe=":'()"))
        return info
    return _cached_json(CACHE_DIR / 'pcgw' / f"{appid}.json", 7 * DAY, fetch, refresh)


def pcgw(appids, refresh: bool = False) -> dict[int, dict]:
    ids = sorted({int(a) for a in appids})
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = dict(zip(ids, pool.map(lambda a: _pcgw_one(a, refresh), ids)))
    return {a: v for a, v in got.items() if v}


def facts(appid: int | None, refresh: bool = False) -> dict:
    """Everything above for one game, fetched in parallel."""
    if not appid:
        return {'anticheat': None, 'deck': None, 'pcgw': None}
    with ThreadPoolExecutor(max_workers=3) as pool:
        ac = pool.submit(anticheat, [appid], refresh)
        dk = pool.submit(_deck_one, appid, refresh)
        pw = pool.submit(_pcgw_one, appid, refresh)
        return {'anticheat': ac.result().get(appid), 'deck': dk.result(),
                'pcgw': pw.result()}
