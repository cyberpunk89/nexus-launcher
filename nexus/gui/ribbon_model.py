"""Theatre's Ribbon layout (w-108): what it shows, without Qt.

`categories(feed)` turns what Theatre already holds (its games, the Library
sort and filter, Your week, friends playing, the Theatre settings, which
tabs are on) into the Ribbon's line of categories, each with its column of
items. `nav` moves through them. `RibbonView` (ribbon.py) only paints the
result, so the tests read this module, not pixels."""

from dataclasses import dataclass, field, replace

KINDS = ('game', 'card', 'page', 'setting', 'action', 'folder', 'empty')
HOME_PLAYED = 8                       # Jump back in: this many recently played games

# The glyph of each Theatre setting on the Ribbon (theatre_settings.ROWS).
SETTING_GLYPH = {'theatre_layout': 'grid', 'ribbon_motion': 'sparkles', 'pad_a': 'gamepad',
                 'theatre_on_pad': 'gamepad', 'theatre_start': 'tv', 'attract': 'moon',
                 'week_home': 'calendar', 'sounds': 'bell', 'volume': 'activity',
                 'theme': 'palette', 'art_tint': 'image'}

NO_GAMES = "No games yet"
NO_GAMES_SUB = "Add a folder or sign in to a store in All settings."


@dataclass(frozen=True)
class Item:
    """One entry in a category's column. `key` is stable across refreshes
    ('game:<slug>', 'set:<key>', 'page:discover', 'empty:library'), so focus
    stays on it. `primary` is what A does: open, play, resume, step,
    tab:<key>, sheet, all_settings, or none."""
    key: str
    kind: str                         # one of KINDS
    title: str
    sub: str = ''                     # a line under the title (rich text for games)
    slug: str | None = None           # the game it stands for (art, its page)
    glyph: str | None = None          # a G key, for items without art
    primary: str = 'open'
    value: object = None              # a setting's value
    choices: tuple = ()               # a setting's ((value, text), ...)
    kicker: str = ''                  # small caps over the title: 'NOW PLAYING'
    children: tuple = ()              # a folder's own column (later phases)

    @property
    def value_text(self) -> str:
        """A setting's value as words (the first choice for an unknown one)."""
        return dict(self.choices).get(self.value, self.choices[0][1]) if self.choices else ''


@dataclass(frozen=True)
class Category:
    key: str                          # home, library, discover, downloads or settings
    title: str
    glyph: str
    items: tuple
    sub: str = ''                     # under its name: "Installed · A to Z · 48 games"
    live: bool = False                # a game is running (a dot on Home's icon)


@dataclass
class Feed:
    """What Theatre hands the model. Games are the library's dicts."""
    games: list = field(default_factory=list)          # installed games
    library: list = field(default_factory=list)        # the Library's games, sorted
    lib_groups: list | None = None                     # By collection: [(title, games)]
    lib_label: str = ''                                # "Installed · A to Z"
    lib_show_label: str = 'Installed'
    playing: dict | None = None                        # the game running or starting
    playing_kicker: str = 'NOW PLAYING'
    week: dict | None = None                           # {'sub', 'slug'}: Your week, shown
    friends: list = field(default_factory=list)        # [{'key', 'title', 'sub', 'slug'}]
    settings: list = field(default_factory=list)       # [(key, title, choices, value, enabled)]
    discover: bool = True                              # the tabs' gates (sync_tabs)
    downloads: bool = True
    downloads_sub: str = ''
    fact: object = None                                # game -> its fact line (fact_text)
    a_plays: bool = False                              # Settings: A plays a game
    pins: list = field(default_factory=list)           # [(category key, title, games)] (w-100)


def clamp(i: int, n: int) -> int:
    """i kept to 0..n-1 (0 for an empty list)."""
    return max(0, min(n - 1, i))


def _name(g: dict) -> str:
    return g.get('display_name') or g.get('slug') or ''


def _game(g: dict, feed: Feed, kicker: str = '', key: str | None = None) -> Item:
    return Item(key=key or f"game:{g['slug']}", kind='game', title=_name(g),
                sub=feed.fact(g) if feed.fact else '', slug=g['slug'], kicker=kicker,
                primary='play' if feed.a_plays else 'open')


def _home(feed: Feed) -> Category:
    items = []
    run = feed.playing
    if run:
        items.append(Item(key=f"game:{run['slug']}", kind='game', title=_name(run),
                          slug=run['slug'], sub=feed.fact(run) if feed.fact else '',
                          kicker=feed.playing_kicker,
                          primary='resume' if run.get('playing') else 'open'))
    live = bool(run and run.get('playing'))      # not while it's only starting
    skip = run['slug'] if run else None
    played = sorted((g for g in feed.games if g.get('last_played') and g['slug'] != skip),
                    key=lambda g: g['last_played'], reverse=True)
    items += [_game(g, feed) for g in played[:HOME_PLAYED]]
    if feed.week:
        items.append(Item(key='card:week', kind='card', title="Your week",
                          sub=feed.week.get('sub', ''), slug=feed.week.get('slug'),
                          glyph='calendar', kicker='THIS WEEK'))
    for e in feed.friends:
        items.append(Item(key=e['key'], kind='card', title=e['title'], sub=e.get('sub', ''),
                          slug=e.get('slug'), glyph='friends', kicker='FRIENDS PLAYING'))
    if not items:
        items.append(Item(key='empty:home', kind='empty', glyph='library', title=NO_GAMES,
                          sub=NO_GAMES_SUB, primary='all_settings'))
    return Category('home', "Home", 'home', tuple(items), live=live)


