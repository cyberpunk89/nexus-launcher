"""Game-db integration: wishlist deals, upcoming releases and backlog picks
from the user's Game-db instance (github.com/cyberpunk89/Game-db).

`GET {url}/api/nexus/v1/summary`, authenticated with a personal token
(`Authorization: Bearer gdb_…`, minted on Game-db's /account page). The
response is cached for 45 min under ~/.cache/nexus/gamedb/ (0600 — it's
personal data) and served stale, with the error attached, when Game-db is
unreachable or the token stops working.

v2 adds `meta` (what the server offers, asked once a day) and `games`
(ProtonDB, play length, reviews and release status for your own library and
wishlist), kept in games.json and synced with `?since=` deltas at most every
6 h. `meta(appid)` reads that copy, never the network, so protondb, hltb and
storeinfo can skip their own fetch for games Game-db already knows. An old
server (404) or one without `games` simply leaves the feature off.

v2 writes (play sessions, the other-store library) need a token minted with
that scope: a 403 `{"error": "scope"}` is GameDbError kind `scope`, and
meta's `scopes` tells Settings beforehand. Writes have their own rate limit
on the server, so a 429 on a write holds back writes only, until its
Retry-After.

Every call says which Nexus is asking (`X-Nexus-Client`, `X-Nexus-Device`),
which Game-db shows next to the token on /account.

The token is read from `[gamedb] token` in config.toml or NEXUS_GAMEDB_TOKEN,
never taken on a command line, and never appears in an error message. The
fetch refuses redirects: urllib would otherwise forward the Authorization
header to wherever the server points it."""

import hashlib
import json
import os
import re
import socket
import time
import tomllib
import urllib.parse
from datetime import datetime, timezone

from . import __version__, net, paths, platform

# No built-in server: Game-db is self-hosted, so the address comes from [gamedb] url
# (or NEXUS_GAMEDB_URL). Shown in empty fields only.
EXAMPLE_URL = "https://gamedb.example.com"
ENDPOINT = "/api/nexus/v1/summary"
META_ENDPOINT = "/api/nexus/v2/meta"
GAMES_ENDPOINT = "/api/nexus/v2/games"
MAX_AGE = 45 * 60
META_AGE = 24 * 3600            # v2/meta: asked about once a day
GAMES_AGE = 6 * 3600            # v2/games: a since-delta at most this often
SCHEMA = 1
SCHEMA_V2 = 2
MAX_PAGES = 200                 # 100k games at 500 a page: a cursor loop stops here
LIMIT = 16 << 20
STEAMID64_BASE = 76561197960265728
_LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
TOKEN_RE = re.compile(r'^gdb_[A-Za-z0-9_-]{43}$')


class GameDbError(net.NetError):
    """kind: config | auth | rate | net | schema | missing (404: the server
    has no such endpoint, e.g. an older Game-db without v2) | scope (403:
    the token may not do this write; `need` names the scope) | invalid (400
    on a write: the server refused the body, nothing was written)."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None,
                 need: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.need = need


# The scope each v2 write needs, and what Settings says when the token lacks it.
SCOPE_SESSIONS = 'write:sessions'
SCOPE_LIBRARY = 'write:library'
SCOPE_HINT = {
    SCOPE_SESSIONS: "This token can only read. On Game-db's Account page, make one with "
                    "'Upload play sessions'.",
    SCOPE_LIBRARY: "This token can't send your library. On Game-db's Account page, make one "
                   "with 'Send my other-store library'.",
}


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
        'games': data.get('games', True) is not False,
        'device': device_label(data.get('device')),
        # What goes up (nexus/gamedb_upload.py): sessions are opt-in; the
        # library also needs a token minted with write:library.
        'upload_sessions': data.get('upload_sessions', False) is True,
        'upload_other': data.get('upload_other', False) is True,
        'upload_library': data.get('upload_library', True) is not False,
    }


def device_label(value=None) -> str:
    """What this PC is called on Game-db's /account: [gamedb] device, else
    the hostname. Printable ASCII (an HTTP header), at most 64 characters,
    and never a path — a value with / or \\ falls back to the hostname."""
    def clean(v) -> str:
        v = ''.join(c for c in str(v or '') if ' ' <= c <= '~').strip()[:64].strip()
        return '' if '/' in v or '\\' in v else v
    return clean(value) or clean(socket.gethostname().split('.')[0]) or 'nexus'


def client_label() -> str:
    """`nexus/0.5.0 linux` for X-Nexus-Client."""
    return f"nexus/{__version__} {platform.OS_NAME}"


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


def _headers(token: str, device: str | None = None) -> dict:
    return {
        'User-Agent': net.UA,
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
        'X-Nexus-Client': client_label(),
        'X-Nexus-Device': device_label(device),
    }


def _error_body(e) -> dict:
    """The JSON object in an HTTP error's body, or {} (read capped, never raises)."""
    try:
        got = json.loads(e.read(4096) or b'{}')
    except (OSError, ValueError, AttributeError):
        return {}
    return got if isinstance(got, dict) else {}


