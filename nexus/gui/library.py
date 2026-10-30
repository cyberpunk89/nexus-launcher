"""Library views: the grid, the list, the Home shelves and hero carousel."""

import html
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (pyqtSignal, QEasingCurve, QEvent, QItemSelection,
    QItemSelectionModel, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QTimer,
    QVariantAnimation)
from PyQt6.QtGui import (QBrush, QColor, QIcon, QImage, QImageReader, QLinearGradient,
    QPainter, QPainterPath, QPixmap)
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..features import has
from .fmt import fmt_hours, fmt_relative, fmt_size
from .theme import FS, G, GRAD, P, pill_qss, R, SP, TILE_LIFT, TILE_W


class _TileSignals:
    """Mixin for the three library views: they all emit the same signals and
    leave selection bookkeeping to the main window."""

    def _wire(self, tile: 'GameTile'):
        tile.selected.connect(self.clicked)
        tile.play.connect(self.play)
        tile.menu.connect(self.menu)
        tile.activated.connect(self.activated)
        tile.double.connect(self.double)


class LibraryView(QScrollArea, _TileSignals):
    """Reflowing grid of tiles with a centered empty state.

    The empty message is a sibling of the flow rather than an item inside it —
    a FlowLayout sizes each child to its own hint, so a label placed in it would
    sit top-left however it's aligned."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setMinimumWidth(180 + SP['5'] * 2 + 12)
        self.container = QWidget()
        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.flow_host = QWidget()
        self.flow = FlowLayout(self.flow_host, margin=SP['5'], spacing=SP['4'], justify=True)
        outer.addWidget(self.flow_host)
        self._empty = EmptyState(G['library'], "")
        self._empty.setVisible(False)
        outer.addWidget(self._empty, 1)
        outer.addStretch()
        self.setWidget(self.container)
        self.tiles: dict[str, list[GameTile]] = {}
        self.tile_width = TILE_W

    def set_games(self, games, empty_text=None, pending_jobs=()):
        # Tiles are kept and re-pointed at the new game dicts where they can
        # be (same game, same size): a search, a favourite or a library
        # reload then costs a re-layout, not a few hundred new widgets.
        spare: dict[str, list[GameTile]] = {}
        while self.flow.count():
            item = self.flow.takeAt(0)
            w = item.widget()
            if isinstance(w, GameTile) and not w.pending and w.tw == self.tile_width:
                spare.setdefault(w.game.get('slug'), []).append(w)
            elif w is not None:
                w.hide()                       # gone now, not at the next event loop
                w.deleteLater()
        self.tiles = {}
        order = []
        for job in pending_jobs:
            t = GameTile({'slug': f'__pending_{id(job)}', 'display_name': job.name},
                         pending=True, width=self.tile_width)
            t.set_job(job)
            t.selected.connect(lambda _g, _m, j=job: self.pending_clicked.emit(j))
            order.append(t)
        for g in games:
            pool = spare.get(g['slug'])
            if pool:
                tile = pool.pop()
                tile.set_game(g)
            else:
                tile = GameTile(g, width=self.tile_width)
                self._wire(tile)
            order.append(tile)
            self.tiles.setdefault(g['slug'], []).append(tile)
        self.flow.add_widgets(order)
        for pool in spare.values():
            for w in pool:
                w.hide()
                w.deleteLater()
        shown = bool(games) or bool(pending_jobs)
        self._empty.set(*empty_state_text(empty_text))
        self._empty.setVisible(not shown)
        self.flow_host.setVisible(shown)

    def mark_selected(self, slugs: set):
        for s, ts in self.tiles.items():
            for t in ts:
                t.set_selected(s in slugs)

    def reveal(self, slug):
        for t in self.tiles.get(slug, [])[:1]:
            self.ensureWidgetVisible(t)

    def set_jobs(self, jobs: 'JobManager'):
        by_slug = jobs.tile_jobs()
        for slug, ts in self.tiles.items():
            for t in ts:
                t.set_job(by_slug.get(slug))


class KeepPlace:
    """`with KeepPlace(view): rebuild…` — a rebuild of `view`'s shelves
    (a library reload, an install finishing) keeps the keyboard/controller
    user where they were: the same game's tile gets the focus back, and each
    shelf goes back to its sideways offset."""

    def __init__(self, scope: QWidget):
        self.scope = scope

    def _rows(self):
        return [r for r in self.scope.findChildren(ShelfRow) if not r.isHidden()]

    def __enter__(self):
        f = QApplication.focusWidget()
        self.focus = None
        if isinstance(f, GameTile) and self.scope.isAncestorOf(f):
            row = f.parentWidget()
            while row is not None and not isinstance(row, ShelfRow):
                row = row.parentWidget()
            self.focus = (f.game.get('slug'), row.title if row is not None else None)
        self.offsets = {r.title: r.bar.value() for r in self._rows() if r.bar.value()}
        return self

    def __exit__(self, *exc):
        if self.focus is not None or self.offsets:
            # New rows are shown by the layout on the next event loop pass;
            # a hidden tile can't take the focus, so put things back then.
            # A child timer: it dies with the view. (A lambda, not the bound
            # method: PyQt holds a plain object's method only weakly, and
            # this KeepPlace is gone once the `with` block ends.)
            t = QTimer(self.scope)
            t.setSingleShot(True)
            t.timeout.connect(lambda: self._restore())
            t.timeout.connect(t.deleteLater)
            t.start(0)
        return False

    def _restore(self):
        rows = self._rows()
        for r in rows:
            if r.title in self.offsets:
                r.restore_offset(self.offsets[r.title])
        if self.focus is None:
            return
        slug, title = self.focus
        cur = QApplication.focusWidget()
        # Hiding the old tile moved Qt's focus to some neighbour (or nowhere).
        if not (isinstance(cur, GameTile) and not cur.isHidden()
                and cur.game.get('slug') == slug):
            same = [t for r in rows if r.title == title for t in r.tiles
                    if t.game.get('slug') == slug]
            anywhere = [t for t in self.scope.findChildren(GameTile)
                        if t.game.get('slug') == slug and not t.isHidden()]
            target = (same or anywhere or [None])[0]
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)


def shelf_page_target(value: int, maximum: int, viewport_w: int, pitch: int,
                      direction: int) -> int:
    """Where a shelf scrolls for one arrow press: a viewport's worth of whole
    tiles in `direction` (-1 / +1), snapped to the tile pitch, clamped."""
    if maximum <= 0:
        return 0
    if pitch <= 0:
        step = max(1, viewport_w)
        return max(0, min(maximum, value + direction * step))
    step = max(1, viewport_w // pitch) * pitch
    target = round((value + direction * step) / pitch) * pitch
    return max(0, min(maximum, target))


class _EdgeFade(QWidget):
    """Soft fade over a shelf edge, hinting that there's more that way."""

    def __init__(self, parent, side: str):
        super().__init__(parent)
        self.side = side
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, ev):
        pt = QPainter(self)
        g = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        solid = QColor(P['bg']); solid.setAlphaF(0.85)
        clear = QColor(P['bg']); clear.setAlphaF(0.0)
        g.setColorAt(0.0, solid if self.side == 'left' else clear)
        g.setColorAt(1.0, clear if self.side == 'left' else solid)
        pt.fillRect(self.rect(), QBrush(g))
        pt.end()


