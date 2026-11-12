"""Theatre's Ribbon layout (w-108): what it shows, without Qt.

`categories(feed)` turns what Theatre already holds (its games, the Library
sort and filter, Your week, friends playing, the Theatre settings, which
tabs are on) into the Ribbon's line of categories, each with its column of
items. `nav` moves through them. `RibbonView` (ribbon.py) only paints the
result, so the tests read this module, not pixels."""

from dataclasses import dataclass, field, replace

KINDS = ('game', 'card', 'page', 'setting', 'action', 'folder', 'empty', 'news', 'deal',
         'download')
HOME_PLAYED = 8                       # Jump back in: this many recently played games
FOLDER_CAP = 40                       # games in a category folder (then: See all)
GROUP_KICKER = {'ready': "READY TO PLAY", 'progress': "IN PROGRESS", 'queue': "UP NEXT"}

# The glyph of each Theatre setting on the Ribbon (theatre_settings.ROWS).
SETTING_GLYPH = {'theatre_layout': 'grid', 'ribbon_motion': 'sparkles', 'ribbon_classic': 'palette',
                 'pad_a': 'gamepad',
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
    children: tuple = ()              # a folder's own column
    track: tuple = ()                 # a download's pipeline stages (pipeline.cards track)
    figure: str = ''                  # a download's one big number
    tags: tuple = ()                  # ((text, role), ...): drawn as soft tags instead of `sub`

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
    tags: tuple = ()                  # the same as soft tags under the icon bar
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
    fact_tags: object = None                           # game -> its facts as tags (fact_tags)
    a_plays: bool = False                              # Settings: A plays a game
    pins: list = field(default_factory=list)           # [(category key, title, games)] (w-100)
    groups: list = field(default_factory=list)         # categories.browse(...) with no caps
    shelves: list = field(default_factory=list)        # theatre_discover.shelves: (title, kind, items)
    gamedb_off: bool = False                           # Discover: Game-db isn't on (setup item)
    dl_rows: list = field(default_factory=list)        # downloads.theatre_rows


def clamp(i: int, n: int) -> int:
    """i kept to 0..n-1 (0 for an empty list)."""
    return max(0, min(n - 1, i))


# Tag roles: a palette key ('subtext' = plain, 'accent', 'success', 'warning')
# or a '#rrggbb' colour (a ProtonDB tier). The painter tints the capsule.
def count_tag(text: str) -> tuple:
    return ((text, 'accent'),)


def split_tags(line: str, role: str = 'subtext') -> tuple:
    """'Installed  ·  A to Z' -> two plain tags."""
    return tuple((t.strip(), role) for t in (line or '').split('·') if t.strip())


def _name(g: dict) -> str:
    return g.get('display_name') or g.get('slug') or ''


def _game(g: dict, feed: Feed, kicker: str = '', key: str | None = None) -> Item:
    return Item(key=key or f"game:{g['slug']}", kind='game', title=_name(g),
                sub=feed.fact(g) if feed.fact else '', slug=g['slug'], kicker=kicker,
                tags=feed.fact_tags(g) if feed.fact_tags else (),
                primary='play' if feed.a_plays else 'open')


def _home(feed: Feed) -> Category:
    items = []
    run = feed.playing
    if run:
        items.append(Item(key=f"game:{run['slug']}", kind='game', title=_name(run),
                          slug=run['slug'], sub=feed.fact(run) if feed.fact else '',
                          tags=feed.fact_tags(run) if feed.fact_tags else (),
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
    count = f"{n} game{'s' if n != 1 else ''}"
    sub = "  ·  ".join(p for p in (feed.lib_label, count) if p)
    # Pinned categories first (w-100): A opens the category's page.
    pins = [Item(key=f"cat:{key}", kind='action', title=title, glyph='pin', primary=f"cat:{key}",
                 sub=_count(c, 'game'), tags=count_tag(_count(c, 'game')))
            for key, title, c in feed.pins]
    return Category('library', "Library", 'library', tuple(pins + items), sub=sub,
                    tags=count_tag(count) + split_tags(feed.lib_label))


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


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def _cat_folder(c, feed: Feed) -> Item:
    """A category (categories.Category) as a folder of its games; a long one
    ends with an item that opens its page (w-100)."""
    games = list(c.games)
    kids = [_game(g, feed) for g in games[:FOLDER_CAP]]
    if len(games) > FOLDER_CAP:
        kids.append(Item(key=f"seeall:{c.key}", kind='action', title="See all",
                         sub=f"All {len(games)} games", glyph='grid', primary=f"cat:{c.key}"))
    if not kids:
        kids = [Item(key=f"empty:{c.key}", kind='empty', title="Nothing here yet", glyph='folder',
                     primary='none')]
    return Item(key=f"col:{c.key}", kind='folder', title=c.title, glyph='folder',
                sub=_count(len(games), 'game'), tags=count_tag(_count(len(games), 'game')),
                children=tuple(kids))


def _collections(feed: Feed) -> Category:
    """Yours (favourites, collections, shelves) as folders of games, then
    For you, Genres and Stores as folders of category folders."""
    items = []
    for g in feed.groups:                       # Yours first, whatever the groups' order
        if g.key == 'yours':
            items += [_cat_folder(c, feed) for c in g.cats]
    for g in feed.groups:
        if g.key != 'yours' and g.cats:
            n = _count(len(g.cats), 'category', 'categories')
            items.append(Item(key=f"group:{g.key}", kind='folder', title=g.title,
                              glyph='folder', sub=n, tags=count_tag(n),
                              children=tuple(_cat_folder(c, feed) for c in g.cats)))
    if not items:
        items.append(Item(key='empty:collections', kind='empty', glyph='folder',
                          title="No collections yet",
                          sub="Collections, genres and stores appear as your library grows.",
                          primary='none'))
    n = _count(sum(len(g.cats) for g in feed.groups), 'category', 'categories')
    return Category('collections', "Collections", 'folder', tuple(items), sub=n,
                    tags=count_tag(n))


SHELF_NOUN = {'news': ("story", "stories"), 'deal': ("deal", "deals"),
              'upcoming': ("game", "games")}


def _story_key(n: dict, i: int) -> str:
    return f"news:{n.get('link') or n.get('url') or n.get('title') or i}"


def _discover(feed: Feed) -> Category:
    items = [Item(key='page:discover', kind='page', title="All of Discover", glyph='news',
                  sub="Every shelf, with its pictures.", primary='tab:discover')]
    for title, kind, found in feed.shelves:
        if not found:
            continue
        if kind == 'news':
            kids = tuple(Item(key=_story_key(n, i), kind='news', title=n.get('title') or "",
                              sub=" · ".join(x for x in (n.get('source'),) if x), glyph='news',
                              tags=split_tags(n.get('source') or ''),
                              value=n) for i, n in enumerate(found))
        else:
            kids = tuple(Item(key=f"{kind}:{d.get('appid') or d.get('title') or i}", kind='deal',
                              title=d.get('title') or "", glyph='sparkles', value=d,
                              primary=f"deal:{kind}") for i, d in enumerate(found))
        one, many = SHELF_NOUN.get(kind, ("item", "items"))
        n = _count(len(found), one, many)
        items.append(Item(key=f"shelf:{title}", kind='folder', title=title, glyph='folder',
                          sub=n, tags=count_tag(n), children=kids))
    if feed.gamedb_off:
        items.append(Item(key='action:gamedb_setup', kind='action', title="Sales and coming soon",
                          sub="Connect Game-db to see your wishlist's sales here. "
                              "Leaves Theatre for the desktop's Settings.",
                          glyph='external', primary='all_settings'))
    return Category('discover', "Discover", 'news', tuple(items))


def _downloads(feed: Feed) -> Category:
    items = [Item(key=f"dl:{r['key']}", kind='download', title=r['title'], sub=r.get('state', ''),
                  glyph='download', kicker=GROUP_KICKER.get(r.get('group'), ''),
                  track=tuple(r.get('track') or ()), figure=r.get('big') or '',
                  primary=f"dl:{r.get('primary') or 'options'}") for r in feed.dl_rows]
    items.append(Item(key='page:downloads', kind='page', title="All downloads", glyph='download',
                      sub=feed.downloads_sub or "Nothing downloading.", primary='tab:downloads'))
    return Category('downloads', "Downloads", 'download', tuple(items), sub=feed.downloads_sub,
                    tags=split_tags(feed.downloads_sub, 'accent'))


def _friends(feed: Feed) -> Category:
    items = [Item(key=e['key'], kind='card', title=e['title'], sub=e.get('sub', ''),
                  slug=e.get('slug'), glyph='friends', kicker='FRIENDS PLAYING')
             for e in feed.friends]
    return Category('friends', "Friends", 'friends', tuple(items))


def categories(feed: Feed) -> list[Category]:
    """The Ribbon's categories, left to right: Home, Library, Collections,
    Discover and Downloads (the same gates as Theatre's tabs), Friends (only
    with someone playing), Settings."""
    cats = [_home(feed), _library(feed), _collections(feed)]
    if feed.discover:
        cats.append(_discover(feed))
    if feed.downloads:
        cats.append(_downloads(feed))
    if feed.friends:
        cats.append(_friends(feed))
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
    """Where the Ribbon is: the category, the folders opened in it (`path`,
    their keys) and each column's focused item (kept while you visit
    others). A column's id is the category key, then 'cat>folder>folder'."""
    cat: str = 'home'
    focus: dict = field(default_factory=dict)
    path: tuple = ()

    def index(self, cats: list[Category]) -> int:
        return next((i for i, c in enumerate(cats) if c.key == self.cat), 0)

    def item_index(self, cat: Category) -> int:
        return clamp(self.focus.get(cat.key, 0), len(cat.items))


def column_id(cat_key: str, path) -> str:
    return ">".join((cat_key, *path))


def resolve(state: RibbonState, cats: list[Category]) -> RibbonState:
    """`state` with its path cut at the first folder that is gone (a refresh
    can remove one); the same state when it is fine."""
    if not state.path:
        return state
    cat = next((c for c in cats if c.key == state.cat), None)
    items, keep = (cat.items if cat else ()), []
    for key in state.path:
        f = next((it for it in items if it.key == key and it.kind == 'folder'), None)
        if f is None:
            break
        keep.append(key)
        items = f.children
    return state if len(keep) == len(state.path) else RibbonState(state.cat, state.focus,
                                                                  tuple(keep))


def columns(state: RibbonState, cats: list[Category]) -> list[tuple[str, tuple]]:
    """The open columns, left to right: [(column id, items)], the category's
    own first, then one per opened folder (a vanished one cuts the rest)."""
    if not cats:
        return []
    cat = cats[state.index(cats)]
    state = resolve(RibbonState(cat.key, state.focus, state.path), cats)
    out, items = [(cat.key, cat.items)], cat.items
    for k, key in enumerate(state.path):
        items = next(it.children for it in items if it.key == key)
        out.append((column_id(cat.key, state.path[:k + 1]), items))
    return out


def column(state: RibbonState, cats: list[Category]) -> tuple[str, tuple]:
    """The innermost open column: (id, items)."""
    cols = columns(state, cats)
    return cols[-1] if cols else ('', ())


def crumbs(state: RibbonState, cats: list[Category]) -> list[Item]:
    """The folders opened in the current category, outermost first."""
    out, items = [], (cats[state.index(cats)].items if cats else ())
    for key in resolve(state, cats).path:
        f = next(it for it in items if it.key == key)
        out.append(f)
        items = f.children
    return out


def current(state: RibbonState, cats: list[Category]) -> Item | None:
    if not cats:
        return None
    cid, items = column(state, cats)
    return items[clamp(state.focus.get(cid, 0), len(items))] if items else None


def nav(state: RibbonState, cats: list[Category], action: str) -> RibbonState:
    """One move: left and right (the category, stopping at the ends; inside a
    folder Left goes up and Right opens a folder), in (open the focused
    folder) and out (up one), next and prev (the category, wrapping: LB/RB),
    up and down (the item, stopping at the ends), home (Home's first item).
    Next, prev, home and a category move close every folder. A new state."""
    if not cats:
        return state
    state = resolve(state, cats)
    i = state.index(cats)
    focus = dict(state.focus)
    path = state.path
    if action in ('in', 'right'):
        cid, items = column(state, cats)
        it = items[clamp(focus.get(cid, 0), len(items))] if items else None
        if it is not None and it.kind == 'folder':
            return RibbonState(state.cat, focus, path + (it.key,))
        if action == 'in' or path:
            return state                        # nothing to open (inside a folder: stays)
    elif action in ('out', 'left') and path:
        return RibbonState(state.cat, focus, path[:-1])
    elif action == 'out':
        return state
    if action in ('left', 'right'):
        i = clamp(i + (1 if action == 'right' else -1), len(cats))
        path = ()
    elif action in ('next', 'prev'):
        i = (i + (1 if action == 'next' else -1)) % len(cats)
        path = ()
    elif action in ('up', 'down'):
        cid, items = column(state, cats)
        j = clamp(focus.get(cid, 0), len(items)) + (1 if action == 'down' else -1)
        focus[cid] = clamp(j, len(items))
    elif action == 'home':
        i = next((k for k, c in enumerate(cats) if c.key == 'home'), 0)
        focus[cats[i].key] = 0
        path = ()
    if cats[i].key != state.cat:
        path = ()
    return RibbonState(cats[i].key, focus, path)


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