def _get(base: str, token: str, endpoint: str, schema: int, query: dict | None = None,
         timeout: float = 20, device: str | None = None) -> dict:
    """One authenticated GET of a Game-db JSON endpoint; GameDbError on
    anything but a JSON object of the expected schema. The token is only
    ever in the Authorization header, never in a message."""
    return _request(base, token, endpoint, schema=schema, query=query, timeout=timeout,
                    device=device)


def _request(base: str, token: str, endpoint: str, method: str = 'GET',
             schema: int | None = None, query: dict | None = None, body=None,
             timeout: float = 20, device: str | None = None) -> dict:
    """One authenticated request; `body` is sent as JSON. Writes answer
    without a `schema` (schema=None), reads must carry the expected one."""
    import urllib.error
    import urllib.request
    url = base + endpoint + ('?' + urllib.parse.urlencode(query) if query else '')
    headers = _headers(token, device)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_no_redirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(LIMIT + 1)
    except urllib.error.HTTPError as e:
        detail = _error_body(e) if e.code in (400, 403) else {}
        e.close()
        if e.code == 403 and detail.get('error') == 'scope':
            need = detail.get('need') if isinstance(detail.get('need'), str) else None
            raise GameDbError('scope', SCOPE_HINT.get(need) or
                              "This token may not do that; make one with more rights on "
                              f"{base}/account", need=need) from None
        if e.code in (401, 403):
            raise GameDbError('auth', "Game-db rejected the token — create a new one on "
                                      f"{base}/account") from None
        if e.code == 400 and method != 'GET':
            why = detail.get('detail') if isinstance(detail.get('detail'), str) else ''
            raise GameDbError('invalid', f"{endpoint}: Game-db refused the upload"
                                         + (f" ({why[:200]})" if why else '')) from None
        if e.code == 429:
            try:
                retry = float(e.headers.get('Retry-After') or 600)
            except ValueError:
                retry = 600.0
            raise GameDbError('rate', f"Game-db is rate-limiting; retry in {int(retry)}s",
                              retry_after=retry) from None
        if e.code == 404:
            raise GameDbError('missing', f"{endpoint}: this Game-db doesn't have it") from None
        raise GameDbError('net', f"{endpoint}: HTTP {e.code}") from None
    except GameDbError:
        raise
    except net.errors() as e:
        raise GameDbError('net', f"{base}: {getattr(e, 'reason', e)}") from None
    if len(raw) > LIMIT:
        raise GameDbError('net', f"{endpoint}: response larger than {LIMIT} bytes")
    try:
        data = json.loads(raw)
    except ValueError:
        raise GameDbError('schema', f"{endpoint}: not JSON") from None
    if not isinstance(data, dict):
        raise GameDbError('schema', f"{endpoint}: not a JSON object")
    if schema is not None and data.get('schema') != schema:
        got = data.get('schema')
        raise GameDbError('schema', f"Game-db API schema {got!r}, this Nexus understands "
                                    f"{schema} — update one of them")
    return data


def _fetch(base: str, token: str, timeout: float = 20, device: str | None = None) -> dict:
    """v1 summary. Unknown fields are ignored; only `schema` is checked."""
    return _get(base, token, ENDPOINT, SCHEMA, timeout=timeout, device=device)


# ---- cache ----------------------------------------------------------------

def _fingerprint(url: str, token: str) -> str:
    return hashlib.sha256(f"{url}\0{token}".encode()).hexdigest()[:16]


def _summary_path():
    return paths.GAMEDB_CACHE / 'summary.json'


def _state_path():
    return paths.GAMEDB_CACHE / 'state.json'


def _meta_path():
    return paths.GAMEDB_CACHE / 'meta.json'


