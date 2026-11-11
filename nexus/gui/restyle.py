"""A look change styles what shows (w-111).

Any app-wide setStyleSheet costs about a second whatever the sheet holds (Qt
re-polishes every widget), so after start-up the app sheet is never set again.
The new sheets go on the window's "roots" instead: the widget children of the
containers from the window down to the page stack (each page, the top bar,
Details, the activity strip, Theatre, the hint bar, the overlays). Roots on
screen get the sheet at once; the others when they are next shown. An own
sheet beats the app sheet, and every look has the same selectors, so the
start-up sheet never shows through."""

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QWidget

from .theme import build_stylesheet


class Restyler(QObject):
    """Child of the main window (which has shell, desk, split, stack, theatre, hints)."""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self._css: dict[str, str] = {}
        self._changed = False                    # no change yet: the start-up app sheet rules
        for con in self._containers():
            con.installEventFilter(self)         # ChildPolished: roots built later

    def _containers(self) -> list:
        w = self.win
        return [w, w.centralWidget(), w.shell, w.desk, w.split, w.stack]

    def roots(self) -> list:
        path = self._containers()
        return [c for con in path for c in con.children()
                if isinstance(c, QWidget) and c not in path]

    def sheet(self, kind: str) -> str:
        """'desk' or 'theatre': the sheet for the current look, built once per change."""
        if kind not in self._css:
            from .theatre import THEATRE_SCALE
            self._css[kind] = build_stylesheet(THEATRE_SCALE) if kind == 'theatre' \
                else build_stylesheet()
        return self._css[kind]

    def _kind(self, root) -> str:
        w = self.win
        theatre = root is getattr(w, 'theatre', None) or (
            root is getattr(w, 'hints', None) and getattr(w, '_theatre', False))
        return 'theatre' if theatre else 'desk'

    def restyle(self):
        """The look changed: new sheets, on what shows now and on the rest as it shows."""
        self._css = {}
        self._changed = True
        for root in self.roots():
            self._watch(root)
            if root.isVisible():
                self._apply(root)

    def _watch(self, root):
        root.installEventFilter(self)            # its Show: restyle a page that waited
        # (Qt never installs one filter twice; no destroyed() hook: it ran during teardown)

    def _apply(self, root):
        css = self.sheet(self._kind(root))
        if root.styleSheet() != css:
            root.setStyleSheet(css)

    def eventFilter(self, obj, ev):
        try:
            self._filter(obj, ev)
        except RuntimeError:                     # a wrapped C++ object went away (teardown)
            pass
        return False

    def _filter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.Show:
            if self._changed and isinstance(obj, QWidget) and obj not in self._containers():
                self._apply(obj)
        elif t == QEvent.Type.ChildPolished:
            child = ev.child()
            if isinstance(child, QWidget) and child.parent() is obj and \
                    child not in self._containers():
                self._watch(child)
                if self._changed:
                    self._apply(child)
