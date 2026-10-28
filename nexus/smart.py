"""Smart shelves: Library rows that sort themselves from your play data and
the facts index (nexus/facts.py). Pure functions over plain dicts, so the
Library, the Home widget and the status filter share one set of rules.

A game is a library dict (slug, last_played as a local ISO time,
playtime_minutes, added_at, installed, protondb); `facts` is the index's
`games` ({slug: record}). Keys: `smart:<rule>` for the fixed rules and
`genre:<Nexus genre>` for the genre shelves (a game page's genre chip sends
the same key)."""

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
