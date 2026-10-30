"""Smart shelves: Library rows that sort themselves from your play data and
the facts index (nexus/facts.py). Pure functions over plain dicts, so the
Library, the Home widget and the status filter share one set of rules.

A game is a library dict (slug, last_played as a local ISO time,
playtime_minutes, added_at, installed, protondb); `facts` is the index's
`games` ({slug: record}). Keys: `smart:<rule>` for the fixed rules and
`genre:<Nexus genre>` for the genre shelves (a game page's genre chip sends
the same key), and `user:<id>` for shelves you build from rules (w-51, at
the end of this file)."""

from datetime import datetime

from .facts import NEXUS_GENRES

MIN_GAMES = 3           # a shelf with fewer stays out
MAX_SHELVES = 6         # at most this many smart rows at once
TOP_GENRES = 3

RECENT_DAYS = 14        # Continue playing
IDLE_DAYS = 30          # Unfinished: not played for this long
UNFINISHED_MIN = 60     # Unfinished: played at least this many minutes
DONE_AT = 0.8           # "nearly through" from 80 % of the main length
SHORT_HOURS = 5

# (rule, title, rule as a sentence for the tooltip, needs facts), in the
# order shelves are offered. The genre shelves go after 'never'.
RULES = (
    ('continue', "Continue playing",
     f"Played in the last {RECENT_DAYS} days and not near the end yet", False),
    ('unfinished', "Unfinished",
     f"Played at least 1 h, not in {IDLE_DAYS} days, under 80 % of the main story", True),
    ('almost', "Almost done", "Between 80 % and 100 % of the main story", True),
    ('never', "Never played", "Installed, no play time yet", False),
    ('short', "Short games", f"The main story takes under {SHORT_HOURS} h", True),
    ('coop', "Co-op", "The store lists co-op", True),
    ('early', "Early access", "Still in early access", True),
    ('linux', "Runs great on Linux", "ProtonDB rates it Platinum", False),
)
TITLES = {k: t for k, t, _tip, _f in RULES}
TIPS = {k: tip for k, _t, tip, _f in RULES}
# The Home widget picks one of these (not the genres: they change).
WIDGET_RULES = ('unfinished', 'continue', 'almost', 'never', 'short', 'coop', 'early', 'linux')
WIDGET_DEFAULT = 'unfinished'


def _when(iso) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _days_since(g: dict, now: datetime) -> float | None:
    t = _when(g.get('last_played'))
    return None if t is None else (now - t).total_seconds() / 86400


def _hours(g: dict) -> float:
    return (g.get('playtime_minutes') or 0) / 60


def main_hours(fact: dict | None) -> float | None:
    """The main-story length in hours, or None when unknown or when the game
    has no story to finish (online multiplayer without single-player)."""
    if not fact:
        return None
    modes = fact.get('modes') or ()
    if 'online' in modes and 'single' not in modes:
        return None
    main = (fact.get('length') or {}).get('main')
    try:
        main = float(main)
    except (TypeError, ValueError):
        return None
    return main if main > 0 else None


def progress(g: dict, fact: dict | None) -> float | None:
    """Your play time as a share of the main story, or None."""
    main = main_hours(fact)
    return None if main is None else _hours(g) / main


def label(key: str) -> str:
    if key.startswith('genre:'):
        return key[6:]
    return TITLES.get(key.removeprefix('smart:'), key)


def tip(key: str) -> str:
    if key.startswith('genre:'):
        return f"Your {key[6:]} games, from their store genres"
    return TIPS.get(key.removeprefix('smart:'), '')


def needs_facts(key: str) -> bool:
    if key.startswith('genre:'):
        return True
    return next((f for k, _t, _tip, f in RULES if k == key.removeprefix('smart:')), False)


def rule_keys(linux: bool = True) -> list[str]:
    return [f'smart:{k}' for k, *_ in RULES if linux or k != 'linux']


