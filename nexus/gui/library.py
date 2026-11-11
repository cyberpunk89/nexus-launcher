"""Library views: the grid, the list, the Home shelves and hero carousel."""

import html
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (pyqtSignal, QEasingCurve, QEvent, QItemSelection,
    QItemSelectionModel, QObject, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QTimer,
    QVariantAnimation)
from PyQt6.QtGui import (QBrush, QColor, QIcon, QImage, QImageReader, QLinearGradient,
    QPainter, QPainterPath, QPixmap)
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import categories, smart
from ..features import has
from .fmt import fmt_hours, fmt_relative, fmt_size
from .theme import (collection_look, FS, G, GRAD, P, pill_qss, R, SP, STAGE_BTN_H, TILE_LIFT,
    TILE_W)


class _TileSignals:
    """Mixin for the three library views: they all emit the same signals and
    leave selection bookkeeping to the main window."""

    def _wire(self, tile: 'GameTile'):
        tile.selected.connect(self.clicked)
        tile.play.connect(self.play)
        tile.menu.connect(self.menu)
        tile.activated.connect(self.activated)
        tile.double.connect(self.double)
        tile.friends_badge = self.friends_badge

    friends_badge = False       # tiles paint who of your friends owns them (w-81)

    def _friend_tiles(self):
        return [t for _slug, t in self._each_tile()]

    def set_friends_badge(self, on: bool):
        """The Friends own filter is on (or off): tiles show (or drop) their
        owners' avatars."""
        on = bool(on)
        if on == self.friends_badge:
            return
        self.friends_badge = on
        for t in self._friend_tiles():
            t.friends_badge = on
            t.update()

    def refresh_friends(self):
        """Friends data changed: tooltips say who owns it, badges repaint."""
        for t in self._friend_tiles():
            t.setToolTip(t._tooltip())
            t.update()

    def _friend_avatars(self):
        """An avatar arrived: repaint the badges that show one."""
        if self.friends_badge:
            for t in self._friend_tiles():
                if t.game.get('friends'):
                    t.update()


