"""The facts index: what Nexus knows about each library game, merged from
local caches into one file (~/.cache/nexus/facts.json) that the Library
and the game page read without waiting on the network.

Sources, best first per fact: the Steam store (storeinfo, by store appid,
so repacks with a umu id are covered), HowLongToBeat (hltb), and Game-db's
`v2/games` row. Heroic's Epic, GOG and Amazon games are found by their
store id in Game-db's rows (Nexus sends them that library), so they get a
record too once Game-db has resolved them; a game with neither a Steam
appid nor such a row has no entry.

build() is cache-only. fill() spends a small hourly budget per site
(Steam appdetails, HowLongToBeat) on what is missing or stale, and stops
for an hour after a rate limit or a run of failures; the agent and, on
Windows, the window call it through `nexus-cli --game-facts --fill N`."""

import contextlib
import json
import re
import time

from . import hltb, net, platform, storeinfo
from .paths import CACHE_DIR

INDEX_V = 1

# ── genres and modes ─────────────────────────────────────────────────────

NEXUS_GENRES = ('Action', 'Adventure', 'RPG', 'Strategy', 'Shooter', 'Simulation', 'Racing',
                'Sports', 'Puzzle', 'Platformer', 'Horror', 'Indie')

# Steam store and IGDB genre names (lowercase) -> a Nexus genre. Steam's
# own genre list is short (no Shooter, Puzzle, Platformer, Horror: those are
# user tags), so those come from IGDB. Casual, Free to Play, Massively
# Multiplayer (a mode), Early Access (a status), Music, Pinball and the
# software "genres" map to nothing and stay in genresRaw.
GENRE_MAP = {
    'action': 'Action', 'fighting': 'Action', 'arcade': 'Action',
    "hack and slash/beat 'em up": 'Action',
    'adventure': 'Adventure', 'point-and-click': 'Adventure', 'visual novel': 'Adventure',
    'rpg': 'RPG', 'role-playing': 'RPG', 'role-playing (rpg)': 'RPG',
    'strategy': 'Strategy', 'real time strategy (rts)': 'Strategy',
    'turn-based strategy (tbs)': 'Strategy', 'tactical': 'Strategy', 'moba': 'Strategy',
    'card & board game': 'Strategy',
    'shooter': 'Shooter',
    'simulation': 'Simulation', 'simulator': 'Simulation',
    'racing': 'Racing',
    'sports': 'Sports', 'sport': 'Sports',
    'puzzle': 'Puzzle', 'quiz/trivia': 'Puzzle',
    'platform': 'Platformer', 'platformer': 'Platformer',
    'horror': 'Horror', 'survival horror': 'Horror',
    'indie': 'Indie',
}

# Steam store category ids -> modes. 1 Multi-player, 47 LAN PvP and 49 PvP
# say "multiplayer" without saying how: online, unless a category above
# already gave a mode.
CATEGORY_MODES = {
    2: ('single',),
    9: ('coop',), 38: ('coop', 'online'), 48: ('coop',), 39: ('coop', 'local'),
    36: ('online',), 20: ('online',), 27: ('online',),
    24: ('local',), 37: ('local',),
}
_VAGUE_MULTI = {1, 47, 49}
# By English description, for a category without a known id.
_MODE_WORDS = (('single-player', 'single'), ('co-op', 'coop'), ('online', 'online'),
               ('split screen', 'local'), ('shared/split', 'local'), ('mmo', 'online'))
MODES = ('single', 'coop', 'online', 'local')


def nexus_genres(names) -> list[str]:
    """Nexus genres for store genre names, in source order, no duplicates."""
    out = []
    for n in names or ():
        g = GENRE_MAP.get(str(n).strip().lower())
        if g and g not in out:
            out.append(g)
    return out


# IGDB game mode names (lowercase), as Game-db's v2/games `gameModes` sends them.
IGDB_MODES = {'single player': ('single',), 'co-operative': ('coop',),
              'split screen': ('local',), 'multiplayer': ('online',),
              'massively multiplayer online (mmo)': ('online',), 'battle royale': ('online',)}


def igdb_modes(names) -> list[str] | None:
    """Modes from IGDB game mode names; None when there are none."""
    if not names:
        return None
    got = {m for n in names for m in IGDB_MODES.get(str(n).strip().lower(), ())}
    return [m for m in MODES if m in got] or None


