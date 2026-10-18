"""ProtonDB compatibility tiers for Steam appids, cached for a day per game
(the same public endpoint protongen uses). Fetched in parallel, and only for
the games asked about — never the whole catalogue."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import net
from .paths import CACHE_DIR

URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
PAGE = "https://www.protondb.com/app/{appid}"
MAX_AGE = 24 * 3600


def _cache(appid: int):
    return CACHE_DIR / 'protondb' / f"{appid}.json"


def _one(appid: int, refresh: bool) -> dict:
    path = _cache(appid)
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    try:
        raw = json.loads(net.get(URL.format(appid=appid), timeout=10))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        info = {'tier': raw.get('tier'), 'trending': raw.get('trendingTier'),
                'best': raw.get('bestReportedTier'), 'confidence': raw.get('confidence'),
                'total': raw.get('total')}
    except (net.NetError, ValueError) as e:
        if path.exists():                  # keep serving yesterday's answer offline
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        info = {'tier': None, 'url': PAGE.format(appid=appid)}
        if getattr(e, 'status', None) != 404:
            return info                    # offline: don't remember "no reports" for a day
    info['url'] = PAGE.format(appid=appid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = net._tmp_name(path)
        tmp.write_text(json.dumps(info))
        tmp.replace(path)
    except OSError:
        pass
    return info


def tiers(appids, refresh: bool = False) -> dict[int, dict]:
    ids = sorted({int(a) for a in appids if a and int(a) < 0x80000000})
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(ids, pool.map(lambda a: _one(a, refresh), ids)))
