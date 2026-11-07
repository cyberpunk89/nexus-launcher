"""Home's presets and saved layouts (w-92): pure, no Qt.

A preset is code: `dash_preset(name)` lists the widgets it shows, in order,
and every other widget hidden. A saved layout is data in library.json
(`dashboard.layouts`), cleaned on load by `clean_layouts`; `layout_for`
picks the one whose daily time window holds a wall-clock time.

dashboard.py imports this module, so `dash_normalize` and the widget table
are imported inside the functions."""

import re
from datetime import datetime

MAX_LAYOUTS = 12                 # saved layouts, at most
NAME_MAX = 40                    # a layout's name, at most (the prompt allows 32)
PROMPT_MAX = 32
OLD_HOME = "My old Home"         # dashboard_v1 offered back as a layout
MY_HOME = "My Home"              # an unnamed Home the clock had to leave
DAY = 24 * 60

# (key, label, one-line tooltip)
PRESETS = (
    ('cinematic', "Cinematic", "The Stage, For you now on it, a small row and one shelf"),
    ('dashboard', "Dashboard", "A wide Stage with Your save, System, News and Deals; compact"),
    ('minimal', "Minimal", "A banner Stage, the For you now strip and Continue"),
    ('shelves', "Shelf-first", "No Stage: your shelves, compact"),
)
PRESET_LABELS = {k: name for k, name, _tip in PRESETS}

# The widgets each preset shows, in order: (id, size, extra keys). Every
# preset uses Game art, Row on art, no pin, and shelf rules that exist on
# every platform (never `linux`).
_STAGE = {'style': 'row', 'bg': 'art'}
_PRESET_ITEMS = {
    'cinematic': [('stage', 'cinematic', _STAGE), ('foryou', 'full', {'style': 'stage'}),
                  ('week', 'small', {}), ('pick', 'small', {}), ('friends', 'small', {}),
                  ('smart', 'full', {'shelf': 'unfinished'})],
    'dashboard': [('stage', 'wide', _STAGE), ('save', 'small', {}),
                  ('foryou', 'full', {'style': 'strip'}), ('system', 'wide', {}),
                  ('week', 'small', {}), ('friends', 'small', {}), ('pick', 'small', {}),
                  ('news', 'small', {}), ('deals', 'wide', {}),
                  ('smart', 'full', {'shelf': 'unfinished'})],
    'minimal': [('stage', 'banner', _STAGE), ('foryou', 'full', {'style': 'strip'}),
                ('smart', 'full', {'shelf': 'continue'})],
    'shelves': [('foryou', 'full', {'style': 'strip'}), ('smart', 'full', {'shelf': 'unfinished'}),
                ('installed', 'full', {}), ('favourites', 'full', {}),
                ('collections', 'full', {}), ('updates', 'full', {})],
}
# Hidden but placed first, so it comes back where it belongs (Shelf-first's Stage).
_PRESET_HIDDEN_FIRST = {'shelves': [('stage', 'cinematic', _STAGE)]}
PRESET_DENSITY = {'cinematic': 'comfortable', 'dashboard': 'compact',
                  'minimal': 'comfortable', 'shelves': 'compact'}


def dash_preset(name: str) -> list[dict]:
    """A preset's full widget list: the ones it shows in order, then every
    other widget hidden (DASH_KINDS order), normalised."""
    from .dashboard import DASH_DEFAULT_SIZE, DASH_KINDS, dash_normalize
    spec = _PRESET_ITEMS.get(name, _PRESET_ITEMS['cinematic'])
    first = _PRESET_HIDDEN_FIRST.get(name, [])
    out = [{'id': k, 'size': s, 'hidden': True, **extra} for k, s, extra in first]
    out += [{'id': k, 'size': s, 'hidden': False, **extra} for k, s, extra in spec]
    seen = {i['id'] for i in out}
    out += [{'id': k, 'size': DASH_DEFAULT_SIZE[k], 'hidden': True}
            for k, *_rest in DASH_KINDS if k not in seen]
    return dash_normalize(out)


# ---- names and rules ----
def clean_name(name) -> str | None:
    """A layout's name stripped, 1-NAME_MAX characters, else None."""
    if not isinstance(name, str):
        return None
    name = ' '.join(name.split())
    return name if 0 < len(name) <= NAME_MAX and name.isprintable() else None


