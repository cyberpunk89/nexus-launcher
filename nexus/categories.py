"""Library categories: every way the Library groups your games (a smart
rule, a genre, your favourites, a collection, a shelf you built, a store),
as one model the category page, Browse and Theatre share. Pure functions
over plain dicts, no Qt (Theatre and the Ribbon import it too).

Keys: the smart keys of nexus/smart.py (`smart:<rule>`, `genre:<name>`,
`user:<id>`) plus five of its own, which never start like a smart key:
`fav` (your favourites), `col:<name>` (a collection; the name may hold
':', so it is everything after the prefix), `src:<source>` (a store), and
`dev:<id>` / `pub:<id>` (a studio, kinds 'developer' / 'publisher').
Play together's `friends:` keys stay a filter, not a category.

Studios (w-112), from the facts index's `developers` / `publishers`:
- `studio_name(raw)` is the name shown: company suffixes stripped (Inc.,
  Ltd., Co., LLC, S.A., ...), a few known aliases merged (ALIASES).
  `studio_id(raw)` is that name case-folded: what a key holds, so CAPCOM,
  Capcom and CAPCOM Co., Ltd. are one studio. `studio_key(role, raw)`
  builds the key (role 'dev' or 'pub'); `studio_switch(key)` is the same
  studio's other role (the studio page's Developer / Publisher switch; it
  may be no category). `raw_names(cat, facts)` lists the credits that
  merged into one ("Merged: ...").
- The `studios` group (after Genres, before Stores) lists developers by
  default (STUDIO_DEFAULT; `categories(..., studios='pub')` for
  publishers), most games first. A studio needs STUDIO_MIN games for the
  hub and a page (keep_empty keeps an open page or a pin), smart.MIN_GAMES
  for a Browse row, and Browse shows at most CAPS['studios'] rows."""

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import smart
from .facts import NEXUS_GENRES
from .icons import COLLECTION_ICONS, ICONS
from .smart import USER

FAV, COL, SRC = 'fav', 'col:', 'src:'
DEV, PUB = 'dev:', 'pub:'
STUDIO_KINDS = ('developer', 'publisher')
STUDIO_DEFAULT = 'dev'       # the studios group lists developers unless told 'pub'
STUDIO_MIN = 2               # games a studio needs for the hub and a page
STORE_KEYS = tuple(k for k, _t in smart.STORES)
# The groups, in the order every place shows them.
GROUPS = (('for_you', "For you"), ('yours', "Yours"), ('genres', "Genres"),
          ('studios', "Studios"), ('stores', "Stores"))
SORTS = ('played', 'name', 'most', 'added', 'size')       # the Library's sort keys
VIEWS = ('rows', 'grid', 'list')
ROW_CAP = 20                 # tiles in one row of a page's Rows view (and of Browse)
# Browse shows at most this many rows of a group (the rest: the hub).
CAPS = {'for_you': 6, 'genres': 6, 'studios': 6}
# The All categories hub's place key: never a category (is_key is False).
HUB = 'all'
PIN_MAX = 6                  # pinned categories (tabs in the Library)

# Glyphs (nexus/icons.py names): the smart rules and stores from ICONS, the
# genres from the collection icons.
RULE_GLYPH = {'continue': 'history', 'unfinished': 'timer', 'almost': 'medal',
              'never': 'sparkles', 'short': 'clock', 'coop': 'friends', 'early': 'wrench',
              'linux': 'proton'}
GENRE_GLYPH = {'Action': 'zap', 'Adventure': 'map', 'RPG': 'swords', 'Strategy': 'brain',
               'Shooter': 'crosshair', 'Simulation': 'castle', 'Racing': 'car',
               'Sports': 'trophy', 'Puzzle': 'puzzle', 'Platformer': 'gamepad-2',
               'Horror': 'ghost', 'Indie': 'heart'}
STORE_GLYPH = {'nexus': 'folder', 'steam': 'steam', 'heroic': 'wine', 'epic': 'library'}
STUDIO_GLYPH = {'developer': COLLECTION_ICONS['users'], 'publisher': COLLECTION_ICONS['rocket']}
# Each group's glyph character (Browse's jump chips).
GROUP_GLYPH = {'for_you': ICONS['sparkles'], 'yours': ICONS['star'], 'genres': ICONS['grid'],
               'studios': COLLECTION_ICONS['users'], 'stores': ICONS['folder']}


