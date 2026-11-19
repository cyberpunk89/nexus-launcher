"""The Friends digest (w-160): what the Friends card says right now, worked out
from plain data. Pure: no Qt, no Steam, no disk; read-only.

    friends_digest(answer, games, facts, wishlist, now) -> {
        'state':    'hidden' | 'setup' | 'now' | 'together' | 'picks' | 'quiet',
        'now':      [{appid, title, slug, owned, people, minutes}],
        'together': {'people': [id], 'games': [{slug, title, appid, owners, all, with}]} | None,
        'picks':    [{slug, title, appid, minutes, recent, yours, score, why}],
        'wishlist': [{appid, title, owners}],
        'counts':   {'public', 'private', 'total'},
        'stale':    bool,           # presence is too old to say who plays now
        'updated':  the answer's time}

`answer` is `nexus-cli --friends --json` (nexus/cli/friends.py `answer()`),
`games` the library's game dicts, `facts` {slug: facts record} (modes and
length), `wishlist` [{appid, title}]. The ladder takes the best rung that has
something: Now (a public friend is in a game, presence fresh), Play together
(friends online and a game you own that more than one of you can play),
Friends' picks (your games friends put hours into), else quiet. Lower rungs
are filled too, so a wide card can show Now beside Picks.

Private friends (game details hidden) are counted, never shown. Game-db
friends show without a Steam key; Steam rows then don't (the cache is only
kept, not shown). A Game-db row that is a Steam friend is one person."""

import hashlib
import time
from datetime import date, datetime

from . import smart

PLAYING_STALE = 600            # s: presence older than this isn't "now" (the CLI's rule)
UNSET_ERRORS = ('no-key', 'no-account')
ONLINE = range(1, 7)           # Steam personastate: online, busy, away, snooze, trading, looking
FINISHED_SHARE = 0.2           # a pick you already played past this share of its length is dropped
PICKS = 3
TOGETHER_GAMES = 3
WISHLIST = 3


def _int(v) -> int:
    if isinstance(v, bool):
        return 0
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _ts(v) -> float | None:
    """An ISO time or epoch seconds as epoch."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if v > 0 else None
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except (TypeError, ValueError):
        return None


def _minutes(d) -> dict[str, int]:
    return {str(k): _int(v) for k, v in d.items()} if isinstance(d, dict) else {}


def _appid(g: dict) -> int | None:
    """The Steam app id of a library game (the same rule as the GUI's
    `_game_store_id`, kept here because that module needs Qt)."""
    if g.get('source') == 'steam' and str(g.get('appid', '')).isdigit():
        return int(g['appid'])
    if str(g.get('store_appid') or '').isdigit() and 0 < int(g['store_appid']) < 0x80000000:
        return int(g['store_appid'])
    v = str(g.get('umu_gameid') or '').removeprefix('umu-')
    return int(v) if v.isdigit() and 0 < int(v) < 0x80000000 else None


def _people(answer: dict, steam_unset: bool) -> list[dict]:
    """One dict per person: {id, name, public, steam, owned, recent, playing,
    state}. A Game-db row for a Steam friend merges into the Steam one."""
    steam, gdb = {}, []
    for r in answer.get('friends') or ():
        if not isinstance(r, dict):
            continue
        gamedb = r.get('source') == 'gamedb'
        pid = str(r.get('id') if gamedb else r.get('steamid') or '')
        if not pid or (gamedb and not pid.startswith('gdb:')):
            continue
        ev = r.get('evidence') if isinstance(r.get('evidence'), dict) else {}
        p = {'id': pid, 'name': r.get('name') or pid, 'public': r.get('visible') is not False,
             'steam': not gamedb, 'owned': _minutes(r.get('owned')),
             'recent': _minutes(r.get('recent')),
             'playing': r.get('playing') if isinstance(r.get('playing'), dict) else None,
             'state': _int(ev.get('personastate')),
             'link': str(r.get('steamid') or '') if gamedb else ''}
        if gamedb:
            gdb.append(p)
        elif not steam_unset and pid not in steam:
            steam[pid] = p
    people = list(steam.values())
    for p in gdb:
        mate = steam.get(p['link'])
        if mate is None:
            people.append(p)
            continue
        for app, m in p['owned'].items():
            mate['owned'][app] = max(mate['owned'].get(app, 0), m)
    return people


def _library(games) -> dict[str, dict]:
    """{appid: game} over the library's visible, non-duplicate games."""
    out: dict[str, dict] = {}
    for g in games or ():
        if not isinstance(g, dict) or g.get('hidden') or g.get('dup_of'):
            continue
        appid = _appid(g)
        if appid is not None:
            out.setdefault(str(appid), g)
    return out


def _title(g: dict | None, fallback: str) -> str:
    return (g or {}).get('display_name') or (g or {}).get('title') or fallback


def _hours(m: int) -> str:
    return f"{m // 60} h" if m >= 60 else f"{m} min"


