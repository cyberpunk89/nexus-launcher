"""The A-Z jump strip of the Library's All games tab (w-116).

A column of letters on the grid's right, # for names that don't start
with a letter. Letters with a game are lit and take focus (Tab, the
arrows, the pad); the others are dimmed and skipped. A click or A on one
emits `jump(letter)`: the window moves focus to that letter's first tile
through the grid's `tile_for`, so a far row is built first."""

import unicodedata

from PyQt6.QtCore import pyqtSignal, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QAbstractButton, QSizePolicy, QVBoxLayout, QWidget

from .theme import FONTS, P, scaled

LETTERS = ('#',) + tuple(chr(c) for c in range(ord('A'), ord('Z') + 1))


def letter_of(name: str) -> str:
    """The strip letter a name sorts under: its first letter, accents
    dropped ("Ōkami" is O), or # for a digit or anything else."""
    for ch in unicodedata.normalize('NFKD', (name or '').strip()):
        if unicodedata.combining(ch):
            continue
        up = ch.upper()
        return up if 'A' <= up <= 'Z' else '#'
    return '#'


def first_by_letter(names) -> dict[str, int]:
    """{letter: index of the first name under it} over names in shown order."""
    out: dict[str, int] = {}
    for i, n in enumerate(names):
        out.setdefault(letter_of(n), i)
    return out


class _Letter(QAbstractButton):
    """One letter, painted (theme changes need no restyle)."""

    def __init__(self, letter: str, parent=None):
        super().__init__(parent)
        self.setText(letter)
        self.setAccessibleName(f"Jump to {letter}")
        self.setToolTip(f"Jump to {letter}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored)
        self.setMinimumHeight(1)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isEnabled()
        if on and (self.underMouse() or self.hasFocus()):
            bg = QColor(P['accent'])
            bg.setAlpha(60)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            d = min(self.width(), self.height())
            p.drawRoundedRect(QRectF((self.width() - d) / 2, (self.height() - d) / 2, d, d),
                              d / 2, d / 2)
        c = QColor(P['text'] if on else P['muted'])
        if not on:
            c.setAlpha(110)
        p.setPen(c)
        f = QFont(FONTS.ui)
        f.setPixelSize(max(8, min(scaled(11), self.height() - 2)))
        f.setBold(on)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()

    def enterEvent(self, ev):
        super().enterEvent(ev)
        self.update()

    def leaveEvent(self, ev):
        super().leaveEvent(ev)
        self.update()


class LetterJump(QWidget):
    """The strip. `set_lit(letters)` lights the letters that have a game
    (an unlit one is disabled, so focus and the pad pass it by)."""
    jump = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LetterJump")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, scaled(4), 0, scaled(4))
        lay.setSpacing(0)
        self.buttons: dict[str, _Letter] = {}
        for ch in LETTERS:
            b = _Letter(ch, self)
            b.setFixedWidth(scaled(22))
            b.clicked.connect(lambda _=False, c=ch: self.jump.emit(c))
            lay.addWidget(b, 1, Qt.AlignmentFlag.AlignHCenter)
            self.buttons[ch] = b
        self.setFixedWidth(scaled(26))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_lit(self, letters):
        letters = set(letters)
        for ch, b in self.buttons.items():
            on = ch in letters
            if b.isEnabled() != on:
                b.setEnabled(on)
                b.update()

    def lit(self) -> list[str]:
        return [ch for ch, b in self.buttons.items() if b.isEnabled()]
