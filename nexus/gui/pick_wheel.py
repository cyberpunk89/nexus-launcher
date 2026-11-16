"""The wheel of Pick for me's Wheel style (w-142): up to 8 games on a painted
wheel. The pick is decided first (the caller hands `spin_to` the winning
segment) and the wheel only animates to it; with Motion off it lands at once."""

from PyQt6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

from .art import anim_ms
from .theme import FONTS, FS, P, scaled

SEGMENTS = 8                              # games on the wheel
SPIN_MS = 2400                            # the spin, before the Motion setting
TURNS = 3                                 # whole turns before it settles


def landing_angle(index: int, count: int, current: float, turns: int = TURNS) -> float:
    """The wheel angle (degrees, clockwise) that puts segment `index` under
    the pointer at the top, at least `turns` whole turns past `current`."""
    base = (-index * 360.0 / count) % 360.0
    k = -(-(current + 360.0 * turns - base) // 360.0)         # ceil
    return base + 360.0 * k


def initial(name: str) -> str:
    for ch in name:
        if ch.isalnum():
            return ch.upper()
    return "?"


class PickWheel(QWidget):
    """Painted wheel. Segments are game names (the first letter is drawn);
    `landed` is the winning one once a spin ends."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.names: list[str] = []
        self.angle = 0.0
        self.landed: int | None = None
        self.anim: QVariantAnimation | None = None
        self.setAccessibleName("Pick wheel")
        self.setToolTip("The wheel lands on the game Spin picks")
        self.setFixedSize(scaled(132), scaled(132))

    @property
    def spinning(self) -> bool:
        return self.anim is not None

    def set_names(self, names: list[str]):
        """The games on the wheel (at most SEGMENTS). Ignored mid-spin."""
        if self.spinning:
            return
        names = list(names)[:SEGMENTS]
        if names != self.names:
            self.names = names
            self.landed = None
            self.angle = 0.0
            self._describe()
            self.update()

    def _describe(self):
        if self.landed is not None and self.landed < len(self.names):
            self.setAccessibleDescription(f"Landed on {self.names[self.landed]}")
        else:
            self.setAccessibleDescription(f"{len(self.names)} games on the wheel")

    def spin_to(self, index: int, done=None) -> bool:
        """Turn to segment `index`, then call `done`. With Motion off nothing
        is animated: the wheel lands at once and `done` runs now. False when
        `index` is not on the wheel."""
        n = len(self.names)
        if not 0 <= index < n:
            return False
        self.landed = None
        target = landing_angle(index, n, self.angle)
        ms = anim_ms(SPIN_MS)
        if not ms:
            self._land(index, target, done)
            return True
        anim = self.anim = QVariantAnimation(self)
        anim.setDuration(ms)
        anim.setStartValue(self.angle)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(self._turn)
        anim.finished.connect(lambda: self._land(index, target, done))
        anim.start()
        return True

    def _turn(self, v):
        self.angle = float(v)
        self.update()

    def _land(self, index: int, target: float, done):
        anim, self.anim = self.anim, None
        if anim is not None:
            anim.deleteLater()
        self.angle = target % 360.0
        self.landed = index
        self._describe()
        self.update()
        if done is not None:
            done()

    def paintEvent(self, ev):
        n = len(self.names)
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = float(min(self.width(), self.height()))
        pad = side * 0.07
        box = QRectF((self.width() - side) / 2.0 + pad, (self.height() - side) / 2.0 + pad,
                     side - 2.0 * pad, side - 2.0 * pad)
        c = box.center()
        accent = QColor(P['accent'])
        if not n:
            pt.setPen(QPen(QColor(P['overlay']), 2.0))
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.drawEllipse(box)
            return
        step = 360.0 / n
        font = QFont(FONTS.ui)
        font.setPixelSize(scaled(FS['sm']))
        font.setBold(True)
        pt.setFont(font)
        for i in range(n):
            theta = self.angle + i * step            # clockwise from the top
            start = 90.0 - theta - step / 2.0        # Qt: counter-clockwise from 3 o'clock
            won = i == self.landed
            tone = QColor(accent)
            tone.setAlphaF(1.0 if won else (0.34 if i % 2 else 0.18))
            pt.setPen(QPen(QColor(P['surface_solid']), 1.5))
            pt.setBrush(tone)
            pt.drawPie(box, int(round(start * 16)), int(round(step * 16)))
            pt.save()
            pt.translate(c)
            pt.rotate(theta)
            pt.setPen(QColor(P['on_accent'] if won else P['text']))
            r = box.width() / 2.0
            pt.drawText(QRectF(-r * 0.25, -r * 0.88, r * 0.5, r * 0.4),
                        Qt.AlignmentFlag.AlignCenter, initial(self.names[i]))
            pt.restore()
        pt.setPen(QPen(accent, 2.0))
        pt.setBrush(Qt.BrushStyle.NoBrush)
        pt.drawEllipse(box)
        # The hub, and the pointer at the top.
        hub = box.width() * 0.13
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['surface_solid']))
        pt.drawEllipse(c, hub, hub)
        pt.setBrush(accent)
        pt.drawEllipse(c, hub * 0.45, hub * 0.45)
        w = box.width() * 0.07
        tip = QPointF(c.x(), box.top() + box.width() * 0.1)
        pt.setBrush(QColor(P['text']))
        pt.drawPolygon(QPolygonF([QPointF(c.x() - w, box.top() - w * 0.9),
                                  QPointF(c.x() + w, box.top() - w * 0.9), tip]))
