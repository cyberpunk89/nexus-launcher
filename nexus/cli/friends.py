"""Steam friends: who they are, what they play right now and what they own,
from the Steam Web API with the user's own key ([steam] web_api_key or
STEAM_WEB_API_KEY). Read-only: nothing about the user is sent anywhere
except the key and their SteamID64, to Steam itself.

    nexus-cli --friends [--refresh] [--presence] [--json]

The cache, CACHE_DIR/friends.json (0600: it is personal), is what every
answer comes from. Steam is asked only for the parts that are stale:

    friends list         ISteamUser/GetFriendList         every 6 h
    names, presence      ISteamUser/GetPlayerSummaries    every 120 s, 100 ids a call
    owned games          IPlayerService/GetOwnedGames     every 24 h per public friend,
                                                          one friend every 2 s

A refresh is bounded in time; the friends it didn't reach keep their old
owned games and are first in line next time. A failed call never replaces
good data: a 429, a refused key or no network leave the last good copy in
place, and are reported in `error`.

Private friends are kept and flagged (`visible: false`), never dropped.

Each record keeps the raw evidence for three Steam facts that decide the
wording of the feature (`evidence`, and `meta.evidence` for the list
itself): whether GetFriendList answers when your own list is private, whether
"friends only" game details answer GetOwnedGames with nothing, and whether
GetPlayerSummaries shows the current game of a public friend.

stdlib only, and the same on Windows: the Web API is not a Linux feature."""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from nexus import net
from nexus import paths as NX

API_BASE = 'https://api.steampowered.com'
BASE_ENV = 'NEXUS_STEAM_API_BASE'          # tests point this at a fake local server
KEY_ENV = 'STEAM_WEB_API_KEY'
KEY_URL = 'https://steamcommunity.com/dev/apikey'
PRIVACY_URL = 'https://steamcommunity.com/my/edit/settings'

CACHE_V = 1
LIST_TTL = 6 * 3600
PLAYING_TTL = 120
PRESENCE_TTL = 60              # --presence: summaries only, at most once a minute
PLAYING_STALE = 600            # presence older than this isn't shown as "playing now"
OWNED_TTL = 24 * 3600
OWNED_GAP = 2.0                # seconds between two friends' owned games
BATCH = 100                    # GetPlayerSummaries takes up to 100 ids
BUDGET = 15.0                  # seconds a plain --friends may spend on owned games
REFRESH_BUDGET = 60.0          # ... and --friends --refresh
TIMEOUT = 15
BACKOFF = 600                  # after a 429 without Retry-After
LIMIT = 8 << 20

STEAMID_RE = re.compile(r'^7656119\d{10}$')
KEY_RE = re.compile(r'^[0-9A-Fa-f]{32}$')
_LOOPBACK = {'localhost', '127.0.0.1', '::1'}
_STEAMID_BASE = 76561197960265728          # SteamID64 of accountid 0