def unique_name(layouts, name: str) -> str:
    """`name`, or `name 2`, `name 3`… when another layout has it (any case)."""
    taken = {lt['name'].casefold() for lt in layouts}
    base = name[:NAME_MAX]
    n = 2
    while name.casefold() in taken:
        tail = f" {n}"
        name = base[:NAME_MAX - len(tail)] + tail
        n += 1
    return name


_HHMM = re.compile(r'([01]\d|2[0-3]):([0-5]\d)')


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def hhmm(mins: int) -> str:
    mins %= DAY
    return f"{mins // 60:02d}:{mins % 60:02d}"


def clean_rule(rule) -> dict | None:
    """A daily window {'from': 'HH:MM', 'to': 'HH:MM'} (24 h, zero-padded,
    from != to; it may cross midnight), keys a newer Nexus added kept; else
    None."""
    if not isinstance(rule, dict):
        return None
    a, b = rule.get('from'), rule.get('to')
    if not (isinstance(a, str) and isinstance(b, str) and _HHMM.fullmatch(a)
            and _HHMM.fullmatch(b)) or a == b:
        return None
    return dict(rule)


def rule_text(rule) -> str:
    """'from 20:00 to 06:00', or '' without a rule."""
    rule = clean_rule(rule)
    return f"from {rule['from']} to {rule['to']}" if rule else ''


def _spans(rule) -> list[tuple[int, int]]:
    a, b = minutes(rule['from']), minutes(rule['to'])
    return [(a, b)] if a < b else [(a, DAY), (0, b)]


def overlaps(rule, others) -> list[tuple[str, dict]]:
    """The (name, rule) pairs in `others` whose window shares a minute with
    `rule`'s."""
    rule = clean_rule(rule)
    if rule is None:
        return []
    mine = _spans(rule)
    out = []
    for name, other in others:
        other = clean_rule(other)
        if other and any(a < d and c < b for a, b in mine for c, d in _spans(other)):
            out.append((name, other))
    return out


def layout_for(layouts, now: datetime) -> str | None:
    """The layout whose rule holds `now`'s wall-clock time of day (a naive
    local time; no date arithmetic, so DST changes need nothing). From
    before To: from <= t < to; crossing midnight: t >= from or t < to.
    Several: the window that started most recently, a tie to the earlier
    layout. None when no rule holds."""
    t = now.hour * 60 + now.minute
    best = None
    for lt in layouts or ():
        rule = clean_rule(lt.get('rule')) if isinstance(lt, dict) else None
        if rule is None:
            continue
        a, b = minutes(rule['from']), minutes(rule['to'])
        if a < b:
            hit, since = a <= t < b, t - a
        else:
            hit, since = (t >= a or t < b), (t - a if t >= a else t + DAY - a)
        if hit and (best is None or since < best[0]):
            best = (since, lt['name'])
    return best[1] if best else None


# ---- the saved list ----
def clean_layouts(raw) -> list[dict]:
    """Saved layouts made safe (never raises): a list of dicts, at most
    MAX_LAYOUTS; each named (unique ignoring case; a later duplicate is
    dropped) with a non-empty `items` list (normalised); `density` kept
    when valid; `rule` when valid (else dropped, the layout kept); `auto`
    when True. Keys a newer Nexus added are kept."""
    from .dashboard import DASH_DENSITY, dash_normalize
    out, seen = [], set()
    for lt in raw if isinstance(raw, list) else ():
        if len(out) >= MAX_LAYOUTS:
            break
        if not isinstance(lt, dict):
            continue
        name = clean_name(lt.get('name'))
        items = lt.get('items')
        if name is None or name.casefold() in seen or not (isinstance(items, list) and items):
            continue
        seen.add(name.casefold())
        new = {k: v for k, v in lt.items() if k not in ('name', 'items', 'density', 'rule',
                                                         'auto')}
        new['name'] = name
        new['items'] = dash_normalize(items)
        dens = lt.get('density')
        if isinstance(dens, str) and dens in DASH_DENSITY:
            new['density'] = dens
        rule = clean_rule(lt.get('rule'))
        if rule is not None:
            new['rule'] = rule
        if lt.get('auto') is True:
            new['auto'] = True
        out.append(new)
    return out


def find_layout(layouts, name) -> dict | None:
    """The layout called `name` (ignoring case), or None."""
    if not isinstance(name, str):
        return None
    key = name.casefold()
    return next((lt for lt in layouts if lt['name'].casefold() == key), None)


def same_layout(lt: dict, items, density: str) -> bool:
    return lt['items'] == items and lt.get('density', density) == density
