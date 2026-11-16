"""What the Deals home shows, without Qt (w-146).

Plain functions over the dicts the Deals page already holds, so the tests
read this module, not pixels.

Wishlist item (the `--gamedb --json` wishlist entries, as `DealCard` takes):
    {'appid': 10, 'title': str, 'owned': bool?, 'genres': [str]?,
     'price': {'current': float|None, 'onSale': bool, 'cutPct': int,
               'atHistoricalLow': bool},
     'reviews': {'steamPercent': int, 'metacritic': int},
     'hltb': {'main': hours}}
`current` is in major units (9.99), so a currency without minor units
(JPY, KRW, ...) scales the "under 10" line by 100.

Taste (from smart.top_genres plus play time): [(genre, weight, top_title)],
heaviest first; `top_title` is the game you play most in that genre.

Catalogue row (`repackindex.catalogue`):
    {'slug', 'title', 'tail', 'date': 'YYYY-MM-DD', 'hv': bool, 'older': [..],
     'keys': [normalised title keys], 'marks': ['wishlist'|'nexus'|'steam']}
"""

import re

SHELF_CAP = 12
CATALOGUE_CAP = 100
FOR_YOU_COUNT = 3
UNDER_LINE = 10
SHORT_HOURS = 10
BIG_CUT = 75
NO_DECIMAL = frozenset({'JPY', 'KRW', 'VND', 'IDR', 'CLP', 'ISK', 'HUF', 'COP', 'PYG', 'UGX'})
FALLBACK_REASON = "Top rated on your wishlist, at its lowest ever"
FALLBACK_REASON_PLAIN = "Top rated on your wishlist"


def _price(g: dict) -> dict:
    return g.get('price') or {}


def _review(g: dict) -> int:
    return (g.get('reviews') or {}).get('steamPercent') or 0


def _on_sale(g: dict) -> bool:
    p = _price(g)
    return bool(p.get('onSale')) and p.get('current') is not None


def under_threshold(currency: str) -> float:
    """The "Under 10" line in the shop's own units."""
    return UNDER_LINE * (100 if (currency or '').upper() in NO_DECIMAL else 1)


def _deal_rank(g: dict):
    """Best deal first: at its low, biggest cut, best reviews."""
    p = _price(g)
    return (not p.get('atHistoricalLow'), -(p.get('cutPct') or 0), -_review(g))


def _shelf(key: str, title: str, sub: str, items: list[dict]) -> dict:
    ranked = sorted(items, key=_deal_rank)
    return {'key': key, 'title': title, 'sub': sub, 'count': len(ranked),
            'items': ranked[:SHELF_CAP]}


SHELVES = (('atl', "All-time lows", "At their lowest price ever"),
           ('under', "Under 10", "Priced below 10"),
           ('big', f"{BIG_CUT}% off or more", "The deepest cuts"),
           ('short', f"Short, under {SHORT_HOURS} h", "Quick to finish, by time to beat"))


def in_shelf(g: dict, key: str, currency: str = '') -> bool:
    """Whether a wishlist game belongs on that shelf (on sale, priced). The
    See all grid filters with this too."""
    if not _on_sale(g):
        return False
    p = _price(g)
    if key == 'atl':
        return bool(p.get('atHistoricalLow'))
    if key == 'under':
        return p['current'] < under_threshold(currency)
    if key == 'big':
        return (p.get('cutPct') or 0) >= BIG_CUT
    if key == 'short':
        return 0 < ((g.get('hltb') or {}).get('main') or 0) < SHORT_HOURS
    return False


def shelves(items: list[dict], currency: str = '', hide_owned: bool = False) -> list[dict]:
    """The four shelves, always in this order, each
    {key, title, sub, count, items (best first, capped)}. Only games on sale
    count; owned ones go when `hide_owned`."""
    pool = [g for g in items if not (hide_owned and g.get('owned'))]
    return [_shelf(key, title, sub, [g for g in pool if in_shelf(g, key, currency)])
            for key, title, sub in SHELVES]


