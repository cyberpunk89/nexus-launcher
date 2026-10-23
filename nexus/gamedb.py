"""Game-db integration: wishlist deals, upcoming releases and backlog picks
from the user's Game-db instance (github.com/cyberpunk89/Game-db).

One read-only endpoint, `GET {url}/api/nexus/v1/summary`, authenticated with
a personal token (`Authorization: Bearer gdb_…`, minted on Game-db's /account
page). The response is cached for 45 min under ~/.cache/nexus/gamedb/ (0600 —
it's personal data) and served stale, with the error attached, when Game-db
is unreachable or the token stops working.

The token is read from `[gamedb] token` in config.toml or NEXUS_GAMEDB_TOKEN,
never taken on a command line, and never appears in an error message. The
fetch refuses redirects: urllib would otherwise forward the Authorization
header to wherever the server points it."""

import hashlib
import json
import os
import re
import time
import tomllib
import urllib.parse
from datetime import datetime, timezone

from . import net, paths

# No built-in server: Game-db is self-hosted, so the address comes from [gamedb] url
# (or NEXUS_GAMEDB_URL). Shown in empty fields only.
EXAMPLE_URL = "https://gamedb.example.com"
ENDPOINT = "/api/nexus/v1/summary"
MAX_AGE = 45 * 60
SCHEMA = 1
LIMIT = 16 << 20
STEAMID64_BASE = 76561197960265728
_LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
TOKEN_RE = re.compile(r'^gdb_[A-Za-z0-9_-]{43}$')


