"""The game page's rail in Theatre's Ribbon layout (w-145): what it shows,
without widgets.

A on a game in the Ribbon opens its page as a rail: the logo and a strip of
facts, a big Play, a column of glass cards (the drawer's sections, each with
a count when there is one), a small grid of actions, and a pane that
previews the focused section. `rail` turns the drawer's visible sections,
what is known about the game and its options sheet's actions into those
cards; `preview` says what the pane shows for one section. The widgets
(theatre_game.TheatreDetails) only lay the result out, so the tests read
this module."""

from typing import NamedTuple

from .fmt import fmt_hours, fmt_relative, fmt_size
from .ribbon_model import progress, SEP

# The drawer's sections as rail cards: (title, glyph).
SECTION_CARDS = {'overview': ("Overview", 'info'), 'friends': ("Friends", 'friends'),
                 'media': ("Media", 'image'), 'news': ("News", 'news'),
                 'achievements': ("Achievements", 'trophy'),
                 'compat': ("Compatibility", 'shield')}
PREVIEW_KICKER = {'overview': "YOUR PROGRESS", 'friends': "FRIENDS", 'media': "MEDIA",
                  'news': "NEWS", 'achievements': "ACHIEVEMENTS", 'compat': "COMPATIBILITY"}


class GameInfo(NamedTuple):
    """What the game page knows: the facts (overview_facts), --store-info,
    --game-tips, the news, the friends who own it, the achievements
    (done, total)."""
    rec: dict
    store: dict | None
    tips: dict | None
    news: list
    owners: list
    ach: tuple | None


class Card(NamedTuple):
    """A glass card on the rail, or a cell of the action grid. `action` is
    the options sheet key a grid cell runs ('' for a section or a group)."""
    key: str                           # a section key, or favorite / collections / maintenance / uninstall
    title: str
    glyph: str                         # a G key
    count: str = ''                    # "18 of 47", "3 trailers", "2 own it", "Platinum"
    danger: bool = False
    checked: bool | None = None
    action: str = ''


class PreviewText(NamedTuple):
    kicker: str
    big: str
    progress: tuple | None = None      # (fraction, words)
    line: str = ''
    about: str = ''                    # Overview only: ABOUT and the text
    hint: str = ''


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def trailers(info: GameInfo) -> list:
    """The store's trailers that can play (https only, as the drawer)."""
    st = (info.store or {}).get('store') or {}
    return [t for t in st.get('trailers') or [] if str(t.get('url') or '').startswith('https://')]


def tier(info: GameInfo, g: dict | None = None) -> str:
    return ((info.tips or {}).get('protondb') or {}).get('tier') or (g or {}).get('protondb') or ''


def _count(key: str, info: GameInfo, g: dict | None) -> str:
    if key == 'achievements' and info.ach:
        return f"{info.ach[0]} of {info.ach[1]}"
    if key == 'media' and trailers(info):
        return _plural(len(trailers(info)), "trailer", "trailers")
    if key == 'friends' and info.owners:
        n = len(info.owners)
        return f"{n} owns it" if n == 1 else f"{n} own it"
    if key == 'compat' and tier(info, g):
        return tier(info, g).capitalize()
    return ''


def rail(visible_keys, info: GameInfo, actions=(), g: dict | None = None) -> tuple[list, list]:
    """(cards, grid): a card per visible drawer section in that order, with
    its count when known; then up to four actions, each only when the game's
    options sheet (game_actions(g, theatre=True)) offers it: Favourite,
    Collections, Maintenance, Uninstall."""
    cards = [Card(k, *SECTION_CARDS[k], count=_count(k, info, g))
             for k in visible_keys if k in SECTION_CARDS]
    by_key = {a.key: a for a in actions}
    subs = {a.sub for a in actions}
    grid = []
    if 'favorite' in by_key:
        fav = bool(by_key['favorite'].checked)
        grid.append(Card('favorite', "Favourite", 'star' if fav else 'star_outline',
                         checked=fav, action='favorite'))
    if "Collections" in subs:
        grid.append(Card('collections', "Collections", 'folder'))
    if "Maintenance" in subs:
        grid.append(Card('maintenance', "Maintenance", 'wrench'))
    un = next((a for a in actions if a.key in ('uninstall', 'steam-uninstall')), None)
    if un is not None:
        grid.append(Card('uninstall', "Uninstall…", 'trash', danger=True, action=un.key))
    return cards, grid


def _overview(g: dict, info: GameInfo) -> PreviewText:
    rec = info.rec or {}
    minutes = g.get('playtime_minutes') or 0
    hours = fmt_hours(minutes)
    main = (rec.get('length') or {}).get('main')
    parts = []
    if main:
        try:
            parts.append(f"About {float(main):g} h for the main story")
        except (TypeError, ValueError):
            pass
    last = fmt_relative(g.get('last_played'))
    added = fmt_relative(g.get('added_at') or g.get('installed_at'))
    size = fmt_size(g.get('disk_bytes'))
    parts += [x for x in (last and f"Last played {last}", added and f"Installed {added}",
                          size and f"{size} on disk") if x]
    st = (info.store or {}).get('store') or {}
    about = (rec.get('description') or st.get('blurb') or '').strip()
    return PreviewText(PREVIEW_KICKER['overview'], f"{hours} played" if hours else "Not played yet",
                       progress(g, rec), SEP.join(parts), about, "Press A for the full Overview")


def preview(key: str, g: dict, info: GameInfo) -> PreviewText:
    """What the preview pane shows while a section's card has focus; only
    what is known (no count, no About block without data)."""
    hint = f"Press A to open {SECTION_CARDS.get(key, ('it',))[0]}"
    if key == 'overview':
        return _overview(g, info)
    kicker = PREVIEW_KICKER.get(key, '')
    if key == 'achievements':
        if not info.ach:
            return PreviewText(kicker, "No achievements", hint=hint)
        done, total = info.ach
        return PreviewText(kicker, f"{done} of {total} unlocked",
                           (done / total, f"{round(100 * done / total)}% of this game's achievements"),
                           hint=hint)
    if key == 'media':
        ts = trailers(info)
        names = SEP.join(t.get('name') for t in ts[:3] if t.get('name'))
        return PreviewText(kicker, _plural(len(ts), "trailer", "trailers"), line=names, hint=hint)
    if key == 'friends':
        from .friends import names_text                  # (Qt widgets: at call time)
        names = [r.get('name') or '' for r in info.owners if r.get('name')]
        n = len(info.owners)
        line = names_text(names)                         # "Ana and Ben", "Ana, Ben +2"
        return PreviewText(kicker, f"{n} friend{'s' if n != 1 else ''} own{'s' if n == 1 else ''} it",
                           line=line, hint=hint)
    if key == 'news':
        mine = [i for i in info.news if g.get('appid') and i.get('appid') == g.get('appid')]
        first = mine[0] if mine else {}
        return PreviewText(kicker, first.get('title') or "No news",
                           line=SEP.join(x for x in (fmt_relative(first.get('date')),) if x),
                           hint=hint)
    if key == 'compat':
        t = tier(info, g)
        pdb = (info.tips or {}).get('protondb') or {}
        line = _plural(pdb['total'], "report", "reports") if pdb.get('total') else ''
        return PreviewText(kicker, f"ProtonDB {t.capitalize()}" if t else "No rating yet",
                           line=line, hint=hint)
    return PreviewText(kicker, '', hint=hint)
