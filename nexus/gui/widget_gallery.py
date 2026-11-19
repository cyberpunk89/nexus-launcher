"""The Customise panel's Widgets tab (w-158): every widget as a tile with a
live preview (widget_preview.py), grouped Stage / Play / Social / Library /
Stats / Store with "n of m on Home", a search, an All / On Home / Not added
filter and a "Recommended for you" row whose tiles say why.

A tile's face is one button: on a widget that is not on Home it adds it (at
its saved place); on one that is, it shows that card on Home. A widget with
more than one size has a chip per size, each drawing the size's shape, that
adds it at that size. A tile can be dragged onto Home (application/x-nexus-
widget, "kind:size"); Home shows a dashed slot where it would land.

The gallery only shows and emits (`add`, `reveal`); the Dashboard is the
controller, like LayoutsBar. Tiles are built once per kind and kept. Their
widths are constants from the dock's width (w-101), never measured."""

from PyQt6.QtCore import pyqtSignal, QEvent, QMimeData, QPoint, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QDrag, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget)

from .customise_panel import DOCK_W
from .common import alive
from .theme import FONTS, FS, G, P, pill, R, scaled, SP
from .widget_preview import PreviewCache, theme_sig
from .widget_spec import GROUPS, span, SPEC, SPECS
from .widgets import FlowLayout, label, plain_label, SegmentedControl

MIME = 'application/x-nexus-widget'
CHIP_LABELS = {'small': "Small", 'tall': "Tall", 'wide': "Wide", 'full': "Full",
               'cinematic': "Cinematic", 'banner': "Banner"}
FILTERS = (('all', "All"), ('on', "On Home"), ('off', "Not added"))
RECOMMEND_ORDER = ('friends', 'updates', 'week', 'news', 'onthisday', 'milestones', 'backlog',
                   'spacemap', 'heatmap', 'deals')
RECOMMEND_MAX = 2
WHY_NOTE = "Picked from what Nexus already knows: your play, friends, updates and news."


def _repolish(*ws):
    """A property the sheet's rules read changed: polish again. Descendant
    rules (#GalleryTile[on] QPushButton#TileFace) need the child polished too."""
    for w in ws:
        w.style().unpolish(w)
        w.style().polish(w)


def mime_text(kind: str, size: str) -> str:
    return f"{kind}:{size}"


def parse_mime(text: str) -> tuple[str, str] | None:
    kind, _, size = (text or '').partition(':')
    if kind in SPEC and size in SPEC[kind].sizes:
        return kind, size
    return None


def shape_cells(size: str) -> list[tuple[int, int, bool]]:
    """A size drawn as cells (row, col, filled): a row of three columns
    with as many filled as the size spans; Tall is three by two with the
    first column filled in both rows."""
    if size == 'tall':
        return [(r, c, c == 0) for r in range(2) for c in range(3)]
    n = span(size)
    return [(0, c, c < n) for c in range(3)]


def recommended(cands: dict, order=RECOMMEND_ORDER, limit: int = RECOMMEND_MAX) -> list[str]:
    """The kinds to recommend: those of `cands` ({kind: why}) with a reason,
    in `order` (then the rest by name), at most `limit`."""
    have = [k for k, why in cands.items() if why]
    rank = {k: n for n, k in enumerate(order)}
    have.sort(key=lambda k: (rank.get(k, len(rank)), k))
    return have[:limit]


def _hfw(lay, w: int) -> int:
    """A layout's height at width w (its size hint when nothing in it wraps)."""
    return lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else lay.totalSizeHint().height()


GALLERY_SIDE = SP['4']                      # the Widgets tab's side margins


def inner_width() -> int:
    """The gallery's width: the dock less the tab's margins and its scroll
    bar. A constant of the dock, never measured (w-101)."""
    return scaled(DOCK_W) - 2 * GALLERY_SIDE - SP['2_5']


def tile_width(size: str) -> int:
    full = inner_width()
    return full if span(size) > 1 else (full - SP['2']) // 2


FACE_PAD = SP['1']


def preview_box(size: str) -> QSize:
    """The board's boxes: ~136x92 narrow, ~294x84 wide (scaled)."""
    w = tile_width(size) - 2 * FACE_PAD
    return QSize(w, scaled(92) if span(size) == 1 else scaled(84))


