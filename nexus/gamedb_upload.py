"""What Nexus sends to Game-db, both opt-in and both off unless the token
was minted for it: play sessions (`POST /api/nexus/v2/sessions`, scope
write:sessions) and Heroic's owned Epic, GOG and Amazon games (`POST
/api/nexus/v2/library`, scope write:library).

Sessions come from sessions.jsonl (nexus/history.py). Each gets an id,
sha256(device id | slug | start), so a retried upload adds nothing twice;
an `uploaded_until` byte mark in ~/.cache/nexus/gamedb/sessions-upload.json
(0600) keeps the file from being re-read. Steam games go as `steam` + appid,
Heroic's as `epic`/`gog`/`amazon` + app name; everything else (repacks,
desktop games) only with `[gamedb] upload_other`, as `other` with a cleaned
title: never a path, a store id or a repack name.

Both run in the agent's deals tick (on Windows, the window's --check-alerts
stand-in) and never raise: Game-db off, offline, old (no `sessions` or
`library` in v2/meta) or a read-only token all leave things as they are."""

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone

from . import gamedb, history, paths

SESSIONS_ENDPOINT = '/api/nexus/v2/sessions'
LIBRARY_ENDPOINT = '/api/nexus/v2/library'
BATCH = 200                     # sessions per POST (the server takes 1-200)
LIBRARY_BATCH = 1000            # games per POST (1-1000)
MAX_CALLS = 10                  # per run: the server allows 60 writes per 10 min
LIBRARY_AGE = 24 * 3600         # the library goes up once a day
STORES = ('steam', 'epic', 'gog', 'amazon', 'heroic', 'other')
HEROIC_STORES = {'legendary': 'epic', 'gog': 'gog', 'nile': 'amazon'}
_ID_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')


# ---- device id and session ids -----------------------------------------------

def _device_id_path():
    return paths.DATA_DIR / 'device-id'


def device_id() -> str:
    """A random id for this install, made once and kept with the data (not
    the cache), so session ids stay the same when the cache is cleared."""
    path = _device_id_path()
    try:
        v = path.read_text(encoding='utf-8').strip()
        if re.fullmatch(r'[0-9a-f]{32}', v):
            return v
    except OSError:
        pass
    v = secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gamedb.net.write_atomic(path, v, mode=0o600)
    except OSError:
        pass
    return v


def session_id(dev: str, slug: str, start: str) -> str:
    return hashlib.sha256(f"{dev}|{slug}|{start}".encode()).hexdigest()[:32]


def store_of(slug: str, source: str | None) -> tuple[str, str] | None:
    """(store, storeId) for a store game's slug, or None (a repack or any
    other source): steam-<appid>, heroic-<runner>-<app>, epic-<app>."""
    slug = slug or ''
    if source == 'steam' and (m := re.fullmatch(r'steam-(\d+)', slug)):
        return 'steam', m[1]
    if source == 'heroic':
        for runner, store in HEROIC_STORES.items():
            if slug.startswith(f'heroic-{runner}-') and len(slug) > len(runner) + 8:
                return store, slug[len(runner) + 8:]
    if source == 'epic' and slug.startswith('epic-') and len(slug) > 5:
        return 'epic', slug[5:]
    return None


def clean_title(name) -> str | None:
    """A game title fit to leave the PC: no bracketed or parenthesised parts
    (where repack names keep their version, DLC and group), no version
    numbers or 'repack' words, nothing that looks like a path."""
    t = str(name or '')
    if '/' in t or '\\' in t:
        return None
    t = re.sub(r'\[[^\]]*\]|\([^)]*\)|\{[^}]*\}', ' ', t)
    t = re.sub(r'\b(v|build\s*)\d[\w.+-]*', ' ', t, flags=re.I)
    t = re.sub(r'\b(fitgirl|dodi|elamigos|repack|multi\d*|gog|steamrip)\b', ' ', t, flags=re.I)
    t = re.sub(r'[\s_]+', ' ', t).strip(' -+_.,:')
    t = ''.join(c for c in t if c.isprintable())
    return t[:200] or None


def store_title(name) -> str | None:
    """A store game's own title, as the store names it: printable, no path."""
    t = ''.join(c for c in str(name or '') if c.isprintable()).strip()
    return t[:200] if t and '/' not in t and '\\' not in t else None


def _iso_utc(text: str) -> str | None:
    """sessions.jsonl has local times without a zone: as UTC ISO."""
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat(timespec='seconds')
    except (TypeError, ValueError):
        return None


def session_item(line: dict, dev: str, other: bool) -> dict | None:
    """One sessions.jsonl line as an upload item, or None when it isn't
    sent (not a store game and `other` is off) or isn't valid."""
    slug, start = line.get('slug'), line.get('start')
    if not isinstance(slug, str) or not isinstance(start, str):
        return None
    store = store_of(slug, line.get('source'))
    if store is None and not other:
        return None
    try:
        minutes = int(round(float(line.get('minutes'))))
    except (TypeError, ValueError):
        return None
    begin, end = _iso_utc(start), _iso_utc(line.get('end'))
    title = store_title(line.get('name')) if store else clean_title(line.get('name') or slug)
    if store is None and title is None:
        return None
    if not begin or not end:
        return None
    item = {'id': session_id(dev, slug, start), 'store': store[0] if store else 'other',
            'storeId': store[1] if store else None, 'title': title or store[1],
            'start': begin, 'end': end, 'minutes': max(1, min(1440, minutes))}
    return item if _ID_RE.match(item['id']) and item['store'] in STORES else None


