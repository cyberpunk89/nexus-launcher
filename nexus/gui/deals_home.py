"""The Deals home (w-146): For you cards, jump chips, four shelves and the
price-watch rail. The page (deals.DealsPage) owns the data and lays these
out from its scroll viewport's width; nothing here asks the CLI."""

import html
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QPoint, Qt
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QMenu, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget)

from nexus import facts, gamedb

from . import deal_model
# deals.py imports this module just before DealsPage, by which time these exist.
from .deals import _pill, deal_links, dim, DealArt, DealCard
from .theme import G, P, R, SP

RAIL_W = 320                  # the price-watch rail (board: 320)
FOLD_W = 1100                 # below this much page width the rail folds into a button
THUMB_W, THUMB_H = 64, 36     # a rail row's art, a catalogue row's tile (board: 64)
SHELF_TILE = 200              # the narrowest shelf tile (4 a row at a 1280 window)
FOR_YOU_TILE = 260            # the narrowest For you card (3 a row)
GAP = SP['3']


def price_like(text: str, value: float) -> str:
    """`value` written the way `text` ("$8.99", "8,99€") writes a price."""
    import re
    if not text or not re.search(r'\d', text):
        return f"{value:.2f}"
    sep = ',' if re.search(r'\d,\d{2}(?!\d)', text) else '.'
    return re.sub(r'\d[\d.,]*', f"{value:.2f}".replace('.', sep), text, count=1)


def since_text(ts) -> str:
    """"since Thursday" within a week, "since 3 Nov" before that."""
    if not ts:
        return ''
    try:
        when = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return ''
    days = (datetime.now().date() - when.date()).days
    if days <= 0:
        return "since today"
    if days == 1:
        return "since yesterday"
    return f"since {when.strftime('%A')}" if days < 7 else f"since {when.day} {when:%b}"