def _match(rule: str, g: dict, fact: dict | None, now: datetime, linux: bool) -> bool:
    days = _days_since(g, now)
    if rule == 'continue':
        p = progress(g, fact)
        return days is not None and days <= RECENT_DAYS and (p is None or p < DONE_AT)
    if rule == 'unfinished':
        p = progress(g, fact)
        return (p is not None and p < DONE_AT and (g.get('playtime_minutes') or 0) >= UNFINISHED_MIN
                and days is not None and days > IDLE_DAYS)
    if rule == 'almost':
        p = progress(g, fact)
        return p is not None and DONE_AT <= p < 1.0
    if rule == 'never':
        return g.get('installed', True) is not False and not g.get('playtime_minutes') \
            and not g.get('last_played')
    if rule == 'short':
        main = main_hours(fact)
        return main is not None and main < SHORT_HOURS
    if rule == 'coop':
        return 'coop' in ((fact or {}).get('modes') or ())
    if rule == 'early':
        return (fact or {}).get('status') == 'early_access'
    if rule == 'linux':
        return linux and str(g.get('protondb') or '').lower() == 'platinum'
    return False


def _order(rule: str, items: list[dict], facts: dict) -> list[dict]:
    """A shelf's games in the order that reads best for it."""
    name = lambda g: (g.get('display_name') or g.get('slug') or '').lower()
    items = sorted(items, key=name)
    if rule == 'almost':
        items.sort(key=lambda g: progress(g, facts.get(g['slug'])) or 0, reverse=True)
    elif rule == 'never':
        items.sort(key=lambda g: g.get('added_at') or '', reverse=True)
    elif rule == 'short':
        items.sort(key=lambda g: main_hours(facts.get(g['slug'])) or 0)
    else:
        items.sort(key=lambda g: g.get('last_played') or '', reverse=True)
    return items


def games_for(key: str, games: list[dict], facts: dict, now: datetime | None = None,
              linux: bool = True) -> list[dict]:
    """The games under one key (`smart:<rule>` or `genre:<name>`), ordered
    for its shelf."""
    now = now or datetime.now()
    facts = facts or {}
    if key.startswith('genre:'):
        genre = key[6:]
        hit = [g for g in games if genre in ((facts.get(g.get('slug')) or {}).get('genres') or ())]
        return _order('genre', hit, facts)
    rule = key.removeprefix('smart:')
    hit = [g for g in games if _match(rule, g, facts.get(g.get('slug')), now, linux)]
    return _order(rule, hit, facts)


def top_genres(games: list[dict], facts: dict, n: int = TOP_GENRES) -> list[tuple[str, int]]:
    """The Nexus genres with the most of your games: [(genre, count)], most
    first, ties in NEXUS_GENRES order."""
    count: dict[str, int] = {}
    for g in games:
        for genre in (facts.get(g.get('slug')) or {}).get('genres') or ():
            if genre in NEXUS_GENRES:
                count[genre] = count.get(genre, 0) + 1
    ranked = sorted(count.items(), key=lambda kv: (-kv[1], NEXUS_GENRES.index(kv[0])))
    return ranked[:n]


def candidate_keys(games: list[dict], facts: dict, linux: bool = True) -> list[str]:
    """Every key in shelf priority order: the fixed rules with the top
    genres after "Never played"."""
    out = []
    for key in rule_keys(linux):
        out.append(key)
        if key == 'smart:never':
            out += [f'genre:{genre}' for genre, _n in top_genres(games, facts)]
    return out


def shelves(games: list[dict], facts: dict, hidden=(), now: datetime | None = None,
            linux: bool = True) -> list[dict]:
    """The smart shelves to show: [{key, title, tip, games}] in priority
    order, each with at least MIN_GAMES games, at most MAX_SHELVES, none
    of the `hidden` keys. Shelves that need no facts come first on a first
    run on their own; the rest join as the index fills."""
    now = now or datetime.now()
    facts = facts or {}
    out = []
    for key in candidate_keys(games, facts, linux):
        if key in hidden:
            continue
        items = games_for(key, games, facts, now, linux)
        if len(items) >= MIN_GAMES:
            out.append({'key': key, 'title': label(key), 'tip': tip(key), 'games': items})
            if len(out) >= MAX_SHELVES:
                break
    return out


