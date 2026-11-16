"""The day card of Home's Your year heatmap (w-148, Option B): a card painted
inside the window, beside the day's cell, with the day's total, sessions,
games (cover, share bar, source word) and a quiet footer. It is a child of
the window (never a window of its own), transparent to the mouse, and takes
no focus, so the hover or keyboard ring keeps working under it.

Pure helpers first (`card_text`, `place_card`: tested without Qt state), then
`DayCard`, which only paints what `card_text` decided. The words are also the
widget's accessibleName/Description, so a screen reader reads what the eye
sees. Paint calls here use float overloads only (PyQt6 refuses ints mixed
with floats in one call)."""

from dataclasses import dataclass, field
from datetime import date

from PyQt6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath,
    QPen)
from PyQt6.QtWidgets import QWidget

from .art import anim_ms, art_lookup, ART, COVER_KINDS, screen_dpr
from .common import alive
from .fmt import fmt_duration
from .theme import FONTS, FS, GRAD, is_light, P, R, scaled, SP

CARD_W = 300                    # logical px at scale 1
COVER_W, COVER_H = 24, 32
AVG_NEAR = 5                    # minutes: closer to the average than this is "about average"
SOURCE_WORDS = {'nexus': "Nexus", 'steam': "Steam", 'heroic': "Heroic", 'epic': "Epic",
                'estimated': "Estimated"}
COVER_LOOKS = ('ember', 'dusk', 'tide', 'aurora', 'dawn')
FADE_MS = 120


# ---- pure helpers ----
def source_word(source) -> str:
    s = str(source or 'nexus').lower()
    return SOURCE_WORDS.get(s, s.title())


def _date_head(d: date, today: date | None) -> str:
    year = f" {d.year}" if today is not None and d.year != today.year else ""
    return f"{d:%A} {d.day} {d:%b}{year}"           # no %-d: that is Linux only


@dataclass
class Row:
    slug: str
    name: str
    time: str
    share: float                  # 0..1 of the day's total
    source: str                   # the word: "Nexus", "Steam", "Estimated"
    key: str                      # 'nexus', 'steam', 'heroic', 'estimated': picks the bar colour

    @property
    def pill(self) -> str:
        """The source word shown beside the name; Nexus is the default and shows none."""
        return self.source if self.key != 'nexus' else ""


@dataclass
class CardText:
    head: str
    total: str                    # "2 h 20 min", or "no play" for an empty day
    line2: str = ""
    rows: list[Row] = field(default_factory=list)
    more: str = ""
    footer: str = ""

    def text(self) -> str:
        """The words as one line each, for accessibleDescription and tests."""
        if not self.rows and not self.line2:
            return f"{self.head} · {self.total}"
        parts = [f"{self.head}, {self.total}"]
        if self.line2:
            parts.append(self.line2)
        for r in self.rows:
            parts.append(f"{r.name}, {r.time}" + (f", {r.pill}" if r.pill else ""))
        if self.more:
            parts.append(self.more)
        if self.footer:
            parts.append(self.footer)
        return ". ".join(parts)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _compare(minutes: float, avg: float) -> str:
    if not avg:
        return ""
    diff = minutes - avg
    if abs(diff) < AVG_NEAR:
        return "About your average day"
    return f"{fmt_duration(abs(diff))} {'above' if diff > 0 else 'below'} average"


def card_text(d: date, minutes: float, detail: dict | None, avg: float = 0.0,
              today: date | None = None) -> CardText:
    """What the card says about day `d`. `detail` is the payload's day_games
    entry (n, games, more, first, last, streak_pos), `avg` its avg_minutes.
    A day with no play is one line; kind words only, never red."""
    head = _date_head(d, today)
    if not minutes:
        return CardText(head, "no play")
    ct = CardText(head, fmt_duration(minutes))
    det = detail or {}
    bits = []
    n = det.get('n') or 0
    if n:
        bits.append(_plural(n, "session"))
    first, last = det.get('first'), det.get('last')
    if first:
        bits.append(first if not last or last == first else f"{first} – {last}")
    ct.line2 = " · ".join(bits)
    for g in det.get('games') or []:
        m = g.get('minutes') or 0
        key = str(g.get('source') or 'nexus').lower()
        ct.rows.append(Row(str(g.get('slug') or ''), str(g.get('name') or g.get('slug') or ''),
                           fmt_duration(m), max(0.0, min(1.0, m / minutes)),
                           source_word(key), key))
    if det.get('more'):
        ct.more = f"+ {det['more']} more"
    foot = []
    pos = det.get('streak_pos')
    if isinstance(pos, dict) and pos.get('day') and pos.get('of'):
        foot.append(f"Day {pos['day']} of a {pos['of']}-day streak")
    cmp_ = _compare(minutes, avg or 0.0)
    if cmp_:
        foot.append(cmp_)
    ct.footer = " · ".join(foot)
    return ct