class Row(QFrame):
    """A focusable, clickable row with a real layout (rail, suggestions,
    catalogue). Return/Space/click = clicked; the Menu key (X on a pad)
    or a right-click = menu(global pos)."""
    clicked = pyqtSignal()
    menu = pyqtSignal(QPoint)

    def __init__(self):
        super().__init__()
        self.setObjectName("DealsRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        elif ev.key() == Qt.Key.Key_Menu:
            self.menu.emit(self.mapToGlobal(QPoint(self.width() // 3, self.height() // 2)))
        else:
            super().keyPressEvent(ev)

    def contextMenuEvent(self, ev):
        self.menu.emit(ev.globalPos())

    def mute_children(self):
        for w in self.findChildren(QWidget):
            if not isinstance(w, QPushButton):
                w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)


def run_menu(owner: QWidget, title: str, items: list, pos: QPoint):
    """(text, glyph, fn) items: the sheet on a pad, a QMenu otherwise."""
    sheet = getattr(owner.window(), 'sheet', None)
    if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
        sheet.choose(title, items)
        return
    m = QMenu(owner)
    for text, _glyph, fn in items:
        m.addAction(text, fn)
    if not m.isEmpty():
        m.exec(pos)
    m.deleteLater()


class ForYouCard(DealCard):
    """A deal tile with its reason line ("Because you play a lot of RPG")."""

    def __init__(self, pick: dict, *args, **kw):
        super().__init__(pick['item'], 'deal', *args, **kw)
        self.reason = ElideLabel(pick['reason'], "ForYouReason")
        self.reason.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.reason.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.layout().itemAt(1).layout().insertWidget(2, self.reason)


class ShelfGrid(QWidget):
    """One shelf: title, count, sub line and See all over one row of
    tiles; place() shows as many as the row has columns for."""
    see_all = pyqtSignal(str)

    def __init__(self, shelf: dict):
        super().__init__()
        self.key = shelf['key']
        self.cards: list[DealCard] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        self.title = label(shelf['title'], "ShelfTitle")
        self.count = label(str(shelf['count']), "Muted")
        self.sub = label(shelf['sub'], "Muted")
        head.addWidget(self.title, 0, Qt.AlignmentFlag.AlignBottom)
        head.addWidget(self.count, 0, Qt.AlignmentFlag.AlignBottom)
        head.addWidget(self.sub, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch()
        self.see_all_btn = QPushButton("See all")
        self.see_all_btn.setObjectName("Ghost")
        self.see_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.see_all_btn.clicked.connect(self._see_all)
        head.addWidget(self.see_all_btn)
        lay.addLayout(head)
        self.row = QHBoxLayout()
        self.row.setSpacing(GAP)
        self.row.addStretch()
        lay.addLayout(self.row)

    def _see_all(self):
        self.see_all.emit(self.key)

    def add(self, card: DealCard):
        self.cards.append(card)
        self.row.insertWidget(self.row.count() - 1, card, 0, Qt.AlignmentFlag.AlignTop)

    def place(self, cols: int, w: int, right=None):
        """`right(widget)`: what Right from the row's last tile reaches (the rail)."""
        for i, card in enumerate(self.cards):
            if i < cols:
                card.set_width(w)
            card.setVisible(i < cols)
            last = i == min(cols, len(self.cards)) - 1
            card.nav_jump = (lambda key, c=card: right(c) if key == Qt.Key.Key_Right else None) \
                if last and right else None

    def visible_cards(self) -> list:
        return [c for c in self.cards if not c.isHidden()]


class DealsHome(QWidget):
    """For you, the jump chips and the shelves. fill() builds the tiles
    for one set of data; place(avail) lays them out for a width."""
    see_all = pyqtSignal(str)
    activated = pyqtSignal(str)
    repack = pyqtSignal(str, dict, dict)

    def __init__(self):
        super().__init__()
        self.for_you_cards: list[ForYouCard] = []
        self.right = None                 # the page's rail_for (pad: Right into the rail)
        self.jump_chips: list[QPushButton] = []
        self.shelves: dict[str, ShelfGrid] = {}
        self._cols = None
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(SP['4'])

    def all_cards(self) -> list:
        return self.for_you_cards + [c for s in self.shelves.values() for c in s.cards]

    def fill(self, items: list[dict], currency: str, hide_owned: bool, taste, make):
        """`items` carry 'owned' and 'genres'; make(item, kind, width) -> DealCard."""
        clear_layout(self.lay)
        self.for_you_cards, self.jump_chips, self.shelves = [], [], {}
        self._cols = None
        picks = deal_model.for_you(items, taste)
        shelves = [s for s in deal_model.shelves(items, currency, hide_owned) if s['count']]
        if not picks and not shelves:
            self.lay.addWidget(EmptyState(G['deals'], "Nothing on sale right now."))
            return
        if picks:
            self.lay.addLayout(self._heading("FOR YOU", "Picked from your wishlist",
                                             "ranked by what you play"))
            self.fy_grid = QGridLayout()
            self.fy_grid.setSpacing(GAP)
            self.fy_grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for p in picks:
                card = make(p, 'foryou', FOR_YOU_TILE)
                self.for_you_cards.append(card)
            self.lay.addLayout(self.fy_grid)
        chips = FlowLayout(None, 0, SP['1_5'])
        for s in shelves:
            b = QPushButton(f"{s['title']}  {s['count']}")
            b.setObjectName("Chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=s['key']: self.jump(k))
            chips.addWidget(b)
            self.jump_chips.append(b)
        chip_host = QWidget()
        chip_host.setLayout(chips)
        self.lay.addWidget(chip_host)
        kicker = label("ON SALE FROM YOUR WISHLIST", "SectionKicker")
        self.lay.addWidget(kicker)
        for s in shelves:
            grid = ShelfGrid(s)
            grid.see_all.connect(self.see_all)
            for g in s['items']:
                grid.add(make(g, 'shelf', SHELF_TILE))
            self.shelves[s['key']] = grid
            self.lay.addWidget(grid)

    @staticmethod
    def _heading(kicker: str, title: str, sub: str) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(SP['0_5'])
        box.addWidget(label(kicker, "SectionKicker"))
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        row.addWidget(label(title, "ShelfTitle"), 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(label(sub, "Muted"), 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch()
        box.addLayout(row)
        return box

    def jump(self, key: str):
        """A jump chip: to that shelf's first tile."""
        grid = self.shelves.get(key)
        cards = grid.visible_cards() if grid else []
        if cards:
            focus_and_reveal(cards[0])
            p = grid.parentWidget()
            while p is not None and not hasattr(p, 'ensureWidgetVisible'):
                p = p.parentWidget()
            if p is not None:
                p.ensureWidgetVisible(grid, 0, 0)

    def place(self, avail: int):
        """Columns from the width the page measured (its scroll viewport)."""
        cols = max(1, (avail + GAP) // (SHELF_TILE + GAP))
        w = (avail - GAP * (cols - 1)) // cols
        fy_cols = max(1, min(3, (avail + GAP) // (FOR_YOU_TILE + GAP)))
        fy_w = (avail - GAP * (fy_cols - 1)) // fy_cols
        if (cols, w, fy_cols) == self._cols:
            return False
        self._cols = (cols, w, fy_cols)
        for grid in self.shelves.values():
            grid.place(cols, w, self.right)
        if self.for_you_cards:
            for c in self.for_you_cards:
                self.fy_grid.removeWidget(c)
            for i, c in enumerate(self.for_you_cards):
                c.set_width(fy_w)
                self.fy_grid.addWidget(c, i // fy_cols, i % fy_cols)
                c.show()
        return True


class PriceWatchRail(QFrame):
    """Your price watch: what dropped since your last visit, new all-time
    lows today, the newest FitGirl posts and what comes out this month.
    A quiet section says so in one line."""
    open_deal = pyqtSignal(dict)          # a wishlist game
    open_title = pyqtSignal(str)          # a FitGirl game: search it
    see_newest = pyqtSignal()
    see_upcoming = pyqtSignal()
    QUIET = "None today. Nexus looks again at the next sync."
    SECTIONS = (('dropped', "DROPPED SINCE YOUR LAST VISIT"),
                ('new_atl', "NEW ALL-TIME LOWS TODAY"),
                ('newest', "NEWEST ON FITGIRL"), ('coming', "COMING THIS MONTH"))
    NEWEST = 3

    def __init__(self, docked: bool = True):
        super().__init__()
        if docked:
            self.setObjectName("Card")
            self.setFixedWidth(RAIL_W)
        self._rows: dict[str, list] = {}
        self._quiet: dict[str, str] = {}
        self.lay = QVBoxLayout(self)
        m = SP['4'] if docked else 0
        self.lay.setContentsMargins(m, m, m, m)
        self.lay.setSpacing(SP['2'])

    def rows(self, key: str) -> list:
        return self._rows.get(key, [])

    def quiet(self, key: str) -> str:
        return self._quiet.get(key, '')

    def count(self) -> int:
        return len(self.rows('dropped'))

    def set_data(self, watch: dict, by_appid: dict, newest: list, coming: list,
                 fitgirl_on: bool):
        clear_layout(self.lay)
        self._rows, self._quiet = {}, {}
        head = QHBoxLayout()
        head.addWidget(label("Your price watch", "ShelfTitle"))
        head.addStretch()
        head.addWidget(label(since_text((watch or {}).get('since')), "Muted"))
        self.lay.addLayout(head)
        dropped = [(d, by_appid[str(d['appid'])]) for d in (watch or {}).get('dropped') or ()
                   if str(d.get('appid')) in by_appid]
        lows = [by_appid[str(a)] for a in (watch or {}).get('new_atl') or ()
                if str(a) in by_appid]
        first = True
        for key, title in self.SECTIONS:
            if key == 'newest' and not fitgirl_on:
                continue
            if not first:
                self.lay.addWidget(self._rule())
            first = False
            n = {'dropped': len(dropped), 'new_atl': len(lows), 'coming': len(coming)}.get(key)
            sh = QHBoxLayout()
            sh.addWidget(label(f"{title}  {n}" if n else title, "SectionKicker"))
            sh.addStretch()
            link = {'newest': ("See all", self.see_newest),
                    'coming': ("Upcoming", self.see_upcoming)}.get(key)
            if link:
                b = QPushButton(link[0])
                b.setObjectName("Ghost")
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(link[1])
                sh.addWidget(b)
            self.lay.addLayout(sh)
            rows = []
            if key == 'dropped':
                rows = [self._deal_row(g, self._drop_line(d, g)) for d, g in dropped]
            elif key == 'new_atl':
                rows = [self._deal_row(g, f"{(g.get('price') or {}).get('currentStr') or ''}"
                                          " · all-time low") for g in lows]
            elif key == 'newest':
                rows = [self._fitgirl_row(r) for r in newest[:self.NEWEST]]
            else:
                rows = [self._coming_row(g) for g in coming]
            self._rows[key] = rows
            for r in rows:
                self.lay.addWidget(r)
            if not rows:
                self._quiet[key] = self.QUIET if key in ('dropped', 'new_atl') else (
                    "Nothing new on FitGirl yet." if key == 'newest'
                    else "Nothing from your wishlist this month.")
                self.lay.addWidget(label(self._quiet[key], "Muted", wrap=True))
        self.lay.addStretch()

    @staticmethod
    def _rule() -> QFrame:
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet(f"background: {P['border']};")
        return f

    @staticmethod
    def _drop_line(d: dict, g: dict) -> str:
        now_str = (g.get('price') or {}).get('currentStr') or ''
        was = html.escape(price_like(now_str, d['was']))
        now = html.escape(price_like(now_str, d['now']))
        pct = round((d['was'] - d['now']) / d['was'] * 100) if d.get('was') else 0
        return (f"<s>{was}</s> → <span style='color:{P['accent']}'><b>{now}</b></span>"
                + (f" · -{pct}%" if pct else ''))

    def _row(self, title: str, sub: str, tile: QWidget, tag: str = '') -> Row:
        r = Row()
        r.title = title
        lay = QHBoxLayout(r)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2_5'])
        lay.addWidget(tile, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = ElideLabel(title, "NewsTitle")
        t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(t)
        s = label(sub, "Muted")
        s.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(s)
        lay.addLayout(col, 1)
        if tag:
            lay.addWidget(_pill(tag, 'accent'), 0, Qt.AlignmentFlag.AlignVCenter)
        r.mute_children()
        return r

    def _deal_row(self, g: dict, sub: str) -> Row:
        art = DealArt(THUMB_W, THUMB_H, R['sm'])
        art.round_all = True
        art.set_url(g.get('headerImage'))
        r = self._row(g.get('title') or '', sub, art)
        r.item = g
        r.clicked.connect(lambda g=g: self.open_deal.emit(g))
        return r

    def _fitgirl_row(self, row: dict) -> Row:
        when = row.get('date') or ''
        sub = " · ".join(x for x in (_day(when), row.get('tail')) if x)
        r = self._row(row.get('title') or '', html.escape(sub), initials_tile(row.get('title')),
                      "HV" if row.get('hv') else ("In wishlist" if 'wishlist' in
                                                  (row.get('marks') or ()) else ''))
        r.clicked.connect(lambda t=row.get('title') or '': self.open_title.emit(t))
        return r

    def _coming_row(self, g: dict) -> Row:
        when = gamedb.release_label(g) or ''
        day = label(when, "Muted")
        day.setFixedWidth(THUMB_W)
        day.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price = (g.get('price') or {}).get('currentStr')
        sub = "Wishlist · " + (f"pre-order {price}" if price else "no price yet")
        r = self._row(g.get('title') or '', html.escape(sub), day)
        r.item = g
        r.clicked.connect(lambda g=g: self.open_deal.emit(g))
        return r


def _day(iso: str) -> str:
    """"Today", "Yesterday", "12 Mar 2021" for a post date."""
    try:
        d = datetime.strptime(iso[:10], '%Y-%m-%d').date()
    except ValueError:
        return ''
    days = (datetime.now().date() - d).days
    return "Today" if days == 0 else "Yesterday" if days == 1 else f"{d.day} {d:%b %Y}"


def initials_tile(name: str | None, w: int = THUMB_W, h: int = THUMB_H) -> QWidget:
    """A catalogue game's stand-in art: its initials on a quiet tile."""
    t = label(_initials(name or '?') or '?', "Initials")
    t.setFixedSize(w, h)
    t.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return t


def this_month(upcoming: list[dict]) -> list[dict]:
    """The upcoming wishlist games that come out this calendar month."""
    now = datetime.now()
    out = []
    for g in upcoming:
        try:
            when = datetime.fromisoformat(str(g.get('releaseDate') or '').replace('Z', '+00:00'))
        except ValueError:
            continue
        if (when.year, when.month) == (now.year, now.month):
            out.append(g)
    return out


class HomeMixin:
    """DealsPage's home half: filling For you and the shelves, the rail
    and its fold, the ⋮ menu."""
    SHELF_FILTERS = ('under', 'big', 'short')     # See all keys beyond DEAL_FILTERS
    _watch_pop = None

    def _home_items(self) -> list[dict]:
        """The wishlist with 'owned' and 'genres' (Nexus genres) on each."""
        owned = self._owned()
        genres = self.res.get('genres') or {}
        out = []
        for g in self._data().get('wishlist') or []:
            names = genres.get(str(g.get('appid'))) or g.get('genres') or ()
            out.append({**g, 'owned': bool(g.get('owned') or g.get('appid') in owned),
                        'genres': facts.nexus_genres(names)})
        return out

    def _fill_home(self):
        d = self._data()
        self.home.fill(self._home_items(), d.get('currency') or '', self._hide_owned,
                       self._taste, self._home_card)
        self._feed_rail()
        self._place()

    def _home_card(self, obj: dict, kind: str, width: int) -> DealCard:
        g = obj['item'] if kind == 'foryou' else obj
        r = self.res
        args = (self._base(), set(r.get('installed_appids') or []),
                self.repacks().get(str(g.get('appid'))), bool(g.get('owned')))
        card = (ForYouCard(obj, *args, width=width) if kind == 'foryou'
                else DealCard(g, 'deal', *args, width=width))
        if g.get('owned'):
            dim(card)
        card.activated.connect(self._open)
        card.repack_action.connect(self.repack)
        card.set_download(self._downloads.get(str(g.get('appid'))))
        return card

    def _rail_data(self) -> tuple:
        d = self._data()
        by_appid = {str(g.get('appid')): g for g in d.get('wishlist') or []}
        fitgirl = self._repacks_on and self.catalogue is not None
        newest = sorted((self.catalogue or {}).get('rows') or [] if fitgirl else [],
                        key=lambda r: r.get('date') or '', reverse=True)
        return (self.res.get('watch') or {}, by_appid, newest,
                this_month(gamedb.upcoming(d)), fitgirl)

    def _feed_rail(self):
        self.rail.set_data(*self._rail_data())
        self._sync_rail()

    def _sync_rail(self):
        """Docked from FOLD_W of page width (viewport + the rail when it
        shows), else the "Price watch N" button. Run from the viewport's
        Resize, so a page shown for the first time gets it right."""
        wanted = bool(self._data()) and self.view() == 'home'
        vp = self.scroll.viewport().width()
        docked = not self.rail_scroll.isHidden()
        self._folded = vp + (RAIL_W + SP['4'] if docked else 0) < FOLD_W
        self.rail_scroll.setVisible(wanted and not self._folded)
        n = self.rail.count()
        self.watch_btn.setText(f"Price watch  {n}" if n else "Price watch")
        self.watch_btn.setVisible(wanted and self._folded)

    def rail_for(self, w: QWidget) -> QWidget | None:
        """Right from a shelf's last tile (pad): the rail's row nearest in
        height, when the rail is docked (it sits beside, not in line)."""
        if self.rail_scroll.isHidden():
            return None
        y = w.mapTo(self, QPoint(0, w.height() // 2)).y()
        rows = [r for k in ('dropped', 'new_atl', 'newest', 'coming') for r in self.rail.rows(k)
                if r.isVisible()]
        return min(rows, key=lambda r: abs(r.mapTo(self, QPoint(0, r.height() // 2)).y() - y),
                   default=None)

    def _open_watch(self):
        """The folded rail: a popover, or the sheet on a pad."""
        watch, by_appid, newest, coming, fitgirl = self._rail_data()
        win = self.window()
        sheet = getattr(win, 'sheet', None)
        if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
            rail = PriceWatchRail(docked=False)
            rail.set_data(watch, by_appid, newest, coming, fitgirl)
            items = []
            for key in ('dropped', 'new_atl', 'coming'):
                for r in rail.rows(key):
                    items.append((r.title, G['deals'], lambda g=r.item: self._open_deal(g)))
            for r in rail.rows('newest'):
                items.append((f"{r.title} · FitGirl", G['download'],
                              lambda t=r.title: self.open_results(t)))
            rail.deleteLater()
            sheet.choose("Your price watch", items or [("Close", G['close'], lambda: None)],
                         rail.QUIET if not items else '')
            return
        if self._watch_pop is None:
            self._watch_pop = Popover(win, RAIL_W)
            self._watch_rail = PriceWatchRail(docked=False)
            for sig, fn in ((self._watch_rail.open_deal, self._open_deal),
                            (self._watch_rail.open_title, self.open_results),
                            (self._watch_rail.see_newest, self._see_newest),
                            (self._watch_rail.see_upcoming, self._see_upcoming)):
                sig.connect(fn)
            self._watch_pop.body.addWidget(self._watch_rail)
        self._watch_rail.set_data(watch, by_appid, newest, coming, fitgirl)
        self._watch_pop.open_at(self.watch_btn)

    def _close_watch(self):
        if self._watch_pop is not None:
            self._watch_pop.close_popover(restore=False)

    def _open_deal(self, g: dict):
        self._close_watch()
        url = deal_links(g, 'deal', self._base(), set())[0]
        if url:
            self._open(url)

    def _see_newest(self):
        self._close_watch()
        self.open_results('')

    def _see_upcoming(self):
        self._close_watch()
        self._pick('_section', 'upcoming')

    def _more_menu(self):
        run_menu(self.btn_more, "Deals", [("Open Game-db", G['external'], self._open_site)],
                 self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height())))



def late_imports():
    """Sibling imports, run by deals.py once DealsPage exists: they import
    the window, which imports DealsPage back."""
    global ActionSheet, _initials, ElideLabel, focus_and_reveal, input_mode, Popover
    global clear_layout, EmptyState, FlowLayout, label
    from .actions import ActionSheet
    from .art import _initials
    from .details import ElideLabel
    from .input import focus_and_reveal, input_mode
    from .popover import Popover
    from .widgets import clear_layout, EmptyState, FlowLayout, label
