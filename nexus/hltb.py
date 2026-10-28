"""How long to beat a game, from HowLongToBeat, for any game by title
(Game-db already covers Steam games on its lists; this covers the rest).

HowLongToBeat has no public API. Its own search page asks
/api/search/site/init for a short-lived token, then POSTs the search with
it in x-auth-token — Nexus does the same, once per title, and caches the
answer for 30 days (a miss for a week, a failure for an hour). If the site
changes, lookups quietly return nothing until this is updated.

Given the Steam appid of a game in your Game-db library or wishlist, Game-db's
play length (HowLongToBeat's, or IGDB's when HLTB has none) is used instead,
while younger than HIT_AGE; no request goes to HowLongToBeat then."""

import difflib
import json
import re
import time

from . import net
from .paths import CACHE_DIR

SITE = "https://howlongtobeat.com"
INIT = SITE + "/api/search/site/init?t={ms}"
SEARCH = SITE + "/api/search/site"
GAME_PAGE = SITE + "/game/{id}"
HIT_AGE, MISS_AGE, FAIL_AGE = 30 * 86400, 7 * 86400, 3600


def title_key(name: str) -> str:
    n = re.sub(r'[™®©]', '', (name or '').lower()).replace('’', "'").replace("'", '')
    return re.sub(r'[\W_]+', ' ', n).strip()


def _body(terms: list[str]) -> bytes:
    return json.dumps({
        'searchType': 'games', 'searchTerms': terms, 'searchPage': 1, 'size': 20,
        'searchOptions': {
            'games': {'userId': 0, 'platform': '', 'sortCategory': 'popular',
                      'rangeCategory': 'main', 'rangeTime': {'min': None, 'max': None},
                      'gameplay': {'perspective': '', 'flow': '', 'genre': '', 'difficulty': ''},
                      'rangeYear': {'min': '', 'max': ''}, 'modifier': ''},
            'users': {'sortCategory': 'postcount'}, 'lists': {'sortCategory': 'follows'},
            'filter': '', 'sort': 0, 'randomizer': 0},
        'useCache': True}).encode()


def _headers() -> dict:
    return {'Referer': SITE + '/', 'Origin': SITE}


def search(name: str) -> list[dict]:
    raw = json.loads(net.get(INIT.format(ms=int(time.time() * 1000)), timeout=15,
                             headers=_headers(), limit=1 << 16))
    token = (raw or {}).get('token')
    if not isinstance(token, str) or not token:
        raise net.NetError("HowLongToBeat gave no search token")
    terms = [t for t in re.sub(r'[^\w\s]', ' ', name).split() if t]
    data = json.loads(net.post(SEARCH, _body(terms),
                               {**_headers(), 'Content-Type': 'application/json',
                                'x-auth-token': token}, timeout=15))
    return data.get('data') or [] if isinstance(data, dict) else []


def best_match(name: str, rows: list[dict], year: int | None = None) -> dict | None:
    key = title_key(name)
    scored = []
    for r in rows:
        names = [r.get('game_name') or '', *(str(r.get('game_alias') or '').split(','))]
        ratio = max(difflib.SequenceMatcher(None, key, title_key(n)).ratio() for n in names)
        if year and r.get('release_world') == year:
            ratio += 0.05
        scored.append((ratio, r))
    if not scored:
        return None
    ratio, r = max(scored, key=lambda x: x[0])
    return r if ratio >= 0.85 else None


def _hours(seconds) -> float | None:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return None
    return round(s / 3600, 1) if s > 0 else None


def _path(key: str):
    return CACHE_DIR / 'hltb' / (re.sub(r'\s+', '-', key)[:120] + '.json')


def _read(path) -> tuple[dict | None, bool]:
    """The cached record and whether it is still fresh."""
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        age = max(0.0, time.time() - path.stat().st_mtime)
        limit = (HIT_AGE if record.get('hit') else
                 FAIL_AGE if record.get('failed') else MISS_AGE)
        return record, age < limit
    except (OSError, ValueError, AttributeError):
        return None, False


def cached(name: str) -> tuple[bool, dict | None]:
    """lookup() from the cache only: (fresh, the hit at any age or None)."""
    key = title_key(name)
    if not key:
        return True, None                  # nothing to look up, ever
    record, fresh = _read(_path(key))
    return fresh, (record or {}).get('hit')


def from_gamedb(appid) -> dict | None:
    """Game-db's play length for one of your games ({main, plus, complete,
    source 'hltb'|'igdb', count, url, name}), or None when it has none or
    it's older than HIT_AGE."""
    if not appid:
        return None
    from . import gamedb
    row = gamedb.meta(appid)
    pl = gamedb.play_length(row)
    if not pl or not gamedb.fresh(((row or {}).get('playLength') or {}).get('fetchedAt'),
                                  HIT_AGE):
        return None
    return pl


def lookup(name: str, year: int | None = None, refresh: bool = False,
           errors: list | None = None, appid: int | None = None) -> dict | None:
    """{main, plus, complete (hours), id, url, name} or None. A failed
    search is appended to `errors` (when given). From Game-db ({…, source,
    count}) when `appid` is one of your games it has hours for."""
    if not refresh and (hit := from_gamedb(appid)):
        return hit
    key = title_key(name)
    if not key:
        return None
    path = _path(key)
    old, fresh = _read(path)
    if old is not None and not refresh and fresh:
        return old.get('hit')
    try:
        row = best_match(name, search(name), year)
        hit = None if row is None else {
            'id': row.get('game_id'), 'name': row.get('game_name'),
            'main': _hours(row.get('comp_main')), 'plus': _hours(row.get('comp_plus')),
            'complete': _hours(row.get('comp_100')),
            'url': GAME_PAGE.format(id=int(row['game_id'])) if str(
                row.get('game_id', '')).isdigit() else None}
        record = {'hit': hit}
    except (net.NetError, ValueError, KeyError, TypeError) as e:
        if errors is not None:
            errors.append(e)
        if old and old.get('hit'):
            return old['hit']                    # keep serving the old answer
        hit, record = None, {'failed': str(e)[:200]}
    net.save_json(path, record)
    return hit
