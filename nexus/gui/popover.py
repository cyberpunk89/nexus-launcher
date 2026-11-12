"""An in-window popover (w-117): a card under the button that opened it,
over a light scrim. A child of the desk, never a window (niri floats
those; see overlay.py). A click on the scrim, Escape, B or the opening
button again close it; focus goes back to that button.

The window's `nav_scope` returns `card` while it is open, so the arrows and
the D-pad stay inside it."""

from PyQt6.QtCore import pyqtSignal, QPoint, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QWidget

from .overlay import Overlay
from .theme import SP


class Popover(Overlay):
    closed = pyqtSignal()
    SCRIM = 40
    GAP = 6                                      # px between the button and the card

    def __init__(self, parent: QWidget, width: int = 420):
        super().__init__(parent)
        self.setObjectName("PopoverScrim")
        self._want_w = width
        self._anchor: QWidget | None = None
        self.card = QFrame(self)
        self.card.setObjectName("Popover")
        self.body = QVBoxLayout(self.card)
        m = SP['4']
        self.body.setContentsMargins(m, m, m, m)
        self.body.setSpacing(SP['2'])

    # ---- open / close ----
    def open_at(self, anchor: QWidget):
        self._anchor = anchor
        self._remember_focus()
        self._cover()
        self._place()
        self.card.show()
        first = self.default_focus()
        if first is not None:
            first.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_popover(self, restore: bool = True):
        if not self.isVisible():
            return
        self.hide()
        self._restore_focus(restore)
        self.closed.emit()

    def toggle(self, anchor: QWidget):
        if self.isVisible():
            self.close_popover()
        else:
            self.open_at(anchor)

    def default_focus(self) -> QWidget | None:
        for w in self.card.findChildren(QWidget):
            if w.isVisibleTo(self.card) and w.isEnabled() \
                    and w.focusPolicy() & Qt.FocusPolicy.TabFocus:
                return w
        return None

    # ---- geometry ----
    def _place(self):
        """Under the anchor, its right edges lined up, kept inside."""
        par = self.parentWidget()
        w = min(self._want_w, par.width() - 2 * SP['4'])
        w = max(200, w)
        self.card.setFixedWidth(w)
        lay = self.card.layout()
        need = lay.heightForWidth(w) if lay.hasHeightForWidth() else -1
        if need <= 0:
            need = self.card.sizeHint().height()
        h = min(need, par.height() - 2 * SP['4'])
        a = self._anchor
        if a is not None and a.isVisible():
            tl = a.mapTo(par, QPoint(0, 0))
            x = tl.x() + a.width() - self.card.width()
            y = tl.y() + a.height() + self.GAP
        else:
            x, y = par.width() - self.card.width() - SP['4'], SP['8']
        x = max(SP['4'], min(x, par.width() - self.card.width() - SP['4']))
        y = max(SP['4'], min(y, par.height() - h - SP['4']))
        self.card.setGeometry(x, y, self.card.width(), h)

    def refit(self):
        """The content changed size (a row came or went)."""
        if self.isVisible():
            self._place()

    def eventFilter(self, obj, ev):
        handled = super().eventFilter(obj, ev)
        if obj is self.parentWidget() and ev.type() == ev.Type.Resize and self.isVisible():
            self._place()
        return handled

    # ---- input ----
    def mousePressEvent(self, ev):
        if not self.card.geometry().contains(ev.position().toPoint()):
            self.close_popover()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.close_popover()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, self.SCRIM))
        pt.end()
