"""Library category pages (w-98): one page per category of nexus/categories.py
(a smart rule, a genre, your favourites, a collection, a shelf you built, a
store). A cover-mosaic header, the smart cuts as chips, a sort, and Rows /
Grid / List of its games, then More like this.

The page is one of the Library's views (MainWindow.views): it answers to
the same calls as LibraryView and ListView (tiles, games, tile_for, reveal,
mark_selected, set_jobs) and emits the same tile signals, so selection, the
game page, jobs and focus need nothing of their own. Its header and chips
("top") scroll with the games: they are the grid's head (LibraryView
set_head, so the grid stays virtual), the first item of the Rows scroll, or
a strip over the List."""

from PyQt6.QtCore import pyqtSignal, QPoint, Qt
from PyQt6.QtWidgets import QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from .. import categories, smart
from .common import alive
from .library import _TileSignals, LibraryView, ListView, ShelfRow
from .theme import G, scaled, SP
from .tiles import GameTile
from .widgets import clear_layout, FlowLayout, label, SegmentedControl, set_glyph_icon
from .mosaic import (KIND_WORDS, SORT_NAMES, COVER_MAX, WALL_COVERS, KIND_TINT, CONTINUE_CHARS, NO_GAMES, HOME_TILE, BROWSE_TAB, GAMES_TAB, _short, story, stat_items, _played, studio_items, merged_line, story_lines, two_lines, _Mosaic, _HeaderActions, MosaicHeader, StatBlock, _CachedLayer, CoverWallHeader, MosaicTile)  # noqa: F401  (moved; re-exported)
from .studio import (StudioHeader, _StudioButton, StudioChip, StudioCard, _caption, timeline_layout, StudioTimeline, _Progress, game_meta, StudioGameCard)  # noqa: F401  (moved; re-exported)
from .cathub import (CategoryTabs, CategoryHub)  # noqa: F401  (moved; re-exported)


CARD_CAP = 48               # a studio page's game cards before "Show all N games" (w-123)


class _WidthBox(QWidget):
    """A box that says when its width changes (the studio page sizes its
    cards to the columns that fit, scroll bar or not)."""
    width_changed = pyqtSignal(int)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if ev.size().width() != ev.oldSize().width():
            self.width_changed.emit(ev.size().width())


def _section_head(kicker: QLabel, note) -> QHBoxLayout:
    """A section's kicker with a muted note after it, on one baseline."""
    head = QHBoxLayout()
    head.setContentsMargins(0, 0, 0, 0)
    head.setSpacing(SP['3'])
    head.addWidget(kicker, 0, Qt.AlignmentFlag.AlignBottom)
    head.addWidget(note if isinstance(note, QLabel) else label(note, "StudioNote"), 0,
                   Qt.AlignmentFlag.AlignBottom)
    head.addStretch(1)
    return head