class _GridHost(QWidget):
    """The grid's canvas: as tall as every row of the library, though only
    the rows near the view hold tiles (LibraryView places them), so its
    height comes from the count, not from child widgets. The geometry is
    FlowLayout's with `justify` for same-size items: full rows reach both
    edges and the last row keeps their columns."""

    def __init__(self, margin: int, spacing: int):
        super().__init__()
        self.margin, self.spacing = margin, spacing
        self.count = 0
        self.cell = QSize(TILE_W, int(TILE_W * 3 / 2) + TILE_LIFT)
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def set_shape(self, count: int, cell: QSize):
        if count != self.count or cell != self.cell:
            self.count, self.cell = count, QSize(cell)
            self.updateGeometry()

    def columns(self, width: int) -> int:
        avail = width - 2 * self.margin
        return max(1, (avail + self.spacing) // (self.cell.width() + self.spacing))

    def rows(self, width: int) -> int:
        return -(-self.count // self.columns(width))

    def pitch(self) -> int:
        return self.cell.height() + self.spacing

    def xs(self, width: int) -> list[int]:
        """Each column's left edge, rounded the way FlowLayout rounds."""
        cols, cw = self.columns(width), self.cell.width()
        gap = float(self.spacing)
        if self.rows(width) > 1 and cols > 1:      # one row only: plain spacing
            gap = max(gap, (width - 2 * self.margin - cols * cw) / (cols - 1))
        out, x = [], float(self.margin)
        for _ in range(cols):
            out.append(round(x))
            x += cw + gap
        return out

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        rows = self.rows(width)
        return 2 * self.margin + rows * self.cell.height() + max(0, rows - 1) * self.spacing

    def sizeHint(self):
        return QSize(self.cell.width() + 2 * self.margin, self.heightForWidth(self.width()))

    def minimumSizeHint(self):
        return QSize(self.cell.width() + 2 * self.margin,
                     2 * self.margin + (self.cell.height() if self.count else 0))


class LibraryView(QScrollArea, _TileSignals):
    """The grid of tiles, with a centered empty state.

    Virtual (w-50): only the rows on screen, and one more over each edge,
    have tiles, so the arrow keys and the D-pad always find a next row.
    Scrolling hands the tiles that leave to the games that come in
    (`GameTile.set_game`), so 3,000 games cost the tiles of a screen or
    two. The focused tile, and the rows around it, stay wherever the view
    goes. `tiles` holds the built tiles only; `games` maps every shown game
    to its place; `tile_for` builds the tile of a game that has none.

    The empty message is a sibling of the grid rather than inside it."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)

    BUFFER_ROWS = 1          # built beyond each edge of the view
    theatre = False          # Theatre's tiles (a category page there, w-100)

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setMinimumWidth(180 + SP['5'] * 2 + 12)
        self.container = QWidget()
        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.flow_host = _GridHost(SP['5'], SP['4'])
        outer.addWidget(self.flow_host)
        self._empty = EmptyState(G['library'], "")
        self._empty.setVisible(False)
        outer.addWidget(self._empty, 1)
        outer.addStretch()
        self.setWidget(self.container)
        self.tiles: dict[str, list[GameTile]] = {}
        self.games: dict[str, int] = {}          # slug -> place in the grid
        self.tile_width = TILE_W
        self._games: list[dict] = []
        self._pending: list[GameTile] = []       # install jobs: first, always built
        self._built: dict[int, GameTile] = {}    # place -> its game's tile
        self._spare: list[GameTile] = []         # hidden, ready for the next rows
        self._pin: int | None = None             # a place tile_for asked for
        self._sel: set = set()
        self._job_of: dict = {}
        self.flow_host.installEventFilter(self)
        self._head: QWidget | None = None        # over the grid, in its scroll (w-98)
        self._foot: QWidget | None = None        # under it
        FRIENDS.avatar_ready.connect(self._friend_avatars)

    def set_head(self, w: QWidget | None):
        """A widget over the grid that scrolls with it (a category page's
        header and chips); None takes it out. The caller shows it once it
        is in (a widget shown before it has a parent is a window)."""
        self._head = self._slot(self._head, w, foot=False)

    def set_foot(self, w: QWidget | None):
        """A widget under the grid (More like this), in the same scroll."""
        self._foot = self._slot(self._foot, w, foot=True)

    def _slot(self, old, w, foot: bool):
        lay = self.container.layout()
        if old is w:
            return w
        if old is not None and lay.indexOf(old) >= 0:
            lay.removeWidget(old)
            if old.parentWidget() is self.container:
                old.hide()                     # still ours: out of sight
        if w is not None:
            lay.insertWidget(lay.indexOf(self._empty) + 1 if foot else 0, w)
        return w

    def _each_tile(self):
        for t in [*self._built.values(), *self._spare]:
            yield t.game.get('slug'), t

    def set_games(self, games, empty_text=None, pending_jobs=()):
        # Tiles are kept and re-pointed at the new game dicts (same size): a
        # search, a favourite or a library reload re-uses the few tiles
        # there are. The focused one keeps its game, wherever that moved.
        host = self.flow_host
        for t in self._pending:
            t.hide()                           # gone now, not at the next event loop
            t.deleteLater()
        self._pending = []
        for job in pending_jobs:
            t = GameTile({'slug': f'__pending_{id(job)}', 'display_name': job.name},
                         pending=True, width=self.tile_width)
            t.setParent(host)
            t.set_job(job)
            t.selected.connect(lambda _g, _m, j=job: self.pending_clicked.emit(j))
            self._pending.append(t)
        old = [*self._built.values(), *self._spare]
        self._built, self._spare = {}, []
        if old and old[0].tw != self.tile_width:   # a new tile size: new tiles
            for t in old:
                t.hide()
                t.deleteLater()
            old = []
        self._spare = old
        self._games = list(games)
        n = len(self._pending)
        self.games = {}
        for i, g in enumerate(self._games):
            self.games.setdefault(g['slug'], n + i)
        self._pin = None
        f = QApplication.focusWidget()
        if f in old and f.game.get('slug') in self.games:
            self._spare.remove(f)
            self._built[self.games[f.game['slug']]] = f
        host.set_shape(n + len(self._games),
                       QSize(self.tile_width, int(self.tile_width * 3 / 2) + TILE_LIFT))
        self._fill()
        shown = bool(games) or bool(pending_jobs)
        self._empty.set(*empty_state_text(empty_text, repacks_on(self)))
        self._empty.setVisible(not shown)
        host.setVisible(shown)

    def scrollContentsBy(self, dx, dy):
        # The bar has its new value; the rows are filled before the grid
        # moves, so the move's own enter/leave finds the tile now under the
        # mouse (its hover lift), as it did when every tile existed.
        self._fill()
        super().scrollContentsBy(dx, dy)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fill()                           # a taller view shows more rows

    def eventFilter(self, obj, ev):
        if obj is self.flow_host and ev.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._fill()                       # new width: new columns; a head grew
        return super().eventFilter(obj, ev)

    def _focus_place(self) -> int | None:
        f = QApplication.focusWidget()
        return next((i for i, t in self._built.items() if t is f), None)

    def _fill(self):
        """Give the rows near the view their tiles, from the spares first;
        hide the tiles that are left over."""
        host = self.flow_host
        total, n = host.count, len(self._pending)
        width = host.width()
        cols, pitch = host.columns(width), host.pitch()
        want: set[int] = set()
        if total > n:
            vh = self.viewport().height()
            if not self.isVisible():           # not laid out yet: guess a screenful
                scr = self.screen()
                vh = max(vh, scr.availableGeometry().height() if scr else 900)
            # The bar catches up with a shorter grid only at the next layout.
            v = min(self.verticalScrollBar().value(),
                    max(0, host.y() + host.heightForWidth(width) - vh))
            reach = pitch * self.BUFFER_ROWS
            top = v - host.y() - host.margin - reach
            bottom = v + vh - host.y() - host.margin + reach
            first, last = max(0, top // pitch), max(0, bottom // pitch)
            want.update(range(first * cols, min(total, (last + 1) * cols)))
            for anchor in (self._focus_place(), self._pin):
                if anchor is not None and anchor < total:
                    r = anchor // cols             # its row and the rows around it
                    want.update(range(max(0, (r - 1) * cols), min(total, (r + 2) * cols)))
            want.difference_update(range(n))
        for i in [i for i in self._built if i not in want]:
            self._spare.append(self._built.pop(i))
        fresh = {}
        need = sorted(i for i in want if i not in self._built)
        if need:
            # The same game's old tile first (its art is ready), then any.
            by_slug: dict[str, list[GameTile]] = {}
            for t in self._spare:
                by_slug.setdefault(t.game.get('slug'), []).append(t)
            rest = []
            for i in need:
                pool = by_slug.get(self._games[i - n]['slug'])
                if pool:
                    fresh[i] = pool.pop()
                else:
                    rest.append(i)
            left = [t for ts in by_slug.values() for t in ts]
            for i in rest:
                fresh[i] = left.pop() if left else self._make(self._games[i - n])
            self._spare = left
            self._built.update(fresh)
        xs = host.xs(width)
        for i, t in enumerate(self._pending):
            self._place(t, i, cols, pitch, xs)
        tiles: dict[str, list[GameTile]] = {}
        for i in sorted(self._built):
            t, g = self._built[i], self._games[i - n]
            if t.game is not g or i in fresh:
                if t.game is not g:
                    t.set_game(g)
                t.rest()
                t.set_selected(g['slug'] in self._sel)
                t.set_job(self._job_of.get(g['slug']))
            self._place(t, i, cols, pitch, xs)
            tiles.setdefault(g['slug'], []).append(t)
        self.tiles = tiles
        keep = max(len(self._built), 2 * cols)
        for t in self._spare[keep:]:
            t.hide()
            t.deleteLater()
        del self._spare[keep:]
        for t in self._spare:
            if not t.isHidden():
                t.hide()

    def _place(self, t: 'GameTile', i: int, cols: int, pitch: int, xs: list[int]):
        r, c = divmod(i, cols)
        p = QPoint(xs[c], self.flow_host.margin + r * pitch)
        if t.pos() != p:
            t.move(p)
        if t.isHidden():
            t.show()

    def _make(self, g: dict) -> 'GameTile':
        t = GameTile(g, width=self.tile_width, theatre=self.theatre)
        t.setParent(self.flow_host)
        self._wire(t)
        return t

    def tile_for(self, slug: str | None = None) -> 'GameTile | None':
        """The tile of `slug` (of the first game when None), built if its
        row had none; it stays until the next set_games or until it's
        focused and left. None when the game isn't shown."""
        i = self.games.get(slug) if slug else (len(self._pending) if self._games else None)
        if i is None:
            return None
        if i not in self._built:
            self._pin = i
            self._fill()
        return self._built.get(i)

    def mark_selected(self, slugs: set):
        self._sel = set(slugs)
        for s, ts in self.tiles.items():
            for t in ts:
                t.set_selected(s in self._sel)

    def reveal(self, slug):
        t = self.tile_for(slug)
        if t is not None:
            self.ensureWidgetVisible(t)

    def set_jobs(self, jobs: 'JobManager'):
        self._job_of = jobs.tile_jobs()
        for slug, ts in self.tiles.items():
            for t in ts:
                t.set_job(self._job_of.get(slug))


class KeepPlace:
    """`with KeepPlace(view): rebuild…` — a rebuild of `view`'s shelves
    (a library reload, an install finishing) keeps the keyboard/controller
    user where they were: the same game's tile gets the focus back, and each
    shelf goes back to its sideways offset. Rows are told apart by their
    `key` (a category, w-99) or else their title, so a renamed shelf keeps
    its place. With `scroll`, the view's own scroll comes back too (Browse:
    a row taken out above the place would otherwise pull the page up)."""

    def __init__(self, scope: QWidget, scroll: bool = False):
        self.scope = scope
        self._keep_scroll = scroll
        self.scroll = None

    def _rows(self):
        return [r for r in self.scope.findChildren(ShelfRow) if not r.isHidden()]

    @staticmethod
    def _id(row) -> str | None:
        return getattr(row, 'key', None) or row.title

    def _view_scroll(self) -> int | None:
        bar = getattr(self.scope, 'verticalScrollBar', None)
        return bar().value() if bar is not None else None

    def __enter__(self):
        f = QApplication.focusWidget()
        self.focus = None
        if isinstance(f, GameTile) and self.scope.isAncestorOf(f):
            row = f.parentWidget()
            while row is not None and not isinstance(row, ShelfRow):
                row = row.parentWidget()
            self.focus = (f.game.get('slug'), self._id(row) if row is not None else None)
        self.offsets = {self._id(r): r.bar.value() for r in self._rows() if r.bar.value()}
        if self._keep_scroll:
            self.scroll = self._view_scroll() or None
        return self

    def take(self) -> 'KeepPlace':
        """A snapshot to put back later (a category page opened from
        Browse): the focus, each shelf's offset and the view's scroll."""
        self.__enter__()
        self.scroll = self._view_scroll()
        return self

    def put_back(self):
        """Back to the snapshot (`take`), now and once the view is laid
        out again."""
        self._scroll_back()
        self._later()

    def _scroll_back(self):
        if self.scroll is not None:
            self.scope.verticalScrollBar().setValue(self.scroll)

    def __exit__(self, *exc):
        self._scroll_back()
        if self.focus is not None or self.offsets or self.scroll:
            self._later()
        return False

    def _later(self):
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

    def _restore(self):
        self._scroll_back()
        rows = self._rows()
        for r in rows:
            if self._id(r) in self.offsets:
                r.restore_offset(self.offsets[self._id(r)])
        if self.focus is None:
            return
        slug, rid = self.focus
        cur = QApplication.focusWidget()
        # Hiding the old tile moved Qt's focus to some neighbour (or nowhere).
        if not (isinstance(cur, GameTile) and not cur.isHidden()
                and cur.game.get('slug') == slug):
            same = [t for r in rows if self._id(r) == rid for t in r.tiles
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
        self.role = 'bg'              # the colour it fades into; 'scrim' over art (the Stage)
        # Or one end of a GRAD pair, (name, 0 or 1): the Stage's accent
        # gradient (w-90) is a different colour at each edge.
        self.grad: tuple[str, int] | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def color(self) -> QColor:
        return QColor(GRAD[self.grad[0]][self.grad[1]] if self.grad else P[self.role])

    def paintEvent(self, ev):
        pt = QPainter(self)
        g = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        solid = self.color(); solid.setAlphaF(0.85)
        clear = self.color(); clear.setAlphaF(0.0)
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
    focused = pyqtSignal(object)              # a tile of this row got focus
    hovered = pyqtSignal(object)              # the mouse came onto a tile
    unhovered = pyqtSignal(object)            # ... and left it

    def __init__(self, title: str, tile_w: int, kind: str = 'cover', count: int | None = None,
                 see_all: bool = False, menu: bool = False, tip: str = '',
                 look: tuple | None = None, tile_h: int | None = None):
        super().__init__()
        self.title = title
        self.smart: str | None = None             # a smart shelf's key (HomeView sets it)
        self.key: str | None = None               # its category (Browse, w-99): KeepPlace
        self.look = look
        self._want: int | None = None             # an offset to come back to (KeepPlace)
        self.tile_w, self.kind = tile_w, kind
        tile_h = tile_h or (int(tile_w * 9 / 16) if kind == 'wide' else int(tile_w * 3 / 2))
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
        self._title_link = see_all            # the title opens the page too (w-98)
        if see_all:
            name.setCursor(Qt.CursorShape.PointingHandCursor)
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
        if see_all:                           # last: eventFilter needs the parts above
            name.installEventFilter(self)

    @property
    def pitch(self) -> int:
        return self.tile_w + self.row.spacing()

    def set_empty(self, text: str):
        """No tiles: a muted line instead of a tile-high gap."""
        self.body.hide()
        self.layout().addWidget(label(text, "Muted"))

    def add(self, tile: 'GameTile'):
        self.row.addWidget(tile)
        tile.installEventFilter(self)
        self.tiles.append(tile)

    def finish(self):
        self.row.addStretch()

    def set_title(self, title: str, tip: str = ''):
        """A new name for the row (a shelf renamed): the row stays."""
        self.title = title
        if self.name_label.text() != title:
            self.name_label.setText(title)
        self.name_label.setToolTip(tip)

    def set_games(self, games: list[dict], make, count: int | None = None):
        """Point the row at `games` (a refresh): the tiles of games still in
        it are kept and re-pointed at the fresh dicts, `make(game)` makes the
        missing ones. The same games in the same order touch no layout.
        `count`: what the count says when the row shows only some (a cap)."""
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
            self.count_label.setText(str(len(games) if count is None else count))

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
        elif obj is self.name_label and ev.type() == QEvent.Type.MouseButtonRelease \
                and ev.button() == Qt.MouseButton.LeftButton and self._title_link \
                and self.name_label.rect().contains(ev.position().toPoint()):
            self.see_all.emit()
        elif ev.type() == QEvent.Type.FocusIn and obj in self.tiles:
            self._reveal(obj)
            self.focused.emit(obj)
        elif ev.type() == QEvent.Type.Enter and obj in self.tiles:
            self.hovered.emit(obj)
        elif ev.type() == QEvent.Type.Leave and obj in self.tiles:
            self.unhovered.emit(obj)
        return False

    def _reveal(self, tile):
        vw, v, margin = self.scroll.viewport().width(), self.bar.value(), SP['6']
        x0, x1 = tile.x(), tile.x() + tile.width()
        if x0 < v + margin:
            self.scroll_to(max(0, x0 - margin))
        elif x1 > v + vw - margin:
            self.scroll_to(min(self.bar.maximum(), x1 - vw + margin))


class BrowseKicker(QWidget):
    """A group's kicker on Browse ("FOR YOU", "GENRES"…), with an "All
    genres" link on Genres: the hub, at that group."""
    hub = pyqtSignal(str)

    LINKS = {'genres': "All genres"}

    def __init__(self, gkey: str, title: str):
        super().__init__()
        self.gkey = gkey
        self.setObjectName("BrowseKicker")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        self.text = label(title.upper(), "SectionKicker")
        lay.addWidget(self.text, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addStretch(1)
        self.btn_all = None
        if gkey in self.LINKS:
            b = self.btn_all = QPushButton(self.LINKS[gkey])
            b.setObjectName("Ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip("Every genre, in All categories")
            set_glyph_icon(b, G['grid'], 14, 'subtext', hover_role='text')
            b.clicked.connect(lambda: self.hub.emit(self.gkey))
            lay.addWidget(b)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)


class BrowseHead(QWidget):
    """The top of Browse: a jump chip per group (scrolls there), then on
    the right All categories (the hub), "N hidden" (bring a row back) and
    New shelf; under them a muted note while the facts index is filling."""
    jump = pyqtSignal(str)
    hub = pyqtSignal()
    hidden_menu = pyqtSignal(QPoint)
    new_shelf = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("BrowseHead")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        self.bar = WrapRow(SP['2'])
        self.bar.left.setSpacing(SP['1_5'])
        self._chips: dict[str, QPushButton] = {}
        self._chip_sig = None
        self.bar.left.addStretch(1)
        right = QWidget()
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(SP['1'])
        self.btn_hub = self._ghost("All categories", G['grid'], "Every category, as tiles")
        self.btn_hub.clicked.connect(self.hub)
        self.btn_hidden = self._ghost("", None, "Bring back a row you hid")
        self.btn_hidden.clicked.connect(lambda: self.hidden_menu.emit(
            self.btn_hidden.mapToGlobal(QPoint(0, self.btn_hidden.height()))))
        self.btn_new = self._ghost("New shelf", G['plus'], "Make your own shelf from rules")
        self.btn_new.clicked.connect(self.new_shelf)
        for b in (self.btn_hub, self.btn_hidden, self.btn_new):
            rl.addWidget(b)
        lay.addWidget(self.bar)
        self.note = label("", "Muted", wrap=True)
        self.note.hide()
        lay.addWidget(self.note)
        self.bar.set_right(right)                 # last: its filter reads the parts above
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    @staticmethod
    def _ghost(text: str, glyph: str | None, tip: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("Ghost")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        if glyph:
            set_glyph_icon(b, glyph, 15, 'subtext', hover_role='text')
        return b

    def set_state(self, groups: list[tuple[str, str]], note: str = '', hidden: int = 0,
                  can_add: bool = False):
        """`groups`: (key, title) of the groups Browse shows, in order."""
        sig = tuple(groups)
        if sig != self._chip_sig:
            self._chip_sig = sig
            old, self._chips = self._chips, {}
            while self.bar.left.count():
                self.bar.left.takeAt(0)
            for gkey, title in groups:
                b = old.pop(gkey, None)
                if b is None:
                    b = QPushButton(title)
                    b.setObjectName("Chip")
                    b.setCursor(Qt.CursorShape.PointingHandCursor)
                    b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
                    b.setToolTip(f"Jump to {title}")
                    set_glyph_icon(b, categories.GROUP_GLYPH.get(gkey, G['grid']), 14,
                                   'subtext', hover_role='text')
                    b.clicked.connect(lambda _=False, k=gkey: self.jump.emit(k))
                self.bar.left.addWidget(b)
                self._chips[gkey] = b
            self.bar.left.addStretch(1)
            for b in old.values():
                b.hide()
                b.deleteLater()
        self.btn_hub.setVisible(bool(groups))
        self.btn_hidden.setText(f"{hidden} hidden")
        self.btn_hidden.setVisible(hidden > 0)
        self.btn_new.setVisible(can_add)
        self.note.setText(note)
        self.note.setVisible(bool(note))

    def chips(self) -> dict[str, QPushButton]:
        return dict(self._chips)


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
        # The Play row; the Stage adds For you now's pill at its end (w-89).
        row = self.actions = QHBoxLayout()
        row.setSpacing(SP['2'])
        btn_h = int(STAGE_BTN_H * scale)
        self.btn = QPushButton("Play")
        self.btn.setObjectName("PlayBig")
        set_glyph_icon(self.btn, G['play'], int(20 * scale), 'on_accent', hover_role=None)
        self.btn.setMinimumSize(int(150 * scale), btn_h)
        self.btn.clicked.connect(lambda: self.play.emit(self.game))
        self.btn_details = QPushButton("Details")
        self.btn_details.setObjectName("Glass")
        self.btn_details.setMinimumHeight(btn_h)
        self.btn_details.clicked.connect(lambda: self.open.emit(self.game))
        self.btn_more = icon_button(G['more'], "More actions", btn_h, "GlassIcon")
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
        self._fill_facts(g)
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
        self.tint.emit(self._tint(path))
        self.dots.update()
        self.update()

    def _fill_facts(self, g: dict):
        """The pills under the logo (Theatre's spotlight shows one line)."""
        last = fmt_relative(g.get('last_played'))
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
                self.facts.addWidget(self._fact(text, color))
        self.facts.addStretch()

    def _fact(self, text: str, color: str | None) -> QLabel:
        """One fact pill: on glass, `color` or on_art."""
        h = int(24 * self.scale)
        lab = label(text, None)
        lab.setStyleSheet(pill_qss(P['glass'], color or P['on_art'], h, int(10 * self.scale))
                          + f" font-size: {int(FS['xs'] * self.scale)}px;")
        lab.setFixedHeight(h)
        return lab

    def _tint(self, path):
        """The colour the window's background takes from the art."""
        return dominant_color(path)

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
    def _frame(self, src, size: tuple[int, int] | None = None) -> QPixmap | None:
        """The art scaled to fill this banner, or `size` (Theatre paints the
        spotlight's art full screen); cached per size."""
        if src is None:
            return None
        path, portrait = src
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return None
        w, h = size or (self.width(), self.height())
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


class Dwell(QObject):
    """Act on a value once it has rested for a while: focus moving along a
    row (or a mouse resting on a tile) only counts where it stops, so
    holding the D-pad doesn't strobe the stage or a tint. `push(value)`
    restarts the wait; `settle(value)` runs when it ends. With Motion off
    (read at push time) it settles at once."""

    def __init__(self, ms: int, settle, parent=None):
        super().__init__(parent)
        self.ms = ms
        self.settle = settle
        self.value = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._fire)

    def push(self, value, ms: int | None = None):
        self.value = value
        wait = anim_ms(self.ms if ms is None else ms)
        if wait:
            self.timer.start(wait)
        else:
            self.timer.stop()
            self._fire()

    def cancel(self):
        self.timer.stop()
        self.value = None

    def _fire(self):
        self.settle(self.value)


class FollowHero(HeroCarousel):
    """A hero that shows one game and moves only when told (`show_game`):
    no timer, no arrows or dots, no swipes. A running or starting game is
    pinned and stays until it ends. Theatre's spotlight and the desk Home's
    Stage build on it."""

    def __init__(self, scale: float = 1.0):
        super().__init__(scale=scale)
        self.timer.stop()
        self.pinned: dict | None = None          # the running game, while there is one
        for w in (self.btn_prev, self.btn_next, self.dots):
            w.hide()
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_games(self, games: list[dict]):
        """The game to show first: the one running or starting (pinned),
        else the fresh copy of the one shown, if it's still there; `show_game`
        moves it from then on, unless one is pinned."""
        self.pinned = now_playing(games)
        keep = self.game.get('slug')
        cur = self.pinned or next((g for g in games if g.get('slug') == keep), None) or \
            (games[0] if games else None)
        moved = cur is not None and cur.get('slug') != keep and self._cur is not None
        self.games, self.i = ([cur] if cur else []), 0
        if self.games:
            self._show(animate=moved)

    def show_game(self, g: dict):
        if self.pinned is not None:
            return                               # the running game stays
        if g.get('slug') != self.game.get('slug'):
            self.games, self.i = [g], 0
            self._show(animate=True)

    def is_pinned(self) -> bool:
        return self.pinned is not None and self.pinned.get('slug') == self.game.get('slug')

    def step(self, d: int):
        pass                                     # no rotation

    def wheelEvent(self, ev):
        ev.ignore()


def _pickable(games: list[dict]) -> list[dict]:
    """What a random pick may land on: installed, visible, finished, and
    not running or starting."""
    return [g for g in games if is_installed(g) and not g.get('hidden')
            and not g.get('incomplete') and not g.get('playing') and not g.get('launching')]


# Pick for me's vibe chips (w-91): (key, chip label). Every key but 'any' is
# a smart rule (nexus/smart.py), so a chip only shows when that rule has games.
VIBES = (('any', "Any"), ('short', "Quick session"), ('coop', "Co-op"),
         ('unfinished', "Unfinished"), ('almost', "Almost done"), ('never', "Never played"))
VIBE_LABELS = dict(VIBES)


def vibe_pools(games: list[dict], facts: dict | None, now: datetime | None = None,
               linux: bool = True) -> dict[str, list[dict]]:
    """{vibe: [games]} over what surprise_pick may pick, only the vibes with
    at least one game, 'any' first. Without facts (HowLongToBeat, store
    modes) there is no short, co-op or almost-done pool."""
    base = _pickable(games)
    if not base:
        return {}
    out = {'any': base}
    for key, _label in VIBES[1:]:
        hit = smart.games_for(f'smart:{key}', base, facts or {}, now, linux=linux)
        if hit:
            out[key] = hit
    return out


KICKER_TITLE_MAX = 24


def pick_kicker(vibe: str = 'any', pin_title: str | None = None) -> str:
    """The Stage's kicker over a pick: 'PICKED FOR YOU', plus the vibe
    ('PICKED FOR YOU · CO-OP'); over Pinned art it says the pin stays
    ('PICKED FOR YOU · HADES STAYS PINNED', the title cut at 24)."""
    if pin_title:
        t = pin_title if len(pin_title) <= KICKER_TITLE_MAX \
            else pin_title[:KICKER_TITLE_MAX - 1].rstrip() + "…"
        return f"PICKED FOR YOU  ·  {t.upper()} STAYS PINNED"
    if vibe in VIBE_LABELS and vibe != 'any':
        return f"PICKED FOR YOU  ·  {VIBE_LABELS[vibe].upper()}"
    return "PICKED FOR YOU"


def surprise_pick(games: list[dict], rng, recent=(), now: datetime | None = None):
    """"Surprise me": one installed, visible game that isn't running, picked
    at random — weighted toward games never played (×3) or not played for a
    month (×2) and favourites (×1.5), and never one of the `recent` picks
    while there's anything else. None when there's nothing to pick."""
    now = now or datetime.now()
    cands = _pickable(games)
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
    """What the hero shows: the game being played (or starting), then the
    most recently played, then a favourite, an update or something new — no repeats."""
    out: list[dict] = []

    def add(g):
        if g is not None and g not in out and len(out) < n:
            out.append(g)
    for g in games:
        if g.get('playing'):
            add(g)
    for g in games:                     # then one that's starting (Theatre pins it)
        if g.get('launching'):
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

    mode='home'    — the Home page: the dashboard widgets (the Stage,
                     shelves and cards).
    mode='shelves' — the Library's Browse view (w-99): a head (jump chips,
                     All categories, N hidden, New shelf), the Installing
                     row, then the category groups of nexus/categories.py,
                     each under a kicker, one row per category."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)
    see_all = pyqtSignal(str, str)         # (chip, sort): a Home source row's See all
    show_filter = pyqtSignal(str)          # a row's See all or title: its category key
    shelf_menu = pyqtSignal(str, QPoint)   # a row's ⋮ (key, where)
    hidden_menu = pyqtSignal(QPoint)       # Browse's "N hidden"
    new_shelf = pyqtSignal()               # Browse's "New shelf"
    open_hub = pyqtSignal(str)             # All categories ('') / All genres (a group key)

    SHELF_TILE = 150
    WIDE_TILE = 300
    WIDE_KEYS = ('smart:continue',)        # Browse rows with wide tiles

    def __init__(self, mode: str = 'home'):
        super().__init__()
        self.mode = mode
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.lay = QVBoxLayout(host)
        self.lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['6'])
        self.lay.setSpacing(SP['5'])
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['5'])
        self.lay.addLayout(self.rows)
        self.group_lay = QVBoxLayout()        # Browse: the kickers and category rows
        self.group_lay.setSpacing(0)          # set per gap (_relayout): a kicker hugs its row
        self.lay.addLayout(self.group_lay)
        self.extra = QVBoxLayout()            # Home: the dashboard goes here
        self.lay.addLayout(self.extra)
        self.lay.addStretch()
        self.setWidget(host)
        self.tiles: dict[str, list[GameTile]] = {}
        self._group_rows: dict[str, ShelfRow] = {}       # Browse: category key -> row
        self._kickers: dict[str, BrowseKicker] = {}       # Browse: group key -> kicker
        self._order: list[QWidget] = []                   # Browse: group_lay's widgets
        self.head = None
        if mode == 'shelves':
            # Into the layout before it is shown (a parentless shown widget
            # is a window).
            self.head = BrowseHead()
            self.head.hide()
            self.head.jump.connect(self.jump_to)
            self.head.hub.connect(lambda: self.open_hub.emit(''))
            self.head.hidden_menu.connect(self.hidden_menu)
            self.head.new_shelf.connect(self.new_shelf)
            self.lay.insertWidget(0, self.head)
        FRIENDS.avatar_ready.connect(self._friend_avatars)

    def set_games(self, games, empty_text=None, pending_jobs=()):
        """The Installing row (Home and Browse) and the empty state; Browse's
        category rows come from set_groups."""
        self.tiles = {}             # the dashboard re-adds its shelves' tiles after this
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        if pending_jobs:
            self._shelf("Installing", [], pending_jobs, 'cover', None)
        for row in self._group_rows.values():
            self._track(row)
        if self.mode == 'shelves' and not games:
            self.set_groups([])
        if not games and not pending_jobs:
            self.rows.addWidget(EmptyState(
                G['library'], *empty_state_text(empty_text, repacks_on(self))))

    def set_groups(self, groups, note: str = '', hidden: int = 0, can_add: bool = False):
        """Browse's groups (categories.browse()): a kicker each, then one row
        per category ({see all, ⋮}, at most categories.ROW_CAP tiles, the
        count says all). A row that's still there with games is kept and
        re-pointed (its tiles too); one with no game is made fresh, saying
        so (a shelf of yours). The same rows in the same order touch no
        layout. `note`, `hidden` and `can_add` are for the head."""
        if self.head is None:
            return
        self.head.set_state([(g.key, g.title) for g in groups], note, hidden, can_add)
        self.head.setVisible(bool(groups or hidden or can_add))
        old_rows, self._group_rows = self._group_rows, {}
        old_kick, self._kickers = self._kickers, {}
        order: list[QWidget] = []
        for g in groups:
            k = old_kick.pop(g.key, None)
            if k is None:
                k = BrowseKicker(g.key, g.title)
                k.hub.connect(self.open_hub)
            self._kickers[g.key] = k
            order.append(k)
            for c in g.cats:
                kind = 'wide' if c.key in self.WIDE_KEYS else 'cover'
                look = self._look(c)
                games = list(c.games[:categories.ROW_CAP])
                row = old_rows.pop(c.key, None)
                if row is not None:
                    self._untrack(row)
                if row is not None and row.kind == kind and row.look == look and games \
                        and row.tiles:
                    row.set_games(games, lambda gm, r=row: self._tile(gm, r.tile_w, r.kind),
                                  count=len(c.games))
                    row.set_title(c.title, c.tip)
                else:
                    if row is not None:                # was empty, or is now: a fresh row
                        self._drop(row)
                    row = self._browse_row(c, kind, look, games)
                self._track(row)
                self._group_rows[c.key] = row
                order.append(row)
        for w in list(old_rows.values()) + list(old_kick.values()):
            self._drop(w)
        if order != self._order:
            self._relayout(order)

    def _relayout(self, order: list[QWidget]):
        """group_lay in `order`, with a row's gap between rows, a wider one
        before a kicker and a small one under it. The widgets stay children
        of the view while they're out."""
        while self.group_lay.count():
            self.group_lay.takeAt(0)
        prev = None
        for w in order:
            if prev is not None:
                self.group_lay.addSpacing(SP['2'] if isinstance(prev, BrowseKicker)
                                          else SP['6'] if isinstance(w, BrowseKicker)
                                          else SP['5'])
            self.group_lay.addWidget(w)
            prev = w
        self._order = order

    def _drop(self, w: QWidget):
        if isinstance(w, ShelfRow):
            self._untrack(w)
        if self.group_lay.indexOf(w) >= 0:
            self.group_lay.removeWidget(w)      # the order differs now: _relayout follows
        w.hide()                                    # gone now, not at the next event loop
        w.deleteLater()

    @staticmethod
    def _look(c) -> tuple | None:
        """The glyph before a Yours row's title: ★ in gold, a collection's
        own icon and colour (as its chip)."""
        if c.kind == 'fav':
            return (c.glyph, 'col_gold' if 'col_gold' in P else 'accent')
        if c.kind == 'collection':
            return collection_look(categories.collection_name(c.key))
        return None

    def _browse_row(self, c, kind: str, look, games: list[dict]) -> 'ShelfRow':
        w = self.WIDE_TILE if kind == 'wide' else self.SHELF_TILE
        row = ShelfRow(c.title, w, kind, count=len(c.games), see_all=True, menu=True,
                       tip=c.tip, look=look)
        row.key = c.key
        row.see_all.connect(lambda k=c.key: self.show_filter.emit(k))
        row.menu_asked.connect(lambda pos, k=c.key: self.shelf_menu.emit(k, pos))
        for g in games:
            row.add(self._tile(g, w, kind))
        row.finish()
        if not games:
            row.set_empty("No games match these rules yet" if c.kind == 'shelf'
                          else "No games here right now.")
        return row

    def row_for(self, key: str) -> 'ShelfRow | None':
        """Browse's row for a category key (None when it isn't shown)."""
        return self._group_rows.get(key)

    def kicker(self, gkey: str) -> 'BrowseKicker | None':
        return self._kickers.get(gkey)

    def group_rows(self) -> list['ShelfRow']:
        """Browse's category rows, in order."""
        return [w for w in self._order if isinstance(w, ShelfRow)]

    def jump_to(self, gkey: str):
        """A jump chip: the group's kicker to the top, the focus on its
        first row's first tile (its ⋮ when it has none)."""
        k = self._kickers.get(gkey)
        if k is None:
            return
        self.widget().layout().activate()
        y = k.mapTo(self.widget(), QPoint(0, 0)).y()
        self.verticalScrollBar().setValue(max(0, y - SP['2']))
        i = self._order.index(k)
        row = self._order[i + 1] if i + 1 < len(self._order) else None
        if isinstance(row, ShelfRow):
            target = row.tiles[0] if row.tiles else getattr(row, 'btn_menu', None)
            if target is not None:
                focus_and_reveal(target)

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

    COLS = ["", "Game", "Source", "Size", "Played", "Last played", "Proton", "Friends"]
    NUMERIC = (3, 4, 7)

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
        self.setColumnHidden(self.COLS.index("Friends"), True)     # until friends answer
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

    AUTO_COLS = (0, 2, 3, 4, 5, 6, 7)

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
                (str(len(g.get('friends') or ())) if g.get('friends') else '—',
                 len(g.get('friends') or ())),
            ]
            for c, (text, key) in enumerate(cells, start=1):
                it = SortItem(text, key)
                if c in self.NUMERIC:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if c == 7 and g.get('friends'):
                    it.setToolTip(html.escape(FRIENDS.owned_by_text(g['friends'])))
                if c == 1:
                    it.setData(Qt.ItemDataRole.UserRole, g['slug'])
                    if g.get('favorite'):
                        it.setText(f"★ {text}")
                self.setItem(r, c, it)
        self.setColumnHidden(self.COLS.index("Friends"), not FRIENDS.usable())
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
from .friends import FRIENDS  # noqa: E402
from .gameinfo import (is_installed, now_playing, play_enabled, play_label, SOURCE_LABEL,  # noqa: E402
    TIER_COLORS)
from .input import focus_and_reveal, input_mode  # noqa: E402
from .storage import SortItem  # noqa: E402
from .tiles import GameTile  # noqa: E402
from .widgets import (clear_layout, empty_state_text, repacks_on, EmptyState, icon_button,
    icon_label, label, set_glyph_icon, WrapRow)  # noqa: E402