@dataclass(frozen=True)
class PrefsView:
    """What the model needs from library.json and the platform, as plain
    values: collection names in order, their looks ({name: {'icon',
    'color'}}), your shelves (smart.clean_shelf dicts), whether repacks are
    on (config) and whether Heroic is (features.has('heroic'))."""
    collections: tuple = ()
    looks: dict = field(default_factory=dict)
    shelves: tuple = ()
    repacks: bool = True
    heroic: bool = False


@dataclass(frozen=True)
class Category:
    key: str
    kind: str                 # smart, genre, fav, collection, shelf, store, developer, publisher
    title: str
    tip: str
    glyph: str                # the glyph character
    color: str | None         # an accent key (a coloured collection), else None
    games: tuple = ()


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    cats: tuple = ()
    more: int = 0             # Browse: rows past the group's cap (in the hub)


@dataclass(frozen=True)
class Cut:
    key: str
    title: str
    glyph: str
    count: int


@dataclass(frozen=True)
class Stats:
    count: int
    minutes: int
    finished: int             # at least smart.DONE_AT of the main story
    bytes: int


_PREFIX_KINDS = (('smart:', 'smart'), ('genre:', 'genre'), (USER, 'shelf'),
                 (COL, 'collection'), (SRC, 'store'), (DEV, 'developer'),
                 (PUB, 'publisher'))


def kind_of(key) -> str | None:
    """'smart', 'genre', 'shelf', 'fav', 'collection', 'store', 'developer'
    or 'publisher'; None for anything else (a Play together key, a bad one)."""
    if not isinstance(key, str):
        return None
    if key == FAV:
        return 'fav'
    for prefix, kind in _PREFIX_KINDS:
        if key.startswith(prefix) and len(key) > len(prefix):
            return kind
    return None


def is_key(key) -> bool:
    return kind_of(key) is not None


def is_page_key(key) -> bool:
    """A place the Library can show: a category, or the hub."""
    return key == HUB or is_key(key)


def collection_name(key) -> str | None:
    """The collection a `col:` key names (never split on ':')."""
    return key[len(COL):] if kind_of(key) == 'collection' else None


def from_chip(chip) -> str | None:
    """The category a Library chip stands for ("See all" on a source row,
    the ★ chip, a collection chip); None for All, Not installed, Friends."""
    if chip == 'favorite':
        return FAV
    if kind_of(chip) == 'collection':
        return chip
    if chip in STORE_KEYS:
        return SRC + chip
    return None


def _name(g: dict) -> str:
    """The sort key by name (the Library's)."""
    return (g.get('display_name') or g.get('slug') or '').lower()


def _by_played(games) -> tuple:
    """Most recently played first, then by name."""
    out = sorted(games, key=_name)
    out.sort(key=lambda g: g.get('last_played') or '', reverse=True)
    return tuple(out)


def _store_label(src: str) -> str:
    return dict(smart.STORES).get(src, src)


def _store_ok(src: str, prefs: PrefsView) -> bool:
    if src == 'nexus':
        return prefs.repacks
    if src == 'heroic':
        return prefs.heroic
    return src in STORE_KEYS


# ---- studios ----
# Company suffixes, stripped from the end (repeatedly: "Co., Ltd.").
_SUFFIX = re.compile(r'[\s,]+(inc|ltd|co|llc|l\.l\.c|s\.a|corp|corporation|limited|gmbh)'
                     r'\.?\s*$', re.IGNORECASE)
# Known spellings of one studio, by the folded name after the suffixes go.
ALIASES = {'warner bros. interactive entertainment': 'Warner Bros. Games',
           'warner bros. games': 'Warner Bros. Games', 'wb games': 'Warner Bros. Games',
           'monolith productions': 'Monolith Productions', 'capcom': 'Capcom'}
_ROLE_FIELD = {'dev': 'developers', 'pub': 'publishers'}
_ROLE_PREFIX = {'dev': DEV, 'pub': PUB}
_KIND_ROLE = {'developer': 'dev', 'publisher': 'pub'}


def studio_name(raw) -> str:
    """The studio name shown for a credit: whitespace tidied, company
    suffixes stripped (never down to nothing), a known alias merged. ''
    for no name."""
    if not isinstance(raw, str):
        return ''
    name = ' '.join(raw.split())
    while True:
        cut = _SUFFIX.sub('', name).rstrip(' ,')
        if not cut or cut == name:
            break
        name = cut
    return ALIASES.get(name.casefold(), name)


def studio_id(raw) -> str:
    """What a studio key holds: studio_name, case-folded."""
    return studio_name(raw).casefold()