def _games_path():
    return paths.GAMEDB_CACHE / 'games.json'


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
    """{error: {kind, message, at}|None, retry_at: float|None (reads),
    write_retry_at: float|None (writes)}."""
    s = _read_json(_state_path())
    return s if isinstance(s, dict) else {}


def _save_state(**changes) -> None:
    _write_private(_state_path(), {**last_state(), **changes})


def clear_cache() -> None:
    for p in (_summary_path(), _state_path(), _meta_path(), _games_path()):
        p.unlink(missing_ok=True)
    _games_memo.clear()


def _note_rate(e: GameDbError, now: float, write: bool = False) -> None:
    """v1 and v2 reads share one rate-limit budget on the server, so a 429
    from either holds both back (summary() checks the same retry_at). Writes
    have their own budget (write_retry_at)."""
    if e.kind != 'rate' or not e.retry_after:
        return
    _save_state(**{'write_retry_at' if write else 'retry_at': now + e.retry_after})


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
        data = _fetch(s['url'], s['token'], device=s['device'])
    except GameDbError as e:
        err = {'kind': e.kind, 'message': str(e), 'at': now}
        _save_state(error=err, retry_at=now + e.retry_after if e.retry_after else None)
        return stale(err)
    _write_private(_summary_path(), {'fingerprint': fp, 'fetched_at': now, 'data': data})
    _save_state(error=None, retry_at=None)
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


# ---- v2: meta and the games cache -------------------------------------------

def server_meta(refresh: bool = False, s: dict | None = None) -> dict | None:
    """What the server offers (v2/meta), asked at most once a day per
    account. {'off': True} for a server without v2 (404) — remembered for a
    day too, so an old Game-db costs one request a day. None when unknown
    (never asked, or the last try failed and nothing is cached)."""
    s = s or settings()
    if not configured(s):
        return None
    fp = _fingerprint(s['url'], s['token'])
    c = _read_json(_meta_path())
    c = c if isinstance(c, dict) and c.get('fingerprint') == fp else None
    now = time.time()
    if c and not refresh and now - c.get('fetched_at', 0) < META_AGE:
        return c.get('data')
    if (last_state().get('retry_at') or 0) > now:
        return (c or {}).get('data')
    try:
        data = _get(s['url'], s['token'], META_ENDPOINT, SCHEMA_V2, device=s['device'])
    except GameDbError as e:
        _note_rate(e, now)
        if e.kind == 'missing':
            data = {'off': True}
        elif e.kind == 'schema':         # a v3-only server: as good as no v2
            data = {'off': True, 'schema': True}
        else:
            return (c or {}).get('data')
    _write_private(_meta_path(), {'fingerprint': fp, 'fetched_at': now, 'data': data})
    return data


def _offers(m: dict | None, feature: str) -> bool:
    return isinstance(m, dict) and not m.get('off') and feature in (m.get('features') or ())


def _offers_games(m: dict | None) -> bool:
    return _offers(m, 'games')


def _cached_meta(s: dict) -> dict | None:
    """meta.json's answer for this url and token, or None (never asked)."""
    if not configured(s):
        return None
    c = _read_json(_meta_path())
    if not (isinstance(c, dict) and c.get('fingerprint') == _fingerprint(s['url'], s['token'])):
        return None
    return c.get('data') if isinstance(c.get('data'), dict) else None


def offers(feature: str, s: dict | None = None) -> bool | None:
    """Cache-only: whether the server offers a v2 feature ('games',
    'sessions', 'library'); None while unknown. For Settings, which never
    asks the server itself."""
    m = _cached_meta(s or settings())
    return None if m is None else _offers(m, feature)


def games_available(s: dict | None = None) -> bool | None:
    return offers('games', s)


def token_scopes(s: dict | None = None) -> list[str] | None:
    """Cache-only: this token's scopes as v2/meta listed them (a server from
    before scopes means read only); None while unknown."""
    m = _cached_meta(s or settings())
    if m is None or m.get('off'):
        return None
    got = m.get('scopes')
    return [x for x in got if isinstance(x, str)] if isinstance(got, list) else ['read']


def write_status(feature: str, scope: str, s: dict | None = None) -> str:
    """Cache-only, for Settings and --doctor: 'unknown' (not asked yet),
    'unavailable' (the server lacks the feature), 'read_only' (the token
    lacks the scope) or 'ok'."""
    s = s or settings()
    m = _cached_meta(s)
    if m is None:
        return 'unknown'
    if not _offers(m, feature):
        return 'unavailable'
    return 'ok' if scope in (token_scopes(s) or ()) else 'read_only'