def taste(games: list[dict], facts_games: dict) -> list[tuple]:
    """[(genre, weight, top_title)] for For you, heaviest first: the Nexus
    genres smart.top_genres finds in your library, weighted by how many of
    your games have it plus the hours you played them; top_title is your
    most-played game of that genre."""
    from nexus import smart
    out = []
    for genre, n in smart.top_genres(games, facts_games or {}):
        mine = [g for g in games
                if genre in ((facts_games or {}).get(g.get('slug')) or {}).get('genres', ())]
        minutes = sum(g.get('playtime_minutes') or 0 for g in mine)
        top = max(mine, key=lambda g: g.get('playtime_minutes') or 0, default=None)
        title = (top.get('display_name') or top.get('title') or '') \
            if top and top.get('playtime_minutes') else ''
        out.append((genre, n * 60 + minutes, title))
    out.sort(key=lambda t: -t[1])
    return out


def for_you(items: list[dict], taste: list[tuple] | None = None,
            hide_owned: bool = True) -> list[dict]:
    """Up to three picks [{item, reason}] from games on sale. Each is matched
    to the heaviest genre it shares with your taste ("Because you play a lot
    of X (Top Title)"); what is left is filled with the top rated sale at its
    lowest ever (the fallback also covers an empty taste)."""
    pool = [g for g in items if _on_sale(g) and not (hide_owned and g.get('owned'))]
    weights = {genre: (i, w, top) for i, (genre, w, top) in enumerate(taste or ())}
    matched = []
    for g in pool:
        shared = [(weights[x][0], x) for x in (g.get('genres') or ()) if x in weights]
        if shared:
            genre = min(shared)[1]
            matched.append((weights[genre][0], _deal_rank(g), g, genre))
    matched.sort(key=lambda t: (t[0], t[1]))
    picks, seen = [], set()
    for _i, _rank, g, genre in matched:
        if len(picks) == FOR_YOU_COUNT:
            break
        top = weights[genre][2]
        reason = f"Because you play a lot of {genre}" + (f" ({top})" if top else "")
        picks.append({'item': g, 'reason': reason})
        seen.add(id(g))
    rest = sorted((g for g in pool if id(g) not in seen),
                  key=lambda g: (-_review(g), _deal_rank(g)))
    for g in rest:
        if len(picks) == FOR_YOU_COUNT:
            break
        low = _price(g).get('atHistoricalLow')
        picks.append({'item': g, 'reason': FALLBACK_REASON if low else FALLBACK_REASON_PLAIN})
    return picks


def _norm(text: str) -> str:
    return re.sub(r'[^0-9a-z]+', ' ', (text or '').casefold()).strip()


def catalogue_match(rows: list[dict], query: str = '', hide_have: bool = False,
                    hide_hv: bool = False, sort: str = 'new') -> dict:
    """Filter the catalogue: {rows (capped at 100), total (before the cap)}.
    Every word of the query must appear in the title or one of the row's
    keys. `hide_have` drops what is on Nexus or Steam, `hide_hv` repacks that
    need a hypervisor. sort: 'new' (newest post first, the default) or 'name'."""
    words = _norm(query).split()
    out = []
    for r in rows:
        marks = r.get('marks') or ()
        if hide_have and ('nexus' in marks or 'steam' in marks):
            continue
        if hide_hv and r.get('hv'):
            continue
        if words:
            hay = ' '.join([_norm(r.get('title'))] + [_norm(k) for k in r.get('keys') or ()])
            if not all(w in hay for w in words):
                continue
        out.append(r)
    if sort == 'name':
        out.sort(key=lambda r: _norm(r.get('title')))
    else:
        out.sort(key=lambda r: r.get('date') or '', reverse=True)
    return {'rows': out[:CATALOGUE_CAP], 'total': len(out)}
