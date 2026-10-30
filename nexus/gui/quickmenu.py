"""Theatre's quick menu: the system panel on Guide / Start / F10."""

from datetime import datetime
from typing import NamedTuple

from PyQt6.QtCore import pyqtSignal, QEvent, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .theme import G, P, SP


class QuickRow(NamedTuple):
    """One row of the quick menu. `key` is what `chosen` sends. Rows with a
    `section` sit at the bottom of the panel under that heading (POWER),
    away from the default focus."""
    key: str
    label: str
    glyph: str
    danger: bool = False
    section: str = ''


class QuickMenu(QFrame):
    """A left-side panel over the dimmed Theatre page: clock and date, the
    connected pads, then the rows the window hands over (Exit Theatre, Quit
    Nexus). A child overlay like ActionSheet, never a dialog; the right
    side stays the game's own Options sheet. Two states: the list, and a
    question asked in place of the rows (`ask`), with Cancel focused. B
    steps back one state at a time (`back`)."""
    chosen = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, parent: QWidget, scale: float = 1.0):
        super().__init__(parent)
        self.setObjectName("QuickScrim")
        self.scale = scale
        self.state = 'closed'                    # closed | list | confirm
        self._back_to = None                     # focus before the menu opened
        self._row_from = None                    # the row a question came from
        self._rows: list[QuickRow] = []
        self.buttons: dict[str, QPushButton] = {}
        sc = lambda v: int(v * scale)            # noqa: E731
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame()
        self.panel.setObjectName("QuickPanel")
        self.panel.setFixedWidth(sc(400))
        outer.addWidget(self.panel)
        outer.addStretch()
        lay = QVBoxLayout(self.panel)
        m = sc(SP['5'])
        lay.setContentsMargins(m, sc(SP['6']), m, m)
        lay.setSpacing(sc(SP['1']))
        self.clock = label("", "QuickClock")
        lay.addWidget(self.clock)
        self.date = label("", "QuickDate")
        lay.addWidget(self.date)
        lay.addSpacing(sc(SP['4']))
        self.pads = QFrame()
        self.pads.setObjectName("QuickPad")
        self.pad_rows = QVBoxLayout(self.pads)
        self.pad_rows.setContentsMargins(sc(SP['3']), sc(SP['3']), sc(SP['3']), sc(SP['3']))
        self.pad_rows.setSpacing(SP['2'])
        lay.addWidget(self.pads)
        lay.addSpacing(sc(SP['4']))
        self.body = QVBoxLayout()                # the rows, or a question
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(sc(SP['1']))
        lay.addLayout(self.body)
        lay.addStretch()
        self.foot = QVBoxLayout()                # rows with a section, at the bottom
        self.foot.setContentsMargins(0, 0, 0, 0)
        self.foot.setSpacing(sc(SP['1']))
        lay.addLayout(self.foot)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(20000)
        self._tick_timer.timeout.connect(self._tick)
        parent.installEventFilter(self)          # follow the parent's size
        self.hide()

    # ---- open / close ----
    def open(self, rows: list[QuickRow], pads: list[dict], focus: str | None = None):
        """Show the list. `pads`: [{'name', 'percent' (or None), 'charging'}];
        the strip hides with none. `focus`: the row that starts focused
        (else the first)."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self._rows = list(rows)
        self._fill_pads(pads)
        self._tick()
        self._tick_timer.start()
        self._show_list(focus)
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._focus(self.buttons.get(focus) or self.default_focus())

    def ask(self, title: str, text: str, yes: str, glyph: str, on_yes, danger: bool = True):
        """A question in place of the rows; Cancel has the focus, so A pressed
        out of habit never confirms anything. B or Cancel go back to the
        list, to the row it came from."""
        f = QApplication.focusWidget()
        self._row_from = f if f is not None and self.isAncestorOf(f) else None
        self.state = 'confirm'
        self._clear()
        sc = lambda v: int(v * self.scale)       # noqa: E731
        self.body.addWidget(label(title, "SheetTitle", wrap=True))
        if text:
            self.body.addSpacing(sc(SP['1']))
            self.body.addWidget(plain_label(text, "Muted", wrap=True))
        self.body.addSpacing(sc(SP['3']))
        ok = self._button(yes, glyph, danger)
        ok.clicked.connect(lambda: (self.close_menu(restore=False), on_yes()))
        no = self._button("Cancel", G['close'])
        no.clicked.connect(self.back)
        self.body.addWidget(ok)
        self.body.addWidget(no)
        self._focus(no)

    def back(self) -> bool:
        """B: a question goes back to the list, the list closes. True if
        the menu was open."""
        if self.state == 'confirm':
            came = self._row_from
            key = next((k for k, b in self.buttons.items() if b is came), None)
            self._show_list(key)
            self._focus(self.buttons.get(key) or self.default_focus())
            return True
        if self.state == 'list':
            self.close_menu()
            return True
        return False

    def close_menu(self, restore: bool = True):
        if self.isHidden():
            return
        self.hide()
        self.state = 'closed'
        self._tick_timer.stop()
        back, self._back_to = self._back_to, None
        try:
            if restore and back is not None and back.isVisible():
                focus_and_reveal(back)
        except RuntimeError:                     # rebuilt while the menu was open
            pass
        self.closed.emit()

    def default_focus(self):
        return next((b for b in self.panel.findChildren(QPushButton)
                     if b.isEnabled() and not b.isHidden()), None)

    # ---- building ----
    def _show_list(self, focus: str | None = None):
        self.state = 'list'
        self._clear()
        sc = lambda v: int(v * self.scale)       # noqa: E731
        section = None
        for r in self._rows:
            b = self._button(r.label, r.glyph, r.danger)
            b.clicked.connect(lambda _=False, k=r.key: self._run(k))
            self.buttons[r.key] = b
            if not r.section:
                self.body.addWidget(b)
                continue
            if r.section != section:
                if section is not None:
                    self.foot.addSpacing(sc(SP['2']))
                self.foot.addWidget(label(r.section.upper(), "SheetSection"))
                section = r.section
            self.foot.addWidget(b)

    def _button(self, text: str, glyph: str, danger: bool = False) -> QPushButton:
        b = QPushButton(amp(text))
        b.setObjectName("SheetRow")
        b.setProperty('danger', danger)
        set_glyph_icon(b, glyph, int(18 * self.scale), 'danger' if danger else 'subtext',
                       hover_role=None)
        b.setMinimumHeight(int(44 * self.scale))
        return b

    def _fill_pads(self, pads: list[dict]):
        clear_layout(self.pad_rows)
        px = int(20 * self.scale)
        for p in pads:
            row = QHBoxLayout()
            row.setSpacing(int(SP['3'] * self.scale))
            row.addWidget(icon_label(G['gamepad'], px, P['subtext']))
            row.addWidget(label(p.get('name') or "Controller", "QuickPadName"), 1)
            pct = p.get('percent')
            if pct is not None:
                glyph = (G['battery_charging'] if p.get('charging') else G['battery_full']
                         if pct >= 80 else G['battery_medium'] if pct >= 40
                         else G['battery_low'])
                low = pct < 20 and not p.get('charging')
                row.addWidget(icon_label(glyph, px, P['danger' if low else 'subtext']))
                row.addWidget(label(f"{pct}%", "QuickPadPct"))
            self.pad_rows.addLayout(row)
        self.pads.setVisible(bool(pads))

    def _clear(self):
        self.buttons = {}
        clear_layout(self.body)
        clear_layout(self.foot)

    def _focus(self, w):
        if w is not None:
            w.setFocus(Qt.FocusReason.TabFocusReason)

    def _run(self, key: str):
        self.chosen.emit(key)

    def _tick(self):
        now = datetime.now()
        self.clock.setText(now.strftime('%H:%M'))
        self.date.setText(f"{now:%A} {now.day} {now:%B}")   # %-d isn't on Windows

    # ---- events ----
    def keyPressEvent(self, ev):
        # X (Menu) does nothing here; Escape (B) goes through the window's
        # back order like everything else.
        if ev.key() == Qt.Key.Key_Menu:
            return
        super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        if not self.panel.geometry().contains(ev.position().toPoint()):
            self.close_menu()
        super().mousePressEvent(ev)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, 115))     # the same wash as ActionSheet
        pt.end()


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .input import focus_and_reveal  # noqa: E402
from .widgets import (amp, clear_layout, icon_label, label, plain_label,  # noqa: E402
    set_glyph_icon)