def filter_keys(games: list[dict], facts: dict, now: datetime | None = None,
                linux: bool = True) -> list[str]:
    """Keys for the Library's status filter: each one that matches at least
    one game (hidden shelves included), in shelf order."""
    now = now or datetime.now()
    facts = facts or {}
    return [k for k in candidate_keys(games, facts, linux)
            if games_for(k, games, facts, now, linux)]


# ── shelves you build (w-51) ─────────────────────────────────────────────
# A user shelf is a name plus rules joined by all/any, kept in library.json
# (`smart_shelves`, see gui/prefs.py) as plain dicts:
#   {'id': 'a1b2c3d4', 'name': "Quick RPGs", 'match': 'all' | 'any',
#    'rules': [{'field': 'genre', 'op': 'is', 'value': 'RPG'}, ...],
#    'removed': False}
# Its key is `user:<id>`, so a rename changes nothing that points at it (the
# status filter, the Home widget, `smart_hidden`). "Remove" only sets
# `removed`: the shelf stays in the file and can come back.

USER = 'user:'
KEY_PREFIXES = ('smart:', 'genre:', USER)     # every smart filter key starts with one
MAX_USER_SHELVES = 50
MAX_RULES = 8
MAX_NAME = 40
UNTITLED = "Untitled shelf"

# Choices instead of typed numbers, so a controller can pick them.
HOURS = (1, 2, 5, 10, 20, 50, 100)
DAYS = (7, 14, 30, 60, 90, 180, 365)
GIGABYTES = (1, 5, 10, 25, 50, 100)
LENGTHS = (2, 5, 10, 20, 40, 80)
_NUMBERS = {'hours': HOURS, 'days': DAYS, 'gb': GIGABYTES, 'length': LENGTHS}
_UNITS = {'hours': "h", 'length': "h", 'days': "days", 'gb': "GB"}
_DEFAULTS = {'hours': 5, 'days': 30, 'gb': 25, 'length': 10}
STORES = (('nexus', "Repacks"), ('steam', "Steam"), ('heroic', "Heroic"), ('epic', "Epic"))
MODE_NAMES = (('single', "Single-player"), ('coop', "Co-op"), ('online', "Online"),
              ('local', "Local multiplayer"))
_NAMES = dict(STORES) | dict(MODE_NAMES)

# field -> (label, ((op, op label, value kind or None), ...), needs facts).
# Value kinds: 'hours', 'days', 'gb', 'length' (numbers from the lists
# above), 'store', 'genre', 'mode' (names). The first op is the default.
FIELDS = {
    'playtime': ("Play time", (('none', "none yet", None), ('some', "any", None),
                               ('atleast', "at least", 'hours'), ('under', "under", 'hours')),
                 False),
    'last': ("Last played", (('within', "in the last", 'days'),
                             ('over', "not in the last", 'days')), False),
    'added': ("Added", (('within', "in the last", 'days'),
                        ('over', "more than", 'days')), False),
    'store': ("Store", (('is', "is", 'store'), ('not', "is not", 'store')), False),
    'size': ("Size on disk", (('over', "over", 'gb'), ('under', "under", 'gb')), False),
    'favorite': ("Favourite", (('yes', "yes", None), ('no', "no", None)), False),
    'genre': ("Genre", (('is', "is", 'genre'), ('not', "is not", 'genre')), True),
    'mode': ("Mode", (('has', "has", 'mode'), ('lacks', "doesn't have", 'mode')), True),
    'length': ("Main story", (('under', "under", 'length'), ('over', "over", 'length')), True),
}


def is_user(key) -> bool:
    return isinstance(key, str) and key.startswith(USER) and len(key) > len(USER)