def modes(categories) -> list[str] | None:
    """Modes from Steam categories ([{id, name}]); None when unknown (a
    cache file from before categories were kept)."""
    if categories is None:
        return None
    got, vague = set(), False
    for c in categories:
        if not isinstance(c, dict):
            continue
        cid, name = c.get('id'), str(c.get('name') or '').lower()
        if cid in CATEGORY_MODES:
            got.update(CATEGORY_MODES[cid])
        elif cid in _VAGUE_MULTI or name in ('multi-player', 'pvp'):
            vague = True
        else:
            got.update(m for word, m in _MODE_WORDS if word in name)
    if vague and not got - {'single'}:
        got.add('online')
    return [m for m in MODES if m in got]


# ── release dates ────────────────────────────────────────────────────────

# Steam writes dates in English whatever the locale: parse them by hand
# (strptime's %b follows the locale, which differs on Windows).
_MONTHS = {m: i for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}


def _month(word: str) -> int | None:
    return _MONTHS.get(word[:3].lower()) if len(word) >= 3 else None


def _year(text: str) -> int | None:
    y = int(text)
    return y if 1970 <= y <= 2100 else None


def parse_release(text) -> tuple[int | None, str | None]:
    """(year, date) from a Steam release text: '17 Sep, 2020' and
    'Sep 17, 2020' -> (2020, '2020-09-17'); 'October 2026' -> (2026,
    '2026-10'); 'Q2 2027' or '2027' -> (2027, '2027'); 'Coming soon' ->
    (None, None). An ISO date ('2014-09-30T00:00:00Z') works too."""
    t = re.sub(r'\s+', ' ', str(text or '')).strip().replace(',', '')
    if m := re.match(r'^(\d{4})-(\d{2})-(\d{2})', t):
        y = _year(m[1])
        return (y, f"{m[1]}-{m[2]}-{m[3]}") if y else (None, None)
    if m := re.match(r'^(\d{1,2}) ([A-Za-z]+)\.? (\d{4})$', t):
        day, mon, y = int(m[1]), _month(m[2]), _year(m[3])
    elif m := re.match(r'^([A-Za-z]+)\.? (\d{1,2}) (\d{4})$', t):
        day, mon, y = int(m[2]), _month(m[1]), _year(m[3])
    elif m := re.match(r'^([A-Za-z]+)\.? (\d{4})$', t):
        mon, y = _month(m[1]), _year(m[2])
        return (y, f"{y}-{mon:02d}") if y and mon else (y, str(y) if y else None)
    elif m := re.match(r'^(?:Q[1-4] )?(\d{4})$', t, re.I):
        y = _year(m[1])
        return y, str(y) if y else None
    else:
        return None, None
    if not y:
        return None, None
    if not mon or not 1 <= day <= 31:
        return y, str(y)
    return y, f"{y}-{mon:02d}-{day:02d}"


# ── one layer per source ─────────────────────────────────────────────────

def from_steam(d: dict | None) -> dict | None:
    """The facts in a storeinfo.details() answer. Keys an older cache file
    lacks are None (unknown), not empty."""
    if not isinstance(d, dict):
        return None
    year, release = parse_release(d.get('release'))
    v2 = d.get('v') == storeinfo.STORE_V
    return {'name': d.get('name'), 'description': d.get('blurb') or None,
            'developers': list(d.get('developers') or []),
            'publishers': list(d.get('publishers') or []) if v2 else None,
            'releaseText': d.get('release'), 'year': year, 'release': release,
            'comingSoon': d.get('coming_soon') if v2 else None,
            'genres': list(d.get('genres') or []),
            'categories': d.get('categories') if v2 else None,
            'metacritic': (d.get('metacritic') or {}).get('score')}


def status_from_steam(layer: dict | None) -> str | None:
    if not layer:
        return None
    if layer.get('comingSoon'):
        return 'upcoming'
    if any(str(g).lower() == 'early access' for g in layer.get('genres') or ()):
        return 'early_access'
    return 'released' if layer.get('comingSoon') is False else None


def from_hltb(hit: dict | None) -> dict | None:
    if not isinstance(hit, dict) or not any(hit.get(k) for k in ('main', 'plus', 'complete')):
        return None
    return {'main': hit.get('main'), 'plus': hit.get('plus'), 'complete': hit.get('complete'),
            'url': hit.get('url')}


