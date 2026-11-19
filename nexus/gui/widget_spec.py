"""One spec per Home widget (w-136): the tables Home reads are derived from
SPECS, so adding a widget is one WidgetSpec here plus its DashWidget class.
No Qt imports."""

from dataclasses import dataclass, field

# Pick for me's vibe chips (w-91): (key, chip label). Every key but 'any' is
# a smart rule (nexus/smart.py), so a chip only shows when that rule has games.
VIBES = (('any', "Any"), ('short', "Quick session"), ('coop', "Co-op"),
         ('unfinished', "Unfinished"), ('almost', "Almost done"), ('never', "Never played"))


# The options the edit bar cycles with a button (w-137): key -> (button
# prefix, tooltip). An option of a spec with a key here gets its button with
# no edit-bar code; one without (Pick's vibe) is changed on the card itself.
OPTION_BAR = {'style': ("Style", "Change how this shows"),
              'bg': ("Background", "What's behind the Stage")}


@dataclass(frozen=True)
class WidgetSpec:
    kind: str
    title: str
    sizes: tuple
    default: str
    hidden: bool = False
    # {option key: ((value, label), ...)}; the first value is the default.
    options: dict = field(default_factory=dict)
    after: str | None = None        # a saved layout without this widget puts it next to that one
    # "Refresh me when any of these refresh": kinds and events (midnight,
    # hour, sessions, library, insights) are one namespace (w-138).
    feeds: tuple = ()
    needs: str | None = None
    card: str | None = None
    # Packed grid (w-155): given more height than it needs (a short column), the
    # card shows more rows instead of empty space.
    grows: bool = False


_STYLE = {
    'stage': (('row', "Row on art"), ('clean', "Clean")),
    'week': (('bars', "Bars"), ('number', "Number only")),
    'foryou': (('stage', "On stage"), ('strip', "Strip")),
    # 'today' (w-164): Game of the day's face, one game a day with Play and Another day.
    'pick': (('chips', "Chips"), ('wheel', "Wheel"), ('today', "Today's pick")),
}
_BG = (('art', "Game art"), ('pinned', "Pinned art"), ('gradient', "Accent gradient"),
       ('surface', "Plain"))

# Default order packs evenly in 3 columns.
SPECS = (
    WidgetSpec('stage', "Stage", ('cinematic', 'wide', 'banner'), 'cinematic',
               options={'style': _STYLE['stage'], 'bg': _BG}),
    WidgetSpec('save', "Your save", ('small',), 'small', True, after='stage'),
    WidgetSpec('foryou', "For you now", ('full',), 'full',
               options={'style': _STYLE['foryou']}, after='stage',
               feeds=('stage', 'friends', 'system', 'deals', 'upcoming', 'updates', 'smart')),
    WidgetSpec('friends', "Friends playing now", ('small', 'wide', 'full'), 'full',
               after='stage', grows=True),
    WidgetSpec('pick', "Pick for me", ('small',), 'small',
               options={'vibe': VIBES, 'style': _STYLE['pick']}, after='stage',
               feeds=('midnight', 'insights')),
    WidgetSpec('system', "System", ('small', 'wide'), 'wide'),
    WidgetSpec('news', "News", ('small', 'tall', 'wide', 'full'), 'small', grows=True),
    WidgetSpec('installed', "Recently installed", ('full',), 'full'),
    WidgetSpec('smart', "Smart shelf", ('full',), 'full', after='installed'),
    WidgetSpec('deals', "Wishlist deals", ('small', 'tall', 'wide', 'full'), 'wide',
               grows=True),
    WidgetSpec('upcoming', "Upcoming", ('small', 'tall', 'wide', 'full'), 'small', after='deals',
               grows=True),
    WidgetSpec('week', "Your week", ('small', 'wide'), 'small',
               options={'style': _STYLE['week']}, after='upcoming',
               feeds=('midnight', 'sessions')),
    WidgetSpec('favourites', "Favourites", ('full',), 'full'),
    WidgetSpec('collections', "Collections", ('full',), 'full', after='favourites'),
    WidgetSpec('updates', "Updates ready", ('full',), 'full'),
    # The insight widgets (w-139, w-140): read the insights payload; last, hidden,
    # so a saved layout is unchanged and they wait in Add widgets.
    WidgetSpec('onthisday', "On this day", ('small', 'wide'), 'small', True,
               feeds=('midnight', 'insights')),
    WidgetSpec('heatmap', "Your year", ('wide', 'full'), 'full', True,
               feeds=('midnight', 'insights')),
    WidgetSpec('milestones', "Next milestones", ('small', 'wide'), 'small', True,
               feeds=('insights',), grows=True),
    # The Stats widgets (w-141): the same, with the library parts of the payload.
    WidgetSpec('backlog', "Backlog health", ('small', 'wide'), 'small', True,
               feeds=('insights', 'library')),
    WidgetSpec('spacemap', "Where your space goes", ('wide', 'full'), 'wide', True,
               feeds=('insights', 'library'), grows=True),
)
SPEC = {s.kind: s for s in SPECS}


def dash_tables(specs) -> dict:
    """The tables Home reads, derived from the specs (in their order)."""
    opts, styles, bgs = {}, {}, {}
    for s in specs:
        if s.options:
            opts[s.kind] = {k: tuple(v for v, _l in pairs) for k, pairs in s.options.items()}
        if 'style' in s.options:
            styles[s.kind] = dict(s.options['style'])
        if 'bg' in s.options:
            bgs[s.kind] = dict(s.options['bg'])
    fed_by: dict[str, set] = {}
    for s in specs:
        for f in s.feeds:
            fed_by.setdefault(f, set()).add(s.kind)
    # PLACE_AFTER keyed by the widget that moves next to its anchor (`after`).
    return {
        'KINDS': [(s.kind, s.sizes, s.default, s.hidden) for s in specs],
        'SIZES': {s.kind: s.sizes for s in specs},
        'DEFAULT_SIZE': {s.kind: s.default for s in specs},
        'OPTIONS': opts,
        'STYLE_LABELS': styles,
        'BG_LABELS': bgs,
        'PLACE_AFTER': {s.kind: s.after for s in specs if s.after},
        'FED_BY': fed_by,                     # {event or kind: the kinds it refreshes}
    }