def _forget_scope(s: dict, need: str | None) -> None:
    """A 403 scope: drop it from the cached meta, so Settings shows the hint
    at once (a token's scopes never grow; a new token is a new fingerprint)."""
    c = _read_json(_meta_path())
    if not need or not isinstance(c, dict) or not isinstance(c.get('data'), dict) \
            or c.get('fingerprint') != _fingerprint(s['url'], s['token']):
        return
    scopes = [x for x in (token_scopes(s) or ['read']) if x != need]
    _write_private(_meta_path(), {**c, 'data': {**c['data'], 'scopes': scopes}})


def write(endpoint: str, body, method: str = 'POST', s: dict | None = None) -> dict:
    """One v2 write (the answer's JSON object). GameDbError as for reads, plus
    kind `scope` (the token lacks the write's scope; remembered in meta.json)
    and `rate` without a request while an earlier write's Retry-After runs."""
    s = s or settings()
    if not configured(s):
        raise GameDbError('config', "Game-db is not set up")
    now = time.time()
    wait = (last_state().get('write_retry_at') or 0) - now
    if wait > 0:
        raise GameDbError('rate', f"Game-db asked us to slow down; retry in {int(wait)}s",
                          retry_after=wait)
    try:
        return _request(s['url'], s['token'], endpoint, method=method, body=body, timeout=30,
                        device=s['device'])
    except GameDbError as e:
        _note_rate(e, now, write=True)
        if e.kind == 'scope':
            _forget_scope(s, e.need)
        raise


def _games_cache(fp: str):
    c = _read_json(_games_path())
    if isinstance(c, dict) and c.get('fingerprint') == fp and isinstance(c.get('games'), dict):
        return c
    return None


def _fetch_games(s: dict, since: str | None) -> dict:
    """Every page of one v2/games answer: {full, nextSince, removed, games}.
    nextSince and removed come from the first page; the cursor carries the
    rest. Raises on any failure so a half-read delta is never applied."""
    first = _get(s['url'], s['token'], GAMES_ENDPOINT, SCHEMA_V2,
                 {'since': since} if since else None, timeout=30, device=s['device'])
    games = list(first.get('games') or [])
    cursor = first.get('nextCursor')
    for _ in range(MAX_PAGES):
        if not cursor:
            break
        page = _get(s['url'], s['token'], GAMES_ENDPOINT, SCHEMA_V2, {'cursor': cursor},
                    timeout=30, device=s['device'])
        games += page.get('games') or []
        cursor = page.get('nextCursor')
    else:
        raise GameDbError('net', f"{GAMES_ENDPOINT}: more than {MAX_PAGES} pages")
    return {'full': bool(first.get('full')) or not since,
            'nextSince': first.get('nextSince') if isinstance(first.get('nextSince'), str)
            else None,
            # Steam appids in `removed`, "store:storeId" keys in `removedKeys`.
            'removed': [*(first.get('removed') or []), *(first.get('removedKeys') or [])],
            'games': games}


