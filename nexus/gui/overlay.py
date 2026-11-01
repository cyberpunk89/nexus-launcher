"""What every in-window overlay shares (ActionSheet, Modal): a dimmed
scrim over its parent that follows the parent's size, and the focus it
takes back when it closes. A child widget, never a window: niri floats
dialogs, so Nexus has none."""

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QFrame, QWidget


class Overlay(QFrame):
    """A child of `parent` covering all of it. `_remember_focus()` when it
    opens, `_cover()` to show it, `_restore_focus()` when it closes."""
    SCRIM = 115                                  # the wash's alpha over the page

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._back_to = None                     # focus before it opened
        parent.installEventFilter(self)          # follow the parent's size
        self.hide()

    def _remember_focus(self):
        """Where focus goes back to. Opening again while open (a sheet
        refilled) keeps the first one."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to

    def _cover(self):
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()

    def _restore_focus(self, restore: bool = True):
        back, self._back_to = self._back_to, None
        try:
            if restore and back is not None and back.isVisible():
                focus_and_reveal(back)
        except RuntimeError:                     # rebuilt while the overlay was open
            pass

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, self.SCRIM))
        pt.end()


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .input import focus_and_reveal  # noqa: E402