class ShapeGlyph(QWidget):
    """A size's shape in miniature (shape_cells): 6 px cells, 1 px apart,
    filled in the accent, empty in the overlay. Mouse-transparent."""
    CELL, GAP = 6, 1

    def __init__(self, size: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.size_ = ''
        self.set_size(size)

    def set_size(self, size: str):
        if size == self.size_:
            return
        self.size_ = size
        self.cells = shape_cells(size)
        rows = 1 + max(r for r, _c, _f in self.cells)
        c, g = scaled(self.CELL), max(1, scaled(self.GAP))
        self.setFixedSize(3 * c + 2 * g, rows * c + (rows - 1) * g)
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, g = scaled(self.CELL), max(1, scaled(self.GAP))
        on, off = QColor(P['accent']), QColor(P['overlay'])
        pt.setPen(Qt.PenStyle.NoPen)
        for r, col, filled in self.cells:
            pt.setBrush(on if filled else off)
            pt.drawRoundedRect(QRectF(col * (c + g), r * (c + g), c, c), 1.2, 1.2)
        pt.end()


class SizeChip(QPushButton):
    """Add at this size: the size's shape and its name."""

    def __init__(self, size: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SizeChip")
        self.size_ = size
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Add at {CHIP_LABELS.get(size, size)} size")
        row = QHBoxLayout(self)
        row.setContentsMargins(SP['1_5'], 0, SP['2'], 0)
        row.setSpacing(SP['1'])
        self.glyph = ShapeGlyph(size, self)
        row.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignVCenter)
        self.text_label = label(CHIP_LABELS.get(size, size), "SizeChipText")
        self.text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(self.text_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(scaled(26))

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint().expandedTo(QSize(0, scaled(26)))


class PreviewPane(QWidget):
    """The tile's picture: the preview the cache holds for `key`, asked for
    when it paints (a tile keeps the key, never the pixmap: w-46)."""

    def __init__(self, cache: PreviewCache, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.cache = cache
        self.key = None

    def set_key(self, key, box: QSize):
        self.key = key
        self.setFixedSize(box)
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        pm = self.cache.pixmap(self.key) if self.key is not None else None
        if pm is None:
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(P['surface_2']))
            pt.drawRoundedRect(r, R['sm'], R['sm'])
        else:
            pt.drawPixmap(r, pm, QRectF(pm.rect()))
        pt.end()


class TileFace(QPushButton):
    """The tile's button: its preview, title, shape and the note or the
    reason it is recommended. Children are mouse-transparent (as PresetRow).
    Dragging it past the start distance starts a drag onto Home."""
    drag_started = pyqtSignal()

    def __init__(self, tile: 'GalleryTile'):
        super().__init__(tile)
        self.setObjectName("TileFace")
        self.tile = tile
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._press: QPoint | None = None

    def sizeHint(self) -> QSize:
        w = tile_width(self.tile.size_)                 # a constant, never measured (w-101)
        return QSize(w, _hfw(self.layout(), w))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._press = ev.position().toPoint()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._press is not None and ev.buttons() & Qt.MouseButton.LeftButton and \
                (ev.position().toPoint() - self._press).manhattanLength() >= \
                QApplication.startDragDistance():
            self._press = None
            self.setDown(False)
            self.tile.start_drag()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._press = None
        super().mouseReleaseEvent(ev)


class GalleryTile(QFrame):
    """One widget in the gallery. `on_home` and `size` come from sync."""
    add = pyqtSignal(str, str)              # kind, size
    reveal = pyqtSignal(str)                # kind: show its card on Home
    settings = pyqtSignal()                 # a tile that needs Game-db: open Settings (w-163)

    def __init__(self, kind: str, cache: PreviewCache, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("GalleryTile")
        self.kind = kind
        self.spec = SPEC[kind]
        self.cache = cache
        self.on_home = False
        self.size_ = self.spec.default
        self.options: dict | None = None
        self.why = ''
        self.needs = ''                     # what it needs before it can show (w-163)
        self.dragging = False
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SP['1_5'])
        self.face = TileFace(self)
        self.face.clicked.connect(self._pressed)
        fl = QVBoxLayout(self.face)
        fl.setContentsMargins(FACE_PAD, FACE_PAD, FACE_PAD, SP['2'])
        fl.setSpacing(SP['1'])
        self.preview = PreviewPane(cache, self.face)
        fl.addWidget(self.preview)
        head = QHBoxLayout()
        head.setSpacing(SP['1_5'])
        self.title = plain_label(self.spec.title, "TileTitle")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self.title, 1)
        self.shape = ShapeGlyph(self.size_, self.face)
        head.addWidget(self.shape, 0, Qt.AlignmentFlag.AlignVCenter)
        fl.addLayout(head)
        self.note = plain_label(self.spec.note, "TileNote", wrap=True)
        fl.addWidget(self.note)
        self.note.setVisible(bool(self.spec.note))
        for w in (self.title, self.note):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        col.addWidget(self.face)
        self.tick = QLabel(self.face)
        self.tick.setObjectName("TileTick")
        self.tick.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.tick.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tick.setText(G['check'])
        f = QFont(FONTS.icon)
        f.setPixelSize(scaled(11))
        self.tick.setFont(f)
        self.tick.setFixedSize(scaled(18), scaled(18))
        self.tick.setStyleSheet(f"border-radius: {pill(scaled(18))}px;")     # a disc
        self.tick.hide()
        self.chips_host = QWidget(self)
        self.chips = FlowLayout(self.chips_host, margin=0, spacing=SP['1'])
        self.chip_btns: dict[str, SizeChip] = {}
        for z in self.spec.sizes if len(self.spec.sizes) > 1 else ():
            c = SizeChip(z, self.chips_host)
            c.clicked.connect(lambda _=False, z=z: self.add.emit(self.kind, z))
            self.chips.addWidget(c)
            self.chip_btns[z] = c
        col.addWidget(self.chips_host)
        self.chips_host.setVisible(bool(self.chip_btns))
        self._apply()

    def sizeHint(self) -> QSize:
        w = tile_width(self.size_)
        return QSize(w, _hfw(self.layout(), w))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ---- state ----
    def set_state(self, on_home: bool, size: str, options: dict | None = None):
        size = size if size in self.spec.sizes else self.spec.default
        if (on_home, size, options) == (self.on_home, self.size_, self.options):
            return
        self.on_home, self.size_, self.options = on_home, size, options
        self._apply()

    def set_why(self, why: str):
        """A recommended tile's reason (instead of the merged note)."""
        self.why = why or ''
        self._sync_note()

    def set_needs(self, needs: str):
        """What the card needs ("Needs Game-db · Open Settings"): shown instead of
        the note or the reason, and a click opens Settings instead of adding."""
        needs = needs or ''
        if needs != self.needs:
            self.needs = needs
            self._sync_note()
            self._apply()

    def _sync_note(self):
        text = self.needs or self.why or self.spec.note
        self.note.setText(text)
        self.note.setProperty('why', bool(self.why) and not self.needs)
        self.note.style().unpolish(self.note)
        self.note.style().polish(self.note)
        self.note.setVisible(bool(text))

    def set_selected(self, on: bool):
        if bool(self.property('selected')) != on:
            self.setProperty('selected', on)
            _repolish(self, self.face)

    def _apply(self):
        w = tile_width(self.size_)
        self.setFixedWidth(w)
        self.box = preview_box(self.size_)
        key = self.preview_key()
        if key != self.preview.key and self.cache.active and not self.cache.has(key):
            self.cache.queue([self.preview_job()], front=True)    # never a grey box left behind
        self.preview.set_key(key, self.box)
        self.shape.set_size(self.size_)
        self.tick.setVisible(self.on_home)
        chips = bool(self.chip_btns) and not self.on_home and not self.needs
        f = QApplication.focusWidget()
        if not chips and f is not None and self.chips_host.isAncestorOf(f):
            self.face.setFocus(Qt.FocusReason.OtherFocusReason)   # added: stay on this tile
        self.chips_host.setVisible(chips)
        self.setProperty('on', self.on_home)
        _repolish(self, self.face)
        self._elide(w)
        self.face.setToolTip("Open Settings to connect it" if self.needs else
                             "On Home: show it there" if self.on_home else
                             f"Add {self.spec.title} to Home")
        self.tick.move(w - self.tick.width() - SP['2'], SP['2'])
        self.updateGeometry()

    def _elide(self, w: int):
        """The title as far as the tile's width goes (a constant, w-101)."""
        self.title.ensurePolished()
        room = w - 2 * FACE_PAD - self.shape.width() - SP['1_5'] - 2
        full = self.spec.title
        self.title.setText(self.title.fontMetrics().elidedText(full, Qt.TextElideMode.ElideRight,
                                                               room))
        self.title.setToolTip(full if self.title.text() != full else '')

    def preview_key(self):
        return self.cache.key(self.kind, self.size_, preview_box(self.size_), self.options)

    def preview_job(self):
        return (self.kind, self.size_, preview_box(self.size_), self.options)

    def matches(self, text: str) -> bool:
        text = text.strip().casefold()
        if not text:
            return True
        hay = " ".join((self.spec.title, dict(GROUPS)[self.spec.group], self.spec.note,
                        self.why, self.needs)).casefold()
        return all(word in hay for word in text.split())

    def _pressed(self):
        if self.needs:
            self.settings.emit()
        elif self.on_home:
            self.reveal.emit(self.kind)
        else:
            self.add.emit(self.kind, self.size_)

    # ---- drag ----
    def drag_data(self) -> tuple[QMimeData, QPixmap]:
        """What a drag carries: "kind:size", and the preview with a
        "Title · Esc cancels" strip under it."""
        md = QMimeData()
        md.setData(MIME, mime_text(self.kind, self.size_).encode())
        box = self.box
        strip = scaled(22)
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(QSize(int(box.width() * dpr), int((box.height() + strip) * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        pt = QPainter(pm)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        prev = self.cache.pixmap(self.preview_key())
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['surface_2']))
        pt.drawRoundedRect(QRectF(0, 0, box.width(), box.height()), R['sm'], R['sm'])
        if prev is not None:
            pt.drawPixmap(QRectF(0, 0, box.width(), box.height()), prev, QRectF(prev.rect()))
        sr = QRectF(0, box.height() + 2, box.width(), strip - 2)
        pt.setBrush(QColor(P['overlay']))
        pt.drawRoundedRect(sr, sr.height() / 2, sr.height() / 2)
        f = QFont()
        f.setPixelSize(FS['2xs'])
        pt.setFont(f)
        pt.setPen(QColor(P['text']))
        pt.drawText(sr, Qt.AlignmentFlag.AlignCenter, f"{self.spec.title} · Esc cancels")
        pt.end()
        return md, pm

    def start_drag(self):
        md, pm = self.drag_data()
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(pm.width() // int(pm.devicePixelRatio() or 1) // 2, scaled(12)))
        self.dragging = True
        self._dim(True)
        try:
            drag.exec(Qt.DropAction.CopyAction)
        finally:
            if alive(self):                     # a sync in the drag's loop may have deleted it
                self.dragging = False
                self._dim(False)

    def _dim(self, on: bool):
        self.setProperty('dim', on)
        _repolish(self, self.face)


class WidgetGallery(QWidget):
    """The Widgets tab's contents. `sync(items)` after every Home change,
    `open()` when Customise opens, `stop()` when it closes."""
    add = pyqtSignal(str, str)              # kind, size (its saved place)
    reveal = pyqtSignal(str)                # kind on Home: show it
    settings = pyqtSignal()                 # a tile that needs Game-db was pressed (w-163)

    def __init__(self, parent: QWidget | None = None, today=None):
        super().__init__(parent)
        self.setObjectName("WidgetGallery")
        self.cache = PreviewCache(self, today)
        self.cache.ready.connect(self._ready)
        self.tiles: dict[str, GalleryTile] = {}
        self._group_of: dict[str, tuple] = {}
        self._reasons: dict[str, str] = {}
        self._items: list = []
        self._filter = 'all'
        self._theme = theme_sig()
        self._restyle = QTimer(self)               # many StyleChange events, one redraw
        self._restyle.setSingleShot(True)
        self._restyle.setInterval(0)
        self._restyle.timeout.connect(self._restyle_now)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SP['2'])
        self.search = QLineEdit(self)
        self.search.setObjectName("GallerySearch")
        self.search.setPlaceholderText("Search widgets")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(scaled(32))
        self.search.textChanged.connect(lambda _t: self._refilter())
        col.addWidget(self.search)
        self.filter = SegmentedControl('pill', scaled(30), self)
        for key, text in FILTERS:
            self.filter.add(key, text)
        self.filter.changed.connect(self._filter_changed)
        col.addWidget(self.filter)
        # Recommended for you
        self.rec_box = QWidget(self)
        rl = QVBoxLayout(self.rec_box)
        rl.setContentsMargins(0, SP['1'], 0, 0)
        rl.setSpacing(SP['1_5'])
        rh = QHBoxLayout()
        rh.addWidget(label("Recommended for you".upper(), "GalleryKicker"))
        rh.addStretch()
        self.btn_why = QPushButton("Why these", self.rec_box)
        self.btn_why.setObjectName("Ghost")
        self.btn_why.setCheckable(True)
        self.btn_why.toggled.connect(lambda on: self.why_note.setVisible(on))
        rh.addWidget(self.btn_why)
        rl.addLayout(rh)
        self.why_note = label(WHY_NOTE, "Muted", wrap=True)
        self.why_note.hide()
        rl.addWidget(self.why_note)
        self.rec_host = QWidget(self.rec_box)
        self.rec_row = FlowLayout(self.rec_host, margin=0, spacing=SP['2'])
        rl.addWidget(self.rec_host)
        col.addWidget(self.rec_box)
        self.rec_box.hide()
        self.rec_tiles: dict[str, GalleryTile] = {}
        # the groups
        for gkey, gname in GROUPS:
            kinds = [s.kind for s in SPECS if s.group == gkey]
            box = QWidget(self)
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, SP['2'], 0, 0)
            bl.setSpacing(SP['1_5'])
            hh = QHBoxLayout()
            hh.setSpacing(SP['2'])
            hh.addWidget(label(gname.upper(), "GalleryKicker"))
            count = label("", "GalleryCount")
            hh.addWidget(count)
            hh.addStretch()
            bl.addLayout(hh)
            host = QWidget(box)
            flow = FlowLayout(host, margin=0, spacing=SP['2'])
            for k in kinds:
                t = self._make_tile(k, host)
                flow.addWidget(t)
                self.tiles[k] = t
            bl.addWidget(host)
            col.addWidget(box)
            self._group_of[gkey] = (box, count, kinds)
        self.empty = label("No widget matches.", "Muted", wrap=True)
        col.addWidget(self.empty)
        self.empty.hide()
        self.sync([])

    def _make_tile(self, kind: str, parent: QWidget) -> GalleryTile:
        t = GalleryTile(kind, self.cache, parent)
        t.add.connect(self.add)
        t.reveal.connect(self.reveal)
        t.settings.connect(self.settings)
        return t

    # ---- state ----
    def sync(self, items):
        """Home's items now: ticks, sizes, counts, what the filter shows."""
        self._items = [dict(i) for i in items]
        by = {i['id']: i for i in self._items}
        from .dashboard import _dash_options
        for k, t in self.tiles.items():
            it = by.get(k)
            on = it is not None and not it.get('hidden')
            t.set_state(on, (it or {}).get('size') or SPEC[k].default,
                        _dash_options(k, it) if on else None)
        n_on = sum(t.on_home for t in self.tiles.values())
        self.filter.item('all').setText(f"All {len(self.tiles)}")
        self.filter.item('on').setText(f"On Home {n_on}")
        self.filter.item('off').setText(f"Not added {len(self.tiles) - n_on}")
        for _g, (_box, count, kinds) in self._group_of.items():
            on = sum(self.tiles[k].on_home for k in kinds)
            count.setText(f"{on} of {len(kinds)} on Home")
        self._sync_recommended()
        self._refilter()

    def set_reasons(self, reasons: dict):
        """{kind: why} from the widgets (Dashboard); only hidden ones count."""
        if reasons != self._reasons:
            self._reasons = dict(reasons)
            self._sync_recommended()
            self._refilter()

    def set_needs(self, needs: dict):
        """{kind: what it needs} from the widgets; '' for a card with nothing missing."""
        for k, t in self.tiles.items():
            t.set_needs(needs.get(k, ''))
        for k, t in self.rec_tiles.items():
            t.set_needs(needs.get(k, ''))
        self._refilter()

    def recommended_kinds(self) -> list[str]:
        return recommended({k: w for k, w in self._reasons.items()
                            if k in self.tiles and not self.tiles[k].on_home})

    def _sync_recommended(self):
        want = self.recommended_kinds()
        for k in list(self.rec_tiles):
            if k not in want:
                t = self.rec_tiles.pop(k)
                if t.hasFocus() or t.isAncestorOf(QApplication.focusWidget()):
                    self.search.setFocus(Qt.FocusReason.OtherFocusReason)
                self.rec_row.removeWidget(t)
                t.hide()
                t.deleteLater()
        for k in want:
            t = self.rec_tiles.get(k)
            if t is None:
                t = self.rec_tiles[k] = self._make_tile(k, self.rec_host)
                self.rec_row.addWidget(t)
            # narrow tiles side by side: the size a recommended one is added at
            size = SPEC[k].default if span(SPEC[k].default) == 1 else \
                next((z for z in SPEC[k].sizes if span(z) == 1), SPEC[k].default)
            t.set_state(False, size)
            t.set_why(self._reasons.get(k, ''))
        # keep the row in the order picked
        if [self.rec_row.itemAt(n).widget() for n in range(self.rec_row.count())] != \
                [self.rec_tiles[k] for k in want]:
            for k in want:
                self.rec_row.removeWidget(self.rec_tiles[k])
            for k in want:
                self.rec_row.addWidget(self.rec_tiles[k])

    def _filter_changed(self, key: str):
        self._filter = key
        self._refilter()

    def current_filter(self) -> str:
        return self._filter

    def set_filter(self, key: str):
        self.filter.set_current(key, animate=False)
        self._filter_changed(key)

    def _refilter(self):
        f = QApplication.focusWidget()
        had = f if f is not None and self.isAncestorOf(f) else None
        text = self.search.text()
        shown = 0
        for _g, (box, _count, kinds) in self._group_of.items():
            n = 0
            for k in kinds:
                t = self.tiles[k]
                on = t.matches(text) and (self._filter == 'all' or
                                          (self._filter == 'on') == t.on_home)
                t.setVisible(on)
                n += on
            box.setVisible(n > 0)
            shown += n
        rec = bool(self.rec_tiles) and self._filter == 'all' and not text.strip()
        self.rec_box.setVisible(rec)
        self.empty.setVisible(shown == 0)
        if had is not None and not had.isVisibleTo(self):
            # the focused tile was filtered away: focus goes to the filter
            self.filter.item(self._filter).setFocus(Qt.FocusReason.OtherFocusReason)

    def visible_tiles(self) -> list[GalleryTile]:
        out = [t for t in self.rec_tiles.values() if t.isVisibleTo(self)]
        return out + [t for t in self.tiles.values() if t.isVisibleTo(self)]

    def set_selected(self, kind: str | None):
        for k, t in self.tiles.items():
            t.set_selected(k == kind)

    def tile(self, kind: str) -> GalleryTile | None:
        return self.tiles.get(kind)

    # ---- previews ----
    def open(self, items=None):
        """Customise opened: sync, then queue the previews that are missing
        or stale (Recommended first, then the tiles in order)."""
        if items is not None:
            self.sync(items)
        jobs = [t.preview_job() for t in self.rec_tiles.values()]
        jobs += [t.preview_job() for t in self.tiles.values() if t.isVisibleTo(self)]
        jobs += [t.preview_job() for t in self.tiles.values() if not t.isVisibleTo(self)]
        self.cache.active = True
        self.cache.queue(jobs)
        for t in self.all_tiles():
            t.preview.set_key(t.preview_key(), t.box)

    def stop(self):
        self.cache.active = False
        self.cache.stop()
        self._restyle.stop()

    def all_tiles(self) -> list[GalleryTile]:
        return list(self.rec_tiles.values()) + list(self.tiles.values())

    def restyled(self):
        """A theme or Size change: every preview again."""
        self.cache.invalidate()
        for t in self.all_tiles():
            t._apply()
        if self.isVisible():
            self.open()

    def changeEvent(self, ev):
        """A theme or Size change restyles the window: the previews were
        drawn in the old one, so they go and are queued again."""
        if ev.type() == QEvent.Type.StyleChange and theme_sig() != self._theme:
            self._restyle.start()
        super().changeEvent(ev)

    def _restyle_now(self):
        sig = theme_sig()
        if sig != self._theme:
            self._theme = sig
            self.restyled()

    def _ready(self, key):
        for t in self.all_tiles():
            if t.preview.key == key:
                t.preview.update()


class DropSlot(QFrame):
    """Where a dragged widget would land on Home: a dashed card with the
    size's shape, "Title · Size" and "Drop to add it here"."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("DropSlot")
        self.kind = self.size_ = ''
        col = QVBoxLayout(self)
        col.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        col.setSpacing(SP['1'])
        col.addStretch()
        self.shape = ShapeGlyph('small', self)
        col.addWidget(self.shape, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = label("", "DropTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.title)
        hint = label("Drop to add it here", "Muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(hint)
        col.addStretch()
        self.setMinimumHeight(scaled(120))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_widget(self, kind: str, size: str):
        self.kind, self.size_ = kind, size
        self.shape.set_size(size)
        self.title.setText(f"{SPEC[kind].title} · {CHIP_LABELS.get(size, size)}")

    def paintEvent(self, ev):
        super().paintEvent(ev)
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(P['accent']), 2.0, Qt.PenStyle.DashLine)
        pt.setPen(pen)
        pt.setBrush(QColor(P.get('accent_09', P['surface'])))
        pt.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), R['card'], R['card'])
        pt.end()


