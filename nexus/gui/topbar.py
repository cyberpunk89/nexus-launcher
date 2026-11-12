"""The window's top bar: the wordmark, the page switcher, the current
page's tabs, Search and the icon buttons; the Library's collection chips and
the collection sheet's colour and icon picker."""

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon, QImage, QLinearGradient,
    QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLineEdit, QPushButton,
    QToolButton, QVBoxLayout, QWidget)

from nexus.icons import COLLECTION_ICONS
from ..features import has
from .common import WORDMARK_SVG
from .theme import ACCENTS, collection_look, FONTS, FS, G, GRAD, P, pill, SP
from .widgets import SegmentedControl


class Wordmark(QWidget):
    """"Nexus" in the dusk gradient (the accent and its partner colour, so it
    follows Settings → Appearance): the lettering from assets/wordmark.svg,
    used as a mask. `px` is the height of the old text wordmark it replaces;
    without QtSvg (or the file) it falls back to the text in the UI font."""

    LETTER_H = 0.72                 # the lettering's height, as a share of px

    def __init__(self, text="Nexus", px=20):
        super().__init__()
        self.text = text
        self.font_ = QFont(FONTS.ui, -1, QFont.Weight.Bold)
        self.font_.setPixelSize(px)
        self._svg = _wordmark_renderer() if text == "Nexus" else None
        self._mask: QImage | None = None
        if self._svg is not None:
            vb = self._svg.viewBoxF()
            h = round(px * self.LETTER_H)
            w = round(h * vb.width() / (vb.height() or 1))
            self.setFixedSize(w + 4, px + 4)
            self.setToolTip("Nexus")
        else:
            fm = QFontMetrics(self.font_)
            self.setFixedSize(fm.horizontalAdvance(text) + 4, fm.height() + 4)

    def _letters(self) -> QImage:
        """The lettering as an alpha mask, drawn once for this size."""
        if self._mask is None or self._mask.size() != self.size():
            img = QImage(self.size(), QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            pt = QPainter(img)
            pt.setRenderHint(QPainter.RenderHint.Antialiasing)
            vb = self._svg.viewBoxF()
            w = self.width() - 4.0
            h = w * vb.height() / (vb.width() or 1)
            self._svg.render(pt, QRectF(2.0, (self.height() - h) / 2, w, h))
            pt.end()
            self._mask = img
        return self._mask

    def paintEvent(self, ev):
        g = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        a, b = (QColor(c) for c in GRAD['dusk'])
        g.setColorAt(0.0, a)
        g.setColorAt(1.0, b)
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._svg is not None:
            img = QImage(self._letters())
            ip = QPainter(img)
            ip.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            ip.fillRect(img.rect(), QBrush(g))
            ip.end()
            pt.drawImage(0, 0, img)
        else:
            path = QPainterPath()
            fm = QFontMetrics(self.font_)
            path.addText(QPointF(2.0, float(fm.ascent() + 2)), self.font_, self.text)
            pt.fillPath(path, QBrush(g))
        pt.end()


_WORDMARK = []


def _wordmark_renderer():
    """The wordmark SVG, loaded once; None without QtSvg or the file."""
    if not _WORDMARK:
        r = None
        try:
            from PyQt6.QtSvg import QSvgRenderer
            if WORDMARK_SVG.exists():
                r = QSvgRenderer(str(WORDMARK_SVG))
                r = r if r.isValid() else None
        except ImportError:
            pass
        _WORDMARK.append(r)
    return _WORDMARK[0]


class TopIcon(QToolButton):
    """A round icon button in the top bar, with an optional count badge and
    (for System) a disk-usage ring around it."""
    hovered = pyqtSignal()                      # the pointer came in (Settings builds then)

    SIZE = 36

    def __init__(self, glyph, tip):
        super().__init__()
        self.setObjectName("TopIcon")
        self.setText(glyph)
        self.setToolTip(tip)
        self.setCheckable(True)
        self.setFixedSize(self.SIZE + 6, self.SIZE + 6)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"border-radius: {pill(self.SIZE + 6)}px;")
        self._badge, self._role = '', 'accent'
        self.usage: float | None = None        # 0..1, Storage only
        self.progress: float | None = None     # 0..1, Downloads while one runs (w-118)

    def enterEvent(self, ev):
        super().enterEvent(ev)
        self.hovered.emit()

    def set_badge(self, text, role='accent'):
        self._badge, self._role = (str(text) if text not in (None, '', 0) else ''), role
        self.update()

    def badge(self) -> str:
        return self._badge

    def paintEvent(self, ev):
        super().paintEvent(ev)
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = self.usage if self.usage is not None else self.progress
        if ring is not None:
            r = QRectF(3.0, 3.0, self.width() - 6.0, self.height() - 6.0)
            track = QColor(P['overlay']); track.setAlphaF(0.9)
            pt.setPen(QPen(track, 2.0))
            pt.drawEllipse(r)
            low = self.usage is not None and self.usage > 0.90
            pen = QPen(QColor(P['danger'] if low else P['accent']), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pt.setPen(pen)
            pt.drawArc(r, 90 * 16, -int(max(0.0, min(1.0, ring)) * 360 * 16))
        if self._badge:
            f = QFont(FONTS.ui, -1, QFont.Weight.Bold)
            f.setPixelSize(FS['2xs'])
            pt.setFont(f)
            w = max(16.0, pt.fontMetrics().horizontalAdvance(self._badge) + 8.0)
            rect = QRectF(self.width() - w, 0.0, w, 16.0)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(P.get(self._role, P['accent'])))
            pt.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            pt.setPen(QColor(P['on_accent']))
            pt.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._badge)
        pt.end()