def studio_key(role, raw) -> str | None:
    """`dev:<id>` or `pub:<id>` for a credit; None for no name or a role
    other than 'dev' / 'pub'."""
    sid = studio_id(raw)
    return _ROLE_PREFIX[role] + sid if role in _ROLE_PREFIX and sid else None


def studio_switch(key) -> str | None:
    """The same studio in the other role (dev: <-> pub:); None for a key
    that is no studio."""
    kind = kind_of(key)
    if kind == 'developer':
        return PUB + key[len(DEV):]
    if kind == 'publisher':
        return DEV + key[len(PUB):]
    return None


def _credits(g: dict, facts, role: str) -> list:
    names = ((facts or {}).get(g.get('slug')) or {}).get(_ROLE_FIELD[role]) or ()
    return [n for n in names if isinstance(n, str)] if isinstance(names, (list, tuple)) else []


def _studio_counts(games, facts, role: str) -> dict:
    """{studio id: (games, the name shown most)}, each game counted once."""
    count: dict[str, int] = {}
    shown: dict[str, dict[str, int]] = {}
    for g in games:
        for sid in {studio_id(n) for n in _credits(g, facts, role)} - {''}:
            count[sid] = count.get(sid, 0) + 1
        for n in _credits(g, facts, role):
            if (sid := studio_id(n)):
                names = shown.setdefault(sid, {})
                names[studio_name(n)] = names.get(studio_name(n), 0) + 1
    return {sid: (n, min(shown[sid], key=lambda x: (-shown[sid][x], x)))
            for sid, n in count.items()}


def raw_names(cat: Category, facts) -> list[str]:
    """The credits, as the facts index spells them, that merged into this
    studio (sorted); [] for a category that is no studio."""
    role = _KIND_ROLE.get(cat.kind)
    if role is None:
        return []
    sid = cat.key[len(_ROLE_PREFIX[role]):]
    return sorted({n for g in cat.games for n in _credits(g, facts, role)
                   if studio_id(n) == sid})


def category(key, games, facts, prefs: PrefsView, linux: bool = False, friends=None,
             now: datetime | None = None, keep_empty: bool = False) -> Category | None:
    """One category over `games` (the pool the caller shows: not hidden, no
    duplicates, the owned segment applied). None for a key that isn't one,
    or has no game, except a shelf you built (its page says it is empty).
    `keep_empty`: an open page keeps a category whose games all left (a
    genre before the facts index has filled), still None for a bad key."""
    kind = kind_of(key)
    if kind is None:
        return None
    facts = facts or {}
    now = now or datetime.now()
    title = tip = glyph = ''
    color = None
    if kind == 'smart':
        rule = key[len('smart:'):]
        if key not in smart.rule_keys(linux):
            return None
        hit = smart.games_for(key, games, facts, now, linux, friends)
        title, tip, glyph = smart.label(key), smart.tip(key), ICONS[RULE_GLYPH[rule]]
    elif kind == 'genre':
        name = key[len('genre:'):]
        if name not in NEXUS_GENRES:             # not one the facts index gives
            return None
        hit = smart.games_for(key, games, facts, now, linux, friends)
        title, tip = name, smart.tip(key)
        glyph = COLLECTION_ICONS.get(GENRE_GLYPH.get(name, ''), ICONS['grid'])
    elif kind == 'shelf':
        shelf = smart.find_user(list(prefs.shelves), key)
        if shelf is None or shelf.get('removed'):
            return None
        hit = smart.user_games(shelf, games, facts, now, friends)
        title, tip, glyph = smart.title_of(shelf), smart.describe(shelf), ICONS['sparkles']
    elif kind == 'fav':
        hit = _by_played(g for g in games if g.get('favorite'))
        title, tip, glyph, color = "Favourites", "Your favourites", ICONS['star'], 'gold'
    elif kind == 'collection':
        name = collection_name(key)
        if name not in prefs.collections:
            return None
        hit = _by_played(g for g in games if name in (g.get('collections') or ()))
        look = prefs.looks.get(name) or {}
        title, tip = name, "Your collection"
        glyph = COLLECTION_ICONS.get(look.get('icon'), COLLECTION_ICONS['bookmark'])
        color = look.get('color') or None
    elif kind in STUDIO_KINDS:
        role = _KIND_ROLE[kind]
        sid = key[len(_ROLE_PREFIX[role]):]
        hit = _by_played(g for g in games
                         if sid in {studio_id(n) for n in _credits(g, facts, role)})
        if len(hit) < STUDIO_MIN and not keep_empty:
            return None
        names = _studio_counts(hit, facts, role).get(sid)
        title = names[1] if names else sid
        tip = f"Games developed by {title}" if role == 'dev' else f"Games published by {title}"
        glyph = STUDIO_GLYPH[kind]
    else:
        src = key[len(SRC):]
        if not _store_ok(src, prefs):
            return None
        hit = _by_played(g for g in games if g.get('source', 'nexus') == src)
        title = _store_label(src)
        tip = "Games Nexus installed" if src == 'nexus' else f"Your games from {title}"
        glyph = ICONS[STORE_GLYPH[src]]
    if not hit and kind != 'shelf' and not keep_empty:
        return None
    return Category(key, kind, title, tip, glyph, color, tuple(hit))