class CategoryPage(QWidget, _TileSignals):
    """One category's page in the Library (see the module docstring). The
    window fills it (set_category) from nexus/categories.py and refills it
    as the library changes; the sort and view are kept per category in
    `prefs` (category_sort, category_view).

    `theatre` (w-100): Theatre's page, the header `scale` times as big and
    the grid only, of `tile`-wide Theatre tiles; no cuts, view switch or
    More like this. Its sort is the saved per-category sort (Theatre's
    View sheet sets it); the view is never saved.

    A studio (w-114, a developer or publisher, never in Theatre) gets its
    own page in the same place: StudioHeader for the wall, a Developed /
    Published switch with the merged spellings, the timeline of its years,
    its games as cards (real GameTiles, so the window's calls work as in
    Rows) and Other studios you play. No cuts, sort or view switch; its
    view is never saved."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)
    back = pyqtSignal()                     # the crumb
    open_key = pyqtSignal(str)              # More like this
    play_lead = pyqtSignal(dict)            # Continue <game>
    pick = pyqtSignal()                     # Pick for me
    cat_menu = pyqtSignal(str, QPoint)      # ⋮ (key, where)
    shown_changed = pyqtSignal()            # a cut, sort or view changed what is shown
    pin_asked = pyqtSignal(str)             # Pin / Pinned (the category's key)

    DEFAULT_VIEW = 'rows'
    DEFAULT_SORT = 'played'

    def __init__(self, prefs, theatre: bool = False, scale: float = 1.0,
                 tile: int | None = None):
        super().__init__()
        self.prefs = prefs
        self.theatre = theatre
        self.cat: categories.Category | None = None
        self.cut: str | None = None
        self.view = self.DEFAULT_VIEW
        self._cuts: list[categories.Cut] = []
        self._cut_games: dict[str, list[dict]] = {}
        self._related_keys: tuple = ()
        self._studio_keys: tuple = ()
        self._facts: dict = {}
        self._chip_sig = None
        self._empty = ''
        self._shown: list[dict] = []
        self._rows: dict[str | None, ShelfRow] = {}        # cut key (None: All games) -> row
        self._row_tiles: dict[str, list[GameTile]] = {}
        self._sel: set = set()
        self._jobs = None
        self.setObjectName("CategoryPage")
        # The header and the cuts / sort / view bar: they move between the
        # bodies with the view (_place_top).
        self.top = QWidget()
        self.top.setObjectName("CategoryTop")
        tl = QVBoxLayout(self.top)
        tl.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['2'])
        tl.setSpacing(SP['3'])
        self.header = MosaicHeader(theatre, scale) if theatre else CoverWallHeader()
        self._wall_header = self.header
        self.studio_header: StudioHeader | None = None      # made on the first studio
        self._studio_mode = False
        self._cards_all = False              # a studio page shows CARD_CAP cards until asked (w-123)
        self._others_keys: tuple = ()
        self._cards_sig = None
        self._connect_header(self.header)
        tl.addWidget(self.header)
        self.bar = QWidget()                    # cuts, sort, view (not in Theatre)
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(SP['2'])
        self.chips_box = QWidget()
        self._chips = FlowLayout(self.chips_box, margin=0, spacing=SP['1_5'])
        bar.addWidget(self.chips_box, 1)
        bar.addStretch(0)
        self.sort = QComboBox()
        for text, key in SORT_NAMES:
            self.sort.addItem(text, key)
        self.sort.setAccessibleName("Sort")
        self.sort.currentIndexChanged.connect(lambda _i: self.set_sort(self.sort.currentData()))
        bar.addWidget(self.sort, 0, Qt.AlignmentFlag.AlignTop)
        self.view_seg = SegmentedControl('pill', 34)
        for key, glyph, tip in (('rows', G['shelves'], "Rows — one per smart cut"),
                                ('grid', G['grid'], "Grid"), ('list', G['list'], "List")):
            self.view_seg.add(key, glyph=glyph, tip=tip)
        self.view_seg.changed.connect(self.set_view)
        bar.addWidget(self.view_seg, 0, Qt.AlignmentFlag.AlignTop)
        tl.addWidget(self.bar)
        self.bar.setVisible(not theatre)
        # A studio's switch and timeline (w-114), shown in studio mode only.
        self.studio_bar = QWidget()
        sb = QHBoxLayout(self.studio_bar)
        sb.setContentsMargins(0, SP['2'], 0, 0)
        sb.setSpacing(SP['3'])
        self.role_seg = SegmentedControl('pill', 34)
        self.role_seg.add('dev', "Developed")
        self.role_seg.add('pub', "Published")
        self.role_seg.setAccessibleName("Credit")
        self.role_seg.changed.connect(self._role_picked)
        sb.addWidget(self.role_seg, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.merged = label("", "StudioMerged", wrap=True)
        sb.addWidget(self.merged, 1, Qt.AlignmentFlag.AlignVCenter)
        sb.addStretch(0)
        tl.addWidget(self.studio_bar)
        self.studio_bar.hide()
        self.timeline_wrap = QWidget()
        tw = QVBoxLayout(self.timeline_wrap)
        tw.setContentsMargins(0, SP['5'], 0, 0)
        tw.setSpacing(SP['3'])
        self.timeline_title = label("", "SectionKicker")
        tw.addLayout(_section_head(self.timeline_title, "By release date, sized by hours played"))
        self.timeline = StudioTimeline()
        tw.addWidget(self.timeline)
        tl.addWidget(self.timeline_wrap)
        self.timeline_wrap.hide()
        # Never taller than it needs: a short grid in a tall window would
        # otherwise hand the spare height to the head (a gap over the games).
        self.top.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        # More like this, under the games (Rows and Grid).
        self.foot = QWidget()
        self.foot.setObjectName("CategoryFoot")
        fl = QVBoxLayout(self.foot)
        fl.setContentsMargins(SP['5'], SP['5'], SP['5'], SP['6'])
        fl.setSpacing(SP['3'])
        # Other studios you play (w-114, a studio's page).
        self.others_wrap = QWidget()
        ow = QVBoxLayout(self.others_wrap)
        ow.setContentsMargins(0, 0, 0, 0)
        ow.setSpacing(SP['3'])
        ow.addLayout(_section_head(label("OTHER STUDIOS YOU PLAY", "SectionKicker"),
                                   "Ranked by your hours"))
        self.others_box = _WidthBox()
        self._others = FlowLayout(self.others_box, margin=0, spacing=SP['3'])
        self.others_box.width_changed.connect(self._size_others)
        ow.addWidget(self.others_box)
        fl.addWidget(self.others_wrap)
        self.others_wrap.hide()
        # The studios behind the games (w-113; their group comes with w-112).
        self.studios_wrap = QWidget()
        sw = QVBoxLayout(self.studios_wrap)
        sw.setContentsMargins(0, 0, 0, SP['3'])
        sw.setSpacing(SP['3'])
        sw.addWidget(label("STUDIOS BEHIND THESE GAMES", "SectionKicker"))
        self.studios_box = QWidget()
        self._studios = FlowLayout(self.studios_box, margin=0, spacing=SP['3'])
        sw.addWidget(self.studios_box)
        fl.addWidget(self.studios_wrap)
        self.studios_wrap.hide()
        self.related_wrap = QWidget()
        rw = QVBoxLayout(self.related_wrap)
        rw.setContentsMargins(0, 0, 0, 0)
        rw.setSpacing(SP['3'])
        rw.addWidget(label("MORE LIKE THIS", "SectionKicker"))
        self.related_box = QWidget()
        self._related = FlowLayout(self.related_box, margin=0, spacing=SP['4'])
        rw.addWidget(self.related_box)
        fl.addWidget(self.related_wrap)
        self.related_wrap.hide()
        self.foot.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        # The bodies.
        self.grid = LibraryView()
        self.grid.theatre = theatre
        self.grid.tile_width = tile or getattr(prefs, 'tile', self.grid.tile_width)
        self.listview = ListView()
        for v in (self.grid, self.listview):
            for name in ('clicked', 'play', 'menu', 'activated', 'double', 'pending_clicked'):
                getattr(v, name).connect(getattr(self, name))
        self.rows_view = QScrollArea()
        self.rows_view.setWidgetResizable(True)
        self.rows_view.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_host = QWidget()
        self._rows_lay = QVBoxLayout(self.rows_host)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(0)
        rows_box = QWidget()
        self._row_list = QVBoxLayout(rows_box)
        self._row_list.setContentsMargins(SP['5'], SP['3'], SP['5'], 0)
        self._row_list.setSpacing(SP['5'])
        self._rows_lay.addWidget(rows_box)
        self._rows_empty = label("", "Muted", wrap=True)
        self._row_list.addWidget(self._rows_empty)
        self._rows_empty.hide()
        # A studio's games as cards (w-114), in place of the rows.
        self.studio_games = QWidget()
        gw = QVBoxLayout(self.studio_games)
        gw.setContentsMargins(0, SP['2'], 0, 0)
        gw.setSpacing(SP['3'])
        self.games_note = label("", "StudioNote")
        gw.addLayout(_section_head(label("GAMES", "SectionKicker"), self.games_note))
        self.cards_box = _WidthBox()
        self._cards = FlowLayout(self.cards_box, margin=0, spacing=SP['4'])
        self.cards_box.width_changed.connect(self._size_cards)
        gw.addWidget(self.cards_box)
        self.btn_all_cards = QPushButton("")
        self.btn_all_cards.setObjectName("Ghost")
        self.btn_all_cards.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_all_cards.clicked.connect(self._show_all_cards)
        gw.addWidget(self.btn_all_cards, 0, Qt.AlignmentFlag.AlignLeft)
        self.btn_all_cards.hide()
        self._row_list.addWidget(self.studio_games)
        self.studio_games.hide()
        self._rows_lay.addStretch(1)
        self.rows_view.setWidget(self.rows_host)
        self.list_top = QWidget()               # the strip over the List
        self._list_lay = QVBoxLayout(self.list_top)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        for w in (self.rows_view, self.grid, self.listview):
            self.stack.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.list_foot = QWidget()              # More like this under the List
        self._list_foot_lay = QVBoxLayout(self.list_foot)
        self._list_foot_lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.list_top)
        lay.addWidget(self.stack, 1)
        lay.addWidget(self.list_foot)
        self.list_top.hide()
        self.list_foot.hide()
        self._place_top()

    def _connect_header(self, h):
        h.back.connect(self.back)
        h.play_lead.connect(self.play_lead)
        h.pick.connect(self.pick)
        h.menu.connect(self._header_menu)
        h.pin.connect(self._pin)

    def _header_menu(self, pos: QPoint):
        if self.cat is not None:
            self.cat_menu.emit(self.cat.key, pos)

    # ---- filling ----
    def set_category(self, cat: categories.Category, stats: categories.Stats | None = None,
                     lead: dict | None = None, cuts=(), cut_games: dict | None = None,
                     related=(), empty: str = '', default_sort: str | None = None,
                     facts: dict | None = None, studios=(),
                     studio: categories.StudioFacts | None = None, others=()):
        """Show `cat` (a new one, or the same one refreshed). A new category
        starts on All with its own saved sort and view; a refresh keeps the
        cut while it still has games. `default_sort`: the sort to start a
        new category on when none was chosen for it (a store row's See all
        asks for Name, as the row was). `facts` (slug -> fact) give a smart
        row's covers their line ("2 h left"); `studios`: [(category,
        shared)] for the strip over More like this. A studio (not in
        Theatre) shows as a studio page: `studio` its numbers
        (categories.studio_facts), `others` [(studio, minutes)]
        (categories.other_studios)."""
        new = self.cat is None or self.cat.key != cat.key
        if new:
            self._cards_all = False
        studio_mode = cat.kind in categories.STUDIO_KINDS and not self.theatre
        switched = studio_mode != self._studio_mode
        self.cat = cat
        self._cuts = list(cuts)
        self._cut_games = dict(cut_games or {})
        self._empty = empty
        self._facts = facts or {}
        if new or self.cut not in self._cut_games:
            self.cut = None
        self._set_studio_mode(studio_mode)
        if studio_mode:
            self.header.set_category(cat, stats, lead, studio)
        else:
            self.header.set_category(cat, stats, lead)
        self._fill_chips()
        self._fill_related(() if studio_mode else related)
        self._fill_studios(() if studio_mode else studios)
        self._fill_others(others if studio_mode else ())
        if studio_mode:
            self._fill_studio_top(cat, studio)
        self.foot.setVisible(self._foot_wanted())
        if new or switched:
            sort = self.prefs.category_sort.get(cat.key) or (
                default_sort if default_sort in categories.SORTS else self.DEFAULT_SORT)
            i = self.sort.findData(sort)
            self.sort.blockSignals(True)
            self.sort.setCurrentIndex(max(0, i))
            self.sort.blockSignals(False)
            self._show_view('grid' if self.theatre else 'rows' if studio_mode else
                            self.prefs.category_view.get(cat.key, self.DEFAULT_VIEW))
        self._apply()

    # ---- a studio's page (w-114) ----
    def _set_studio_mode(self, on: bool):
        """Swap the wall header for the studio one (or back), the cuts bar
        for the studio switch. The studio header is made on the first studio,
        in the layout before it shows; the focus moves to the header shown
        before the other hides."""
        if on == self._studio_mode:
            return
        self._studio_mode = on
        if on and self.studio_header is None:
            h = StudioHeader()
            self._connect_header(h)
            tl = self.top.layout()
            tl.insertWidget(tl.indexOf(self._wall_header) + 1, h)
            h.hide()
            self.studio_header = h
        old, new = ((self._wall_header, self.studio_header) if on
                    else (self.studio_header, self._wall_header))
        old_bar, new_bar = (self.bar, self.studio_bar) if on else (self.studio_bar, self.bar)
        new.show()
        new_bar.show()
        f = QApplication.focusWidget()
        if f is not None and (old.isAncestorOf(f) or old_bar.isAncestorOf(f)):
            new.btn_back.setFocus(Qt.FocusReason.OtherFocusReason)    # focus first, then hide
        old.hide()
        old_bar.hide()
        self.header = new
        if not on:
            self.timeline_wrap.hide()
            self._drop_cards()

    def _fill_studio_top(self, cat: categories.Category, sf: categories.StudioFacts | None):
        """The Developed N / Published N switch (a side with fewer than
        STUDIO_MIN games greyed out), the merged names, the timeline."""
        if sf is None:
            n = len(cat.games)
            sf = categories.StudioFacts(n if cat.kind == 'developer' else 0,
                                        n if cat.kind == 'publisher' else 0)
        mine = 'dev' if cat.kind == 'developer' else 'pub'
        for k, word, n in (('dev', "Developed", sf.developed), ('pub', "Published", sf.published)):
            b = self.role_seg.item(k)
            text = f"{word} {n}"
            if b.text() != text:
                b.setText(text)
            ok = k == mine or n >= categories.STUDIO_MIN
            b.setEnabled(ok)
            b.setToolTip("" if ok else f"Fewer than {categories.STUDIO_MIN} of your games")
        if self.role_seg.current() != mine:
            self.role_seg.set_current(mine, animate=False)
        line = merged_line(sf.names)
        self.merged.setText(line)
        self.merged.setVisible(bool(line))
        years = categories.studio_years(cat, self._facts)
        self.timeline.set_years(cat, years)
        self.timeline_title.setText(f"YOUR {cat.title.upper()} YEARS")
        self.timeline_wrap.setVisible(bool(years))

    def _role_picked(self, key: str):
        """The switch: the same studio's other role (the window opens it,
        Back returns). The switch shows this page's role until it does."""
        if self.cat is None:
            return
        mine = 'dev' if self.cat.kind == 'developer' else 'pub'
        if key == mine:
            return
        self.role_seg.set_current(mine, animate=False)
        other = categories.studio_switch(self.cat.key)
        if other:
            self.open_key.emit(other)

    def _fill_others(self, others):
        """Other studios you play, as cards (hidden when none)."""
        others = list(others)
        keys = tuple((c.key, c.title, m, tuple(g.get('slug') for g in c.games))
                     for c, m in others)
        if keys == self._others_keys:
            return
        self._others_keys = keys
        f = QApplication.focusWidget()
        had = f.key if isinstance(f, StudioCard) and self.others_box.isAncestorOf(f) else None
        cards = []
        for c, m in others:
            card = StudioCard(c, m)
            card.opened.connect(self.open_key)
            cards.append(card)
        old = [self._others.itemAt(i).widget() for i in range(self._others.count())]
        while self._others.count():
            self._others.takeAt(0)
        self._size_others(cards=cards)
        self._others.add_widgets(cards)
        again = next((c for c in cards if c.key == had), None)
        if again is None and had is not None:
            again = cards[0] if cards else self.header.btn_back
        if again is not None:
            again.setFocus(Qt.FocusReason.OtherFocusReason)    # before the old ones hide
        for w in old:
            if w is not None:
                w.hide()
                w.deleteLater()
        self.others_wrap.setVisible(bool(cards))

    def _size_others(self, _w=None, cards=None):
        """As many columns as fit cards of 180 px (scaled), up to 6."""
        cards = cards if cards is not None else [
            self._others.itemAt(i).widget() for i in range(self._others.count())]
        gap, least = SP['3'], scaled(180)
        avail = max(1, self.others_box.width())
        cols = max(1, min(6, (avail + gap) // (least + gap)))
        w = max(1, (avail - gap * (cols - 1)) // cols)
        for c in cards:
            if c is not None and c.width() != w:
                c.setFixedWidth(w)

    def _drop_cards(self):
        while self._cards.count():
            w = self._cards.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._cards_sig = None
        self.studio_games.hide()

    def studio_cards(self) -> list:
        return [self._cards.itemAt(i).widget() for i in range(self._cards.count())]

    def _fill_studio_games(self):
        """The studio's games as cards, recently played first. Kept (and
        re-pointed at the fresh game dicts) while what they show is the
        same; else made again, the focus kept on the same game."""
        for row in self._rows.values():
            self._row_list.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows = {}
        everyone = self._shown
        games = everyone if self._cards_all else everyone[:CARD_CAP]
        facts = self._facts
        sig = tuple((g.get('slug'), g.get('display_name'), g.get('playtime_minutes'),
                     g.get('last_played'), categories.credit_line(facts.get(g.get('slug'))),
                     smart.progress(g, facts.get(g.get('slug')))) for g in games)
        if sig == self._cards_sig:
            for card, g in zip(self.studio_cards(), games):
                card.game = g
                card.tile.set_game(g)
        else:
            self._cards_sig = sig
            f = QApplication.focusWidget()
            had = f.game.get('slug') if isinstance(f, GameTile) and \
                self.cards_box.isAncestorOf(f) else None
            old = self.studio_cards()
            while self._cards.count():
                self._cards.takeAt(0)
            cards = [StudioGameCard(g, facts.get(g.get('slug')),
                                    self._tile(g, scaled(96))) for g in games]
            self._size_cards(cards=cards)
            self._cards.add_widgets(cards)
            again = next((c.tile for c in cards if c.game.get('slug') == had), None)
            if again is None and had is not None:
                again = cards[0].tile if cards else self.header.btn_back
            if again is not None:
                again.setFocus(Qt.FocusReason.OtherFocusReason)    # before the old ones hide
            for w in old:
                if w is not None:
                    w.hide()
                    w.deleteLater()
        self._row_tiles = {}
        for card in self.studio_cards():
            self._row_tiles.setdefault(card.game.get('slug'), []).append(card.tile)
            card.tile.set_selected(card.game.get('slug') in self._sel)
        n, shown = len(everyone), len(games)
        self.games_note.setText((f"{shown} of {n}" if shown < n else f"{n}")
                                + "  ·  recently played first")
        self.btn_all_cards.setText(f"Show all {n} games")
        self.btn_all_cards.setVisible(shown < n)
        self.studio_games.setVisible(bool(games))
        self._rows_empty.setText(self._empty or NO_GAMES)
        self._rows_empty.setVisible(not games)
        if self._jobs is not None:
            self.set_jobs(self._jobs)

    def _show_all_cards(self):
        """The cap lifts for this studio (until another page opens)."""
        self._cards_all = True
        self._fill_studio_games()

    def _size_cards(self, _w=None, cards=None):
        """One to three columns of cards at least 380 px (scaled) wide."""
        cards = cards if cards is not None else self.studio_cards()
        gap = SP['4']
        avail = max(1, self.cards_box.width())
        cols = max(1, min(3, (avail + gap) // (scaled(380) + gap)))
        w = max(1, (avail - gap * (cols - 1)) // cols)
        for c in cards:
            if c is not None and c.width() != w:
                c.setFixedWidth(w)

    def set_pin_state(self, state: str):
        """The header's Pin (categories.pin_state)."""
        self.header.set_pin_state(state)

    def _pin(self):
        if self.cat is not None:
            self.pin_asked.emit(self.cat.key)

    def _fill_chips(self):
        sig = (self.cat.key, len(self.cat.games), tuple((c.key, c.count) for c in self._cuts))
        if sig != self._chip_sig:
            self._chip_sig = sig
            clear_layout(self._chips)
            made = [self._chip(None, "All", None, len(self.cat.games))]
            made += [self._chip(c.key, c.title, c.glyph, c.count) for c in self._cuts]
            self._chips.add_widgets(made)
        for b in self.chip_buttons():
            b.setChecked(b._cut == self.cut)

    def _chip(self, key, title, glyph, count) -> QPushButton:
        b = QPushButton(f"{title}  {count}".replace('&', '&&'))
        b.setObjectName("CatCut")
        b.setCheckable(True)
        b.setFixedHeight(scaled(38))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b._cut = key
        if glyph:
            set_glyph_icon(b, glyph, 14, 'subtext', on_role='on_accent')
        b.clicked.connect(lambda _=False, k=key: self.set_cut(k))
        return b

    def chip_buttons(self) -> list[QPushButton]:
        return [self._chips.itemAt(i).widget() for i in range(self._chips.count())
                if self._chips.itemAt(i).widget() is not None]

    def _fill_related(self, related):
        if self.theatre:
            related = ()
        mine = {g.get('slug') for g in self.cat.games} if self.cat is not None else set()
        shared = {c.key: len(mine & {g.get('slug') for g in c.games}) for c in related}
        keys = tuple((c.key, len(c.games), shared[c.key]) for c in related)
        if keys == self._related_keys:
            return
        self._related_keys = keys
        clear_layout(self._related)
        tiles = []
        for c in related:
            t = MosaicTile(c)
            n = len(c.games)
            t.set_note(f"{n} game{'s' if n != 1 else ''} · {shared[c.key]} shared")
            t.opened.connect(self.open_key)
            tiles.append(t)
        self._related.add_widgets(tiles)
        self.related_wrap.setVisible(bool(tiles))

    def _fill_studios(self, studios):
        """The strip of studios behind these games (hidden when none)."""
        if self.theatre:
            studios = ()
        studios = list(studios)
        keys = tuple((c.key, len(c.games), n) for c, n in studios)
        if keys == self._studio_keys:
            return
        self._studio_keys = keys
        clear_layout(self._studios)
        chips = []
        for c, n in studios:
            chip = StudioChip(c, n)
            chip.opened.connect(self.open_key)
            chips.append(chip)
        self._studios.add_widgets(chips)
        self.studios_wrap.setVisible(bool(chips))

    def _foot_wanted(self) -> bool:
        return self._related.count() > 0 or self._studios.count() > 0 \
            or self._others.count() > 0

    # ---- cut, sort, view ----
    def set_cut(self, key: str | None):
        """A cut button. In Rows every cut is already a row: its button
        opens that cut's Grid (as the row's See all does)."""
        if self.view == 'rows':
            if key in self._cut_games:
                self._see_cut(key)
            else:
                self._fill_chips()                   # All: where Rows already is
            return
        self.cut = key if key in self._cut_games else None
        for b in self.chip_buttons():
            b.setChecked(b._cut == self.cut)
        self._apply()

    def set_sort(self, sort: str):
        if sort not in categories.SORTS:
            return
        if self.sort.currentData() != sort:
            self.sort.blockSignals(True)
            self.sort.setCurrentIndex(self.sort.findData(sort))
            self.sort.blockSignals(False)
        if self.cat is not None:
            self._remember(self.prefs.category_sort, sort)
        self._apply()

    def set_view(self, view: str):
        if view not in categories.VIEWS or self.theatre or self._studio_mode:
            return
        if self.cat is not None:
            self._remember(self.prefs.category_view, view)
        if view != self.view:
            self._show_view(view)
            self._apply()

    def _remember(self, store: dict, value: str):
        key = self.cat.key
        if store.get(key) != value:
            store.pop(key, None)                     # the newest last (the cap keeps those)
            store[key] = value
            self.prefs.save()

    def _show_view(self, view: str):
        self.view = 'grid' if self.theatre else \
            view if view in categories.VIEWS else self.DEFAULT_VIEW
        if self.view_seg.current() != self.view:
            self.view_seg.set_current(self.view, animate=False)
        self._place_top()
        self.stack.setCurrentWidget({'rows': self.rows_view, 'grid': self.grid,
                                     'list': self.listview}[self.view])
        if self.view == 'rows':
            self.cut = None

    def _place_top(self):
        """Put the header and bar (and More like this) into the body on
        show. Added to the new layout before they are shown: a widget shown
        while it has no parent is a window."""
        f = QApplication.focusWidget()
        had = f if f is not None and self.top.isAncestorOf(f) else None
        for lay in (self._rows_lay, self._list_lay, self._list_foot_lay):
            for w in (self.top, self.foot):
                if lay.indexOf(w) >= 0:
                    lay.removeWidget(w)
        self.grid.set_head(None)
        self.grid.set_foot(None)
        if self.view == 'grid':
            self.grid.set_head(self.top)
            self.grid.set_foot(self.foot)
        elif self.view == 'rows':
            self._rows_lay.insertWidget(0, self.top)
            self._rows_lay.insertWidget(self._rows_lay.count() - 1, self.foot)
        else:
            self._list_lay.addWidget(self.top)
            self._list_foot_lay.addWidget(self.foot)
        self.list_top.setVisible(self.view == 'list')
        self.list_foot.setVisible(self.view == 'list')
        self.top.show()
        self.foot.setVisible(self._foot_wanted())
        if had is not None and alive(had):
            had.setFocus(Qt.FocusReason.OtherFocusReason)

    def _apply(self):
        """Fill the body on show with the cut, sorted."""
        if self.cat is None:
            return
        sort = 'played' if self._studio_mode else self.sort.currentData() or self.DEFAULT_SORT
        pool = self._cut_games.get(self.cut, self.cat.games) if self.cut else self.cat.games
        self._shown = categories.sort_games(pool, sort)
        empty = self._empty or NO_GAMES
        if self.view == 'grid':
            self.grid.set_games(self._shown, empty)
            self._after_fill(self.grid)
        elif self.view == 'list':
            self.listview.set_games(self._shown, empty)
            self._after_fill(self.listview)
        elif self._studio_mode:
            self._fill_studio_games()
        else:
            self._fill_rows(sort)
        self.shown_changed.emit()

    def _after_fill(self, view):
        view.mark_selected(self._sel)
        if self._jobs is not None:
            view.set_jobs(self._jobs)
        if isinstance(view, LibraryView):
            view.set_friends_badge(self.friends_badge)

    def _fill_rows(self, sort: str):
        plan = [(c.key, c.title, self._cut_games.get(c.key, [])) for c in self._cuts]
        if self.cat.games:
            plan.append((None, "All games", list(self.cat.games)))
        old, self._rows = self._rows, {}
        for row in old.values():
            self._row_list.removeWidget(row)
        self._row_tiles = {}
        for key, title, games in plan:
            items = categories.sort_games(games, sort)[:categories.ROW_CAP]
            row = old.pop(key, None)
            if row is not None and row.title == title:
                row.set_games(items, lambda g, r=row: self._tile(g, r.tile_w))
                if row.count_label is not None:
                    row.count_label.setText(str(len(games)))
            else:
                if row is not None:
                    old[('old', key)] = row
                row = ShelfRow(title, HOME_TILE, count=len(games), see_all=True)
                row.see_all.connect(lambda k=key: self._see_cut(k))
                for g in items:
                    row.add(self._tile(g, HOME_TILE))
                row.finish()
            self._row_list.addWidget(row)
            row.show()
            self._rows[key] = row
            for t in row.tiles:
                self._row_tiles.setdefault(t.game.get('slug'), []).append(t)
                t.set_selected(t.game.get('slug') in self._sel)
        for row in old.values():
            row.hide()
            row.deleteLater()
        self._rows_empty.setText(self._empty or NO_GAMES)
        self._rows_empty.setVisible(not self.cat.games)
        self._tag_rows()
        if self._jobs is not None:
            self.set_jobs(self._jobs)

    def _tag_rows(self):
        """A smart row's covers carry a line: "2 h left" in Almost
        finished, "~12 h story" in Never played (the All games row, in the
        category of that rule)."""
        for key, row in self._rows.items():
            rule = key or self.cat.key
            for t in row.tiles:
                t.set_tag(categories.cover_tag(rule, t.game, self._facts.get(t.game.get('slug'))))

    def _tile(self, g: dict, w: int) -> GameTile:
        t = GameTile(g, width=w)
        self._wire(t)
        return t

    def _see_cut(self, key: str | None):
        """A row's See all (or its title): the grid on that cut."""
        self._show_view('grid')                  # this visit only: nothing saved
        self.cut = key if key in self._cut_games else None
        self._fill_chips()
        self._apply()

    # ---- what the window asks of a Library view ----
    def shown_games(self) -> list[dict]:
        """What Pick for me picks from: the cut in Grid and List, the whole
        category in Rows."""
        return list(self._shown)

    def row_titles(self) -> list[str]:
        return [r.title for r in self.rows()]

    def rows(self) -> list[ShelfRow]:
        return [self._row_list.itemAt(i).widget() for i in range(self._row_list.count())
                if isinstance(self._row_list.itemAt(i).widget(), ShelfRow)]

    def body_widget(self) -> QWidget:
        """The scrolled content of the body on show (renders, tests)."""
        return {'rows': self.rows_host, 'grid': self.grid.container,
                'list': self.listview}[self.view]

    @property
    def tiles(self) -> dict:
        if self.view == 'grid':
            return self.grid.tiles
        return self._row_tiles if self.view == 'rows' else {}

    @property
    def games(self) -> dict:
        if self.view == 'grid':
            return self.grid.games
        if self.view == 'list':
            return self.listview.games
        return {g['slug']: i for i, g in enumerate(self._shown)}

    def tile_for(self, slug: str | None = None):
        if self.view == 'grid':
            return self.grid.tile_for(slug)
        if self.view == 'list':
            return self.listview if self.listview.rowCount() else None
        ts = self._row_tiles.get(slug) if slug else None
        if ts:
            return ts[0]
        if slug:
            return None
        rows = self.rows()
        first = next((r.tiles[0] for r in rows if r.tiles), None)
        if first is None and self._studio_mode:
            first = next((c.tile for c in self.studio_cards()), None)
        return first

    def reveal(self, slug: str):
        if self.view == 'grid':
            self.grid.reveal(slug)
        elif self.view == 'list':
            self.listview.reveal(slug)
        else:
            t = self.tile_for(slug)
            if t is not None:
                self.rows_view.ensureWidgetVisible(t)

    def mark_selected(self, slugs):
        self._sel = set(slugs)
        self.grid.mark_selected(self._sel)
        self.listview.mark_selected(self._sel)
        for s, ts in self._row_tiles.items():
            for t in ts:
                t.set_selected(s in self._sel)

    def set_jobs(self, jobs):
        self._jobs = jobs
        self.grid.set_jobs(jobs)
        by = jobs.tile_jobs()
        for s, ts in self._row_tiles.items():
            for t in ts:
                t.set_job(by.get(s))

    def _each_tile(self):
        for s, ts in self._row_tiles.items():
            for t in ts:
                yield s, t

    def set_friends_badge(self, on: bool):
        _TileSignals.set_friends_badge(self, on)
        self.grid.set_friends_badge(on)

    def refresh_friends(self):
        _TileSignals.refresh_friends(self)
        self.grid.refresh_friends()