def ops(field: str) -> tuple:
    """((op, label, value kind), ...) for a field."""
    return FIELDS[field][1] if field in FIELDS else ()


def _op(field, op):
    return next((o for o in ops(field) if o[0] == op), None)


def value_kind(field: str, op: str) -> str | None:
    o = _op(field, op)
    return o[2] if o else None


def default_value(kind):
    return _DEFAULTS.get(kind)


def default_rule(field: str) -> dict:
    op, _label, kind = ops(field)[0]
    return {'field': field, 'op': op, 'value': default_value(kind)}


def clean_rule(r) -> dict | None:
    """A saved rule made safe: an unknown field or op drops it; a value of
    the wrong type becomes None (the rule then waits for one)."""
    if not isinstance(r, dict):
        return None
    o = _op(r.get('field'), r.get('op'))
    if o is None:
        return None
    kind, value = o[2], r.get('value')
    if kind is None:
        value = None
    elif kind in _NUMBERS:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        value = value if ok else None
    elif not isinstance(value, str) or not value:
        value = None
    return {'field': r['field'], 'op': o[0], 'value': value}


def clean_name(name) -> str:
    return ' '.join(str(name or '').split())[:MAX_NAME]


def clean_shelf(d) -> dict | None:
    """A saved shelf made safe, or None when it can't be one (no usable id)."""
    if not isinstance(d, dict):
        return None
    sid = str(d.get('id') or '').strip()
    if not sid or len(sid) > 32 or not sid.replace('-', '').isalnum():
        return None
    rules = d.get('rules') if isinstance(d.get('rules'), list) else []
    return {'id': sid, 'name': clean_name(d.get('name')),
            'match': 'any' if d.get('match') == 'any' else 'all',
            'rules': [c for c in map(clean_rule, rules) if c][:MAX_RULES],
            'removed': d.get('removed') is True}


def clean_shelves(items) -> list[dict]:
    """library.json's `smart_shelves`: each cleaned, ids unique, at most
    MAX_USER_SHELVES (removed ones count: they are kept)."""
    out, seen = [], set()
    for d in items if isinstance(items, list) else ():
        s = clean_shelf(d)
        if s and s['id'] not in seen:
            seen.add(s['id'])
            out.append(s)
    return out[:MAX_USER_SHELVES]


def title_of(shelf: dict) -> str:
    return shelf.get('name') or UNTITLED


def rule_ready(rule: dict) -> bool:
    """A rule counts once it has the value its op needs."""
    o = _op(rule.get('field'), rule.get('op'))
    return o is not None and (o[2] is None or rule.get('value') not in (None, ''))


def rule_match(rule: dict, g: dict, fact: dict | None, now: datetime) -> bool:
    """One ready rule against one game. A rule on store facts (genre, mode,
    main story) never matches a game the facts index knows nothing about,
    whichever way it is put: Nexus doesn't claim what it doesn't know."""
    field, op, value = rule.get('field'), rule.get('op'), rule.get('value')
    if field == 'playtime':
        mins = g.get('playtime_minutes') or 0
        if op in ('none', 'some'):
            return (bool(mins) or bool(g.get('last_played'))) == (op == 'some')
        return mins >= value * 60 if op == 'atleast' else mins < value * 60
    if field in ('last', 'added'):
        t = _when(g.get('last_played' if field == 'last' else 'added_at'))
        if t is None:
            return False
        days = (now - t).total_seconds() / 86400
        return days <= value if op == 'within' else days > value
    if field == 'store':
        return (g.get('source', 'nexus') == value) == (op == 'is')
    if field == 'size':
        b = g.get('disk_bytes')
        if not isinstance(b, (int, float)) or b <= 0:
            return False
        return b / 1e9 > value if op == 'over' else b / 1e9 < value
    if field == 'favorite':
        return bool(g.get('favorite')) == (op == 'yes')
    if field == 'genre':
        genres = (fact or {}).get('genres')
        return bool(genres) and (value in genres) == (op == 'is')
    if field == 'mode':
        modes = (fact or {}).get('modes')
        return modes is not None and (value in modes) == (op == 'has')
    if field == 'length':
        main = main_hours(fact)
        return main is not None and (main < value if op == 'under' else main > value)
    return False