# IGDB `game_status` keys as Game-db's v2/games sends them -> Nexus status.
# Delisted and offline games were released; they keep the Steam status.
IGDB_STATUS = {'released': 'released', 'early_access': 'early_access', 'alpha': 'early_access',
               'beta': 'early_access', 'cancelled': 'cancelled', 'canceled': 'cancelled',
               'rumored': 'upcoming', 'delayed': 'delayed'}
STATUSES = ('released', 'early_access', 'upcoming', 'delayed', 'cancelled')


def from_gamedb(row: dict | None) -> dict | None:
    """The facts in one Game-db `GET /api/nexus/v2/games` row (schema 2):
    genres, developers, publishers, releaseDate/Text, comingSoon, status
    (an IGDB key), playLength {main, mainExtra, completionist, source,
    count}, igdbSlug. Not fetched in this phase; merge() takes it already."""
    if not isinstance(row, dict):
        return None
    year, release = parse_release(row.get('releaseDate') or row.get('releaseDateText'))
    pl = row.get('playLength') if isinstance(row.get('playLength'), dict) else {}
    length = None
    if any(pl.get(k) for k in ('main', 'mainExtra', 'completionist')):
        length = {'main': pl.get('main'), 'plus': pl.get('mainExtra'),
                  'complete': pl.get('completionist'),
                  'source': pl.get('source') if pl.get('source') in ('hltb', 'igdb') else 'igdb',
                  'count': pl.get('count')}
    status = IGDB_STATUS.get(str(row.get('status') or '').lower())
    if status is None and row.get('comingSoon'):
        status = 'upcoming'
    return {'name': row.get('title'), 'description': row.get('description') or None,
            'modes': igdb_modes(row.get('gameModes')),
            'genres': list(row.get('genres') or []),
            'developers': list(row.get('developers') or []),
            'publishers': list(row.get('publishers') or []),
            'releaseText': row.get('releaseDateText'), 'year': year, 'release': release,
            'status': status, 'length': length, 'igdbSlug': row.get('igdbSlug') or None,
            'metacritic': (row.get('reviews') or {}).get('metacritic')}


# ── the merge ────────────────────────────────────────────────────────────

def merge(local: dict, gamedb_row: dict | None = None) -> dict:
    """One game's record from its local layers (`local` = {appid, name,
    steam, hltb}, the from_* outputs) and, from Phase 3 on, its Game-db
    v2/games row. This is the one place that decides which source wins;
    `src` names the winner of each fact.

    - Most facts: Steam, then Game-db (which itself carries Steam's or
      IGDB's), then the library's own name.
    - status: Game-db's IGDB status first (it knows early access, delays
      and cancellations), then what Steam implies.
    - length: HowLongToBeat (Nexus's own lookup, then Game-db's), else
      IGDB's time-to-beat via Game-db — the same rule as Game-db's site.
    - igdbSlug: Game-db only."""
    steam, hl = local.get('steam'), local.get('hltb')
    gdb = from_gamedb(gamedb_row)
    rec = {'appid': local.get('appid')}
    src = {}

    def pick(key, *cands):
        for name, layer in cands:
            v = (layer or {}).get(key)
            if v not in (None, '', []):
                rec[key] = v
                src[key] = name
                return
        rec[key] = None

    order = (('steam', steam), ('gamedb', gdb))
    pick('name', *order, ('library', {'name': local.get('name')}))
    for key in ('description', 'developers', 'publishers', 'releaseText', 'year', 'release',
                'metacritic'):
        pick(key, *order)
    pick('genresRaw', ('steam', {'genresRaw': (steam or {}).get('genres')}),
         ('gamedb', {'genresRaw': (gdb or {}).get('genres')}))
    rec['genres'] = nexus_genres(rec['genresRaw'])
    if rec['genres']:
        src['genres'] = src['genresRaw']
    rec['modes'] = modes((steam or {}).get('categories'))
    if rec['modes'] is not None:
        src['modes'] = 'steam'
    elif (gdb or {}).get('modes') is not None:
        rec['modes'] = gdb['modes']
        src['modes'] = 'gamedb'
    pick('status', ('gamedb', gdb), ('steam', {'status': status_from_steam(steam)}))
    lengths = []
    if hl:
        lengths.append(('hltb', {**{k: hl.get(k) for k in ('main', 'plus', 'complete')},
                                 'source': 'hltb', 'count': None}))
    if gdb and gdb['length']:
        lengths.append(('gamedb', gdb['length']))
    lengths.sort(key=lambda c: c[1]['source'] != 'hltb')       # stable: local HLTB first
    rec['length'] = lengths[0][1] if lengths else None
    if lengths:
        src['length'] = lengths[0][0]
    pick('igdbSlug', ('gamedb', gdb))
    rec['src'] = src
    return rec