def sync_games(force: bool = False) -> dict:
    """Bring games.json up to date: a full fetch the first time, then
    `?since=` deltas at most every GAMES_AGE (`force` skips that wait, not a
    Retry-After). Never raises. {status, count, changed, removed, error}:
    status is off (Game-db not set up, switched off, or the server lacks
    `games`) | fresh | synced | failed."""
    s = settings()
    out = {'status': 'off', 'count': 0, 'changed': 0, 'removed': 0, 'error': None}
    if not configured(s) or not s['games']:
        return out
    fp = _fingerprint(s['url'], s['token'])
    cache = _games_cache(fp)
    now = time.time()
    out['count'] = len((cache or {}).get('games') or {})
    if cache and not force and now - cache.get('synced_at', 0) < GAMES_AGE:
        return {**out, 'status': 'fresh'}
    retry_at = last_state().get('retry_at') or 0
    if retry_at > now:
        return {**out, 'status': 'failed', 'error': {
            'kind': 'rate', 'message': "Game-db asked us to slow down; retry in "
                                       f"{int(retry_at - now)}s"}}
    m = server_meta(s=s)
    if m is None:
        return {**out, 'status': 'failed', 'error': {
            'kind': 'net', 'message': "couldn't ask Game-db what it offers", 'at': now}}
    if not _offers_games(m):
        return out
    since = (cache or {}).get('since')
    try:
        try:
            got = _fetch_games(s, since)
        except GameDbError as e:
            if e.kind != 'net' or not since or 'HTTP 400' not in str(e):
                raise
            since, got = None, _fetch_games(s, None)    # the server forgot our since: start over
    except GameDbError as e:
        _note_rate(e, now)
        if e.kind == 'missing':          # v2 went away: forget it until meta says otherwise
            server_meta(refresh=True, s=s)
        err = {'kind': e.kind, 'message': str(e), 'at': now}
        if cache:
            _write_private(_games_path(), {**cache, 'error': err})
        return {**out, 'status': 'failed', 'error': err}
    games = {} if got['full'] or not cache else dict(cache['games'])
    for g in got['games']:
        if (key := row_key(g)) is not None:
            games[key] = g
    removed = 0
    for a in got['removed']:
        if isinstance(a, (int, str)):
            removed += games.pop(str(a), None) is not None
    _write_private(_games_path(), {
        'fingerprint': fp, 'synced_at': now, 'since': got['nextSince'], 'games': games,
        'error': None})
    _games_memo.clear()
    return {**out, 'status': 'synced', 'count': len(games), 'changed': len(got['games']),
            'removed': removed}


def row_key(g) -> str | None:
    """games.json's key for a v2/games row: its Steam appid, else
    "<store>:<storeId>" of its first store (a game Nexus sent from Heroic
    that IGDB links to no Steam game). None for a row with neither."""
    if not isinstance(g, dict):
        return None
    if isinstance(g.get('appid'), int) and not isinstance(g.get('appid'), bool):
        return str(g['appid'])
    for st in g.get('stores') or ():
        if isinstance(st, dict) and isinstance(st.get('store'), str) \
                and isinstance(st.get('storeId'), str) and st['store'] and st['storeId']:
            return f"{st['store']}:{st['storeId']}"
    return None


_games_memo: dict = {}


def _games_rows(s: dict) -> tuple[dict, float] | None:
    """games.json's rows and sync time, re-read only when the file changes
    (a library list asks for hundreds of appids in one go)."""
    path = _games_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    fp = _fingerprint(s['url'], s['token'])
    key = (str(path), mtime, fp)
    if _games_memo.get('key') != key:
        c = _games_cache(fp)
        _games_memo.clear()
        _games_memo.update(key=key, value=(c['games'], c.get('synced_at') or 0) if c else None)
        by_store = {}
        for row in (c or {}).get('games', {}).values():
            for st in (row.get('stores') or ()) if isinstance(row, dict) else ():
                if isinstance(st, dict) and st.get('store') and st.get('storeId'):
                    by_store.setdefault(f"{st['store']}:{st['storeId']}", row)
        _games_memo['stores'] = by_store
    return _games_memo['value']


def games_status() -> dict:
    """Cache-only: {count, synced_at, error} of games.json, for --doctor."""
    s = settings()
    c = _games_cache(_fingerprint(s['url'], s['token'])) if configured(s) else None
    if not c:
        return {'count': 0, 'synced_at': None, 'error': None}
    return {'count': len(c['games']), 'synced_at': c.get('synced_at'), 'error': c.get('error')}


def meta(appid) -> dict | None:
    """Cache-only (never network): what Game-db's v2/games said about one of
    your own Steam games, or None (not set up, switched off, the server has
    no v2, not synced yet, or not in your library/wishlist). The row as the
    server sent it plus `synced_at` (epoch) and `url` (its Game-db page).
    Callers check each source's own fetchedAt against their TTL (`fresh()`)."""
    try:
        appid = int(appid)
    except (TypeError, ValueError):
        return None
    s = settings()
    if not configured(s) or not s['games'] or games_available(s) is False:
        return None
    got = _games_rows(s)
    row = got[0].get(str(appid)) if got else None
    if not isinstance(row, dict):
        return None
    return {**row, 'synced_at': got[1], 'url': detail_url(s['url'], appid)}