# ---- the sessions queue --------------------------------------------------------

def _state_path():
    return paths.GAMEDB_CACHE / 'sessions-upload.json'


def _library_state_path():
    return paths.GAMEDB_CACHE / 'library-upload.json'


def _read(path) -> dict:
    got = gamedb._read_json(path)
    return got if isinstance(got, dict) else {}


def _pending(path, start: int) -> list[tuple[int, dict]]:
    """(end offset, parsed line) for each complete line after `start`. A
    line still being written (no newline yet) waits for the next run."""
    out = []
    try:
        with open(path, 'rb') as f:
            f.seek(start)
            pos = start
            for raw in f:
                if not raw.endswith(b'\n'):
                    break
                pos += len(raw)
                try:
                    line = json.loads(raw)
                except ValueError:
                    line = None
                out.append((pos, line if isinstance(line, dict) else {}))
    except OSError:
        pass
    return out


def _ready(feature: str, scope: str, s: dict) -> str:
    """'ok', or why not: off | unavailable | read_only | unknown (meta
    couldn't be asked: offline)."""
    st = gamedb.write_status(feature, scope, s)
    if st == 'unknown':
        gamedb.server_meta(s=s)                     # once a day at most; may stay unknown
        st = gamedb.write_status(feature, scope, s)
    return st


def upload_sessions(s: dict | None = None, path=None) -> dict:
    """Send the sessions recorded since the last upload. Never raises.
    {status, sent, accepted, duplicate, error}: status off (switched off or
    Game-db not set up) | unavailable (the server lacks sessions) |
    read_only (the token lacks write:sessions) | unknown (couldn't ask the
    server what it offers) | idle (nothing new) | uploaded | failed."""
    s = s or gamedb.settings()
    out = {'status': 'off', 'sent': 0, 'accepted': 0, 'duplicate': 0, 'error': None}
    if not gamedb.configured(s) or not s.get('upload_sessions'):
        return out
    ready = _ready('sessions', gamedb.SCOPE_SESSIONS, s)
    if ready != 'ok':
        return {**out, 'status': ready,
                'error': {'kind': 'scope', 'message': gamedb.SCOPE_HINT[gamedb.SCOPE_SESSIONS]}
                if ready == 'read_only' else None}
    path = path or history.PATH
    fp = gamedb._fingerprint(s['url'], s['token'])
    st = _read(_state_path())
    mark = st.get('uploaded_until') if st.get('fingerprint') == fp else 0
    mark = mark if isinstance(mark, int) and mark >= 0 else 0
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size < mark:                                 # replaced or cut: ids keep a resend safe
        mark = 0
    lines = _pending(path, mark)
    dev = device_id()
    batch, calls, status, error = [], 0, 'idle', None
    totals = {'accepted': st.get('accepted', 0) if st.get('fingerprint') == fp else 0,
              'duplicate': st.get('duplicate', 0) if st.get('fingerprint') == fp else 0}

    def save():
        gamedb._write_private(_state_path(), {
            'fingerprint': fp, 'uploaded_until': mark, 'uploaded_at': time.time(),
            'error': error, **totals})

    for i, (end, line) in enumerate(lines):
        item = session_item(line, dev, bool(s.get('upload_other')))
        if item:
            batch.append(item)
        last = i == len(lines) - 1
        if not batch:
            mark = end                              # nothing to send up to here
            continue
        if len(batch) < BATCH and not last:
            continue
        if calls >= MAX_CALLS:
            break
        calls += 1
        try:
            got = gamedb.write(SESSIONS_ENDPOINT, {'sessions': batch}, s=s)
        except gamedb.GameDbError as e:
            error = {'kind': e.kind, 'message': str(e), 'at': time.time()}
            if e.kind != 'invalid':
                status = 'failed'
                break
            # The server refused this batch as a whole and would refuse it
            # again: skip it rather than block every later session.
            status = 'failed'
            mark = end
            batch = []
            continue
        out['sent'] += len(batch)
        for k in ('accepted', 'duplicate'):
            n = got.get(k) if isinstance(got.get(k), int) else 0
            out[k] += n
            totals[k] += n
        mark = end
        batch = []
        if status != 'failed':
            status = 'uploaded'
    if lines or error or st.get('fingerprint') != fp:
        save()
    return {**out, 'status': status, 'error': error}