def _now_rung(people, owned, fresh, now) -> list[dict]:
    if not fresh:
        return []
    groups: dict[str, list[tuple[float, dict]]] = {}
    for p in people:
        if not (p['public'] and p['steam'] and p['playing']):
            continue
        appid = p['playing'].get('appid')
        if appid is None or not str(appid).isdigit():
            continue
        since = _ts(p['playing'].get('since'))
        groups.setdefault(str(int(appid)), []).append(
            (now if since is None else min(since, now), p))
    out = []
    for appid, rows in groups.items():
        rows.sort(key=lambda x: (x[0], x[1]['name'].casefold()))
        g = owned.get(appid)
        said = next((p['playing'].get('name') for _t, p in rows if p['playing'].get('name')), None)
        out.append({'appid': int(appid), 'title': _title(g, said or f"App {appid}"),
                    'slug': (g or {}).get('slug'), 'owned': g is not None,
                    'people': [p['id'] for _t, p in rows],
                    'minutes': max(0, int((now - rows[0][0]) // 60))})
    out.sort(key=lambda e: (-len(e['people']), -e['minutes'], e['title'].casefold()))
    return out


def _together(people, owned, facts, fresh) -> dict | None:
    if not fresh:
        return None
    online = sorted((p for p in people if p['public'] and p['steam']
                     and (p['state'] in ONLINE or p['playing'])),
                    key=lambda p: (p['name'].casefold(), p['id']))
    if not online:
        return None
    games = []
    for appid, g in owned.items():
        slug = g.get('slug')
        if not smart.multiplayer((facts or {}).get(slug)):
            continue
        who = [p['id'] for p in online if appid in p['owned']]
        if who:
            everyone = sum(1 for p in people if p['public'] and appid in p['owned'])
            games.append({'slug': slug, 'title': _title(g, f"App {appid}"), 'appid': int(appid),
                          'owners': len(who), 'all': len(who) == len(online), 'with': who,
                          '_n': everyone})
    games.sort(key=lambda e: (-e['owners'], -e['_n'], e['title'].casefold()))
    for e in games:
        del e['_n']
    if not games:
        return None
    return {'people': [p['id'] for p in online], 'games': games[:TOGETHER_GAMES]}


def _daily(day: str, slug) -> float:
    h = hashlib.sha256(f"{day}:{slug}".encode()).digest()
    return int.from_bytes(h[:4], 'big') / 2 ** 32


def _picks(people, owned, facts, day, limit) -> list[dict]:
    names = {p['id']: p['name'] for p in people}
    cands = []
    for appid, g in owned.items():
        mins = {p['id']: p['owned'][appid] for p in people
                if p['public'] and p['owned'].get(appid, 0) > 0}
        if not mins:
            continue
        recent = {p['id']: p['recent'][appid] for p in people
                  if p['id'] in mins and p['recent'].get(appid, 0) > 0}
        yours = _int(g.get('playtime_minutes'))
        main = smart.main_hours((facts or {}).get(g.get('slug')))
        if main and yours / 60 > FINISHED_SHARE * main:
            continue
        top = sorted(mins, key=lambda i: (-mins[i], names[i].casefold()))[:2]
        why = ' · '.join(f"{names[i]} {_hours(mins[i])}" for i in top)
        why += f" · you {_hours(yours)}" if yours else " · you haven't started"
        cands.append({'slug': g.get('slug'), 'title': _title(g, f"App {appid}"),
                      'appid': int(appid), 'minutes': mins, 'recent': recent, 'yours': yours,
                      'score': sum(mins.values()) + sum(recent.values()), 'why': why})
    cands.sort(key=lambda c: (-c['score'], str(c['slug'])))
    if len(cands) <= limit:
        return cands
    # The strongest pick stays; the rest are drawn from the next few by the day.
    pool = sorted(cands[1:2 * limit], key=lambda c: (_daily(day, c['slug']), str(c['slug'])))
    chosen = [cands[0], *pool[:limit - 1]]
    chosen.sort(key=lambda c: (-c['score'], str(c['slug'])))
    return chosen


def _wishlist(people, wishlist) -> list[dict]:
    out = []
    for w in wishlist or ():
        if not isinstance(w, dict) or not str(w.get('appid', '')).isdigit():
            continue
        appid = str(int(w['appid']))
        who = sorted((p for p in people if p['public'] and appid in p['owned']),
                     key=lambda p: (-p['owned'][appid], p['name'].casefold()))
        if who:
            out.append({'appid': int(appid), 'title': w.get('title') or f"App {appid}",
                        'owners': [p['id'] for p in who]})
    out.sort(key=lambda e: (-len(e['owners']), e['title'].casefold()))
    return out[:WISHLIST]


def friends_digest(answer, games, facts=None, wishlist=None, now: float | None = None,
                   day: str | None = None, limit: int = PICKS) -> dict:
    """See the module docstring. `now` is epoch seconds (default: the clock);
    `day` (an ISO date) seeds which picks the card shows, default from `now`."""
    now = time.time() if now is None else now
    day = day or date.fromtimestamp(now).isoformat()
    out = {'state': 'hidden', 'now': [], 'together': None, 'picks': [], 'wishlist': [],
           'counts': {'public': 0, 'private': 0, 'total': 0}, 'stale': True, 'updated': None}
    if not isinstance(answer, dict) or not answer.get('enabled'):
        return out
    out['updated'] = answer.get('updated')
    t = _ts(answer.get('updated'))
    out['stale'] = t is None or now - t >= PLAYING_STALE
    err = answer.get('error') if isinstance(answer.get('error'), dict) else {}
    steam_unset = err.get('kind') in UNSET_ERRORS
    people = _people(answer, steam_unset)
    if steam_unset and not people:
        out['state'] = 'setup'
        return out
    pub = sum(1 for p in people if p['public'])
    out['counts'] = {'public': pub, 'private': len(people) - pub, 'total': len(people)}
    owned = _library(games)
    fresh = not out['stale']
    out['now'] = _now_rung(people, owned, fresh, now)
    out['together'] = _together(people, owned, facts, fresh)
    out['picks'] = _picks(people, owned, facts, day, limit)
    out['wishlist'] = _wishlist(people, wishlist)
    out['state'] = ('now' if out['now'] else 'together' if out['together']
                    else 'picks' if out['picks'] else 'quiet')
    return out