def place_card(cell: QRectF, size: QSize, bounds: QRectF, gap: float, margin: float) -> QPointF:
    """Top-left of a card of `size` beside `cell` (window coordinates): to the
    right of it, flipped to the left when it would pass the right edge, a
    little above the cell and moved up near the bottom, always inside
    `bounds` minus `margin` (as far as it fits)."""
    w, h = size.width(), size.height()
    x = cell.right() + gap
    if x + w > bounds.right() - margin:
        x = cell.left() - gap - w
    x = max(bounds.left() + margin, min(x, bounds.right() - margin - w))
    y = cell.top() - gap
    y = min(y, bounds.bottom() - margin - h)
    y = max(bounds.top() + margin, y)
    return QPointF(x, y)


def _color(s: str) -> QColor:
    """A theme colour: '#rrggbb', or the 'rgba(r,g,b,a)' strings some tokens are."""
    if s.startswith('rgba('):
        r, g, b, a = [x.strip() for x in s[5:-1].split(',')]
        c = QColor(int(r), int(g), int(b))
        c.setAlphaF(float(a))
        return c
    return QColor(s)


def _font(px: int, weight=QFont.Weight.Normal) -> QFont:
    f = QFont(FONTS.ui)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def _bar_color(key: str) -> QColor:
    return _color({'steam': P['col_foam'], 'heroic': P['col_gold'],
                   'estimated': P['muted']}.get(key, P['accent']))