class _TabHandle:
    """Lets the rest of the window treat a top-bar tab like the old nav
    buttons (badges, visibility) without knowing it's a SegmentedControl item."""

    def __init__(self, seg: 'SegmentedControl', key):
        self.seg, self.key = seg, key

    def set_badge(self, text, role='accent'):
        self.seg.set_badge(self.key, str(text) if text not in (None, '', 0) else '', role)

    def setVisible(self, on):
        self.seg.set_item_visible(self.key, bool(on))

    def isHidden(self):
        return self.seg.item(self.key).isHidden()

    def setChecked(self, on):
        pass                                   # the control tracks its own current


# System-page icons that need a Linux-only feature (features.has), by glyph.
ICON_NEEDS = {G['proton']: 'proton'}


class NavPill(SegmentedControl):
    """Home / Library / Deals / News on one rounded track (w-118): the
    current page shows its icon and label, the others only their icon (the
    label is their tooltip and accessible name)."""

    def __init__(self, height: int = 40, parent=None):
        super().__init__('pill', height, parent)
        self.setObjectName("NavPill")
        self._compact = False

    def add(self, key, text=None, glyph=None, tip=''):
        b = super().add(key, text, glyph, tip or text or '')
        if text:
            b.setAccessibleName(text)
        self._relabel()
        return b

    def set_current(self, key, animate: bool = True, emit: bool = False):
        super().set_current(key, animate=animate, emit=emit)
        self._relabel()

    def _clicked(self, key):
        super()._clicked(key)
        self._relabel()

    def set_compact(self, on: bool):
        """Narrow: the current page shows its icon only too."""
        self._compact = on
        self._relabel()

    def _relabel(self):
        cur = self.current()
        for k, b in self._items.items():
            lab = getattr(b, '_label', None)
            if lab is None:
                continue
            text = lab if k == cur and not self._compact else ""
            if b.text() != text:
                b.setText(text)                  # as SegmentedControl.set_compact does
        self.layout().invalidate()               # the items shrink now, not on a later pass
        self.layout().activate()
        self._sync_later()