def _library(feed: Feed) -> Category:
    if feed.lib_groups is not None:
        # By collection, flat for now: the collection's name over each game,
        # and a game in two collections twice (its key says which).
        items = [_game(g, feed, kicker=title.upper(), key=f"game:{g['slug']}|{title}")
                 for title, gs in feed.lib_groups for g in gs]
        n = len({it.slug for it in items})
    else:
        items = [_game(g, feed) for g in feed.library]
        n = len(items)
    if not items:
        items = [Item(key='empty:library', kind='empty', glyph='tune',
                      title=f"No games under {feed.lib_show_label}",
                      sub="Pick another filter in Sort & filter.", primary='sheet')
                 if feed.games else
                 Item(key='empty:library', kind='empty', glyph='library', title=NO_GAMES,
                      sub=NO_GAMES_SUB, primary='all_settings')]
    sub = "  ·  ".join(p for p in (feed.lib_label, f"{n} game{'s' if n != 1 else ''}") if p)
    # Pinned categories first (w-100): A opens the category's page.
    pins = [Item(key=f"cat:{key}", kind='action', title=title, glyph='pin', primary=f"cat:{key}",
                 sub=f"{count} game{'s' if count != 1 else ''}")
            for key, title, count in feed.pins]
    return Category('library', "Library", 'library', tuple(pins + items), sub=sub)


def _settings(feed: Feed) -> Category:
    items = [Item(key=f"set:{key}", kind='setting', title=title, value=value, choices=choices,
                  glyph=SETTING_GLYPH.get(key, 'settings'),
                  primary='step' if enabled else 'none')
             for key, title, choices, value, enabled in feed.settings]
    items = [replace(it, sub=it.value_text) for it in items]
    items.append(Item(key='action:all_settings', kind='action', title="All settings",
                      sub="Leaves Theatre for the desktop's Settings.", glyph='external',
                      primary='all_settings'))
    return Category('settings', "Settings", 'settings', tuple(items))


def categories(feed: Feed) -> list[Category]:
    """The Ribbon's categories, left to right. Discover and Downloads follow
    the same gates as Theatre's tabs; each opens that tab's page for now."""
    cats = [_home(feed), _library(feed)]
    if feed.discover:
        cats.append(Category('discover', "Discover", 'news', (
            Item(key='page:discover', kind='page', title="News and deals", glyph='news',
                 sub="Stories about your games and deals on them.", primary='tab:discover'),)))
    if feed.downloads:
        cats.append(Category('downloads', "Downloads", 'download', (
            Item(key='page:downloads', kind='page', title="Downloads", glyph='download',
                 sub=feed.downloads_sub or "Nothing downloading.", primary='tab:downloads'),)))
    cats.append(_settings(feed))
    return cats


def step_value(item: Item, d: int = 1, wrap: bool = True):
    """A setting's next (d=1) or previous value; at an end it wraps, or
    stays when not `wrap` (as CouchRow does)."""
    values = [c[0] for c in item.choices]
    if not values:
        return item.value
    i = (values.index(item.value) if item.value in values else 0) + d
    return values[i % len(values)] if wrap else values[clamp(i, len(values))]


@dataclass
class RibbonState:
    """Where the Ribbon is: the category, and each category's focused item
    (kept while you visit the others)."""
    cat: str = 'home'
    focus: dict = field(default_factory=dict)

    def index(self, cats: list[Category]) -> int:
        return next((i for i, c in enumerate(cats) if c.key == self.cat), 0)

    def item_index(self, cat: Category) -> int:
        return clamp(self.focus.get(cat.key, 0), len(cat.items))


def current(state: RibbonState, cats: list[Category]) -> Item | None:
    if not cats:
        return None
    c = cats[state.index(cats)]
    return c.items[state.item_index(c)] if c.items else None


def nav(state: RibbonState, cats: list[Category], action: str) -> RibbonState:
    """One move: left and right (the category, stopping at the ends), next
    and prev (the category, wrapping: LB/RB), up and down (the item,
    stopping at the ends), home (Home's first item). A new state."""
    if not cats:
        return state
    i = state.index(cats)
    focus = dict(state.focus)
    if action in ('left', 'right'):
        i = clamp(i + (1 if action == 'right' else -1), len(cats))
    elif action in ('next', 'prev'):
        i = (i + (1 if action == 'next' else -1)) % len(cats)
    elif action in ('up', 'down'):
        c = cats[i]
        j = state.item_index(c) + (1 if action == 'down' else -1)
        focus[c.key] = clamp(j, len(c.items))
    elif action == 'home':
        i = next((k for k, c in enumerate(cats) if c.key == 'home'), 0)
        focus[cats[i].key] = 0
    return RibbonState(cats[i].key, focus)


def find(cats: list[Category], cat_key: str, item_key: str | None) -> int | None:
    """The index of an item in a category: by key, or for 'game:<slug>' the
    first item of that game (a By collection key carries '|<collection>')."""
    c = next((c for c in cats if c.key == cat_key), None)
    if c is None or item_key is None:
        return None
    for i, it in enumerate(c.items):
        if it.key == item_key or it.key.startswith(item_key + '|'):
            return i
    return None