def delete_sessions(s: dict | None = None) -> dict:
    """DELETE every Nexus play session of this account on Game-db:
    {deleted}. Raises GameDbError (config, scope, auth, rate, net, missing
    on a server without sessions). The upload mark stays where it is, so
    only sessions played from now on go up (if the switch stays on)."""
    s = s or gamedb.settings()
    got = gamedb.write(SESSIONS_ENDPOINT, None, method='DELETE', s=s)
    st = _read(_state_path())
    if st:
        gamedb._write_private(_state_path(), {**st, 'accepted': 0, 'duplicate': 0})
    return {'deleted': got.get('deleted') if isinstance(got.get('deleted'), int) else 0}


def sessions_status(s: dict | None = None) -> dict:
    """Cache-only, for --doctor: {on, ready, uploaded_at, accepted, error}."""
    s = s or gamedb.settings()
    st = _read(_state_path())
    if gamedb.configured(s) and st.get('fingerprint') != gamedb._fingerprint(s['url'], s['token']):
        st = {}
    return {'on': bool(s.get('upload_sessions')),
            'ready': gamedb.write_status('sessions', gamedb.SCOPE_SESSIONS, s)
            if gamedb.configured(s) else 'unknown',
            'uploaded_at': st.get('uploaded_at'), 'accepted': st.get('accepted', 0),
            'error': st.get('error')}


def upload_all() -> dict:
    """Everything that goes up on a deals tick: {sessions, library}."""
    s = gamedb.settings()
    return {'sessions': upload_sessions(s), 'library': upload_library(s)}


def heroic_library() -> list[dict]:
    """[{store, storeId, title}] for every Epic, GOG and Amazon game Heroic's
    store caches say you own (installed or not), one per game. [] without
    Heroic. Cache files only, never the network."""
    from . import steamlib
    out, seen = [], set()
    try:
        games = steamlib.heroic_store_games(steamlib.find_heroic_config(), owned=True)
    except Exception:                                       # noqa: BLE001 — optional
        return []
    for g in games:
        store = HEROIC_STORES.get(g.get('runner'))
        app = g.get('app_name')
        if not store or not isinstance(app, str) or not app or (store, app) in seen:
            continue
        seen.add((store, app))
        out.append({'store': store, 'storeId': app,
                    'title': store_title(g.get('display_name')) or app})
    return out


def upload_library(s: dict | None = None, games=None, force: bool = False) -> dict:
    """Send Heroic's owned Epic, GOG and Amazon games to Game-db, at most once
    a day (`force`: now). The last call of the upload says complete, so the
    server drops games no longer owned. Never raises. {status, sent,
    resolved, pending, error}: status off | unavailable | read_only |
    unknown | fresh (sent within a day) | idle (no Heroic games) | uploaded
    | failed."""
    s = s or gamedb.settings()
    out = {'status': 'off', 'sent': 0, 'resolved': 0, 'pending': 0, 'error': None}
    if not gamedb.configured(s) or not s.get('upload_library'):
        return out
    fp = gamedb._fingerprint(s['url'], s['token'])
    st = _read(_library_state_path())
    st = st if st.get('fingerprint') == fp else {}
    now = time.time()
    if not force and now - (st.get('uploaded_at') or 0) < LIBRARY_AGE:
        return {**out, 'status': 'fresh'}
    ready = _ready('library', gamedb.SCOPE_LIBRARY, s)
    if ready != 'ok':
        return {**out, 'status': ready}
    games = heroic_library() if games is None else games
    if not games:
        return {**out, 'status': 'idle'}            # never an empty "complete": it would wipe
    chunks = [games[i:i + LIBRARY_BATCH] for i in range(0, len(games), LIBRARY_BATCH)]
    error = None
    for i, chunk in enumerate(chunks[:MAX_CALLS]):
        try:
            got = gamedb.write(LIBRARY_ENDPOINT, {'games': chunk,
                                                  'complete': i == len(chunks) - 1}, s=s)
        except gamedb.GameDbError as e:
            error = {'kind': e.kind, 'message': str(e), 'at': now}
            break
        out['sent'] += len(chunk)
        for k in ('resolved', 'pending'):
            out[k] += got.get(k) if isinstance(got.get(k), int) else 0
    if error is None and len(chunks) > MAX_CALLS:
        error = {'kind': 'net', 'message': f"more than {MAX_CALLS * LIBRARY_BATCH} games", 'at': now}
    gamedb._write_private(_library_state_path(), {
        'fingerprint': fp, 'uploaded_at': now if error is None else st.get('uploaded_at'),
        'tried_at': now, 'count': out['sent'] if error is None else st.get('count', 0),
        'error': error})
    return {**out, 'status': 'failed' if error else 'uploaded', 'error': error}


def library_status(s: dict | None = None) -> dict:
    """Cache-only, for --doctor: {on, ready, uploaded_at, count, error}."""
    s = s or gamedb.settings()
    st = _read(_library_state_path())
    if gamedb.configured(s) and st.get('fingerprint') != gamedb._fingerprint(s['url'], s['token']):
        st = {}
    return {'on': bool(s.get('upload_library')),
            'ready': gamedb.write_status('library', gamedb.SCOPE_LIBRARY, s)
            if gamedb.configured(s) else 'unknown',
            'uploaded_at': st.get('uploaded_at'), 'count': st.get('count', 0),
            'error': st.get('error')}