class TopBar(QFrame):
    """The app's navigation (w-118, "merged"): wordmark, the page switcher
    (Home / Library / Deals / News on one rounded track, the current page
    labelled), then the current page's own tabs (`page_tabs`: the Library
    mounts its Browse · All games · pins strip, other pages none), then a
    Search button ("/"), the system pages as one icon group and Install as
    an icon. A faint gradient hairline sits under it."""

    def __init__(self):
        super().__init__()
        self.setObjectName("TopBar")
        self.setFixedHeight(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], 0, SP['5'], 0)
        lay.setSpacing(SP['2'])
        self.wordmark = Wordmark()
        lay.addWidget(self.wordmark)
        lay.addSpacing(SP['3'])
        self.tabs = NavPill(40)
        lay.addWidget(self.tabs)
        self.tabs_sep = QFrame()                  # a hairline before the page's tabs
        self.tabs_sep.setObjectName("TopSep")
        self.tabs_sep.setFixedSize(1, 20)
        self.tabs_sep.hide()
        lay.addSpacing(SP['1'])
        lay.addWidget(self.tabs_sep)
        # The current page's tabs (page_tabs): one widget per page that has any.
        self.page_slot = QWidget()
        self.page_slot.setObjectName("PageTabs")
        self.page_lay = QHBoxLayout(self.page_slot)
        self.page_lay.setContentsMargins(0, 0, 0, 0)
        self.page_lay.setSpacing(0)
        lay.addWidget(self.page_slot, 1)
        self._page_tabs: dict[str, QWidget] = {}
        # A connected controller's battery, only while one reports it.
        self.battery = QWidget()
        bl = QHBoxLayout(self.battery)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(SP['1'])
        self.battery_icon = icon_label(G['battery'], 16, P['muted'])
        bl.addWidget(self.battery_icon)
        self.battery_text = label("", "Muted")
        bl.addWidget(self.battery_text)
        self.battery.setVisible(False)
        lay.addWidget(self.battery)
        # Search: a button ("/" and Ctrl+F too) that opens into the field;
        # the field folds back into the button when it is left empty.
        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("TopSearchButton")
        self.search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_btn.setFixedHeight(36)
        self.search_btn.setToolTip("Search your games (/)")
        set_glyph_icon(self.search_btn, G['search'], 15, 'subtext', hover_role='text')
        self.search_btn.clicked.connect(self.open_search)
        lay.addWidget(self.search_btn)
        self.search = QLineEdit()
        self.search.setObjectName("TopSearch")
        self.search.setPlaceholderText("Search your games")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.setFixedHeight(36)
        # Not in the Tab / D-pad chain: it would grab focus at start-up and
        # swallow arrows and Escape. "/" and Ctrl+F (or a click) focus it.
        self.search.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.search.installEventFilter(self)
        self.search.textChanged.connect(self._search_text)
        self.search.hide()
        lay.addWidget(self.search)
        # Downloads, System, Proton, Settings on one rounded track.
        self.icon_group = QFrame()
        self.icon_group.setObjectName("TopIconGroup")
        self.icons_lay = QHBoxLayout(self.icon_group)
        self.icons_lay.setContentsMargins(2, 2, 2, 2)
        self.icons_lay.setSpacing(0)
        self.icon_group.setFixedHeight(TopIcon.SIZE + 6 + 6)
        lay.addWidget(self.icon_group, 0, Qt.AlignmentFlag.AlignVCenter)
        self.install = QPushButton()
        self.install.setObjectName("Primary")
        set_glyph_icon(self.install, G['plus'], 18, 'on_accent', hover_role=None)
        self.install.setFixedSize(40, 40)
        self.install.setStyleSheet(f"border-radius: {pill(40)}px; padding: 0;")
        self.install.setToolTip("Install a repack (Ctrl+N)")
        self.install.setAccessibleName("Install")
        lay.addWidget(self.install)

    # ---- the page's tabs (w-118) ----
    def mount_page_tabs(self, page: str, w: QWidget):
        """`w` rides in the bar while `page` shows."""
        self._page_tabs[page] = w
        self.page_lay.addWidget(w, 1)
        w.hide()

    def show_page_tabs(self, page: str | None):
        """The tabs of `page` (none for a page without)."""
        mine = self._page_tabs.get(page)
        for p, w in self._page_tabs.items():
            if w is not mine:
                w.setVisible(False)
        if mine is not None:
            mine.setVisible(bool(getattr(mine, 'wanted', True)))
        self._page = page
        self.tabs_sep.setVisible(mine is not None and not mine.isHidden())

    def page_tabs(self) -> QWidget | None:
        """The page tabs on show, if any."""
        w = self._page_tabs.get(getattr(self, '_page', None))
        return w if w is not None and not w.isHidden() else None

    # ---- search ----
    def open_search(self):
        self.search_btn.hide()
        self.search.show()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _fold_search(self):
        if not self.search.text() and not self.search.hasFocus():
            self.search.hide()
            self.search_btn.show()

    def _search_text(self, text):
        if text and self.search.isHidden():         # typed from elsewhere (a test, a pick)
            self.search_btn.hide()
            self.search.show()
        elif not text:
            self._fold_search()

    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == ev.Type.FocusOut:
            QTimer.singleShot(0, self._fold_unless_over)
        return False

    def _fold_unless_over(self):
        """Folds once focus has gone to the page, not into something over it
        (a modal or sheet hands focus back to the field when it closes)."""
        from PyQt6.QtWidgets import QApplication
        from .overlay import Overlay
        f = QApplication.focusWidget()
        while f is not None:
            if isinstance(f, Overlay):
                return
            f = f.parentWidget()
        self._fold_search()

    def set_battery(self, pads: list[dict]):
        """Show the lowest controller battery (gamepad.battery())."""
        self.battery.setVisible(bool(pads))
        if not pads:
            return
        b = pads[0]
        pct = b['percent']
        glyph = (G['battery_charging'] if b['charging'] else G['battery_full'] if pct >= 80
                 else G['battery_medium'] if pct >= 40 else G['battery_low'])
        role = 'danger' if pct < 20 and not b['charging'] else 'muted'
        self.battery_icon.setText(glyph)
        self.battery_icon.setStyleSheet(f"color: {P[role]};")
        self.battery_text.setText(f"{pct}%")
        self.battery.setToolTip(
            "Controller battery" + (" (charging)" if b['charging'] else '')
            + (f" — {len(pads)} controllers, lowest shown" if len(pads) > 1 else ''))

    def add_icon(self, glyph, tip) -> TopIcon:
        """A system-page icon; hidden when its page needs a feature this
        build lacks (the Proton page on Windows)."""
        b = TopIcon(glyph, tip)
        self.icons_lay.addWidget(b)
        if (need := ICON_NEEDS.get(glyph)) and not has(need):
            b.hide()
        return b

    def set_compact(self, on: bool):
        """A narrow window (or a big Size): the current page's label and
        Search's word go; the page tabs fold into More themselves."""
        self.tabs.set_compact(on)
        self.search_btn.setText("" if on else "Search")
        self.search.setFixedWidth(170 if on else 240)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        pt = QPainter(self)
        g = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        a, b = (QColor(c) for c in GRAD['dusk'])
        edge = QColor(P['border'])
        a.setAlphaF(0.45); b.setAlphaF(0.30)
        g.setColorAt(0.0, edge); g.setColorAt(0.25, a); g.setColorAt(0.75, b); g.setColorAt(1.0, edge)
        pt.fillRect(QRectF(0, self.height() - 1, self.width(), 1), QBrush(g))
        pt.end()


