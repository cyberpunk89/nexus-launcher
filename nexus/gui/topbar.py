"""The window's top bar: the wordmark, page tabs, icon buttons, and the
collection chips under the library toolbar."""

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient,
    QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QPushButton, QToolButton, QWidget

from ..features import has
from .common import WORDMARK_SVG
from .theme import FONTS, FS, G, GRAD, P, pill, SP


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
    (for Storage) a disk-usage ring around it."""

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

    def set_badge(self, text, role='accent'):
        self._badge, self._role = (str(text) if text not in (None, '', 0) else ''), role
        self.update()

    def badge(self) -> str:
        return self._badge

    def paintEvent(self, ev):
        super().paintEvent(ev)
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.usage is not None:
            r = QRectF(3.0, 3.0, self.width() - 6.0, self.height() - 6.0)
            track = QColor(P['overlay']); track.setAlphaF(0.9)
            pt.setPen(QPen(track, 2.0))
            pt.drawEllipse(r)
            low = self.usage > 0.90
            pen = QPen(QColor(P['danger'] if low else P['accent']), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pt.setPen(pen)
            pt.drawArc(r, 90 * 16, -int(self.usage * 360 * 16))
        if self._badge:
            f = QFont(FONTS.ui, -1, QFont.Weight.Bold)
            f.setPixelSize(FS['2xs'])
            pt.setFont(f)
            w = max(16.0, pt.fontMetrics().horizontalAdvance(self._badge) + 8.0)
            rect = QRectF(self.width() - w, 0.0, w, 16.0)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(P.get(self._role, P['accent'])))
            pt.drawRoundedRect(rect, 8.0, 8.0)
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


class TopBar(QFrame):
    """The app's navigation: wordmark, the four content tabs, then search,
    the system pages as icons, and Install. A faint gradient hairline sits
    under it."""

    def __init__(self):
        super().__init__()
        self.setObjectName("TopBar")
        self.setFixedHeight(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], 0, SP['5'], 0)
        lay.setSpacing(SP['2'])
        self.wordmark = Wordmark()
        lay.addWidget(self.wordmark)
        lay.addSpacing(SP['4'])
        self.tabs = SegmentedControl('underline', 44)
        lay.addWidget(self.tabs)
        lay.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("TopSearch")
        self.search.setPlaceholderText("Search your games   /")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(340)
        self.search.setFixedHeight(36)
        # Not in the Tab / D-pad chain: it would grab focus at start-up and
        # swallow arrows and Escape. "/" and Ctrl+F (or a click) focus it.
        self.search.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        lay.addWidget(self.search, 1)
        lay.addSpacing(SP['2'])
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
        self.icons_lay = QHBoxLayout()
        self.icons_lay.setSpacing(SP['1'])
        lay.addLayout(self.icons_lay)
        lay.addSpacing(SP['2'])
        self.install = QPushButton("Install")
        self.install.setObjectName("Primary")
        set_glyph_icon(self.install, G['plus'], 16, 'on_accent', hover_role=None)
        self.install.setFixedHeight(36)
        self.install.setToolTip("Install a repack (Ctrl+N)")
        lay.addWidget(self.install)

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
        self.tabs.set_compact(on)
        self.install.setText("" if on else "Install")
        self.search.setMaximumWidth(220 if on else 340)
        self.search.setPlaceholderText("Search   /" if on else "Search your games   /")

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
    """A Library chip for one collection: click filters; right-click or the
    Menu key (X on a pad) offers rename / delete."""
    manage = pyqtSignal(str)

    def __init__(self, name: str):
        super().__init__(amp(name))
        self.name = name
        self.setObjectName("Chip")
        self.setCheckable(True)
        self.setToolTip("Right-click to rename or delete")

    def contextMenuEvent(self, ev):
        self.manage.emit(self.name)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu:
            self.manage.emit(self.name)
        else:
            super().keyPressEvent(ev)


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .widgets import amp, icon_label, label, SegmentedControl, set_glyph_icon  # noqa: E402