# ── the index ────────────────────────────────────────────────────────────

def index_path():
    return CACHE_DIR / 'facts.json'


def hltb_name(row: dict, steam: dict | None) -> str:
    """The title HowLongToBeat is asked about: the store's name, else the
    library's (a repack's folder-ish name matches less often)."""
    return (steam or {}).get('name') or row.get('name') or ''


def store_key(row: dict) -> str | None:
    """'epic:<app>' for a library row of another store, else None."""
    return f"{row['store']}:{row['store_id']}" if row.get('store') and row.get('store_id') \
        else None


def gamedb_rows(library: list[dict]) -> dict:
    """{appid: v2/games row} for the library's games that Game-db's synced
    copy knows, and {'<store>:<id>': row} for its Heroic games (gamedb.meta
    and store_meta: cache only, {} when Game-db is off or old)."""
    from . import gamedb
    out = {}
    try:
        for appid in {r['appid'] for r in library if r.get('appid')}:
            if (row := gamedb.meta(appid)) is not None:
                out[appid] = row
        for r in library:
            if (key := store_key(r)) and (row := gamedb.store_meta(r['store'],
                                                                    r['store_id'])):
                out[key] = row
    except Exception:                                           # noqa: BLE001 — optional
        return {}
    return out


def build(library: list[dict], gamedb_rows: dict | None = None) -> dict:
    """The index from caches alone (no network): {v, built, games: {slug:
    record}, missing: {steam, hltb}}. `library` is [{slug, appid, name}]
    (a Heroic game adds store and store_id); rows with neither a Steam appid
    nor a Game-db row are left out. `gamedb_rows` (see gamedb_rows()) adds
    what Game-db knows about your own games."""
    games, missing = {}, {'steam': 0, 'hltb': 0}
    for row in library:
        appid = row.get('appid')
        if not appid:
            row_g = (gamedb_rows or {}).get(store_key(row))
            if row_g is not None:                   # Heroic: Game-db's row is all there is
                games[row['slug']] = merge({'appid': None, 'name': row.get('name'),
                                            'steam': None, 'hltb': None}, row_g)
            continue
        fresh, d = storeinfo.cached_details(appid)
        steam = from_steam(d)
        missing['steam'] += not fresh
        name = hltb_name(row, steam)
        h_fresh, hit = hltb.cached(name) if name else (True, None)
        missing['hltb'] += not h_fresh
        local = {'appid': appid, 'name': row.get('name'), 'steam': steam,
                 'hltb': from_hltb(hit)}
        row_g = (gamedb_rows or {}).get(appid) or (gamedb_rows or {}).get(store_key(row))
        if steam is None and local['hltb'] is None and row_g is None:
            continue                            # nothing known yet
        games[row['slug']] = merge(local, row_g)
    return {'v': INDEX_V, 'built': int(time.time()), 'games': games, 'missing': missing}


