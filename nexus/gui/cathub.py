"""Pinned-category tabs and the All categories hub (split out of category.py,
w-128)."""

from PyQt6.QtCore import pyqtSignal, QPoint, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget
from .theme import G, scaled, SP
from .widgets import FlowLayout, label, SegmentedControl, set_glyph_icon
from .mosaic import _short, MosaicTile


class CategoryTabs(QWidget):
    """Pinned categories as underline tabs (w-100): Browse (or All games),
    each pin, All categories. `set_tabs` rebuilds the tabs only when they
    change (the same ones just move `current`); `picked(key)` is a click
    or A on one. Titles are cut at TITLE_CHARS, and the pins' shortened
    further, the long ones first, when the strip is too narrow for them
    all (the first and last tab, the way back, stay whole)."""
    picked = pyqtSignal(str)

    TITLE_CHARS = 18
    MIN_TEXT = 40                           # px a squeezed tab keeps for its text

    def __init__(self, height: int = 36, margins: tuple = (0, 0, 0, 0), parent=None):
        super().__init__(parent)
        self.setObjectName("CategoryTabs")
        self._h = height
        self._sig = None
        self._divider_after: str | None = None
        self._titles: dict[str, str] = {}
        self.seg: SegmentedControl | None = None
        self.divider: QFrame | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*margins)
        lay.setSpacing(0)
        # The tabs that don't fit, behind "More" (w-118: a narrow window or
        # a big Size folds the page tabs first).
        self.folded: list[str] = []
        self.more = QPushButton("More")
        self.more.setObjectName("TabItem")
        self.more.setCheckable(True)
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.setFixedHeight(height)
        set_glyph_icon(self.more, G['chevron_down'], 14,
                       'muted', on_role='text')
        self.more.clicked.connect(self._more_menu)
        self.more.hide()
        lay.addWidget(self.more)
        lay.addStretch(1)
        # As wide as the row gives it: the titles fit that (_fit), not the
        # other way round.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height + margins[1] + margins[3])

    def set_tabs(self, tabs, current: str | None, divider_after: str | None = None):
        """`tabs`: [(key, title)] in order; a thin divider follows the tab
        `divider_after` (w-116: Browse · All games │ pins). Never emits
        `picked`."""
        tabs = [(k, t) for k, t in tabs]
        sig = (tuple(tabs), divider_after)
        if sig != self._sig:
            self._sig = sig
            self._divider_after = divider_after
            self._rebuild(tabs, current)
        elif self.seg is not None and self.seg.current() != (current or ''):
            self.seg.set_current(current if current in self._titles else None,
                                 animate=self.isVisible())

    def _rebuild(self, tabs, current):
        old = self.seg
        f = QApplication.focusWidget()
        had = old is not None and f is not None and old.isAncestorOf(f)
        seg = SegmentedControl('underline', self._h, self)
        self._titles = {}
        for key, title in tabs:
            short = _short(title, self.TITLE_CHARS)
            b = seg.add(key, short.replace('&', '&&'))
            b.setToolTip(title)
            b.setAccessibleName(title)
            self._titles[key] = short
        self.divider = None
        keys = [k for k, _ in tabs]
        after = self._divider_after
        if after in keys and keys.index(after) < len(keys) - 1:
            div = QFrame(seg)
            div.setObjectName("TopSep")               # the theme's hairline
            div.setFixedSize(1, max(12, self._h // 2))
            seg.layout().insertWidget(keys.index(after) + 1, div, 0,
                                      Qt.AlignmentFlag.AlignVCenter)
            self.divider = div
        seg.set_current(current if current in self._titles else None, animate=False)
        seg.changed.connect(self.picked)
        lay = self.layout()
        lay.insertWidget(0, seg)                 # in the layout before it shows
        self.seg = seg                           # before show: a resize then fits this one
        self.folded = []                         # a new strip: everything shows until _fit
        self.more.hide()
        seg.show()
        self._fit()
        if had and tabs:                         # focus first, then hide (w-60)
            b = seg.item(current) if current in self._titles else seg.item(tabs[0][0])
            b.setFocus(Qt.FocusReason.OtherFocusReason)
        if old is not None:
            lay.removeWidget(old)
            old.hide()
            old.deleteLater()

    def _fit(self):
        """The titles in the width there is: the shortest stay whole, the
        longer ones share what is left equally, elided. When even short
        titles don't fit, the last tabs (never Browse, All games or the
        current one) fold into More, the rightmost first."""
        seg = self.seg
        if seg is None or not self._titles:
            return
        keys = list(self._titles)
        buttons = {k: seg.item(k) for k in keys}
        if None in buttons.values():
            return
        for b in buttons.values():
            b.ensurePolished()
        b0 = buttons[keys[0]]
        fm = b0.fontMetrics()
        pad = b0.sizeHint().width() - fm.horizontalAdvance(b0.text())
        lay = seg.layout()
        m = lay.contentsMargins()
        om = self.layout().contentsMargins()
        room = self.width() - om.left() - om.right() - m.left() - m.right() \
            - ((self.divider.width() + lay.spacing()) if self.divider is not None else 0)
        textw = {k: fm.horizontalAdvance(self._titles[k]) for k in keys}
        cur = seg.current()
        keep = set(keys[:2]) | {cur}
        shown = list(keys)

        def least(ks):                      # every tab at its shortest (the kept ones whole)
            return sum(pad + (textw[k] if k in keep else min(textw[k], self.MIN_TEXT))
                       + lay.spacing() for k in ks)
        more_w = self.more.sizeHint().width()
        folded = []
        for k in reversed(keys):
            if least(shown) + (more_w if folded else 0) <= room:
                break
            if k in keep:
                continue
            shown.remove(k)
            folded.append(k)
        folded.reverse()
        if folded != self.folded:
            self.folded = folded
            for k in keys:
                seg.set_item_visible(k, k not in folded)
            self.more.setVisible(bool(folded))
        self.more.setChecked(cur in folded)
        avail = room - (more_w if folded else 0) - lay.spacing() * (len(shown) - 1) \
            - pad * len(shown)
        ends = {shown[0], shown[-1]} if len(shown) > 2 and not folded else set()
        ends |= keep & set(shown)                # Browse, All games, the current: whole
        avail -= sum(textw[k] for k in ends)
        need = sorted(textw[k] for k in shown if k not in ends)
        cap = None
        if sum(need) > avail:
            left, n = avail, len(need)           # water-fill: whole while under the cap
            for i, w in enumerate(need):
                if w * (n - i) > left:
                    cap = max(self.MIN_TEXT, left // (n - i))
                    break
                left -= w
        for key in keys:
            b = buttons[key]
            text = self._titles[key]
            if cap is not None and key not in ends and textw[key] > cap:
                text = fm.elidedText(text, Qt.TextElideMode.ElideRight, cap)
            text = text.replace('&', '&&')
            if b.text() != text:
                b.setText(text)

    def _more_menu(self):
        """The folded tabs as a menu; picking one is a tab pick."""
        self.more.setChecked(self.seg is not None and self.seg.current() in self.folded)
        if not self.folded:
            return
        m = QMenu(self)
        for k in self.folded:
            a = m.addAction(self._titles[k].replace('&', '&&'))
            a.setCheckable(True)
            a.setChecked(self.seg is not None and self.seg.current() == k)
            a.triggered.connect(lambda _=False, k=k: self.picked.emit(k))
        m.exec(self.more.mapToGlobal(QPoint(0, self.more.height())))
        m.deleteLater()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange):
            self._fit()

    # ---- what callers ask ----
    def current(self) -> str:
        return self.seg.current() if self.seg is not None else ''

    def keys(self) -> list[str]:
        return list(self._titles)

    def button(self, key: str):
        return self.seg.item(key) if self.seg is not None else None

    def buttons(self) -> list:
        return self.seg.buttons() if self.seg is not None else []


class CategoryHub(QScrollArea):
    """All categories (w-99): every category as a MosaicTile, grouped under
    the same kickers as Browse, rows you hid from Browse included. One of
    the Library's views like CategoryPage (the window routes it as the
    place key categories.HUB); it has no game tiles, so the view calls are
    no-ops and tile_for gives the tile to focus."""
    back = pyqtSignal()                     # the crumb
    open_key = pyqtSignal(str)              # a tile

    TILE_MIN = 260                          # a tile's narrowest (scaled); columns share the rest

    def __init__(self):
        super().__init__()
        self.setObjectName("CategoryHub")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        lay = self._lay = QVBoxLayout(host)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['6'])
        lay.setSpacing(SP['3'])
        self.btn_back = QPushButton("Library")
        self.btn_back.setObjectName("Ghost")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setToolTip("Back to the Library (Esc)")
        set_glyph_icon(self.btn_back, G['chevron_left'], 15, 'subtext')
        self.btn_back.clicked.connect(self.back)
        lay.addWidget(self.btn_back, 0, Qt.AlignmentFlag.AlignLeft)
        self.title = label("All categories", "CatTitle")
        lay.addWidget(self.title)
        self.sections_lay = QVBoxLayout()
        self.sections_lay.setContentsMargins(0, 0, 0, 0)
        self.sections_lay.setSpacing(SP['5'])
        lay.addLayout(self.sections_lay)
        lay.addStretch(1)
        self.setWidget(host)
        self._sig = None
        self._tiles: dict[str, MosaicTile] = {}
        self._sections: dict[str, tuple[QWidget, FlowLayout]] = {}
        self._order: list[str] = []                 # group keys, as shown
        self._last: str | None = None               # the tile opened last (Back lands there)
        self._jump: str | None = None

    # ---- filling ----
    def set_groups(self, groups):
        """Show `groups` (categories.categories(), uncapped). Nothing is
        touched when no tile's text, look or games changed (one game out and
        another in changes the covers); otherwise tiles are kept by key and
        re-pointed, and only new ones are made."""
        sig = tuple((g.key, g.title, tuple((c.key, c.title, c.tip, c.glyph, c.color,
                                            tuple(x.get('slug') for x in c.games))
                                           for c in g.cats))
                    for g in groups)
        if sig == self._sig:
            return
        self._sig = sig
        old_tiles, self._tiles = self._tiles, {}
        old_secs, self._sections = self._sections, {}
        for gkey in self._order:
            if gkey in old_secs:
                self.sections_lay.removeWidget(old_secs[gkey][0])
        for g in groups:
            sec = old_secs.pop(g.key, None)
            if sec is None:
                sec = self._section(g.title)
            box, flow = sec
            inner = flow.parentWidget()
            while flow.count():
                flow.takeAt(0)                       # the tiles stay children of the box
            tiles = []
            for c in g.cats:
                t = old_tiles.pop(c.key, None)
                if t is None:
                    t = MosaicTile(c)
                    t.opened.connect(self._opened)
                else:
                    t.set_category(c)
                if t.parentWidget() is not inner:
                    t.setParent(inner)
                tiles.append(t)
                self._tiles[c.key] = t
            self._size_tiles(tiles)
            flow.add_widgets(tiles)
            self.sections_lay.addWidget(box)
            box.show()
            self._sections[g.key] = sec
        self._order = [g.key for g in groups]
        for t in old_tiles.values():
            t.hide()
            t.deleteLater()
        for box, _flow in old_secs.values():
            box.hide()
            box.deleteLater()

    def tile_width(self) -> int:
        """As many columns as fit tiles of TILE_MIN, sharing the width."""
        m = self._lay.contentsMargins()
        avail = max(1, self.viewport().width() - m.left() - m.right())
        gap, least = SP['4'], scaled(self.TILE_MIN)
        cols = max(1, (avail + gap) // (least + gap))
        return max(least if cols > 1 else 1, (avail - gap * (cols - 1)) // cols)

    def _size_tiles(self, tiles):
        w, h = self.tile_width(), scaled(128)
        for t in tiles:
            if t.width() != w or t.height() != h:
                t.setFixedSize(w, h)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._size_tiles(self._tiles.values())

    def _section(self, title: str) -> tuple[QWidget, FlowLayout]:
        box = QWidget(self.widget())
        box.setObjectName("HubSection")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, SP['2'], 0, 0)
        lay.setSpacing(SP['2'])
        lay.addWidget(label(title.upper(), "SectionKicker"))
        tiles = QWidget()
        flow = FlowLayout(tiles, margin=0, spacing=SP['4'])
        lay.addWidget(tiles)
        return box, flow

    def _opened(self, key: str):
        self._last = key
        self.open_key.emit(key)

    # ---- where to go ----
    def group_keys(self) -> list[str]:
        return list(self._order)

    def tiles_in(self, gkey: str) -> list[MosaicTile]:
        sec = self._sections.get(gkey)
        if sec is None:
            return []
        flow = sec[1]
        return [flow.itemAt(i).widget() for i in range(flow.count())]

    def tile(self, key: str) -> MosaicTile | None:
        return self._tiles.get(key)

    def jump_to(self, gkey: str):
        """Scroll to a group (All genres): now, and once more after the next
        layout pass (a child timer on a bound method, so it dies with the
        hub). The group's first tile becomes tile_for(), where the window
        puts the focus (keys or pad, no overlay up); the hub never moves
        the focus itself, least of all from a timer."""
        tiles = self.tiles_in(gkey)
        if not tiles:
            return
        self._jump = gkey
        self._last = tiles[0].key                   # Back and the focus land here
        self._scroll_to_jump()
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(self._scroll_to_jump)
        t.timeout.connect(t.deleteLater)
        t.start(0)

    def _scroll_to_jump(self):
        sec = self._sections.get(self._jump or '')
        if sec is None:
            return
        self.widget().layout().activate()
        y = sec[0].mapTo(self.widget(), QPoint(0, 0)).y()
        self.verticalScrollBar().setValue(max(0, y - SP['2']))

    # ---- what the window asks of a Library view (no game tiles here) ----
    @property
    def tiles(self) -> dict:
        return {}

    @property
    def games(self) -> dict:
        return {}

    def tile_for(self, slug: str | None = None):
        """The tile to focus: the one opened last, else the first."""
        if slug:
            return None
        t = self._tiles.get(self._last) if self._last else None
        if t is not None:
            return t
        for gkey in self._order:
            ts = self.tiles_in(gkey)
            if ts:
                return ts[0]
        return None

    def shown_games(self) -> list[dict]:
        return []

    def reveal(self, _slug):
        pass

    def mark_selected(self, _slugs):
        pass

    def set_jobs(self, _jobs):
        pass

    def set_friends_badge(self, _on):
        pass

    def refresh_friends(self):
        pass