def user_games(shelf: dict, games: list[dict], facts: dict,
               now: datetime | None = None) -> list[dict]:
    """The games on a user shelf, most recently played first. Rules still
    waiting for a value are left out; a shelf with no ready rule is empty
    rather than the whole library."""
    now = now or datetime.now()
    facts = facts or {}
    rules = [r for r in shelf.get('rules') or () if rule_ready(r)]
    if not rules:
        return []
    join = any if shelf.get('match') == 'any' else all
    hit = [g for g in games
           if join(rule_match(r, g, facts.get(g.get('slug')), now) for r in rules)]
    return _order('user', hit, facts)


def fields_offered(games: list[dict], facts: dict) -> list[str]:
    """The fields the builder offers for this library: Nexus's own data
    always (Added and Size once some game has them), and a store fact only
    once the facts index has it for at least one of your games."""
    facts = facts or {}
    recs = [facts.get(g.get('slug')) or {} for g in games]
    known = {
        'added': any(g.get('added_at') for g in games),
        'size': any(isinstance(g.get('disk_bytes'), (int, float)) and g['disk_bytes'] > 0
                    for g in games),
        'genre': any(r.get('genres') for r in recs),
        'mode': any(r.get('modes') for r in recs),
        'length': any(main_hours(r) is not None for r in recs),
    }
    return [f for f in FIELDS if known.get(f, True)]


def values_for(kind: str, games: list[dict], facts: dict, current=None) -> list[tuple]:
    """(value, label) choices for a value kind. Stores, genres and modes are
    the ones your library has; a rule's current value stays offered even
    when no game has it any more."""
    facts = facts or {}
    if kind in _NUMBERS:
        out = [(n, f"{n} {_UNITS[kind]}") for n in _NUMBERS[kind]]
    elif kind == 'store':
        have = {g.get('source', 'nexus') for g in games}
        out = [(k, t) for k, t in STORES if k in have]
    elif kind in ('genre', 'mode'):
        key = 'genres' if kind == 'genre' else 'modes'
        have = {x for g in games for x in (facts.get(g.get('slug')) or {}).get(key) or ()}
        names = [(x, x) for x in NEXUS_GENRES] if kind == 'genre' else MODE_NAMES
        out = [(k, t) for k, t in names if k in have]
    else:
        return []
    if current not in (None, '') and current not in [v for v, _t in out]:
        out.append((current, _NAMES.get(current, str(current))))
    return out


def describe_rule(rule: dict) -> str:
    o = _op(rule.get('field'), rule.get('op'))
    if o is None:
        return ''
    text, kind, value = f"{FIELDS[rule['field']][0]} {o[1]}", o[2], rule.get('value')
    if kind is None:
        return text
    if value in (None, ''):
        return text + " …"
    if kind in _NUMBERS:
        return f"{text} {value:g} {_UNITS[kind]}"
    return f"{text} {_NAMES.get(value, value)}"


def describe(shelf: dict) -> str:
    """The shelf's rules as one line, for its tooltip."""
    parts = [describe_rule(r) for r in shelf.get('rules') or () if rule_ready(r)]
    if not parts:
        return "No rules yet"
    return (" or " if shelf.get('match') == 'any' else " and ").join(parts)


def new_shelf(sid: str, fields=('playtime',), name: str = '') -> dict:
    """A fresh shelf with one rule on the first offered field."""
    return {'id': sid, 'name': clean_name(name), 'match': 'all',
            'rules': [default_rule(fields[0] if fields else 'playtime')], 'removed': False}


def find_user(shelves: list[dict], key) -> dict | None:
    """The shelf for a `user:<id>` key (a removed one too)."""
    if not is_user(key):
        return None
    return next((s for s in shelves if s.get('id') == key[len(USER):]), None)