def _keys(games, facts, prefs: PrefsView, linux: bool,
          studios: str = STUDIO_DEFAULT) -> list[tuple[str, list[str]]]:
    genres = sorted(_genre_counts(games, facts).items(),
                    key=lambda kv: (-kv[1], NEXUS_GENRES.index(kv[0])))
    role = studios if studios in _ROLE_PREFIX else STUDIO_DEFAULT
    found = sorted(((sid, n) for sid, (n, _name) in _studio_counts(games, facts, role).items()
                    if n >= STUDIO_MIN), key=lambda kv: (-kv[1], kv[0]))
    return [('for_you', smart.rule_keys(linux)),
            ('yours', [FAV] + [COL + n for n in prefs.collections]
             + [USER + s['id'] for s in prefs.shelves if not s.get('removed')]),
            ('genres', [f'genre:{name}' for name, _n in genres]),
            ('studios', [_ROLE_PREFIX[role] + sid for sid, _n in found]),
            ('stores', [SRC + k for k in STORE_KEYS])]


def _genre_counts(games, facts) -> dict:
    count: dict[str, int] = {}
    for g in games:
        for genre in ((facts or {}).get(g.get('slug')) or {}).get('genres') or ():
            if genre in NEXUS_GENRES:
                count[genre] = count.get(genre, 0) + 1
    return count


def categories(games, facts, prefs: PrefsView, linux: bool = False, friends=None,
               now: datetime | None = None, studios: str = STUDIO_DEFAULT) -> list[Group]:
    """Every category with a game (and your shelves, even empty), grouped:
    For you (the smart rules), Yours (favourites, collections, shelves),
    Genres (most games first), Studios (developers, or publishers with
    studios='pub'; STUDIO_MIN games, most first), Stores. Empty groups are
    left out."""
    now = now or datetime.now()
    titles = dict(GROUPS)
    out = []
    for gkey, keys in _keys(games, facts, prefs, linux, studios):
        cats = [c for c in (category(k, games, facts, prefs, linux, friends, now)
                            for k in keys) if c is not None]
        if cats:
            out.append(Group(gkey, titles[gkey], tuple(cats)))
    return out


_MIN_KINDS = ('smart', 'genre') + STUDIO_KINDS


def browse(groups, hidden=(), caps=None) -> list[Group]:
    """What Browse shows of `groups` (categories()): none of the `hidden`
    keys, a smart rule, genre or studio only with smart.MIN_GAMES games, at most
    `caps[group]` rows (CAPS by default; `more` counts the rest), empty
    groups left out. The groups given are not changed: the hub shows them
    all."""
    caps = CAPS if caps is None else caps
    hidden = set(hidden or ())
    out = []
    for g in groups:
        cats = [c for c in g.cats if c.key not in hidden
                and (c.kind not in _MIN_KINDS or len(c.games) >= smart.MIN_GAMES)]
        cap = caps.get(g.key)
        more = 0
        if cap is not None and len(cats) > cap:
            cats, more = cats[:cap], len(cats) - cap
        if cats:
            out.append(Group(g.key, g.title, tuple(cats), more))
    return out


def lookup(groups, key) -> Category | None:
    return next((c for g in groups for c in g.cats if c.key == key), None)


