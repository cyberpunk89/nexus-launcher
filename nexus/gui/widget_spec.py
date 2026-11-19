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
              'bg': ("Background", "What's behind the Stage"),
              'lead': ("Shows", "What the card leads with")}


def span(size: str) -> int:
    """Columns a size covers in a three-column Home (full-width sizes: 3)."""
    return {'small': 1, 'tall': 1, 'wide': 2}.get(size, 3)


# The Customise gallery's groups (w-158), in the order it lists them.
GROUPS = (('stage', "Stage"), ('play', "Play"), ('social', "Social"), ('library', "Library"),
          ('stats', "Stats"), ('store', "Store"))


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
    grow_sizes: tuple = ()            # grows only at these sizes (all of them when empty)
    group: str = 'library'            # the Customise gallery's group (GROUPS)
    note: str = ''                    # what a merged widget took in, shown on its gallery tile


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
               options={'style': _STYLE['stage'], 'bg': _BG}, group='stage'),
    WidgetSpec('save', "Your save", ('small',), 'small', True, after='stage', group='stage'),
    WidgetSpec('foryou', "For you now", ('full',), 'full',
               options={'style': _STYLE['foryou']}, after='stage',
               feeds=('stage', 'friends', 'system', 'deals', 'updates', 'smart'),
               group='stage'),
    WidgetSpec('friends', "Friends", ('small', 'wide', 'full'), 'full',
               options={'lead': (('auto', "Best now"), ('now', "Only playing now"))},
               after='stage', grows=True, group='social'),
    WidgetSpec('pick', "Pick for me", ('small',), 'small',
               options={'vibe': VIBES, 'style': _STYLE['pick']}, after='stage',
               feeds=('midnight', 'insights'), group='play',
               note="Now has Today's pick (was Game of the day)"),
    WidgetSpec('system', "System", ('small', 'wide'), 'wide', group='library'),
    WidgetSpec('news', "News", ('small', 'tall', 'wide', 'full'), 'small', grows=True,
               group='store'),
    WidgetSpec('installed', "Recently installed", ('full',), 'full', group='library'),
    WidgetSpec('smart', "Smart shelf", ('full',), 'full', after='installed', group='library',
               note="Owned, not installed is a rule now"),
    # One ranked list of wishlist sales and releases (w-163); it was Wishlist
    # deals and Upcoming. The kind stays 'deals'.
    WidgetSpec('deals', "Wishlist", ('small', 'tall', 'wide', 'full'), 'wide',
               grows=True, grow_sizes=('small', 'tall'), feeds=('friends',),
               group='store', note="Deals and Upcoming, now one card"),
    WidgetSpec('week', "Your week", ('small', 'wide'), 'small',
               options={'style': _STYLE['week']}, after='deals',
               feeds=('midnight', 'sessions'), group='stats'),
    WidgetSpec('favourites', "Favourites", ('full',), 'full', group='library'),
    WidgetSpec('collections', "Collections", ('full',), 'full', after='favourites',
               group='library'),
    WidgetSpec('updates', "Updates ready", ('full',), 'full', group='library'),
    # The insight widgets (w-139, w-140): read the insights payload; last, hidden,
    # so a saved layout is unchanged and they wait in Add widgets.
    WidgetSpec('onthisday', "On this day", ('small', 'wide'), 'small', True,
               feeds=('midnight', 'insights'), group='play'),
    WidgetSpec('heatmap', "Your year", ('wide', 'full'), 'full', True,
               feeds=('midnight', 'insights'), group='stats',
               note="Takes the best Library fact"),
    WidgetSpec('milestones', "Next milestones", ('small', 'wide'), 'small', True,
               feeds=('insights',), grows=True, group='stats'),
    # The Stats widgets (w-141): the same, with the library parts of the payload.
    WidgetSpec('backlog', "Backlog health", ('small', 'wide'), 'small', True,
               feeds=('insights', 'library'), group='stats'),
    WidgetSpec('spacemap', "Where your space goes", ('wide', 'full'), 'wide', True,
               feeds=('insights', 'library'), grows=True, group='stats'),
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
