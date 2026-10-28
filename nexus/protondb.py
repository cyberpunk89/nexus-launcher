"""ProtonDB compatibility tiers for Steam appids, cached for a day per game
(the same public endpoint protongen uses). Fetched in parallel, and only for
the games asked about — never the whole catalogue. A game in your Game-db
library or wishlist is answered from Game-db's copy (gamedb.meta) while that
is fresh enough, with no request to ProtonDB at all."""

import json
from concurrent.futures import ThreadPoolExecutor

from . import net
from .paths import CACHE_DIR

URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
PAGE = "https://www.protondb.com/app/{appid}"
MAX_AGE = 24 * 3600
# Game-db refreshes ProtonDB once a day and Nexus syncs from it every 6 h, so
# its copy can be up to a day plus a sync old and still be the daily answer.
GAMEDB_MAX_AGE = MAX_AGE + 12 * 3600


def _cache(appid: int):
    return CACHE_DIR / 'protondb' / f"{appid}.json"


def from_gamedb(appid: int) -> dict | None:
    """Game-db's ProtonDB summary for one of your games, in _one()'s shape,
    or None when it has none or it's too old."""
    from . import gamedb
    p = (gamedb.meta(appid) or {}).get('proton')
    if not isinstance(p, dict) or not p.get('tier') \
            or not gamedb.fresh(p.get('fetchedAt'), GAMEDB_MAX_AGE):
        return None
    return {'tier': p.get('tier'), 'trending': p.get('trendingTier'), 'best': None,
            'confidence': None, 'total': p.get('total'), 'url': PAGE.format(appid=appid),
            'source': 'gamedb'}


def _one(appid: int, refresh: bool) -> dict:
    page = PAGE.format(appid=appid)
    if not refresh and (hit := from_gamedb(appid)):
        return hit

    def fetch():
        try:
            raw = json.loads(net.get(URL.format(appid=appid), timeout=10))
        except net.NetError as e:
            if e.status == 404:            # a real "no reports": cached like an answer
                return {'tier': None, 'url': page}
            raise
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        return {'tier': raw.get('tier'), 'trending': raw.get('trendingTier'),
                'best': raw.get('bestReportedTier'), 'confidence': raw.get('confidence'),
                'total': raw.get('total'), 'url': page}
    # Offline: yesterday's answer if there is one, else no tier (not cached).
    return net.cached_json(_cache(appid), MAX_AGE, fetch, refresh) or {'tier': None, 'url': page}


def tiers(appids, refresh: bool = False) -> dict[int, dict]:
    ids = sorted({int(a) for a in appids if a and int(a) < 0x80000000})
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(ids, pool.map(lambda a: _one(a, refresh), ids)))