class CollectionChip(QPushButton):
    """A Library chip for one collection, with its icon in its colour when it
    has them: click filters; right-click or the Menu key (X on a pad) opens
    its sheet (rename, pin, order, colour, icon, delete); Ctrl+← / → moves it."""
    manage = pyqtSignal(str)
    move = pyqtSignal(str, int)

    def __init__(self, name: str):
        super().__init__(amp(name))
        self.name = name
        self.setObjectName("Chip")
        self.setCheckable(True)
        self.setToolTip("Right-click to rename, pin, move, colour or delete")
        self.set_look()

    def set_look(self):
        look = collection_look(self.name)
        if look:
            set_glyph_icon(self, look[0], 14, look[1], hover_role=None)
        else:
            self._glyph_spec = None
            self.setIcon(QIcon())

    def contextMenuEvent(self, ev):
        self.manage.emit(self.name)

    def keyPressEvent(self, ev):
        ctrl = ev.modifiers() & Qt.KeyboardModifier.ControlModifier
        if ev.key() == Qt.Key.Key_Menu:
            self.manage.emit(self.name)
        elif ctrl and ev.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.move.emit(self.name, -1 if ev.key() == Qt.Key.Key_Left else 1)
        else:
            super().keyPressEvent(ev)


class _NoColour(QToolButton):
    """The colour row's first choice: no colour of its own (a hollow ring)."""

    def __init__(self, size: int):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(size, size)
        self.setToolTip("No colour")
        self.setAccessibleName("No colour")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        inner = r.adjusted(5, 5, -5, -5)
        pt.setPen(QPen(QColor(P['muted']), 1.5))
        pt.drawEllipse(inner)
        d = inner.width() * 0.15
        pt.drawLine(QPointF(inner.left() + d, inner.bottom() - d),
                    QPointF(inner.right() - d, inner.top() + d))
        if self.isChecked():
            pt.setPen(QPen(QColor(P['text']), 2.0))
            pt.drawEllipse(r)
        pt.end()