class _ShelfScroll(QScrollArea):
    """A plain vertical wheel belongs to the page (scrolling a shelf sideways
    under the cursor is a common annoyance); horizontal wheel / Shift+wheel
    scroll the shelf."""

    def wheelEvent(self, ev):
        d = ev.angleDelta()
        if d.x() == 0 and not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            ev.ignore()
            return
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - (d.x() or d.y()))
        ev.accept()


class ShelfRow(QWidget):
    """A titled row of tiles that pages sideways with ‹ › buttons instead of
    a scrollbar. Keyboard/controller focus moving along the row scrolls it
    smoothly so the focused tile is always in view. With `menu`, a round ⋮
    sits before "See all" (so "See all" lines up on every row) and asks for
    the row's menu at a global point. `look` (glyph, colour role[, px]) puts
    a collection's icon before the title."""
    see_all = pyqtSignal()
    menu_asked = pyqtSignal(QPoint)

    def __init__(self, title: str, tile_w: int, kind: str = 'cover', count: int | None = None,
                 see_all: bool = False, menu: bool = False, tip: str = '',
                 look: tuple | None = None):
        super().__init__()
        self.title = title
        self.smart: str | None = None             # a smart shelf's key (HomeView sets it)
        self._want: int | None = None             # an offset to come back to (KeepPlace)
        self.tile_w, self.kind = tile_w, kind
        tile_h = int(tile_w * 9 / 16) if kind == 'wide' else int(tile_w * 3 / 2)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        if look:
            head.addWidget(icon_label(look[0], look[2] if len(look) > 2 else 16, P[look[1]]),
                           0, Qt.AlignmentFlag.AlignVCenter)
        name = self.name_label = label(title, "ShelfTitle")
        name.setToolTip(tip)
        head.addWidget(name)
        self.count_label = None
        if count:
            c = self.count_label = label(str(count), "ShelfCount")
            head.addWidget(c, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch()
        if menu:
            m = icon_button(G['more'], "Shelf options", 28, "Ghost", round_=True)
            m.clicked.connect(lambda: self.menu_asked.emit(
                m.mapToGlobal(QPoint(0, m.height()))))
            self.btn_menu = m
            head.addWidget(m)
        if see_all:
            b = QPushButton("See all")
            b.setObjectName("Ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(self.see_all)
            head.addWidget(b)
        lay.addLayout(head)

        self.body = QWidget()
        self.body.setFixedHeight(tile_h + TILE_LIFT)
        self.scroll = _ShelfScroll(self.body)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self.row = QHBoxLayout(host)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(SP['3'])
        self.scroll.setWidget(host)
        self.fades = {side: _EdgeFade(self.body, side) for side in ('left', 'right')}
        self.prev = icon_button(G['chevron_left'], "Previous", 40, "ShelfArrow")
        self.next = icon_button(G['chevron_right'], "Next", 40, "ShelfArrow")
        for btn, d in ((self.prev, -1), (self.next, 1)):
            btn.setParent(self.body)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # D-pad moves along tiles
            btn.clicked.connect(lambda _=False, d=d: self.page(d))
        self.bar = self.scroll.horizontalScrollBar()
        self.bar.valueChanged.connect(self._sync_arrows)
        self.bar.rangeChanged.connect(self._range_changed)
        self._anim = QPropertyAnimation(self.bar, b"value", self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.body.installEventFilter(self)
        lay.addWidget(self.body)
        self.tiles: list[GameTile] = []

    @property
    def pitch(self) -> int:
        return self.tile_w + self.row.spacing()

    def add(self, tile: 'GameTile'):
        self.row.addWidget(tile)
        tile.installEventFilter(self)
        self.tiles.append(tile)

    def finish(self):
        self.row.addStretch()

    def set_games(self, games: list[dict], make):
        """Point the row at `games` (a refresh): the tiles of games still in
        it are kept and re-pointed at the fresh dicts, `make(game)` makes the
        missing ones. The same games in the same order touch no layout."""
        if [t.game.get('slug') for t in self.tiles] == [g['slug'] for g in games]:
            for t, g in zip(self.tiles, games):
                t.set_game(g)
        else:
            spare: dict[str, list[GameTile]] = {}
            for t in self.tiles:
                spare.setdefault(t.game.get('slug'), []).append(t)
            while self.row.count():
                self.row.takeAt(0)              # tiles stay children of the row
            self.tiles = []
            for g in games:
                pool = spare.get(g['slug'])
                if pool:
                    t = pool.pop(0)
                    t.set_game(g)
                    self.row.addWidget(t)
                    self.tiles.append(t)
                else:
                    self.add(make(g))
            for pool in spare.values():
                for t in pool:
                    t.hide()                    # gone now, not at the next event loop
                    t.deleteLater()
            self.finish()
        if self.count_label is not None:
            self.count_label.setText(str(len(games)))

    def page(self, direction: int):
        self.scroll_to(shelf_page_target(self.bar.value(), self.bar.maximum(),
                                         self.scroll.viewport().width(), self.pitch, direction))

    def scroll_to(self, value: int):
        self._anim.stop()
        self._anim.setDuration(anim_ms(320))
        if self._anim.duration() == 0:
            self.bar.setValue(value)
            return
        self._anim.setStartValue(self.bar.value())
        self._anim.setEndValue(value)
        self._anim.start()

    def _range_changed(self, _lo, hi):
        if self._want is not None:                # laid out now: go back there
            self.bar.setValue(min(self._want, hi))
            if hi >= self._want:
                self._want = None
        self._sync_arrows()

    def restore_offset(self, value: int):
        self._want = value or None
        self.bar.setValue(value)

    def _sync_arrows(self):
        v, mx = self.bar.value(), self.bar.maximum()
        for side, btn, on in (('left', self.prev, v > 0), ('right', self.next, v < mx)):
            btn.setVisible(on)
            self.fades[side].setVisible(on)

    def eventFilter(self, obj, ev):
        if obj is self.body and ev.type() == QEvent.Type.Resize:
            w, h = self.body.width(), self.body.height()
            self.scroll.setGeometry(0, 0, w, h)
            fw = 56
            self.fades['left'].setGeometry(0, 0, fw, h)
            self.fades['right'].setGeometry(w - fw, 0, fw, h)
            y = (h - self.prev.height()) // 2
            self.prev.move(SP['2'], y)
            self.next.move(w - self.next.width() - SP['2'], y)
            for wdg in (*self.fades.values(), self.prev, self.next):
                wdg.raise_()
            self._sync_arrows()
        elif ev.type() == QEvent.Type.FocusIn and obj in self.tiles:
            self._reveal(obj)
        return False

    def _reveal(self, tile):
        vw, v, margin = self.scroll.viewport().width(), self.bar.value(), SP['6']
        x0, x1 = tile.x(), tile.x() + tile.width()
        if x0 < v + margin:
            self.scroll_to(max(0, x0 - margin))
        elif x1 > v + vw - margin:
            self.scroll_to(min(self.bar.maximum(), x1 - vw + margin))


class SmartDivider(QWidget):
    """The line between the source rows and the smart shelves: a glyph,
    "Smart shelves" and a muted note on one baseline (one rich-text label),
    and "N hidden" on the right while shelves are hidden."""
    hidden_menu = pyqtSignal(QPoint)

    def __init__(self, note: str, hidden: int = 0):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, SP['2'], 0, 0)
        lay.setSpacing(SP['3'])
        rule = QFrame()
        rule.setObjectName("SmartRule")
        rule.setFixedHeight(1)
        lay.addWidget(rule)
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        head.addWidget(icon_label(G['sparkles'], 16, P['accent']), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        self.text = label("", "SmartHead", wrap=True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setText(
            "Smart shelves" + (f"&nbsp;&nbsp;<span style='color:{P['muted']}; font-size:"
                               f"{FS['xs']}px; font-weight:400'>{html.escape(note)}</span>"
                               if note else ''))
        head.addWidget(self.text, 1)
        self.btn_hidden = None
        if hidden:
            b = QPushButton(f"{hidden} hidden")
            b.setObjectName("Ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip("Bring back a shelf you hid")
            b.clicked.connect(lambda: self.hidden_menu.emit(
                b.mapToGlobal(QPoint(0, b.height()))))
            self.btn_hidden = b
            head.addWidget(b)
        lay.addLayout(head)


class HeroCarousel(QWidget):
    """The cinematic banner on Home: up to five featured games, full-bleed
    art with the game's logo, a few facts, and Play. It cross-fades to the
    next game every few seconds — never while you're pointing at it, while
    it has focus, while it's hidden or while the window is in the
    background. Portrait-only art gets a blurred backdrop with the cover
    inset, so even repacks without a hero image look intentional."""
    play = pyqtSignal(dict)
    open = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    tint = pyqtSignal(object)            # QColor of the current art

    INTERVAL = 8000

    def __init__(self, scale: float = 1.0):
        super().__init__()
        self.scale = scale
        self.games: list[dict] = []
        self.i = 0
        self._frames: dict[tuple, QPixmap] = {}
        self._sources: dict[tuple, QImage] = {}   # decoded art, a little over banner size
        self._logos: dict[tuple, QPixmap] = {}
        # While the window is being resized the old frame is stretched; the
        # art is rebuilt once, when the size settles.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(120)
        self._settle.timeout.connect(self._settled)
        self._cur = self._prev = None          # (path, is_portrait)
        self._fade = 1.0
        self.setMinimumHeight(int(300 * scale))
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.valueChanged.connect(self._faded)
        self.timer = QTimer(self)
        self.timer.setInterval(self.INTERVAL)
        self.timer.timeout.connect(self._auto)

        lay = QVBoxLayout(self)
        m = int(SP['8'] * 1.5 * scale)
        lay.setContentsMargins(m, m, m, int(SP['6'] * scale))
        lay.setSpacing(int(SP['2'] * scale))
        lay.addStretch()
        self.kicker = label("", "HeroKicker")
        lay.addWidget(self.kicker)
        self.logo = QLabel()
        self.logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.logo)
        self.title = label("", "HeroTitle", wrap=True)
        self.title.setStyleSheet(f"font-size: {int(36 * scale)}px;")
        self.title.setMaximumWidth(int(640 * scale))
        lay.addWidget(self.title)
        self.facts = QHBoxLayout()
        self.facts.setSpacing(SP['1_5'])
        lay.addLayout(self.facts)
        lay.addSpacing(int(SP['2'] * scale))
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        self.btn = QPushButton("Play")
        self.btn.setObjectName("PlayBig")
        set_glyph_icon(self.btn, G['play'], int(20 * scale), 'on_accent', hover_role=None)
        self.btn.setMinimumSize(int(150 * scale), int(44 * scale))
        self.btn.clicked.connect(lambda: self.play.emit(self.game))
        self.btn_details = QPushButton("Details")
        self.btn_details.setObjectName("Glass")
        self.btn_details.setMinimumHeight(int(44 * scale))
        self.btn_details.clicked.connect(lambda: self.open.emit(self.game))
        self.btn_more = icon_button(G['more'], "More actions", int(44 * scale), "GlassIcon")
        self.btn_more.clicked.connect(lambda: self.menu.emit(
            self.game, self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height()))))
        row.addWidget(self.btn)
        row.addWidget(self.btn_details)
        row.addWidget(self.btn_more)
        row.addStretch()
        self.btn_prev = icon_button(G['chevron_left'], "Previous", int(36 * scale), "GlassIcon")
        self.btn_next = icon_button(G['chevron_right'], "Next", int(36 * scale), "GlassIcon")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.dots = QWidget()
        self.dots.setFixedSize(1, int(36 * scale))
        self.dots.paintEvent = self._paint_dots
        self.dots.mousePressEvent = self._dot_clicked
        row.addWidget(self.btn_prev)
        row.addWidget(self.dots)
        row.addWidget(self.btn_next)
        lay.addLayout(row)

    @property
    def game(self) -> dict:
        return self.games[self.i] if self.games else {}

    # ---- data ----
    def set_games(self, games: list[dict]):
        keep = self.game.get('slug')
        self.games = games[:5]
        slugs = [g['slug'] for g in self.games]
        self.i = slugs.index(keep) if keep in slugs else 0
        many = len(self.games) > 1
        for w in (self.btn_prev, self.btn_next, self.dots):
            w.setVisible(many)
        self.dots.setFixedWidth(max(1, len(self.games) * 16))
        if self.games:
            self._show(animate=False)
        if many and MOTION:
            self.timer.start()
        else:
            self.timer.stop()

    def step(self, d: int):
        if len(self.games) > 1:
            self.i = (self.i + d) % len(self.games)
            self._show(animate=True)
            self.timer.start()              # a manual step restarts the clock

    def wheelEvent(self, ev):
        # A sideways swipe (touchpad, tilt wheel) flips through the games; an
        # up/down wheel keeps scrolling the page.
        dx, dy = ev.angleDelta().x(), ev.angleDelta().y()
        if abs(dx) > abs(dy) and dx:
            now = time.monotonic()
            if now - getattr(self, '_swiped', 0.0) > 0.45:    # one step per gesture
                self._swiped = now
                self.step(-1 if dx > 0 else 1)
            ev.accept()
        else:
            ev.ignore()

    def _auto(self):
        f = QApplication.focusWidget()
        if not self.isVisible() or (self.underMouse() and input_mode() == 'mouse') \
                or (f and self.isAncestorOf(f)) \
                or not self.window().isActiveWindow():
            return
        self.step(1)

    def _show(self, animate: bool):
        g = self.game
        playing = bool(g.get('playing'))
        last = fmt_relative(g.get('last_played'))
        self.kicker.setText(("Now playing" if playing else
                             "Starting…" if g.get('launching') else
                             "Continue playing" if g.get('last_played') else
                             "Ready to play").upper())
        self.kicker.setProperty('live', playing)
        self.kicker.style().unpolish(self.kicker)
        self.kicker.style().polish(self.kicker)
        self.btn.setText(play_label(g))
        self.btn.setEnabled(play_enabled(g))
        logo, url = art_source(g, LOGO_KINDS)
        pm = self._logo(logo) if logo else QPixmap()
        if not pm.isNull():
            self.logo.setPixmap(pm)
        elif url:
            ART.fetch(url, lambda _p, slug=g.get('slug'): self._arrived(slug))
        self.logo.setVisible(not pm.isNull())
        self.title.setText(g.get('display_name', ''))
        self.title.setVisible(pm.isNull())
        clear_layout(self.facts)
        tier = g.get('protondb') if has('proton') else None
        facts = [(SOURCE_LABEL.get(g.get('source', 'nexus'), ''), None),
                 (f"Played {last}" if last else '', None),
                 (fmt_hours(g.get('playtime_minutes')) or '', None),
                 (tier.capitalize() if tier in TIER_COLORS else '', TIER_COLORS.get(tier)),
                 (fmt_size(g.get('disk_bytes')) or '', None),
                 ("Update ready" if g.get('update_pending') else '', P['warning'])]
        for text, color in facts:
            if text:
                h = int(24 * self.scale)
                lab = label(text, None)
                lab.setStyleSheet(pill_qss(P['glass'], color or P['on_art'], h,
                                           int(10 * self.scale))
                                  + f" font-size: {int(FS['xs'] * self.scale)}px;")
                lab.setFixedHeight(h)
                self.facts.addWidget(lab)
        self.facts.addStretch()
        path, url = art_source(g, HERO_KINDS)
        if not path and url:
            ART.fetch(url, lambda _p, slug=g.get('slug'): self._arrived(slug))
        nxt = (path, self._is_portrait(path)) if path else None
        if animate and self._cur is not None and MOTION:
            self._prev, self._cur, self._fade = self._cur, nxt, 0.0
            self._anim.stop()
            self._anim.setDuration(450)
            self._anim.start()
        else:
            self._prev, self._cur, self._fade = None, nxt, 1.0
        self.tint.emit(dominant_color(path))
        self.dots.update()
        self.update()

    def _arrived(self, slug):
        try:
            if self.game.get('slug') == slug:
                self._show(animate=False)
        except RuntimeError:
            pass

    def _faded(self, v):
        self._fade = float(v)
        if self._fade >= 1.0:
            self._prev = None
        self.update()

    @staticmethod
    def _is_portrait(path) -> bool:
        size = QImageReader(path).size()
        return size.isValid() and size.height() > size.width()

    def _logo(self, path) -> QPixmap:
        """The game's logo at banner size, scaled once per file."""
        try:
            key = (path, Path(path).stat().st_mtime, self.scale)
        except OSError:
            return QPixmap()
        if key not in self._logos:
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(int(420 * self.scale), int(140 * self.scale),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            if len(self._logos) > 8:
                self._logos.clear()
            self._logos[key] = pm
        return self._logos[key]

    def _source(self, path, mtime, w, h) -> QImage | None:
        """The art decoded to cover w x h (with headroom, so a slightly larger
        window doesn't decode again) — a 4K hero image is decoded once, not
        at every size."""
        key = (path, mtime)
        img = self._sources.get(key)
        if img is not None and img.width() >= w and img.height() >= h:
            return img
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            k = min(1.0, max(w / size.width(), h / size.height()) * 1.5)
            if k < 1.0:
                reader.setScaledSize(QSize(max(1, round(size.width() * k)),
                                           max(1, round(size.height() * k))))
        img = reader.read()
        if img.isNull():
            return None
        if len(self._sources) >= 6:
            self._sources.pop(next(iter(self._sources)))
        self._sources[key] = img
        return img

    # ---- paint ----
    def _frame(self, src) -> QPixmap | None:
        """The art scaled to fill this banner (cached per size)."""
        if src is None:
            return None
        path, portrait = src
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return None
        w, h = self.width(), self.height()
        key = (path, mtime, w, h)
        if key in self._frames:
            return self._frames[key]
        if self._settle.isActive():          # mid-resize: stretch the last frame
            old = next((f for k, f in reversed(self._frames.items()) if k[:2] == key[:2]),
                       None)
            if old is not None:
                return old.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                  Qt.TransformationMode.FastTransformation)
        img = self._source(path, mtime, w, h)
        if img is None:
            return None
        pm = QPixmap.fromImage(img)
        out = QPixmap(w, h)
        out.fill(QColor(P['bg']))
        pt = QPainter(out)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        if portrait:
            # Blurred, darkened backdrop from the cover itself, cover inset right.
            tiny = pm.scaled(24, 36, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            blur = tiny.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
            pt.drawPixmap((w - blur.width()) // 2, (h - blur.height()) // 2, blur)
            pt.fillRect(out.rect(), QColor(0, 0, 0, 90))
            ch = int(h * 0.82)
            cw = int(ch * 2 / 3)
            cover = rounded_pixmap(cover_pixmap(path, cw, ch), R['tile'])
            pt.drawPixmap(int(w - cw - w * 0.08), (h - ch) // 2, cover)
        else:
            s = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
            # Hero art keeps the top (logos / faces) — crop from the upper third.
            pt.drawPixmap((w - s.width()) // 2, -(s.height() - h) // 3, s)
        pt.end()
        if len(self._frames) > 12:
            self._frames.clear()
        self._frames[key] = out
        return out

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._settle.start()

    def _settled(self):
        w, h = self.width(), self.height()
        self._frames = {k: f for k, f in self._frames.items() if k[2:] == (w, h)}
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, R['panel'], R['panel'])
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_1']))
        prev, cur = self._frame(self._prev), self._frame(self._cur)
        if prev is not None and self._fade < 1.0:
            pt.drawPixmap(0, 0, prev)
        if cur is not None:
            pt.setOpacity(self._fade if prev is not None else 1.0)
            pt.drawPixmap(0, 0, cur)
            pt.setOpacity(1.0)
        elif prev is None:
            pt.drawPixmap(0, 0, placeholder_pixmap(self.game.get('display_name', ''),
                                                   self.width(), self.height()))
        base = QColor(P['scrim'])       # the page on dark themes; dark under text on light
        # Left scrim for the text, bottom scrim melting into the page.
        g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
        c = QColor(base); c.setAlphaF(0.86); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.45); g.setColorAt(0.38, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(0.72, c)
        pt.fillRect(r, QBrush(g))
        g = QLinearGradient(0.0, r.bottom(), 0.0, r.top())
        c = QColor(base); c.setAlphaF(0.92); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(0.5, c)
        pt.fillRect(r, QBrush(g))
        # A whisper of the dusk gradient along the top edge.
        g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
        a, b = (QColor(x) for x in GRAD['dusk'])
        a.setAlphaF(0.0); b.setAlphaF(0.35)
        g.setColorAt(0.0, a); g.setColorAt(1.0, b)
        pt.fillRect(QRectF(0, 0, r.width(), 2), QBrush(g))
        pt.end()

    def _paint_dots(self, ev):
        pt = QPainter(self.dots)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(Qt.PenStyle.NoPen)
        cy = self.dots.height() / 2
        for i in range(len(self.games)):
            on = i == self.i
            c = QColor(P['on_art']); c.setAlphaF(0.95 if on else 0.35)
            pt.setBrush(c)
            w = 12.0 if on else 6.0
            pt.drawRoundedRect(QRectF(i * 16 + (16 - w) / 2, cy - 3, w, 6.0), 3.0, 3.0)
        pt.end()

    def _dot_clicked(self, ev):
        i = int(ev.position().x() // 16)
        if 0 <= i < len(self.games) and i != self.i:
            self.i = i
            self._show(animate=True)
            self.timer.start()


def surprise_pick(games: list[dict], rng, recent=(), now: datetime | None = None):
    """"Surprise me": one installed, visible game that isn't running, picked
    at random — weighted toward games never played (×3) or not played for a
    month (×2) and favourites (×1.5), and never one of the `recent` picks
    while there's anything else. None when there's nothing to pick."""
    now = now or datetime.now()
    cands = [g for g in games if is_installed(g) and not g.get('hidden')
             and not g.get('incomplete') and not g.get('playing') and not g.get('launching')]
    fresh = [g for g in cands if g.get('slug') not in recent] or cands
    if not fresh:
        return None

    def weight(g):
        w, lp = 1.0, g.get('last_played')
        if not lp:
            w *= 3
        else:
            try:
                if (now - datetime.fromisoformat(str(lp)[:19])).days >= 30:
                    w *= 2
            except ValueError:
                pass
        return w * (1.5 if g.get('favorite') else 1.0)
    return rng.choices(fresh, weights=[weight(g) for g in fresh])[0]


def featured_games(games: list[dict], n: int = 5) -> list[dict]:
    """What the hero shows: the game being played, then the most recently
    played, then a favourite, an update or something new — no repeats."""
    out: list[dict] = []

    def add(g):
        if g is not None and g not in out and len(out) < n:
            out.append(g)
    for g in games:
        if g.get('playing'):
            add(g)
    played = sorted((g for g in games if g.get('last_played')),
                    key=lambda g: g['last_played'], reverse=True)
    for g in played[:3]:
        add(g)
    add(next((g for g in games if g.get('favorite') and g not in out), None))
    add(next((g for g in games if g.get('update_pending') and g not in out), None))
    fresh = sorted((g for g in games if g.get('added_at') and g not in out),
                   key=lambda g: g['added_at'], reverse=True)
    for g in fresh:
        add(g)
    for g in played[3:]:
        add(g)
    return out


class HomeView(QScrollArea, _TileSignals):
    """Rows of games that page sideways.

    mode='home'    — the Home page: the hero carousel, then the dashboard
                     widgets (shelves like Jump back in, and cards).
    mode='shelves' — the Library's shelf view: one row per source."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)
    see_all = pyqtSignal(str, str)         # (chip, sort) for the Library
    show_filter = pyqtSignal(str)          # a smart shelf's "See all": its filter key
    shelf_menu = pyqtSignal(str, QPoint)   # a smart shelf's ⋮ (key, where)
    hidden_menu = pyqtSignal(QPoint)       # the divider's "N hidden"
    tint = pyqtSignal(object)

    SHELF_TILE = 150
    WIDE_TILE = 300

    def __init__(self, mode: str = 'home'):
        super().__init__()
        self.mode = mode
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.lay = QVBoxLayout(host)
        self.lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['6'])
        self.lay.setSpacing(SP['5'])
        self.hero = HeroCarousel()
        self.hero.play.connect(self.play)
        self.hero.open.connect(self.activated)
        self.hero.menu.connect(self.menu)
        self.hero.tint.connect(self.tint)
        self.hero.setVisible(False)
        self.lay.addWidget(self.hero)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['5'])
        self.lay.addLayout(self.rows)
        self.smart_rows = QVBoxLayout()       # Shelves: the divider and smart shelves
        self.smart_rows.setSpacing(SP['5'])
        self.lay.addLayout(self.smart_rows)
        self.extra = QVBoxLayout()            # Home: the dashboard goes here
        self.lay.addLayout(self.extra)
        self.lay.addStretch()
        self.setWidget(host)
        self.tiles: dict[str, list[GameTile]] = {}
        self._smart: list[QWidget] = []
        self._kept: dict[str, ShelfRow] = {}   # Shelves: the source rows, by title

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        h = self.viewport().height()
        self.hero.setFixedHeight(int(max(320, min(560, h * 0.52))))

    def set_games(self, games, empty_text=None, pending_jobs=()):
        self.tiles = {}             # the dashboard re-adds its shelves' tiles after this
        if self.mode == 'home':
            feat = featured_games(games)
            self.hero.setVisible(bool(feat))
            self.hero.set_games(feat)
            # Everything else on Home is a dashboard widget (MainWindow fills
            # it); only an install in progress gets a row of its own.
            shelves = [("Installing", [], pending_jobs, 'cover', None)]
        else:
            self.hero.setVisible(False)
            shelves = [("Installing", [], pending_jobs, 'cover', None)]
            for src, name in (('nexus', "Repacks"), ('steam', "Steam"),
                              *((('heroic', "Heroic"),) if has('heroic') else ()),
                              ('epic', "Epic")):
                items = sorted((g for g in games if g.get('source', 'nexus') == src),
                               key=lambda g: (g.get('display_name') or '').lower())
                shelves.append((name, items, (), 'cover', (src, 'name')))
        # A source row that's still there is kept, with the tiles of the
        # games still in it: a library reload (after every job) or a
        # favourite re-points a few hundred tiles instead of making them again.
        old, self._kept = self._kept, {}
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w is not None and w not in old.values():
                w.hide()
                w.deleteLater()
        for title, items, jobs, kind, link in shelves:
            if not (items or jobs):
                continue
            row = old.pop(title, None) if not jobs else None
            if row is not None and row.kind == kind:
                row.set_games(items, lambda g, r=row: self._tile(g, r.tile_w, r.kind))
                self.rows.addWidget(row)
                self._track(row)
            else:
                if row is not None:
                    old[title] = row
                row = self._shelf(title, items, jobs, kind, link)
            if not jobs:
                self._kept[title] = row
        for row in old.values():
            row.hide()
            row.deleteLater()
        for w in self._smart:
            if isinstance(w, ShelfRow):
                self._track(w)
        if not games:
            self.set_smart([])
        if not games and not pending_jobs:
            self.rows.addWidget(EmptyState(G['library'], *empty_state_text(empty_text)))

    def set_smart(self, shelves: list[dict], note: str = '', hidden: int = 0):
        """The smart shelves after the source rows (Shelves view): a divider,
        then one row per shelf ({key, title, tip, games}) with a ⋮ menu.
        Nothing at all with no shelf and none hidden. Replaces the last ones,
        keeping the row (and tiles) of a shelf that's still there."""
        old = {}
        for w in self._smart:
            self.smart_rows.removeWidget(w)
            if isinstance(w, ShelfRow):
                self._untrack(w)
                old[(w.smart, w.title)] = w
            else:
                w.hide()
                w.deleteLater()
        self._smart = []
        if shelves or hidden:
            div = SmartDivider(note, hidden)
            div.hidden_menu.connect(self.hidden_menu)
            self.smart_rows.addWidget(div)
            self._smart.append(div)
        for s in shelves:
            row = old.pop((s['key'], s['title']), None)
            if row is not None:
                row.set_games(s['games'], lambda g, r=row: self._tile(g, r.tile_w, r.kind))
                row.name_label.setToolTip(s.get('tip', ''))
                self._track(row)
            else:
                row = self.make_shelf(s['title'], s['games'], smart=s['key'],
                                      tip=s.get('tip', ''))
            self.smart_rows.addWidget(row)
            self._smart.append(row)
        for row in old.values():
            row.hide()
            row.deleteLater()

    def make_shelf(self, title, games, kind='cover', link=None, smart: str | None = None,
                   tip: str = '', look: tuple | None = None) -> 'ShelfRow':
        """A ShelfRow whose tiles are wired to this view (for the dashboard).
        `smart` makes it a smart shelf: "See all" asks for that filter and a
        ⋮ asks for the shelf's menu."""
        return self._shelf(title, games, (), kind, link, add=False, smart=smart, tip=tip,
                           look=look)

    def _shelf(self, title, games, jobs, kind, link, add=True, smart=None, tip='', look=None):
        w = self.WIDE_TILE if kind == 'wide' else self.SHELF_TILE
        row = ShelfRow(title, w, kind, count=len(games) if link or smart else None,
                       see_all=bool(link or smart), menu=bool(smart), tip=tip, look=look)
        row.smart = smart
        if smart:
            row.see_all.connect(lambda k=smart: self.show_filter.emit(k))
            row.menu_asked.connect(lambda pos, k=smart: self.shelf_menu.emit(k, pos))
        elif link:
            row.see_all.connect(lambda c=link[0], s=link[1]: self.see_all.emit(c, s))
        for job in jobs:
            t = GameTile({'slug': f'__pending_{id(job)}', 'display_name': job.name},
                         pending=True, width=self.SHELF_TILE)
            t.set_job(job)
            t.selected.connect(lambda _g, _m, j=job: self.pending_clicked.emit(j))
            row.add(t)
        for g in games:
            t = self._tile(g, w, kind)
            row.add(t)
            self.tiles.setdefault(g['slug'], []).append(t)
        row.finish()
        if add:
            self.rows.addWidget(row)
        return row

    def _tile(self, g, w, kind) -> 'GameTile':
        t = GameTile(g, width=w, kind=kind)
        self._wire(t)
        return t

    def _track(self, row: ShelfRow):
        """A kept row's tiles back in `tiles` (selection, jobs, reveal)."""
        for t in row.tiles:
            if not t.pending:
                self.tiles.setdefault(t.game.get('slug'), []).append(t)

    def _untrack(self, row: ShelfRow):
        for t in row.tiles:
            ts = self.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)

    def _each_tile(self):
        for slug, ts in self.tiles.items():
            for t in list(ts):
                try:
                    t.objectName()
                except RuntimeError:           # deleted with its row
                    ts.remove(t)
                    continue
                yield slug, t

    def mark_selected(self, slugs):
        for s, t in self._each_tile():
            t.set_selected(s in slugs)

    def reveal(self, slug):
        for t in self.tiles.get(slug, [])[:1]:
            focus_and_reveal(t)

    def set_jobs(self, jobs):
        by_slug = jobs.tile_jobs()
        for slug, t in self._each_tile():
            t.set_job(by_slug.get(slug))


class ListView(QTableWidget):
    """Dense list: thumbnail, name, source, size, played, last played, Proton."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)

    COLS = ["", "Game", "Source", "Size", "Played", "Last played", "Proton"]
    NUMERIC = (3, 4)

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(52)
        self.setIconSize(QSize(30, 45))
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in self.AUTO_COLS:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        # Headers line up with their cells: text left, numbers right.
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for i in self.NUMERIC:
            self.horizontalHeaderItem(i).setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hh.setMinimumSectionSize(64)
        self.setColumnHidden(self.COLS.index("Proton"), not has('proton'))
        # No sort column until the user picks one: keep the toolbar's order
        # (otherwise Qt sorts by the empty thumbnail column).
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.setSortingEnabled(True)
        # Keep the last column off the window edge (QTableView owns its
        # viewport margins, so pad via QSS instead).
        self.setObjectName("LibraryList")
        self.games: dict[str, dict] = {}
        self._rows: dict[str, QTableWidgetItem] = {}
        self._syncing = False
        self.cellClicked.connect(self._click)
        self.cellDoubleClicked.connect(
            lambda r, _c: self._game(r) and self.double.emit(self._game(r)))
        self.customContextMenuRequested.connect(self._context)
        self.setContentsMargins(SP['5'], 0, SP['5'], 0)
        # Arrow keys move the details panel along with the highlighted row
        # (a click selects on its own; holding a key down settles first).
        self._cur_timer = QTimer(self)
        self._cur_timer.setSingleShot(True)
        self._cur_timer.setInterval(150)
        self._cur_timer.timeout.connect(self._emit_current)
        self.currentCellChanged.connect(self._current_changed)
        # Covers decoded in the background, set a batch at a time (_set_icons).
        self._icons: dict[str, QPixmap] = {}
        self._icon_timer = QTimer(self)
        self._icon_timer.setSingleShot(True)
        self._icon_timer.setInterval(50)
        self._icon_timer.timeout.connect(self._set_icons)

    def _current_changed(self, row, _col, prev, _pcol):
        if self._syncing or row < 0 or row == prev \
                or QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return
        self._cur_timer.start()

    def _emit_current(self):
        row = self.currentRow()
        if row >= 0 and self._game(row):
            self.clicked.emit(self._game(row), Qt.KeyboardModifier.NoModifier)

    def reset_sort(self):
        """The toolbar's Sort was changed: it wins over a header click."""
        self.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

    def keyPressEvent(self, ev):
        row = self.currentRow()
        if row >= 0 and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self._game(row))
        elif row >= 0 and ev.key() == Qt.Key.Key_Menu:
            rect = self.visualItemRect(self.item(row, 1))
            self.menu.emit(self._game(row), self.viewport().mapToGlobal(rect.center()))
        else:
            super().keyPressEvent(ev)

    def _game(self, row):
        it = self.item(row, 1)
        return self.games.get(it.data(Qt.ItemDataRole.UserRole)) if it else {}

    def _click(self, row, _col):
        mods = QApplication.keyboardModifiers()
        self.clicked.emit(self._game(row), mods)

    def _context(self, pos):
        row = self.rowAt(pos.y())
        if row >= 0:
            self.menu.emit(self._game(row), self.viewport().mapToGlobal(pos))

    AUTO_COLS = (0, 2, 3, 4, 5, 6)

    def set_games(self, games, empty_text=None, pending_jobs=()):
        self._syncing = True
        self.setSortingEnabled(False)
        # Columns sized to their contents re-measure every row on each
        # setItem — a few hundred games took most of a second. Fixed while
        # filling, measured once at the end.
        hh = self.horizontalHeader()
        for i in self.AUTO_COLS:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self.setUpdatesEnabled(False)
        self.games = {g['slug']: g for g in games}
        self._rows: dict[str, QTableWidgetItem] = {}      # slug -> its icon cell
        jobs = list(pending_jobs)
        self.setRowCount(len(jobs) + len(games))
        for r, job in enumerate(jobs):             # installs in progress, on top
            self.setItem(r, 0, QTableWidgetItem())
            it = SortItem(f"Installing — {job.name}", '')
            it.setForeground(QColor(P['subtext']))
            self.setItem(r, 1, it)
            for c in range(2, len(self.COLS)):
                self.setItem(r, c, SortItem('', ''))
        for r, g in enumerate(games, start=len(jobs)):
            icon_item = QTableWidgetItem()
            path = g.get('cover') or (ART.cached(g['cover_url']) if g.get('cover_url') else None)
            if path:                       # decoded off the GUI thread, set when ready
                pm = ART.peek(path, 30, 45)
                if pm is not None:
                    icon_item.setIcon(QIcon(rounded_pixmap(pm, R['sm'] / 1.5)))
                else:                      # by slug: the row may have moved or gone
                    ART.request(path, 30, 45, lambda pm, slug=g['slug']: self._cover(slug, pm))
            self.setItem(r, 0, icon_item)
            self._rows[g['slug']] = icon_item
            cells = [
                (g.get('display_name', g['slug']), (g.get('display_name') or '').lower()),
                (SOURCE_LABEL.get(g.get('source', 'nexus'), ''), g.get('source', '')),
                (fmt_size(g.get('disk_bytes')) or '—', g.get('disk_bytes') or 0),
                (fmt_hours(g.get('playtime_minutes')) or '—', g.get('playtime_minutes') or 0),
                (fmt_relative(g.get('last_played')) or 'never', g.get('last_played') or ''),
                (g.get('proton') or '—', g.get('proton') or ''),
            ]
            for c, (text, key) in enumerate(cells, start=1):
                it = SortItem(text, key)
                if c in self.NUMERIC:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if c == 1:
                    it.setData(Qt.ItemDataRole.UserRole, g['slug'])
                    if g.get('favorite'):
                        it.setText(f"★ {text}")
                self.setItem(r, c, it)
        self.setSortingEnabled(True)
        for i in self.AUTO_COLS:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.setUpdatesEnabled(True)
        self._syncing = False

    def _cover(self, slug, pm):
        if pm is not None:
            self._icons[slug] = pm
            if not self._icon_timer.isActive():
                self._icon_timer.start()

    def _set_icons(self):
        # Each icon set while the icon column sizes itself to its contents
        # re-measured the whole column (~4 ms a cover, seconds for a big
        # library): the batch goes in with it fixed, measured once after.
        icons, self._icons = self._icons, {}
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        for slug, pm in icons.items():
            # By slug through the row's own item (it moves with sorting): a
            # covers-arriving burst used to scan every row for each one.
            it = self._rows.get(slug)
            if it is not None:
                try:
                    it.setIcon(QIcon(rounded_pixmap(pm, R['sm'] / 1.5)))
                except RuntimeError:               # the table was refilled since
                    pass
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)

    def mark_selected(self, slugs):
        self._syncing = True
        sel = QItemSelection()
        last = self.columnCount() - 1
        for r in range(self.rowCount()):
            it = self.item(r, 1)
            if it and it.data(Qt.ItemDataRole.UserRole) in slugs:
                sel.select(self.model().index(r, 0), self.model().index(r, last))
        # One select call: selectRow() in a loop replaces the selection under
        # ExtendedSelection, so only the last row stayed highlighted.
        F = QItemSelectionModel.SelectionFlag
        self.selectionModel().select(sel, F.ClearAndSelect | F.Rows)
        self._syncing = False

    def reveal(self, slug):
        for r in range(self.rowCount()):
            it = self.item(r, 1)
            if it and it.data(Qt.ItemDataRole.UserRole) == slug:
                self.scrollToItem(it)

    def set_jobs(self, jobs):
        pass


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import (anim_ms, ART, art_source, cover_pixmap, dominant_color, HERO_KINDS,
    LOGO_KINDS, MOTION, placeholder_pixmap, rounded_pixmap)  # noqa: E402
from .gameinfo import (is_installed, play_enabled, play_label, SOURCE_LABEL, TIER_COLORS)  # noqa: E402
from .input import focus_and_reveal, input_mode  # noqa: E402
from .storage import SortItem  # noqa: E402
from .tiles import GameTile  # noqa: E402
from .widgets import (clear_layout, empty_state_text, EmptyState, FlowLayout, icon_button,
    icon_label, label, set_glyph_icon)  # noqa: E402