# ---- pinned categories (tabs) ----
def clean_pins(raw) -> list[str]:
    """library.json's category_pins as a list of category keys: bad ones
    and repeats dropped, at most PIN_MAX, in the order given."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for key in raw:
        if is_key(key) and key not in out:
            out.append(key)
    return out[:PIN_MAX]


def _live_count(pins, live) -> int:
    return len(pins) if live is None else sum(1 for k in pins if k in live)


def pin_state(pins, key, live=None) -> str:
    """'on' (pinned), 'off', or 'full' (not pinned and no room left).
    `live`: the pinned keys that are a category now (pinned()); a pin
    outside it is kept but takes no place. None: every pin counts."""
    if key in pins:
        return 'on'
    return 'full' if _live_count(pins, live) >= PIN_MAX else 'off'


def toggle_pin(pins, key, live=None) -> list[str]:
    """Pins with `key` unpinned or pinned last (a new list); unchanged when
    the live pins are full or the key isn't a category. Pinning with no
    room left drops pins that aren't live (the oldest first)."""
    out = list(pins)
    if key in out:
        out.remove(key)
        return out
    if not is_key(key) or pin_state(out, key, live) == 'full':
        return out
    while len(out) >= PIN_MAX:
        out.remove(next(k for k in out if live is not None and k not in live))
    out.append(key)
    return out


def pinned(pins, games, facts, prefs: PrefsView, linux: bool = False, friends=None,
           now: datetime | None = None) -> list[Category]:
    """The pinned categories over `games`, in pin order, for the tabs
    (desktop, Theatre) and the Ribbon alike. One with no games now is kept
    (its page says so); a key that is no category here (a collection
    deleted, a store that's off) is skipped and stays in the pins. A row
    hidden from Browse is still a pin."""
    now = now or datetime.now()
    return [c for c in (category(k, games, facts, prefs, linux, friends, now, keep_empty=True)
                        for k in pins) if c is not None]


# ---- what a category page shows ----
def stats(cat: Category, facts) -> Stats:
    """The page's story line: games, minutes played, finished (at least
    smart.DONE_AT of the main story), bytes on disk."""
    facts = facts or {}
    games = cat.games
    done = sum(1 for g in games
               if (smart.progress(g, facts.get(g.get('slug'))) or 0) >= smart.DONE_AT)
    size = sum(b for g in games if isinstance(b := g.get('disk_bytes'), (int, float))
               and not isinstance(b, bool) and b > 0)
    return Stats(len(games), sum(g.get('playtime_minutes') or 0 for g in games), done,
                 int(size))


def lead(cat: Category) -> dict | None:
    """The game played last (Continue <game>), or None when none was."""
    played = [g for g in cat.games if g.get('last_played')]
    return max(played, key=lambda g: g['last_played']) if played else None


def covers(cat: Category, n: int = 6) -> list[dict]:
    """The games whose covers make the header: last played first, then the
    newest added, then by name."""
    out = sorted(cat.games, key=_name)
    out.sort(key=lambda g: g.get('added_at') or '', reverse=True)
    out.sort(key=lambda g: g.get('last_played') or '', reverse=True)
    return out[:max(0, n)]


def cuts(cat: Category, facts, linux: bool = False, now: datetime | None = None) -> list[Cut]:
    """The smart rules that split this category (its chips): each rule with
    some but not all of its games, never the category's own key."""
    now = now or datetime.now()
    out = []
    for key in smart.rule_keys(linux):
        if key == cat.key:
            continue
        n = len(smart.games_for(key, list(cat.games), facts or {}, now, linux))
        if 0 < n < len(cat.games):
            out.append(Cut(key, smart.label(key), ICONS[RULE_GLYPH[key[len('smart:'):]]], n))
    return out


def related(cat: Category, groups, n: int = 4) -> list[Category]:
    """More like this: the categories sharing the most of this one's games
    (shared / together), another kind before the same kind on a tie, then
    in group order. Stores are left out (every game is in one)."""
    mine = {g.get('slug') for g in cat.games}
    scored = []
    for i, c in enumerate(c for g in groups for c in g.cats):
        if c.key == cat.key or c.kind == 'store':
            continue
        theirs = {g.get('slug') for g in c.games}
        shared = len(mine & theirs)
        if shared:
            scored.append((-shared / len(mine | theirs), c.kind == cat.kind, i, c))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:max(0, n)]]


def sort_games(games, sort: str) -> list[dict]:
    """The Library's sorts (a new list): by name, then recently played,
    most played, recently added or size on disk; anything else is by name."""
    out = sorted(games, key=_name)
    key = {'played': lambda g: g.get('last_played') or '',
           'most': lambda g: g.get('playtime_minutes') or 0,
           'added': lambda g: g.get('added_at') or '',
           'size': lambda g: g.get('disk_bytes') or 0}.get(sort)
    if key is not None:
        out.sort(key=key, reverse=True)
    return out
