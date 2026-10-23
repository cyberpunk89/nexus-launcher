"""How long to beat a game, from HowLongToBeat, for any game by title
(Game-db already covers Steam games on its lists; this covers the rest).

HowLongToBeat has no public API. Its own search page asks
/api/search/site/init for a short-lived token, then POSTs the search with
it in x-auth-token — Nexus does the same, once per title, and caches the
answer for 30 days (a miss for a week, a failure for an hour). If the site
changes, lookups quietly return nothing until this is updated."""

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


def lookup(name: str, year: int | None = None, refresh: bool = False) -> dict | None:
    """{main, plus, complete (hours), id, url, name} or None."""
    key = title_key(name)
    if not key:
        return None
    path = CACHE_DIR / 'hltb' / (re.sub(r'\s+', '-', key)[:120] + '.json')
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        age = time.time() - path.stat().st_mtime
        limit = (HIT_AGE if cached.get('hit') else
                 FAIL_AGE if cached.get('failed') else MISS_AGE)
        if not refresh and age < limit:
            return cached.get('hit')
    except (OSError, ValueError, AttributeError):
        cached = None
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
        if cached and cached.get('hit'):
            return cached['hit']                    # keep serving the old answer
        hit, record = None, {'failed': str(e)[:200]}
    net.save_json(path, record)
    return hit