def read_index() -> dict:
    """The last index written, or an empty one."""
    try:
        idx = json.loads(index_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        idx = None
    if not isinstance(idx, dict) or idx.get('v') != INDEX_V:
        return {'v': INDEX_V, 'built': None, 'games': {}, 'missing': None}
    return idx


def write_index(idx: dict) -> bool:
    return net.save_json(index_path(), idx)


# ── the fill: budget and backoff ─────────────────────────────────────────

BUDGET = 40             # requests per site per hour
WINDOW = 3600
BACKOFF = 3600          # seconds to stop after a rate limit or a failure streak
FAIL_STREAK = 3
PACE = 1.0              # seconds between two requests
RATE_LIMITED = (429, 403)
SITES = ('steam', 'hltb')


def state_path():
    return CACHE_DIR / 'facts-fill.json'


@contextlib.contextmanager
def _edit():
    """The fill state, under a lock: the agent, the window and a manual run
    share one budget. {site: {window_start, spent, blocked_until, fails,
    last: {at, status, error}}}."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with platform.file_lock(path.with_name(path.name + '.lock')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        for s in SITES:
            if not isinstance(data.get(s), dict):
                data[s] = {}
        yield data
        net.save_json(path, data)


def wait_seconds(site: str = 'steam', now: float | None = None) -> float:
    """Seconds until `site` may be asked again (0 = now)."""
    now = time.time() if now is None else now
    with _edit() as data:
        st = data[site]
        if (st.get('blocked_until') or 0) > now:
            return st['blocked_until'] - now
        start = st.get('window_start') or 0
        if now - start < WINDOW and (st.get('spent') or 0) >= BUDGET:
            return start + WINDOW - now
        return 0.0


def take(site: str, now: float | None = None) -> bool:
    """Spend one request of `site`'s hourly budget; False when it is spent
    or the site is blocked."""
    now = time.time() if now is None else now
    with _edit() as data:
        st = data[site]
        if (st.get('blocked_until') or 0) > now:
            return False
        if now - (st.get('window_start') or 0) >= WINDOW or now < (st.get('window_start') or 0):
            st['window_start'], st['spent'] = now, 0
        if (st.get('spent') or 0) >= BUDGET:
            return False
        st['spent'] = (st.get('spent') or 0) + 1
        return True


def report(site: str, error: Exception | None, now: float | None = None) -> None:
    """How a request went: a rate limit (429/403) stops the site for an
    hour, and so do FAIL_STREAK failures in a row (offline, or the site
    changed)."""
    now = time.time() if now is None else now
    with _edit() as data:
        st = data[site]
        if error is None:
            st['fails'] = 0
            st['last'] = {'at': int(now), 'status': 200, 'error': None}
            return
        status = getattr(error, 'status', None)
        st['fails'] = (st.get('fails') or 0) + 1
        st['last'] = {'at': int(now), 'status': status, 'error': str(error)[:200]}
        if status in RATE_LIMITED or st['fails'] >= FAIL_STREAK:
            st['blocked_until'] = now + BACKOFF
            st['fails'] = 0


def todo(library: list[dict]) -> tuple[list[int], list[tuple[str, int | None]]]:
    """What a fill would fetch: store appids whose details are missing or
    stale, and (name, year) titles with no fresh HowLongToBeat answer and no
    Game-db figure, v1 summary or v2 games (cache only)."""
    from . import gamedb
    appids, titles, seen = [], [], set()
    rows = [r for r in library if r.get('appid')]
    try:
        gdb = gamedb.lookup([r['appid'] for r in rows])          # cache only, {} when off
    except Exception:                                           # noqa: BLE001 — optional
        gdb = {}
    for r in rows:
        fresh, d = storeinfo.cached_details(r['appid'])
        if not fresh and r['appid'] not in seen:
            appids.append(r['appid'])
            seen.add(r['appid'])
        steam = from_steam(d)
        name = hltb_name(r, steam)
        if not name or ((gdb.get(r['appid']) or {}).get('hltb') or {}).get('main') \
                or hltb.from_gamedb(r['appid']):            # v2: HLTB's or IGDB's hours
            continue
        if not hltb.cached(name)[0] and name not in {t for t, _y in titles}:
            titles.append((name, (steam or {}).get('year')))
    return appids, titles


def fill(library: list[dict], n: int, sleep=time.sleep) -> dict:
    """Fetch up to `n` missing Steam details, then up to `n` HowLongToBeat
    answers, within each site's budget. Returns {steam, hltb (fetched),
    stopped (a site that is blocked or out of budget), left (still to do)}."""
    out = {'steam': 0, 'hltb': 0, 'stopped': [], 'left': 0}
    first = True
    for i, site in enumerate(SITES):
        # HowLongToBeat after the store pass: it asks by the store's name.
        jobs = todo(library)[i]
        done = 0
        for job in jobs:
            if done >= n:
                break
            if not take(site):
                out['stopped'].append(site)
                break
            if not first:
                sleep(PACE)
            first = False
            errs: list = []
            if site == 'steam':
                storeinfo.details(job, refresh=True, errors=errs)
            else:
                hltb.lookup(job[0], job[1], refresh=True, errors=errs)
            report(site, errs[0] if errs else None)
            done += 1
            if errs:
                if wait_seconds(site):
                    out['stopped'].append(site)
                    break
                continue
            out[site] += 1
    appids, titles = todo(library)
    out['left'] = len(appids) + len(titles)
    return out