# ---- the widget ----
class DayCard(QWidget):
    """The painted card. `show_for(ct, games)` sets the words and the covers'
    games; `size` includes the soft shadow's margin (`pad`), so the card
    proper is `card_rect()`."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("DayCard")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ct = CardText("", "")
        self.games: dict = {}
        self.pad = SP['3']                      # room for the shadow around the card
        self._opacity = 1.0
        self._fade: QVariantAnimation | None = None
        self._asked: set = set()
        self.hide()

    # -- content and size --
    def card_width(self) -> int:
        return scaled(CARD_W)

    def _fonts(self):
        return (_font(FS['sm'], QFont.Weight.DemiBold), _font(FS['xs']), _font(FS['2xs']))

    def set_content(self, ct: CardText, games: dict | None = None):
        self.ct, self.games = ct, games or {}
        self._asked = set()
        self.setAccessibleName(ct.head)
        self.setAccessibleDescription(ct.text())
        w = self.card_width() + 2 * self.pad
        self.resize(w, int(self._layout()['height']) + 2 * self.pad)
        self.update()

    def card_rect(self) -> QRectF:
        return QRectF(self.pad, self.pad, self.card_width(), self.height() - 2 * self.pad)

    def _layout(self) -> dict:
        """Positions inside the card (card coordinates), from the fonts."""
        sm, xs, x2 = (QFontMetricsF(f) for f in self._fonts())
        inset, gap = float(SP['3']), float(SP['2'])
        w = float(self.card_width())
        y = inset
        out = {'head': QRectF(inset, y, w - 2 * inset, sm.height())}
        y += sm.height()
        if not self.ct.rows and not self.ct.line2:
            out.update(rows=[], height=y + inset)
            return out
        y += gap - SP['1']
        out['line2'] = QRectF(inset, y, w - 2 * inset, xs.height())
        y += xs.height()
        rows = []
        ch = float(scaled(COVER_H))
        for _r in self.ct.rows:
            y += gap
            rows.append(QRectF(inset, y, w - 2 * inset, ch))
            y += ch
        out['rows'] = rows
        if self.ct.more:
            y += gap
            out['more'] = QRectF(inset, y, w - 2 * inset, xs.height())
            y += xs.height()
        if self.ct.footer:
            y += gap
            out['footer'] = QRectF(inset, y, w - 2 * inset, x2.height())
            y += x2.height()
        out['height'] = y + inset
        return out

    # -- fade --
    def opacity(self) -> float:
        return self._opacity

    def set_opacity(self, v: float):
        self._opacity = v
        self.update()

    def appear(self):
        """Show; a short fade only when Motion is on (anim_ms is 0 otherwise)."""
        if self._fade is not None:
            self._fade.stop()
            self._fade = None
        ms = anim_ms(FADE_MS)
        self.set_opacity(0.0 if ms else 1.0)
        self.show()
        self.raise_()
        if ms:
            a = QVariantAnimation(self)
            a.setDuration(ms)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            a.valueChanged.connect(lambda v: alive(self) and self.set_opacity(float(v)))
            self._fade = a
            a.start()

    def vanish(self):
        if self._fade is not None:
            self._fade.stop()
            self._fade = None
        self.hide()

    # -- covers --
    def _cover(self, g: dict | None, w: int, h: int):
        """The decoded cover from the art cache (asked for once), else None."""
        if not g:
            return None
        k = (g.get('slug'), w, h, screen_dpr())
        path = art_lookup(g, COVER_KINDS)[0]
        key = ART.key(path, w, h) if path else None
        if key is None:
            return None
        pm = ART.get(key)
        if pm is None and k not in self._asked:
            self._asked.add(k)
            ART.request_key(key, lambda _pm, me=self: alive(me) and me.update())
        return pm

    @staticmethod
    def initial(name: str) -> str:
        return next((c for c in name if c.isalnum()), '?').upper()

    # -- paint --
    def paintEvent(self, ev):
        if not self.ct.head:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setOpacity(self._opacity)
        card = self.card_rect()
        lay = self._layout()
        radius = float(R['popover'])
        sm_f, xs_f, x2_f = self._fonts()
        sm, xs, x2 = QFontMetricsF(sm_f), QFontMetricsF(xs_f), QFontMetricsF(x2_f)
        # soft shadow: a few faint rings under the card
        shade = _color(P['scrim_badge'])
        for i in range(4):
            grow = float(i * 2)
            shade.setAlphaF(0.07)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(shade)
            pt.drawRoundedRect(card.adjusted(-grow, -grow + 4.0, grow, grow + 4.0),
                               radius + grow, radius + grow)
        path = QPainterPath()
        path.addRoundedRect(card, radius, radius)
        pt.fillPath(path, _color(P['surface_solid']))
        if not is_light():                      # light themes: plain surface (the glass is for dark)
            pt.fillPath(path, _color(P['glass_raised']))
        pt.setPen(QPen(_color(P['border']), 1.0))
        pt.setBrush(Qt.BrushStyle.NoBrush)
        pt.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        def at(r: QRectF) -> QRectF:
            return r.translated(card.topLeft())

        left, right = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight
        mid = Qt.AlignmentFlag.AlignVCenter
        head = at(lay['head'])
        pt.setFont(sm_f)
        if self.ct.rows or self.ct.line2:
            pt.setPen(_color(P['accent']))
            pt.drawText(head, int(right | mid), self.ct.total)
            tw = sm.horizontalAdvance(self.ct.total) + SP['2']
            pt.setPen(_color(P['text']))
            pt.drawText(head.adjusted(0.0, 0.0, -tw, 0.0), int(left | mid),
                        sm.elidedText(self.ct.head, Qt.TextElideMode.ElideRight, head.width() - tw))
        else:                                     # an empty day: one muted line
            pt.setPen(_color(P['text']))
            pt.drawText(head, int(left | mid), self.ct.head)
            pt.setFont(xs_f)
            pt.setPen(_color(P['muted']))
            pt.drawText(head, int(right | mid), self.ct.total)
            pt.end()
            return
        if 'line2' in lay:
            pt.setFont(xs_f)
            pt.setPen(_color(P['muted']))
            pt.drawText(at(lay['line2']), int(left | mid), self.ct.line2)
        cw, ch = float(scaled(COVER_W)), float(scaled(COVER_H))
        for row, rect in zip(self.ct.rows, lay['rows']):
            self._paint_row(pt, row, at(rect), cw, ch, xs_f, x2_f, xs, x2)
        if 'more' in lay:
            pt.setFont(xs_f)
            pt.setPen(_color(P['muted']))
            pt.drawText(at(lay['more']), int(left | mid), self.ct.more)
        if 'footer' in lay:
            pt.setFont(x2_f)
            pt.setPen(_color(P['muted']))
            pt.drawText(at(lay['footer']), int(left | mid),
                        x2.elidedText(self.ct.footer, Qt.TextElideMode.ElideRight,
                                      lay['footer'].width()))
        pt.end()

    def _paint_row(self, pt, row: Row, rect: QRectF, cw: float, ch: float, xs_f, x2_f, xs, x2):
        left, right = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight
        mid = Qt.AlignmentFlag.AlignVCenter
        gap = float(SP['2'])
        radius = R['sm'] / 2
        cover = QRectF(rect.left(), rect.top(), cw, ch)
        g = self.games.get(row.slug)
        pm = self._cover(g, int(cw), int(ch))
        clip = QPainterPath()
        clip.addRoundedRect(cover, radius, radius)
        if pm is not None:
            pt.save()
            pt.setClipPath(clip)
            pt.drawPixmap(cover.topLeft(), pm)
            pt.restore()
        else:
            a, b = GRAD[COVER_LOOKS[sum(map(ord, row.slug or row.name)) % len(COVER_LOOKS)]]
            grad = QLinearGradient(cover.topLeft(), cover.bottomRight())
            grad.setColorAt(0.0, QColor(a))
            grad.setColorAt(1.0, QColor(b))
            pt.fillPath(clip, grad)
            pt.setFont(_font(FS['xs'], QFont.Weight.DemiBold))
            pt.setPen(_color(P['on_accent']))
            pt.drawText(cover, int(Qt.AlignmentFlag.AlignCenter), self.initial(row.name))
        x0 = cover.right() + gap
        x1 = rect.right()
        top = QRectF(x0, rect.top(), x1 - x0, float(xs.height()))
        pt.setFont(xs_f)
        pt.setPen(_color(P['subtext']))
        pt.drawText(top, int(right | mid), row.time)
        room = top.width() - xs.horizontalAdvance(row.time) - SP['2']
        pill_w = 0.0
        if row.pill:
            pill_w = x2.horizontalAdvance(row.pill) + 2.0 * SP['1_5']
            room -= pill_w + SP['1_5']
        name = xs.elidedText(row.name, Qt.TextElideMode.ElideRight, max(0.0, room))
        pt.setPen(_color(P['text']))
        pt.drawText(top, int(left | mid), name)
        if row.pill:
            px = x0 + xs.horizontalAdvance(name) + SP['1_5']
            pill = QRectF(px, top.top() + (xs.height() - x2.height()) / 2, pill_w, float(x2.height()))
            pp = QPainterPath()
            pp.addRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            pt.fillPath(pp, _color(P['accent_16']))
            pt.setFont(x2_f)
            pt.setPen(_color(P['subtext']))
            pt.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), row.pill)
        bar_h = float(SP['1'])
        bar = QRectF(x0, rect.bottom() - bar_h, x1 - x0, bar_h)
        track = QPainterPath()
        track.addRoundedRect(bar, bar_h / 2, bar_h / 2)
        pt.fillPath(track, _color(P['overlay']))
        fill = QRectF(bar.left(), bar.top(), max(bar_h, bar.width() * row.share), bar_h)
        fp = QPainterPath()
        fp.addRoundedRect(fill, bar_h / 2, bar_h / 2)
        pt.fillPath(fp, _bar_color(row.key))


class CardHost:
    """What a painted widget needs to show a DayCard beside one of its parts
    (w-141: the heatmap and the space map share it). Mix in before QWidget,
    call `init_card_host()` once the widget exists, implement `_timed_show`.
    The card is a child of the window, not of the widget: it goes with it."""

    def init_card_host(self):
        self.card: DayCard | None = None
        self.show_timer = QTimer(self)
        self.show_timer.setSingleShot(True)
        self.show_timer.timeout.connect(self._timed_show)

    def _timed_show(self):
        raise NotImplementedError

    def _card(self) -> DayCard:
        win = self.window()
        if self.card is None or not alive(self.card):
            self.card = DayCard(win)
            self.destroyed.connect(lambda _=None, c=self.card: alive(c) and c.deleteLater())
        elif self.card.parent() is not win:
            self.card.setParent(win)
        return self.card

    def show_card_at(self, rect: QRectF, ct: CardText, games: dict | None = None):
        """Put the card for `ct` beside `rect` (widget coordinates), inside the
        window; flipped left near the right edge and moved up near the bottom."""
        self.show_timer.stop()
        card = self._card()
        win = self.window()
        card.set_content(ct, games)
        top_left = self.mapTo(win, rect.topLeft().toPoint())
        cell = QRectF(float(top_left.x()), float(top_left.y()), rect.width(), rect.height())
        proper = card.card_rect()
        at = place_card(cell, proper.size().toSize(), QRectF(win.rect()), float(SP['2']),
                        float(SP['1']))
        card.move(int(round(at.x() - proper.left())), int(round(at.y() - proper.top())))
        if not card.isVisible():
            card.appear()
        else:
            card.raise_()

    def _hide_card(self):
        self.show_timer.stop()
        if self.card is not None and alive(self.card):
            self.card.vanish()
