"""What the Steam store knows about a game, by store appid: trailers,
screenshots, metacritic score, genres and blurb (appdetails), the review
summary (appreviews) and "more like this" (the store's similar-games page).
No key needed; everything is cached on disk and fails soft — offline, the
last good answer is served, else nothing. The review summary of a game in
your Game-db library or wishlist comes from Game-db's copy (gamedb.meta)
while that is younger than the reviews cache's own 3 days."""

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import net
from .paths import CACHE_DIR

DETAILS = "https://store.steampowered.com/api/appdetails?appids={appid}&l=english&cc=us"
REVIEWS = ("https://store.steampowered.com/appreviews/{appid}?json=1&language=all"
           "&purchase_type=all&num_per_page=0")
MORELIKE = "https://store.steampowered.com/recommended/morelike/app/{appid}/"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"
HEADER = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
DAY = 24 * 3600
_STEAM_MEDIA = re.compile(r'^https://[\w.-]+\.(steamstatic|steampowered)\.com/')


def _https_steam(url) -> str | None:
    return url if isinstance(url, str) and _STEAM_MEDIA.match(url) else None


def _text(s: str | None, limit: int = 600) -> str:
    t = html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))
    t = re.sub(r'\s+', ' ', t).strip()
    return t if len(t) <= limit else t[:limit - 1].rsplit(' ', 1)[0] + '…'


# Bumped when details() keeps more fields: older cache files are refetched
# (and still served while offline).
STORE_V = 2


def _current(v) -> bool:
    return v is None or (isinstance(v, dict) and v.get('v') == STORE_V)


DETAILS_AGE = 7 * DAY


def _details_path(appid: int):
    return CACHE_DIR / 'store' / f"{appid}.json"


def details(appid: int, refresh: bool = False, errors: list | None = None) -> dict | None:
    """The store page's facts, or None when Steam has no such app. A failed
    fetch is appended to `errors` (when given) before the cache serves the
    last good copy, so a caller can tell fresh from stale and see a 429."""
    def fetch():
        try:
            raw = json.loads(net.get(DETAILS.format(appid=appid), timeout=15, limit=8 << 20))
            if not isinstance(raw, dict) or not isinstance(raw.get(str(appid)), dict):
                # Throttled, Steam answers `null`: not "no such app".
                raise net.NetError("appdetails gave no answer")
        except (net.NetError, ValueError) as e:
            if errors is not None:
                errors.append(e)
            raise
        d = raw[str(appid)]
        if not d.get('success'):
            return None
        d = d.get('data') or {}
        trailers = []
        for m in d.get('movies') or []:
            stream = _https_steam(m.get('hls_h264')) or _https_steam(
                (m.get('mp4') or {}).get('max')) or _https_steam((m.get('webm') or {}).get('max'))
            if stream:
                trailers.append({'name': _text(m.get('name'), 120), 'url': stream,
                                 'thumb': _https_steam(m.get('thumbnail')),
                                 'highlight': bool(m.get('highlight'))})
        trailers.sort(key=lambda t: not t['highlight'])
        mc = d.get('metacritic') or {}
        return {
            'v': STORE_V,
            'name': d.get('name'),
            'blurb': _text(d.get('short_description')),
            'genres': [g.get('description') for g in d.get('genres') or [] if g.get('description')],
            'developers': list(d.get('developers') or [])[:3],
            'publishers': list(d.get('publishers') or [])[:3],
            'categories': [{'id': c.get('id'), 'name': c.get('description')}
                           for c in d.get('categories') or [] if isinstance(c, dict)],
            'release': (d.get('release_date') or {}).get('date'),
            'coming_soon': bool((d.get('release_date') or {}).get('coming_soon')),
            'metacritic': ({'score': mc['score'], 'url': mc.get('url') if str(
                mc.get('url', '')).startswith('https://www.metacritic.com/') else None}
                if isinstance(mc.get('score'), int) else None),
            'trailers': trailers[:6],
            'screenshots': [u for s in (d.get('screenshots') or [])[:12]
                            if (u := _https_steam(s.get('path_full')))],
            'url': STORE_PAGE.format(appid=appid),
        }
    return net.cached_json(_details_path(appid), DETAILS_AGE, fetch, refresh, valid=_current)


def cached_details(appid: int) -> tuple[bool, dict | None]:
    """details() from the cache only: (fresh, value at any age)."""
    return net.peek_json(_details_path(appid), DETAILS_AGE, _current)


REVIEWS_AGE = 3 * DAY


def reviews_from_gamedb(appid: int) -> dict | None:
    """Game-db's Steam review summary in reviews()' shape, or None. Its
    age is when Nexus last synced the row (the server refreshes reviews
    every few days on its own)."""
    from . import gamedb
    row = gamedb.meta(appid) or {}
    rv = row.get('reviews')
    if not isinstance(rv, dict) or not rv.get('steamDesc') or not rv.get('steamTotal') \
            or not gamedb.fresh(row.get('synced_at'), REVIEWS_AGE):
        return None
    pct, total = rv.get('steamPercent'), rv['steamTotal']
    return {'desc': rv['steamDesc'], 'score': None, 'total': total,
            'positive': round(pct * total / 100) if isinstance(pct, (int, float)) else None,
            'percent': pct if isinstance(pct, (int, float)) else None, 'source': 'gamedb'}


def reviews(appid: int, refresh: bool = False) -> dict | None:
    if not refresh and (hit := reviews_from_gamedb(appid)):
        return hit

    def fetch():
        raw = json.loads(net.get(REVIEWS.format(appid=appid), timeout=15, limit=1 << 20))
        q = (raw or {}).get('query_summary') or {}
        if not q.get('total_reviews'):
            return None
        return {'desc': q.get('review_score_desc'), 'score': q.get('review_score'),
                'positive': q.get('total_positive'), 'total': q.get('total_reviews'),
                'percent': round(100 * q['total_positive'] / q['total_reviews'])
                if q.get('total_positive') is not None else None}
    return net.cached_json(CACHE_DIR / 'reviews' / f"{appid}.json", REVIEWS_AGE, fetch, refresh)


_SIMILAR = re.compile(r'class="similar_grid_capsule"[^>]*?data-ds-appid="(\d+)"[^>]*?'
                      r'href="https://store\.steampowered\.com/app/\d+/([^/"?]*)')


def parse_morelike(page: str, appid: int) -> list[dict]:
    out, seen = [], {appid}
    for sid, slug in _SIMILAR.findall(page):
        a = int(sid)
        if a in seen:
            continue
        seen.add(a)
        out.append({'appid': a, 'name': html.unescape(slug).replace('_', ' ').strip() or sid,
                    'image': HEADER.format(appid=a), 'url': STORE_PAGE.format(appid=a)})
    return out


def morelike(appid: int, refresh: bool = False) -> list[dict]:
    def fetch():
        page = net.get(MORELIKE.format(appid=appid), timeout=20, limit=4 << 20)
        return parse_morelike(page.decode('utf-8', 'replace'), appid)[:12]
    return net.cached_json(CACHE_DIR / 'morelike' / f"{appid}.json", 14 * DAY, fetch,
                        refresh) or []


def info(appid: int | None, refresh: bool = False) -> dict:
    if not appid:
        return {'store': None, 'reviews': None, 'similar': []}
    with ThreadPoolExecutor(max_workers=3) as pool:
        d = pool.submit(details, appid, refresh)
        r = pool.submit(reviews, appid, refresh)
        m = pool.submit(morelike, appid, refresh)
        return {'store': d.result(), 'reviews': r.result(), 'similar': m.result()}