class CollectionLookPicker(QWidget):
    """A collection's colour and icon, chosen inline (no dialog): a row of
    swatches (the theme's accent presets) and a grid of Lucide icons. Every
    piece takes focus, so the D-pad walks it like any other row. In the
    Manage collection sheet a pick applies at once (`picked`) and the sheet
    stays open; in the new / rename modal (w-67) `.color` and `.icon` are
    read on Save."""
    picked = pyqtSignal(str, object)            # 'color' | 'icon', key or None
    COLS = 6

    def __init__(self, look: dict, scale: float = 1.0):
        super().__init__()
        self.color, self.icon = look.get('color'), look.get('icon')
        sc = lambda v: int(v * scale)             # noqa: E731
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, sc(SP['2']), 0, sc(SP['2']))
        lay.setSpacing(sc(SP['1_5']))
        lay.addWidget(label("COLOUR", "SheetSection"))
        row = QGridLayout()                       # two lines: 12 accents + "no colour"
        row.setSpacing(sc(SP['1']))
        self.swatches: dict[str | None, QWidget] = {None: _NoColour(sc(32))}
        for key in ACCENTS:
            self.swatches[key] = Swatch('color', P[f'col_{key}'], key.capitalize(),
                                        (sc(32), sc(32)))
        per_line = (len(self.swatches) + 1) // 2
        for i, (key, sw) in enumerate(self.swatches.items()):
            sw.clicked.connect(lambda _=False, k=key: self._pick('color', k))
            row.addWidget(sw, i // per_line, i % per_line)
        row.setColumnStretch(per_line, 1)
        lay.addLayout(row)
        lay.addSpacing(sc(SP['2']))
        lay.addWidget(label("ICON", "SheetSection"))
        grid = QGridLayout()
        grid.setSpacing(sc(SP['1']))
        self.icons: dict[str, QToolButton] = {}
        for i, (key, glyph) in enumerate(COLLECTION_ICONS.items()):
            b = QToolButton()
            b.setObjectName("IconPick")
            b.setText(glyph)
            b.setCheckable(True)
            b.setFixedSize(sc(44), sc(44))
            b.setToolTip(key.split('-')[0].capitalize())
            b.setAccessibleName(b.toolTip())
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            b.clicked.connect(lambda _=False, k=key: self._pick('icon', k))
            grid.addWidget(b, i // self.COLS, i % self.COLS)
            self.icons[key] = b
        grid.setColumnStretch(self.COLS, 1)
        lay.addLayout(grid)
        self._sync()

    def _pick(self, what: str, key):
        if what == 'icon' and key == 'bookmark':
            key = None                            # the bookmark is the default icon
        setattr(self, what, key)
        self._sync()
        self.picked.emit(what, key)

    def _sync(self):
        for key, sw in self.swatches.items():
            sw.setChecked(key == self.color)
        role = f'col_{self.color}' if self.color in ACCENTS else 'accent'
        for key, b in self.icons.items():
            on = key == (self.icon or 'bookmark')
            b.setChecked(on)
            # The chosen icon in the chosen colour: the pick, previewed.
            b.setStyleSheet(f"border-radius: {pill(b.height())}px;"
                            + (f" color: {P[role]};" if on else ""))


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .settings import Swatch  # noqa: E402
from .widgets import amp, icon_label, label, set_glyph_icon  # noqa: E402