class GameDbError(net.NetError):
    """kind: config | auth | rate | net | schema."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


# ---- settings -------------------------------------------------------------

def check_url(url: str) -> str:
    """Normalise a Game-db base URL. https only — plain http is allowed for a
    local dev server (`npm run dev`) and nothing else."""
    url = (url or '').strip().rstrip('/')
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        raise GameDbError('config', f"not a URL: {url!r}") from None
    if not u.hostname:
        raise GameDbError('config', f"not a URL: {url!r}")
    if u.scheme == 'https' or (u.scheme == 'http' and u.hostname in _LOCAL_HOSTS):
        return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip('/'), '', ''))
    raise GameDbError('config', f"Game-db URL must be https:// (got {url!r})")


def settings() -> dict:
    """{enabled, url, token, token_source ('env'|'config'|None), url_error}.
    Missing/unparseable config → disabled defaults; never raises."""
    try:
        data = tomllib.loads(paths.CONFIG_PATH.read_text(encoding='utf-8-sig')).get('gamedb', {})
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    env_token = os.environ.get('NEXUS_GAMEDB_TOKEN', '').strip()
    cfg_token = str(data.get('token') or '').strip()
    token = env_token or cfg_token
    raw_url = os.environ.get('NEXUS_GAMEDB_URL', '').strip() or str(data.get('url') or '').strip()
    url, url_error = None, None
    if raw_url:                       # none set: not an error, just not set up
        try:
            url = check_url(raw_url)
        except GameDbError as e:
            url_error = str(e)
    return {
        'enabled': bool(data.get('enabled', False)) or bool(env_token),
        'url': url,
        'token': token or None,
        'token_source': 'env' if env_token else ('config' if cfg_token else None),
        'url_error': url_error,
    }


def configured(s: dict | None = None) -> bool:
    s = s or settings()
    return bool(s['enabled'] and s['url'] and s['token'])


def detail_url(base: str, appid: int) -> str:
    return f"{base}/game/{int(appid)}"


# ---- fetching -------------------------------------------------------------

def _no_redirect():
    """A redirect handler that refuses (urllib loads only when fetching)."""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise GameDbError('net', f"Game-db redirected to {newurl} — set [gamedb] url "
                                     "to the final address")
    return _NoRedirect


def _fetch(base: str, token: str, timeout: float = 20) -> dict:
    import urllib.error
    import urllib.request
    url = base + ENDPOINT
    req = urllib.request.Request(url, headers={
        'User-Agent': net.UA,
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
    })
    opener = urllib.request.build_opener(_no_redirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(LIMIT + 1)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code in (401, 403):
            raise GameDbError('auth', "Game-db rejected the token — create a new one on "
                                      f"{base}/account") from None
        if e.code == 429:
            try:
                retry = float(e.headers.get('Retry-After') or 600)
            except ValueError:
                retry = 600.0
            raise GameDbError('rate', f"Game-db is rate-limiting; retry in {int(retry)}s",
                              retry_after=retry) from None
        raise GameDbError('net', f"{ENDPOINT}: HTTP {e.code}") from None
    except GameDbError:
        raise
    except net.errors() as e:
        raise GameDbError('net', f"{base}: {getattr(e, 'reason', e)}") from None
    if len(raw) > LIMIT:
        raise GameDbError('net', f"{ENDPOINT}: response larger than {LIMIT} bytes")
    try:
        data = json.loads(raw)
    except ValueError:
        raise GameDbError('schema', f"{ENDPOINT}: not JSON") from None
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        got = data.get('schema') if isinstance(data, dict) else None
        raise GameDbError('schema', f"Game-db API schema {got!r}, this Nexus understands "
                                    f"{SCHEMA} — update one of them")
    return data


# ---- cache ----------------------------------------------------------------

def _fingerprint(url: str, token: str) -> str:
    return hashlib.sha256(f"{url}\0{token}".encode()).hexdigest()[:16]


def _summary_path():
    return paths.GAMEDB_CACHE / 'summary.json'


def _state_path():
    return paths.GAMEDB_CACHE / 'state.json'


def _write_private(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    net.write_atomic(path, json.dumps(obj), mode=0o600)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _cached(fp: str):
    c = _read_json(_summary_path())
    if isinstance(c, dict) and c.get('fingerprint') == fp and isinstance(c.get('data'), dict):
        return c
    return None


def last_state() -> dict:
    """{error: {kind, message, at}|None, retry_at: float|None}."""
    s = _read_json(_state_path())
    return s if isinstance(s, dict) else {}


def clear_cache() -> None:
    for p in (_summary_path(), _state_path()):
        p.unlink(missing_ok=True)


def summary(refresh: bool = False, cache_only: bool = False) -> dict:
    """{data, fetched_at, stale, error}. Fresh cache (< MAX_AGE) is reused
    unless `refresh`; any failure falls back to the last good copy with
    `stale=True` and `error={kind, message}`. With no copy, GameDbError."""
    s = settings()
    if not s['url']:
        raise GameDbError('config', s['url_error'] or "no Game-db URL configured")
    if not s['token']:
        raise GameDbError('config', "no Game-db token — create one on "
                                    f"{s['url']}/account and add it in Settings")
    fp = _fingerprint(s['url'], s['token'])
    cache = _cached(fp)
    now = time.time()
    fresh = cache and now - cache.get('fetched_at', 0) < MAX_AGE
    state = last_state()
    backoff = (state.get('retry_at') or 0) > now

    def stale(err):
        if cache:
            return {'data': cache['data'], 'fetched_at': cache['fetched_at'],
                    'stale': not fresh, 'error': err}
        raise GameDbError(err['kind'], err['message'])

    if cache_only or (fresh and not refresh):
        if cache:
            return {'data': cache['data'], 'fetched_at': cache['fetched_at'],
                    'stale': not fresh, 'error': state.get('error')}
        raise GameDbError('net', "no cached Game-db data yet")
    if backoff:
        return stale({'kind': 'rate', 'message': "Game-db asked us to slow down; retry in "
                                                 f"{int(state['retry_at'] - now)}s"})
    try:
        data = _fetch(s['url'], s['token'])
    except GameDbError as e:
        err = {'kind': e.kind, 'message': str(e), 'at': now}
        _write_private(_state_path(), {
            'error': err,
            'retry_at': now + e.retry_after if e.retry_after else None,
        })
        return stale(err)
    _write_private(_summary_path(), {'fingerprint': fp, 'fetched_at': now, 'data': data})
    _write_private(_state_path(), {'error': None, 'retry_at': None})
    return {'data': data, 'fetched_at': now, 'stale': False, 'error': None}


def lookup(appids) -> dict:
    """Cache-only (never network) per-appid info for the details panel:
    {appid: {list, title, price, hltb, completion, release, fetched_at}}."""
    s = settings()
    if not configured(s):
        return {}
    cache = _cached(_fingerprint(s['url'], s['token']))
    if not cache:
        return {}
    want = {int(a) for a in appids}
    data, out = cache['data'], {}
    upcoming = {g.get('appid'): g for g in data.get('upcoming') or []}
    for g in data.get('wishlist') or []:
        if g.get('appid') in want:
            out[g['appid']] = {'list': 'wishlist', 'title': g.get('title'),
                               'price': g.get('price'), 'hltb': g.get('hltb'),
                               'release': upcoming.get(g['appid'])}
    for g in data.get('library') or []:
        if g.get('appid') in want:
            out[g['appid']] = {'list': 'library', 'title': g.get('title'),
                               'price': g.get('price'), 'hltb': g.get('hltb'),
                               'completion': g.get('completion'),
                               'playtime_minutes': g.get('playtimeMinutes')}
    for a, info in out.items():
        info['fetched_at'] = cache['fetched_at']
        info['url'] = detail_url(s['url'], a)
    return out


def owned_library() -> list[dict]:
    """Cache-only (never network): the owned Steam games Game-db knows —
    [{appid, title, playtimeMinutes, lastPlayedAt, ...}] — or [] when Game-db
    isn't set up or hasn't been fetched yet."""
    s = settings()
    if not configured(s):
        return []
    cache = _cached(_fingerprint(s['url'], s['token']))
    lib = (cache or {}).get('data', {}).get('library') or []
    return [g for g in lib if isinstance(g, dict) and isinstance(g.get('appid'), int)]


# ---- helpers --------------------------------------------------------------

def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None


def days_until(s: str | None, now: datetime | None = None) -> int | None:
    d = parse_iso(s)
    if not d:
        return None
    now = now or datetime.now(timezone.utc)
    return -(-int((d - now).total_seconds()) // 86400)        # ceil, like Game-db


def has_day(item: dict) -> bool:
    """Whether the release is a concrete day. Month-only dates ('October
    2026') are stored as the 1st, so a date without a day in its text is
    vague, however precise `releaseDate` looks."""
    text = item.get('releaseDateText') or ''
    if not parse_iso(item.get('releaseDate')):
        return False
    return not text or bool(re.search(r'\b\d{1,2}\b[ ,]', text + ' '))


def release_label(item: dict, now: datetime | None = None) -> str:
    """'in 12 days' for a concrete date, else Steam's own text ('Q2 2027').
    Month-only dates are stored as the 1st, so trust the text when it has no day."""
    text = item.get('releaseDateText') or ''
    days = days_until(item.get('releaseDate'), now)
    if days is None or not has_day(item):
        return text or 'TBA'
    if days < 0:
        return 'out now'
    if days == 0:
        return 'today'
    if days == 1:
        return 'tomorrow'
    return f'in {days} days'


def upcoming(summary: dict | None) -> list[dict]:
    """Unreleased games from a summary, soonest first: `upcoming[]` merged
    with the wishlist's coming-soon items by appid (the wishlist adds the
    price). Built here rather than trusted from `upcoming[]` alone, which
    drops a month-only release ('October 2026') once its stored 1st passes.
    Order: concrete dates, then vague ones (month / quarter), then TBA."""
    merged: dict = {}
    order: list = []
    rows = [g for g in (summary or {}).get('upcoming') or [] if isinstance(g, dict)]
    rows += [g for g in (summary or {}).get('wishlist') or []
             if isinstance(g, dict) and g.get('comingSoon')]
    for g in rows:
        key = g.get('appid') or g.get('title')
        if not key:
            continue
        if key not in merged:
            merged[key] = {}
            order.append(key)
        merged[key].update({k: v for k, v in g.items() if v is not None})

    def rank(g):
        date = g.get('releaseDate') or ''
        if has_day(g):
            return (0, date, '')
        if g.get('releaseDateText') or date:
            return (1, date or '9999', (g.get('title') or '').lower())
        return (2, '', (g.get('title') or '').lower())
    return sorted((merged[k] for k in order), key=rank)


def price_line(item: dict) -> tuple[str, bool]:
    """(text, pre-order) for the cheapest current price, keyshops included:
    '€41.14 · keyshop', '€55.50 · Steam', or 'No price yet'."""
    price = item.get('price')
    if not isinstance(price, dict):
        return "No price yet", False
    best = price.get('best') if isinstance(price.get('best'), dict) else {}
    text = best.get('priceStr') or price.get('currentStr')
    if not text:
        return "No price yet", False
    where = ('keyshop' if best.get('category') == 'keyshop' else best.get('shop')) or ''
    return (f"{text} · {where}" if where else text), bool(price.get('preorder'))


def steamid64(accountid) -> str:
    return str(int(accountid) + STEAMID64_BASE)


def safe_url(u, base: str | None = None) -> str | None:
    """Only https links (or the configured local dev base) are ever opened."""
    u = (u or '').strip() if isinstance(u, str) else ''
    if u.startswith('https://'):
        return u
    if base and base.startswith('http://') and u.startswith(base + '/'):
        return u
    return None