class FriendsError(Exception):
    """kind: no-key | no-account | bad-key | rate-limited | offline | private-list"""

    def __init__(self, kind: str, message: str, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.kind, self.status, self.retry_after = kind, status, retry_after


# ── settings ──────────────────────────────────────────────────────────────

def settings() -> dict:
    """{'key', 'enabled', 'steamid' (the [friends] override or None)}: config
    [steam] web_api_key, which STEAM_WEB_API_KEY beats, and [friends]."""
    from .config import load_config
    c = load_config()
    return {'key': c.get('steam_web_api_key'), 'enabled': bool(c.get('friends_enabled')),
            'steamid': c.get('friends_steamid')}


def mask(text: str, key: str | None) -> str:
    """`text` with the key (and any key= query value) blanked."""
    text = str(text)
    if key and len(key) >= 6:
        text = text.replace(key, '***')
    return re.sub(r'(?i)(key=)[^&\s"\']+', r'\1***', text)


def api_base() -> str:
    """https://api.steampowered.com, or NEXUS_STEAM_API_BASE: https, or http
    on this machine only (a test server)."""
    base = (os.environ.get(BASE_ENV) or API_BASE).rstrip('/')
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(base)
    except ValueError:
        return API_BASE
    if parts.scheme == 'https' or (parts.scheme == 'http'
                                   and (parts.hostname or '') in _LOOPBACK):
        return base
    return API_BASE


def whose(cfg: dict | None = None) -> dict:
    """Whose friends: {'steamid', 'persona', 'source'} — the [friends]
    steamid override, else the pinned / last signed-in Steam account (the
    same choice shortcuts use; never a directory-name sort). steamid None
    when there is neither."""
    cfg = cfg or settings()
    if cfg.get('steamid'):
        if STEAMID_RE.match(cfg['steamid']):
            return {'steamid': cfg['steamid'], 'persona': None, 'source': 'config'}
        return {'steamid': None, 'persona': None, 'source': 'config',
                'problem': "[friends] steamid isn't a SteamID64 (17 digits, 7656119…)"}
    from .steam import resolve_steam_account
    try:
        acct, _why = resolve_steam_account()
    except Exception:        # noqa: BLE001 — a Steam folder Nexus can't read
        acct = None
    if not acct:
        return {'steamid': None, 'persona': None, 'source': 'steam'}
    sid = acct.get('steamid64')
    if not (sid and STEAMID_RE.match(str(sid))):
        sid = str(_STEAMID_BASE + int(acct['accountid']))
    return {'steamid': str(sid), 'persona': acct.get('persona') or None, 'source': 'steam'}


# ── the cache ─────────────────────────────────────────────────────────────

def cache_path():
    return NX.CACHE_DIR / 'friends.json'


def _empty(me: str | None) -> dict:
    return {'v': CACHE_V, 'meta': {'me': me, 'list_at': 0, 'summary_at': 0,
                                   'backoff_until': 0, 'last_error': None,
                                   'evidence': {}},
            'friends': {}}


def load_cache(me: str | None = None) -> dict:
    """The cache, or an empty one; a cache for another account is dropped."""
    try:
        data = json.loads(cache_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = None
    if (not isinstance(data, dict) or data.get('v') != CACHE_V
            or not isinstance(data.get('friends'), dict)
            or not isinstance(data.get('meta'), dict)):
        return _empty(me)
    if me and data['meta'].get('me') != me:
        return _empty(me)
    base = _empty(me or data['meta'].get('me'))
    base['meta'].update(data['meta'])
    base['friends'] = data['friends']
    return base


def save_cache(state: dict) -> None:
    try:
        net.write_atomic(cache_path(), json.dumps(state, separators=(',', ':')), mode=0o600)
    except OSError as e:
        print(f"  [!] Couldn't save {cache_path()}: {e}", file=sys.stderr)


# ── Steam ─────────────────────────────────────────────────────────────────

def _get(path: str, params: dict, key: str) -> tuple[int, object]:
    """GET base/path/?params&key=… -> (200, parsed JSON). Raises FriendsError
    for anything else; the key never appears in its message."""
    import urllib.error
    from urllib.parse import urlencode
    base = api_base()
    url = f"{base}/{path}/?{urlencode({'key': key, **params, 'format': 'json'})}"
    try:
        with net._open(net._request(url), TIMEOUT) as r:
            status, body = r.status, r.read(LIMIT + 1)
    except urllib.error.HTTPError as e:
        status = e.code
        retry = None
        try:
            retry = float(e.headers.get('Retry-After') or '')
        except (TypeError, ValueError):
            pass
        e.close()
        if status == 429:
            raise FriendsError('rate-limited', "Steam says too many requests; Nexus waits "
                               "before asking again.", status, retry) from None
        if status in (401, 403):
            raise FriendsError('bad-key', "Steam refused the Web API key: check it, or "
                               "make a new one.", status) from None
        raise FriendsError('offline', f"Steam answered {status}.", status) from None
    except net.errors() as e:
        reason = mask(getattr(e, 'reason', e), key)
        raise FriendsError('offline', f"Couldn't reach Steam ({reason}).") from None
    if len(body) > LIMIT:
        raise FriendsError('offline', "Steam's answer was too large.", status)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        raise FriendsError('offline', "Steam sent an answer Nexus couldn't read.",
                           status) from None


def friend_list(key: str, me: str) -> list[dict]:
    """[{steamid, friend_since}] — [] only for a real empty list. Steam
    answers 401 when the list is private; a key check (GetPlayerSummaries)
    tells that from a bad key."""
    try:
        _st, data = _get('ISteamUser/GetFriendList/v1', {'steamid': me,
                                                        'relationship': 'friend'}, key)
    except FriendsError as e:
        if e.kind == 'bad-key' and e.status == 401:
            try:
                summaries(key, [me])
            except FriendsError as e2:
                raise e2 from None             # bad key, or Steam unreachable
            raise FriendsError('private-list', "Steam won't list this account's friends. "
                               "Make your friends list public on Steam (Edit Profile › "
                               "Privacy Settings › Friends List).", 401) from None
        raise
    if not isinstance(data, dict) or not isinstance(data.get('friendslist'), dict):
        raise FriendsError('offline', "Steam sent an answer Nexus couldn't read.", 200)
    rows = data['friendslist'].get('friends') or []
    out = []
    for f in rows if isinstance(rows, list) else []:
        sid = str((f or {}).get('steamid') or '')
        if STEAMID_RE.match(sid):
            out.append({'steamid': sid, 'friend_since': f.get('friend_since')})
    return out


def summaries(key: str, ids: list[str]) -> dict[str, dict]:
    """{steamid: player} for up to BATCH ids per call."""
    out = {}
    for i in range(0, len(ids), BATCH):
        _st, data = _get('ISteamUser/GetPlayerSummaries/v2',
                         {'steamids': ','.join(ids[i:i + BATCH])}, key)
        players = ((data or {}).get('response') or {}).get('players') \
            if isinstance(data, dict) else None
        if not isinstance(players, list):
            raise FriendsError('offline', "Steam sent an answer Nexus couldn't read.", 200)
        for p in players:
            if isinstance(p, dict) and p.get('steamid'):
                out[str(p['steamid'])] = p
    return out


def owned_games(key: str, steamid: str) -> tuple[str, dict[str, int], int | None]:
    """('games' | 'empty', {appid: minutes}, game_count). 'empty' is Steam's
    `{"response": {}}`: game details not public to this key."""
    _st, data = _get('IPlayerService/GetOwnedGames/v1',
                     {'steamid': steamid, 'include_played_free_games': 1}, key)
    resp = data.get('response') if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        raise FriendsError('offline', "Steam sent an answer Nexus couldn't read.", 200)
    if 'game_count' not in resp and not resp.get('games'):
        return 'empty', {}, None
    games = {}
    for g in resp.get('games') or []:
        try:
            games[str(int(g['appid']))] = int(g.get('playtime_forever') or 0)
        except (KeyError, TypeError, ValueError):
            continue
    count = resp.get('game_count')
    return 'games', games, count if isinstance(count, int) else len(games)


# ── refresh ───────────────────────────────────────────────────────────────

def _record(sid: str) -> dict:
    return {'steamid': sid, 'name': None, 'avatar': None, 'visible': None,
            'private_reason': None, 'playing': None, 'owned': {}, 'owned_at': 0,
            'fetched_at': {'summary': 0, 'owned': 0}, 'evidence': {}}


def _apply_summary(rec: dict, p: dict, now: float) -> None:
    rec['name'] = p.get('personaname') or rec.get('name')
    rec['avatar'] = p.get('avatarmedium') or p.get('avatar') or rec.get('avatar')
    cvs = p.get('communityvisibilitystate')
    gid, gname = p.get('gameid'), p.get('gameextrainfo')
    ev = rec.setdefault('evidence', {})
    ev.update({'communityvisibilitystate': cvs, 'profilestate': p.get('profilestate'),
               'personastate': p.get('personastate'), 'gameid_present': gid is not None,
               'gameextrainfo_present': gname is not None})
    if gid is not None:
        try:
            n = int(gid)
        except (TypeError, ValueError):
            n = None
        old = rec.get('playing') or {}
        same = old.get('gameid') == str(gid)
        rec['playing'] = {'appid': n if n is not None and n < 1 << 32 else None,
                          'gameid': str(gid), 'name': gname,
                          'since': old.get('since') if same else int(now)}
    else:
        rec['playing'] = None
    if cvs != 3:
        rec['visible'], rec['private_reason'] = False, 'profile'
    elif ev.get('owned_answer') == 'empty':
        rec['visible'], rec['private_reason'] = False, 'game-details'
    else:
        rec['visible'], rec['private_reason'] = True, None
    rec.setdefault('fetched_at', {})['summary'] = int(now)


def _owned_due(state: dict, force: bool, now: float) -> list[str]:
    """Friends whose owned games are due, oldest first; profile-private ones
    are skipped (Steam would answer nothing)."""
    due = [r for r in state['friends'].values()
           if r.get('private_reason') != 'profile'
           and (force or now - (r.get('owned_at') or 0) >= OWNED_TTL)]
    due.sort(key=lambda r: r.get('owned_at') or 0)
    return [r['steamid'] for r in due]


def refresh(state: dict, key: str, me: str, force: bool = False,
            budget: float | None = None, now=time.time, sleep=time.sleep,
            presence: bool = False) -> FriendsError | None:
    """Bring the stale parts of `state` up to date in place, within `budget`
    seconds for owned games. Returns the error that stopped it, or None.
    Never removes good data because of an error. `presence` asks only for
    the summaries (who plays what), once they are PRESENCE_TTL old, and
    leaves the friends list and owned games alone."""
    meta = state['meta']
    t0 = now()
    budget = (REFRESH_BUDGET if force else BUDGET) if budget is None else budget
    if (meta.get('backoff_until') or 0) > t0:
        return FriendsError('rate-limited', "Steam said too many requests; Nexus waits "
                            "before asking again.", 429)
    try:
        if not presence and (force or t0 - (meta.get('list_at') or 0) >= LIST_TTL):
            try:
                rows = friend_list(key, me)
            except FriendsError as e:
                meta['evidence'].update({'friendlist_status': e.status,
                                         'friendlist_answer': e.kind})
                raise
            meta['evidence'].update({'friendlist_status': 200,
                                     'friendlist_answer': 'list' if rows else 'empty'})
            keep = {}
            for row in rows:
                rec = state['friends'].get(row['steamid']) or _record(row['steamid'])
                rec['friend_since'] = row.get('friend_since')
                keep[row['steamid']] = rec
            state['friends'] = keep
            meta['list_at'] = int(t0)
        ttl = PRESENCE_TTL if presence else PLAYING_TTL
        if force or t0 - (meta.get('summary_at') or 0) >= ttl:
            ids = [me, *state['friends']]
            players = summaries(key, ids)
            mine = players.get(me) or {}
            meta['evidence']['my_communityvisibilitystate'] = mine.get('communityvisibilitystate')
            for sid, rec in state['friends'].items():
                if sid in players:
                    _apply_summary(rec, players[sid], t0)
            meta['summary_at'] = int(t0)
        first, key_ok = True, False
        for sid in [] if presence else _owned_due(state, force, t0):
            if not first:
                if now() - t0 + OWNED_GAP > budget:
                    break
                sleep(OWNED_GAP)
            first = False
            rec = state['friends'][sid]
            ev = rec.setdefault('evidence', {})
            try:
                answer, games, count = owned_games(key, sid)
            except FriendsError as e:
                if e.kind == 'rate-limited':
                    raise
                ev['owned_answer'] = f"error-{e.status or 'network'}"
                if e.status is None:
                    raise                      # the network went away: stop here
                if e.kind == 'bad-key':
                    # A refused key, or Steam refusing this one friend's
                    # games? A key check tells (once a run).
                    if not key_ok:
                        summaries(key, [me])
                        key_ok = True
                    rec['visible'], rec['private_reason'] = False, 'game-details'
                continue
            ev.update({'owned_answer': answer, 'game_count': count})
            t = int(now())
            rec['owned'] = games
            rec['owned_at'] = t
            rec.setdefault('fetched_at', {})['owned'] = t
            if answer == 'empty' and rec.get('private_reason') is None:
                rec['visible'], rec['private_reason'] = False, 'game-details'
            elif answer == 'games' and rec.get('private_reason') == 'game-details':
                rec['visible'], rec['private_reason'] = True, None
    except FriendsError as e:
        if e.kind == 'rate-limited':
            meta['backoff_until'] = int(now() + (e.retry_after or BACKOFF))
        meta['last_error'] = {'kind': e.kind, 'message': e.args[0], 'at': int(now())}
        return e
    meta['last_error'] = None
    return None


# ── the answer ────────────────────────────────────────────────────────────

def _iso(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='seconds')


def by_app(friends: dict) -> dict[str, list[str]]:
    """{appid: [steamid, …]} over friends whose game details are public,
    most minutes played first."""
    idx: dict[str, list[tuple[int, str]]] = {}
    for sid, rec in friends.items():
        if rec.get('visible') is False:
            continue
        for appid, minutes in (rec.get('owned') or {}).items():
            idx.setdefault(str(appid), []).append((int(minutes or 0), sid))
    return {a: [s for _m, s in sorted(v, key=lambda x: (-x[0], x[1]))]
            for a, v in sorted(idx.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)}


def answer(state: dict, cfg: dict, who: dict, error: dict | None,
           now: float | None = None) -> dict:
    now = time.time() if now is None else now
    fresh = now - (state['meta'].get('summary_at') or 0) < PLAYING_STALE
    rows = []
    for rec in state['friends'].values():
        r = {k: rec.get(k) for k in ('steamid', 'name', 'avatar', 'visible',
                                     'private_reason', 'owned', 'evidence')}
        r['owned'] = r['owned'] or {}
        r['playing'] = rec.get('playing') if fresh else None
        r['owned_at'] = _iso(rec.get('owned_at'))
        fa = rec.get('fetched_at') or {}
        r['fetched_at'] = {'summary': _iso(fa.get('summary')), 'owned': _iso(fa.get('owned'))}
        rows.append(r)
    rows.sort(key=lambda r: (r['playing'] is None, r['visible'] is False,
                             (r['name'] or r['steamid']).casefold()))
    public = sum(1 for r in rows if r['visible'] is not False)
    meta = state['meta']
    return {
        'enabled': bool(cfg.get('enabled')),
        'me': who.get('steamid'),
        'account': {'persona': who.get('persona'), 'source': who.get('source')},
        'friends': rows,
        'by_app': by_app(state['friends']),
        'counts': {'total': len(rows), 'public': public, 'private': len(rows) - public,
                   'playing': sum(1 for r in rows if r['playing'])},
        'updated': _iso(meta.get('summary_at') or meta.get('list_at')),
        'evidence': meta.get('evidence') or {},
        'error': error,
    }


def friends(force: bool = False, budget: float | None = None,
            presence: bool = False) -> dict:
    """The --friends answer: the cache, with its stale parts refreshed when
    the feature is on (or `force`), a key is set and Steam answers.
    `presence` refreshes only who plays what (see refresh)."""
    cfg = settings()
    who = whose(cfg)
    me = who.get('steamid')
    state = load_cache(me)
    error = None
    if not cfg['key']:
        error = {'kind': 'no-key', 'message': "Add a Steam Web API key in Settings › "
                 "Accounts and keys."}
    elif not me:
        error = {'kind': 'no-account', 'message': who.get('problem') or
                 "No Steam account found on this PC; sign in to Steam once, or set "
                 "a SteamID64 in Settings."}
    elif cfg['enabled'] or force:
        err = refresh(state, cfg['key'], me, force=force, budget=budget,
                      presence=presence)
        state['meta']['me'] = me
        save_cache(state)
        if err:
            error = {'kind': err.kind, 'message': mask(err.args[0], cfg['key'])}
    else:
        last = state['meta'].get('last_error')
        if last:
            error = {'kind': last['kind'], 'message': mask(last['message'], cfg['key'])}
    return answer(state, cfg, who, error)


def cmd_friends(refresh_now: bool, as_json: bool, presence: bool = False) -> None:
    data = friends(force=refresh_now, presence=presence)
    key = settings()['key']
    if as_json:
        text = json.dumps(data)
        print(mask(text, key) if key else text)
        return
    c, err = data['counts'], data['error']
    who = data['account'].get('persona') or data['me'] or 'no account'
    print(f"Steam friends of {who}: {c['total']} · {c['public']} public · "
          f"{c['private']} private · {c['playing']} playing now"
          + (f" (updated {data['updated']})" if data['updated'] else ''))
    for f in data['friends']:
        state = (f"playing {f['playing'].get('name') or f['playing'].get('gameid')}"
                 if f['playing'] else
                 f"private ({f['private_reason']})" if f['visible'] is False else
                 f"{len(f['owned'])} games")
        print(f"  {f['name'] or f['steamid']}: {state}")
    if not data['enabled']:
        print("  (Show friends is off in Settings; only --refresh asks Steam.)")
    if err:
        print(mask(f"  [!] {err['message']}", key), file=sys.stderr)
        if not data['friends']:
            sys.exit(1)