def store_meta(store: str, store_id: str) -> dict | None:
    """Cache-only, like meta(): Game-db's row for a game of another store
    (store epic | gog | amazon, its id there), found by the row's key or any
    of its `stores`. Rows with a Steam appid are found too: a Heroic game
    IGDB links to Steam. `url` is the Game-db page when the row has an appid."""
    if not store or not store_id:
        return None
    s = settings()
    if not configured(s) or not s['games'] or games_available(s) is False:
        return None
    got = _games_rows(s)
    if not got:
        return None
    key = f"{store}:{store_id}"
    row = got[0].get(key) or _games_memo.get('stores', {}).get(key)
    if not isinstance(row, dict):
        return None
    appid = row.get('appid') if isinstance(row.get('appid'), int) else None
    return {**row, 'synced_at': got[1], 'url': detail_url(s['url'], appid) if appid else None}


def fresh(stamp, max_age: float, now: float | None = None) -> bool:
    """Whether an ISO `fetchedAt` (or an epoch number) is under max_age old."""
    if isinstance(stamp, (int, float)):
        t = float(stamp)
    else:
        d = parse_iso(stamp if isinstance(stamp, str) else None)
        if not d:
            return False
        t = d.timestamp()
    return 0 <= (now or time.time()) - t < max_age


def play_length(row: dict | None) -> dict | None:
    """A v2 `playLength` in hltb.lookup()'s shape — {main, plus, complete,
    source ('hltb'|'igdb'), count, url, name} — or None without hours."""
    pl = (row or {}).get('playLength')
    if not isinstance(pl, dict):
        return None

    def hours(v):
        return round(float(v), 1) if isinstance(v, (int, float)) and v > 0 else None
    out = {'main': hours(pl.get('main')), 'plus': hours(pl.get('mainExtra')),
           'complete': hours(pl.get('completionist')),
           'source': 'igdb' if pl.get('source') == 'igdb' else 'hltb',
           'count': pl.get('count') if isinstance(pl.get('count'), int) else None,
           'url': None, 'name': (row or {}).get('title')}
    return out if out['main'] or out['plus'] or out['complete'] else None


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
        if any(k in g for k in STATUS_FIELDS):      # null values are dropped just above
            merged[key]['statusKnown'] = True

    def rank(g):
        date = g.get('releaseDate') or ''
        if has_day(g):
            return (0, date, '')
        if g.get('releaseDateText') or date:
            return (1, date or '9999', (g.get('title') or '').lower())
        return (2, '', (g.get('title') or '').lower())
    return sorted((merged[k] for k in order), key=rank)


# The v1 summary's additive upcoming fields (IGDB Phase C): an older server
# sends none of them, and then no chip shows.
STATUS_FIELDS = ('status', 'previousDate', 'pcDate')
EARLY_ACCESS = ('early_access', 'alpha', 'beta')
OUT_NOW_DAYS = 14
_MONTH_ABBR = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def is_cancelled(item: dict) -> bool:
    return str(item.get('status') or '').lower() in ('cancelled', 'canceled')


def release_chips(item: dict, now: datetime | None = None) -> list[tuple[str, str]]:
    """(text, role) chips for an upcoming row: Early access, Delayed (the
    date moved later), Cancelled, PC <date> (when the PC date differs) and
    Out now for 14 days after a concrete release day. [] for a row from a
    server that sends none of the status fields (upcoming() marks the rows
    that had them as statusKnown)."""
    if not (item.get('statusKnown') or any(item.get(k) for k in STATUS_FIELDS)):
        return []
    now = now or datetime.now(timezone.utc)
    status = str(item.get('status') or '').lower()
    out = []
    if status in ('cancelled', 'canceled'):
        return [("Cancelled", 'danger')]
    if status in EARLY_ACCESS:
        out.append(("Early access", 'accent'))
    rel, prev = parse_iso(item.get('releaseDate')), parse_iso(item.get('previousDate'))
    if rel and prev and prev.date() < rel.date():
        out.append(("Delayed", 'warning'))
    pc = parse_iso(item.get('pcDate'))
    if pc and (not rel or pc.date() != rel.date()):
        text = f"PC {pc.day} {_MONTH_ABBR[pc.month - 1]}"
        out.append((text + (f" {pc.year}" if pc.year != now.year else ''), 'muted'))
    days = days_until(item.get('releaseDate'), now)
    pc_later = pc is not None and pc > now                  # out elsewhere, not on PC yet
    if days is not None and has_day(item) and -OUT_NOW_DAYS < days <= 0 and not pc_later:
        out.append(("Out now", 'success'))
    return out


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
