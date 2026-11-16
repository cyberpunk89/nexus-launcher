"""Theatre's Ribbon layout (w-108): categories on one line, the focused
category's items in a column under it, and the ribbon of light behind them.

`RibbonView` is one focusable widget that paints the bar and the column
itself and keeps every arrow key (NATIVE_ARROWS = 'all'): the content moves,
focus stays put. What it shows comes from ribbon_model. `paint_ribbon` is
the background's ribbon: an original sheet of thin curves (sine waves with
a drift), drawn additively on dark themes; GradientBackground paints it."""

import math
import random
import re
from datetime import datetime
from html import unescape

from PyQt6.QtCore import pyqtSignal, QEasingCurve, QPointF, QRect, QRectF, Qt, QVariantAnimation
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen, QStaticText)
from PyQt6.QtWidgets import QWidget

from .ribbon_model import (clamp, column, columns, crumbs, current, find, nav, rail_pos,
                           resolve, RibbonState, steps_in_place)
from .theme import ACCENTS, FONTS, FS, G, GRAD, LOOK, P, R


# ---- the background's ribbon ----
LINES = 26                            # curves in the sheet
STEPS = 48                            # points per curve


def _curve_y(x: float, i: int, phase: float, h: float) -> float:
    """Height of curve i at x (0..1): two waves at different speeds and a
    per-line offset, so the lines bunch together and fan out like a sheet
    seen nearly edge-on."""
    k = i / (LINES - 1)
    a = 0.075 * h * (0.6 + 0.4 * math.sin(phase * 0.6 + k * 2.1))
    return (0.62 * h
            + a * math.sin(2 * math.pi * 0.85 * x + phase + k * 1.4)
            + 0.045 * h * math.sin(2 * math.pi * 1.9 * x - phase * 0.7 + k * 3.0)
            + (k - 0.5) * 0.06 * h * math.cos(math.pi * x + phase * 0.4))


def paint_ribbon(p: QPainter, rect: QRectF, phase: float, color: QColor, light: bool):
    """The ribbon into `rect`: a soft fill between the outer curves and the
    curves themselves, faded out at both ends. Additive on dark themes; on
    light ones a normal blend at about half the strength."""
    w, h = rect.width(), rect.height()
    if w < 2 or h < 2:
        return
    p.save()
    p.translate(rect.topLeft())
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if not light:
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    xs = [s / STEPS for s in range(STEPS + 1)]
    top = [QPointF(x * w, _curve_y(x, 0, phase, h)) for x in xs]
    bottom = [QPointF(x * w, _curve_y(x, LINES - 1, phase, h)) for x in xs]
    sheet = QPainterPath(top[0])
    for pt in top[1:] + bottom[::-1]:
        sheet.lineTo(pt)
    sheet.closeSubpath()
    g = QLinearGradient(0.0, 0.0, w, 0.0)
    for at, f in ((0.0, 0.0), (0.25, 1.0), (0.75, 1.0), (1.0, 0.1)):
        c = QColor(color)
        c.setAlphaF((0.07 if light else 0.11) * f)
        g.setColorAt(at, c)
    p.fillPath(sheet, g)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for i in range(LINES):
        path = QPainterPath(QPointF(0.0, _curve_y(0.0, i, phase, h)))
        for x in xs[1:]:
            path.lineTo(x * w, _curve_y(x, i, phase, h))
        edge = i in (0, LINES - 1)
        alpha = (0.55 if edge else 0.10) * (0.55 if light else 1.0)
        g = QLinearGradient(0.0, 0.0, w, 0.0)
        for at, f in ((0.0, 0.0), (0.18, 1.0), (0.82, 1.0), (1.0, 0.15)):
            c = QColor(color)
            c.setAlphaF(alpha * f)
            g.setColorAt(at, c)
        p.setPen(QPen(g, max(1.0, h / (540 if edge else 900))))
        p.drawPath(path)
    p.restore()


def ribbon_image(w: int, h: int, phase: float, color: QColor, light: bool,
                 scale: int = 2) -> QImage:
    """The ribbon painted at 1/scale size, to be drawn scaled up: it is all
    soft light, so half size looks the same and costs a quarter."""
    img = QImage(max(1, w // scale), max(1, h // scale), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    paint_ribbon(p, QRectF(0, 0, img.width(), img.height()), phase, color, light)
    p.end()
    return img


def ribbon_color(tint: QColor, light: bool) -> QColor:
    """The ribbon's colour from the background's tint: on a dark theme kept
    a light, not a stain (saturation at most 0.55, value at least 0.85)."""
    c = QColor(tint)
    h, s, v, _ = c.getHsvF()
    if h >= 0 and not light:                     # -1 for greys (Pitfalls - Qt §3)
        c = QColor.fromHsvF(h, min(s, 0.55), max(v, 0.85))
    return c


# Twelve of Nexus's own accents, one per month, January first.
MONTH_ACCENTS = ('blue', 'silver', 'mint', 'rose', 'teal', 'gold',
                 'coral', 'sky', 'iris', 'love', 'foam', 'pine')


def classic_color(now: datetime, light: bool) -> QColor:
    """The ribbon's colour with Classic colours on: the month's accent for
    the hue, the time of day for the brightness. Dark themes: saturation at
    most 0.55, value from about 0.62 at 3 am up to 1.0 at 3 pm. Light
    themes keep the accent and only vary its value a little."""
    c = QColor(ACCENTS[MONTH_ACCENTS[now.month - 1]])
    h, s, v, _ = c.getHsvF()
    day = math.cos(2 * math.pi * ((now.hour + now.minute / 60) - 15) / 24)   # 1 at 15:00
    if light:
        return QColor.fromHsvF(max(h, 0.0), s, max(0.0, min(1.0, v * (0.92 + 0.08 * day))))
    return QColor.fromHsvF(max(h, 0.0), min(s, 0.55), 0.81 + 0.19 * day)


MARKS = ('ring', 'triangle', 'box', 'plus')
MARK_ALPHA = 0.45                                # an outline's best alpha (the cap is 0.55)


def mark_specs(seed) -> list:
    """3 to 7 marks, drawn from three of the four kinds (never all four),
    each in its own slice of the width so none overlaps another."""
    rnd = random.Random(seed)
    kinds = rnd.sample(MARKS, 3)
    n = rnd.randint(3, 7)
    slots = list(range(n))
    rnd.shuffle(slots)
    return [dict(kind=rnd.choice(kinds), slot=slots[i], n=n,
                 y=rnd.uniform(0.2, 0.8), size=rnd.uniform(0.6, 1.0),
                 turn=rnd.uniform(-0.4, 0.4), drift=rnd.uniform(-1.0, 1.0),
                 rise=rnd.uniform(0.04, 0.12)) for i in range(n)]


def paint_marks(p: QPainter, rect: QRectF, t: float, color: QColor, specs: list):
    """The marks at time t (0..1) in `rect`: thin outlines, no fill, fading
    in and out, drifting slowly up and sideways. Nothing at t <= 0 or >= 1."""
    fade = max(0.0, min(1.0, t / 0.25, (1.0 - t) / 0.25))
    if fade <= 0 or rect.width() < 2 or rect.height() < 2:
        return
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(Qt.BrushStyle.NoBrush)
    c = QColor(color)
    c.setAlphaF(MARK_ALPHA * fade)
    unit = min(rect.height() * 0.5, rect.width() / 10)
    for sp in specs:
        slot_w = rect.width() / sp['n']
        r = unit * 0.5 * sp['size']
        r = min(r, slot_w * 0.3)
        cx = rect.left() + slot_w * (sp['slot'] + 0.5) + sp['drift'] * slot_w * 0.12 * (t - 0.5)
        cy = rect.top() + rect.height() * (sp['y'] - sp['rise'] * t)
        p.setPen(QPen(c, max(1.0, unit / 22)))
        p.save()
        p.translate(cx, cy)
        p.rotate(sp['turn'] * 360 * t * 0.25)
        k = sp['kind']
        if k == 'ring':
            p.drawEllipse(QPointF(0, 0), r, r)
        elif k == 'box':
            p.drawRoundedRect(QRectF(-r, -r, 2 * r, 2 * r), r * 0.3, r * 0.3)
        elif k == 'triangle':
            p.drawPolygon([QPointF(0, -r), QPointF(r * 0.95, r * 0.7), QPointF(-r * 0.95, r * 0.7)])
        else:
            a = r * 0.3                                  # a hollow plus: one outline, so no stroke doubles up
            p.drawPolygon([QPointF(x, y) for x, y in (
                (-a, -r), (a, -r), (a, -a), (r, -a), (r, a), (a, a),
                (a, r), (-a, r), (-a, a), (-r, a), (-r, -a), (-a, -a))])
        p.restore()
    p.restore()


# ---- the view ----
def font(key: str, scale: float, weight=QFont.Weight.Normal) -> QFont:
    f = QFont(FONTS.ui)
    f.setPixelSize(max(1, int(FS[key] * scale * LOOK['scale'])))
    f.setWeight(weight)
    return f


def glyph_font(px: float) -> QFont:
    f = QFont(FONTS.icon)
    f.setPixelSize(max(1, int(px)))
    return f


def geometry(w: int, h: int, scale: float) -> dict:
    """Every size from the view's size; u = 1 at 900 px tall."""
    u = max(0.62, min(1.6, h / 900))
    by = int(h * 0.27)
    # The focused category's name, its underline, then its tags (w-145).
    label = QFontMetrics(font('display', scale, QFont.Weight.DemiBold)).height() \
        + int(UNDERLINE[1] * u) + int(20 * u) + QFontMetrics(font('sm', scale)).height()
    # The focused art never runs off the left edge (a narrow window).
    return dict(u=u, ax=max(int(214 * u), int(w * 0.17)), by=by, gap=int(210 * u),
                icon_on=int(72 * u), icon_off=int(46 * u),
                art_on=(int(380 * u), int(178 * u)), art_off=(int(200 * u), int(94 * u)),
                glyph_on=int(112 * u), glyph_off=int(64 * u),
                col_top=by + int(36 * u) + label + int(46 * u),
                step=int(18 * u), focus_gap=int(34 * u))


def qcolor(value: str) -> QColor:
    """A token's colour: '#rrggbb' or the stylesheet's 'rgba(r,g,b,a)'
    (the glass tokens), which QColor can't read itself."""
    m = re.fullmatch(r'\s*rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)\s*', value)
    if m:
        c = QColor(int(m[1]), int(m[2]), int(m[3]))
        c.setAlphaF(max(0.0, min(1.0, float(m[4]))))
        return c
    return QColor(value)


def color(key: str, alpha: float = 1.0) -> QColor:
    c = qcolor(P[key])
    c.setAlphaF(c.alphaF() * alpha)
    return c


def glass(key: str = 'glass_hi') -> QColor:
    """A glass fill for text in the page's own colours: the glass tokens on
    a dark theme; on a light one they are dark (made for white text over
    art), so the raised surface instead."""
    return color('surface_2') if LOOK['light'] else color(key)


def plain(text: str) -> str:
    """Rich text as plain words (for the accessible description)."""
    return ' '.join(unescape(re.sub(r'<[^>]+>', '', text or '')).split())


def tag_h(f: QFont, u: float) -> float:
    return QFontMetrics(f).height() + 12 * u


def paint_tags(p, tags, x: float, y: float, u: float, f: QFont, max_w: float,
               center: bool = False, pad: float = 12.0) -> tuple:
    """Soft tags in one line from (x, y) (centred on x with `center`):
    a tinted capsule per (text, role); a role is a palette key or a
    '#rrggbb' colour (then a dot in it, the text stays plain). Tags that
    don't fit `max_w` are left out. Returns (right edge, height). The
    Ribbon's items and the game rail's strip (TagStrip) both paint them."""
    fm = QFontMetrics(f)
    h, gap, pad = tag_h(f, u), 8 * u, pad * u
    dot = 8 * u
    sized = []
    total = 0.0
    for text, role in tags:
        hexed = role.startswith('#')
        w = fm.horizontalAdvance(text) + 2 * pad + (dot + 6 * u if hexed else 0)
        if sized and total + gap + w > max_w or not sized and w > max_w:
            break
        total += (gap if sized else 0) + w
        sized.append((text, role, hexed, w))
    cx = x - total / 2 if center else x
    p.save()
    p.setFont(f)
    for text, role, hexed, w in sized:
        tint = QColor(role) if hexed else color(role)
        plain_tag = role == 'subtext'
        r = QRectF(cx, y, w, h)
        fill = color('surface_2', 0.85) if plain_tag else QColor(tint)
        if not plain_tag:
            fill.setAlphaF(0.16)
        edge = color('muted', 0.30) if plain_tag else QColor(tint)
        if not plain_tag:
            edge.setAlphaF(0.40)
        p.setBrush(fill)
        p.setPen(QPen(edge, max(1.0, 1.2 * u)))
        p.drawRoundedRect(r, h / 2, h / 2)
        tx = cx + pad
        if hexed:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tint)
            p.drawEllipse(QPointF(tx + dot / 2, r.center().y()), dot / 2, dot / 2)
            tx += dot + 6 * u
        p.setPen(color('subtext') if plain_tag or hexed else tint)
        p.drawText(QRectF(tx, y, w, h), int(Qt.AlignmentFlag.AlignVCenter), text)
        cx += w + gap
    p.restore()
    return cx - gap if sized else x, h


class TagStrip(QWidget):
    """A line of soft tags as a widget (the game rail's facts, w-145),
    painted by paint_tags at the Ribbon's sizes."""

    def __init__(self, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.tags: tuple = ()
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(int(tag_h(self._font(), self._u())) + 2)

    def _u(self) -> float:
        return 1.2

    def _font(self) -> QFont:
        return font('sm', self.scale)

    def set_tags(self, tags):
        self.tags = tuple(tags or ())
        self.setAccessibleName(", ".join(t for t, _r in self.tags))
        self.update()

    def paintEvent(self, ev):
        if not self.tags:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_tags(p, self.tags, 1.0, 1.0, self._u(), self._font(), self.width() - 2.0)
        p.end()


# What A does on the focused item, as a pill: only Play and Resume earn one,
# the hint bar says the rest.
PILLS = {'play': "Play", 'resume': "Resume"}

# w-145 (Ribbon refine), sizes at u = 1: the focused category's underline,
# the position rail (track width, thumb length, gap to the art), the
# progress bar, and the dark wash behind the column (its alpha on dark and
# light themes, where it starts as shares of the height and the width, and
# the share of its width that stays solid).
UNDERLINE = (28, 3)
RAIL = dict(w=3, thumb=44, gap=30)
PROGRESS = dict(w=250, h=6)
WASH = dict(dark=0.70, light=0.50, top=0.43, width=0.57, solid=0.55)
TITLE_ZOOM = 1.25                                # the focused title over the headline size


class RibbonView(QWidget):
    """The Ribbon: Left/Right move the category (stopping at the ends),
    Up/Down the item (each category remembers its own), A activates the
    item, X asks for its options. A move slides the content over
    MOVE_MS (none without motion); a held key just keeps retargeting. A
    slide's frames repaint only the bar (a category move) or the column's
    strip (an item move); the widgets under those rects repaint with them.
    It draws its own focus (OWN_FOCUS_RING: the window's ring skips it).
    Art comes from ART by key at paint time; nothing is kept here."""
    NATIVE_ARROWS = 'all'
    OWN_FOCUS_RING = True
    activated = pyqtSignal(object)               # Item: A, Enter or a click on the focused one
    options = pyqtSignal(object)                 # Item: X
    moved = pyqtSignal(int)                      # a move happened: +1 right/down, -1 left/up
    category_changed = pyqtSignal(str)
    stepped = pyqtSignal(object, int)            # Item, -1 / +1: Left/Right in a settings group (w-145)
    MOVE_MS = 166
    PAINT_BEFORE, PAINT_AFTER = 2, 7             # items painted around the focused one
    PREFETCH = 3                                 # art decoded ahead either side

    def __init__(self, scale: float = 1.0):
        super().__init__()
        self.setObjectName("Ribbon")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scale = scale
        self.cats: list = []
        self.state = RibbonState()
        self.help: dict = {}                     # setting key -> its help line
        self.art_for = lambda slug: (None, None)     # the game's wide art (path, url)
        self._asked: set = set()                 # art keys asked for (decoding)
        self._bad: set = set()                   # art keys that didn't decode: not asked again
        self._fetching: set = set()
        self._texts: dict = {}                   # (text, width) -> QStaticText
        self._hits: list = []                    # (rect, 'cat'|'item', index), from the last paint
        self._off = QPointF()                    # the slide still to go
        self._col_x: tuple | None = None         # the column's painted x range (last paint)
        self._folder_slide = False               # the slide in progress moves the column, not the bar
        self.last_move = 'item'                  # item, folder or category (the last move's sound)
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._anim.valueChanged.connect(self._slide)

    # ---- data ----
    def set_categories(self, cats: list):
        """New categories; the category, its open folders and each column's
        focused item stay (by key), falling back to Home when a category
        went away."""
        keys = {c.key: c.items[self.state.item_index(c)].key for c in self.cats if c.items}
        inner = {}                               # the open folders' columns: id -> focused key
        for cid, items in columns(self.state, self.cats)[1:]:
            if items:
                inner[cid] = items[clamp(self.state.focus.get(cid, 0), len(items))].key
        old_cat = self.state.cat
        focus = {}
        for c in cats:
            i = find(cats, c.key, keys.get(c.key))
            focus[c.key] = i if i is not None else \
                clamp(self.state.focus.get(c.key, 0), len(c.items))
        cat = old_cat if any(c.key == old_cat for c in cats) else 'home'
        state = resolve(RibbonState(cat, focus, self.state.path if cat == old_cat else ()), cats)
        for cid, items in columns(state, cats)[1:]:
            j = next((k for k, it in enumerate(items) if it.key == inner.get(cid)), None)
            focus[cid] = j if j is not None else clamp(self.state.focus.get(cid, 0), len(items))
        self.cats = cats
        self.state = state
        self._texts.clear()
        self._sync_accessible()
        self._prefetch()
        self.update()
        if cat != old_cat:
            self.category_changed.emit(cat)

    def select(self, cat_key: str, item_key: str | None = None):
        """Go to a category (and an item in it: a key, or 'game:<slug>'),
        without sliding."""
        if not any(c.key == cat_key for c in self.cats):
            return
        focus = dict(self.state.focus)
        i = find(self.cats, cat_key, item_key)
        if i is not None:
            focus[cat_key] = i
        changed = cat_key != self.state.cat
        self.state = RibbonState(cat_key, focus)         # folders closed
        if changed:
            self.last_move = 'category'
        self._anim.stop()
        self._off = QPointF()
        self._sync_accessible()
        self._prefetch()
        self.update()
        if changed:
            self.category_changed.emit(cat_key)

    def category(self):
        return self.cats[self.state.index(self.cats)] if self.cats else None

    def current(self):
        """The focused item (None before any categories)."""
        return current(self.state, self.cats)

    # ---- moving ----
    def move(self, action: str) -> bool:
        """left, right, up, down, next, prev, home, in or out
        (ribbon_model.nav). False when nothing moved (an end)."""
        if not self.cats:
            return False
        old = resolve(self.state, self.cats)
        if action in ('left', 'right') and steps_in_place(old, self.cats):
            it = self.current()                  # a settings group: the value, in place
            if it is not None and it.kind == 'setting':
                self.stepped.emit(it, 1 if action == 'right' else -1)
            return False
        new = nav(old, self.cats, action)
        i0, i1 = old.index(self.cats), new.index(self.cats)
        (cid0, items0), (cid1, items1) = column(old, self.cats), column(new, self.cats)
        j0 = clamp(old.focus.get(cid0, 0), len(items0))
        j1 = clamp(new.focus.get(cid1, 0), len(items1))
        if (i0, cid0, j0) == (i1, cid1, j1):
            return False
        self.state = new
        gm = geometry(self.width(), self.height(), self.scale)
        off = QPointF(self._off)
        folder = i0 == i1 and cid0 != cid1
        if i0 != i1:
            off.setX(off.x() + (i1 - i0) * gm['gap'])
            off.setY(0)
            self.last_move = 'category'
        elif folder:                             # in: the new column comes from the right
            deeper = len(new.path) > len(old.path)
            off = QPointF(gm['gap'] * (1 if deeper else -1) * 1.2, 0)
            self.last_move = 'folder'
        else:
            off.setY(off.y() + (j1 - j0) * (gm['art_off'][1] + gm['step']))
            self.last_move = 'item'
        self._folder_slide = folder
        self._start_slide(off)
        self._sync_accessible()
        self._prefetch()
        self.update()
        down = i1 > i0 or (i0 == i1 and (len(new.path) > len(old.path) if folder else j1 > j0))
        self.moved.emit(1 if down else -1)
        if i0 != i1:
            self.category_changed.emit(new.cat)
        return True

    def enter(self) -> bool:
        """A on a folder: open it (its own column, the focus on its first
        or on where you left it)."""
        return self.move('in')

    def leave(self) -> bool:
        """B inside a folder: up one."""
        return self.move('out')

    def depth(self) -> int:
        return len(resolve(self.state, self.cats).path)

    def cycle(self, d: int) -> bool:
        """LB / RB: the next or previous category, wrapping round."""
        return self.move('next' if d > 0 else 'prev')

    def _start_slide(self, off: QPointF):
        self._anim.stop()
        ms = anim_ms(self.MOVE_MS)
        if ms <= 0:
            self._off = QPointF()
            return
        self._off = off
        self._anim.setDuration(ms)
        self._anim.setStartValue(off)
        self._anim.setEndValue(QPointF())
        self._anim.start()

    def _slide(self, v):
        """One frame of a slide: repaint only what moves, the bar during a
        category move, the column during an item move."""
        old, self._off = self._off, QPointF(v)
        if self._folder_slide:
            self.update(self._col_rect())
            return
        if self._off.x() != old.x():
            self.update(self._bar_rect())
        if self._off.y() != old.y():
            self.update(self._col_rect())

    def _bar_rect(self) -> QRect:
        """The band the category bar and its label take."""
        gm = geometry(self.width(), self.height(), self.scale)
        top = gm['by'] - gm['icon_on'] - 8 * gm['u']
        return QRect(0, int(top), self.width(), int(gm['col_top'] - top))

    def _col_rect(self) -> QRect:
        """The column's x range from the last paint, full height (items
        slide in and out at the top and the bottom)."""
        if self._col_x is None or self._folder_slide:
            return self.rect()
        x0, x1 = self._col_x
        return QRect(int(x0) - 1, 0, int(x1 - x0) + 2, self.height())

    def sliding(self) -> bool:
        return self._anim.state() == QVariantAnimation.State.Running

    def _sync_accessible(self):
        """Screen readers get the item: "Library: Hades" (Qt announces a
        name change itself)."""
        c, it = self.category(), self.current()
        if c is None or it is None:
            return
        where = " › ".join([c.title] + [f.title for f in crumbs(self.state, self.cats)])
        self.setAccessibleName(f"{where}: {it.title}")
        self.setAccessibleDescription(it.value_text if it.kind == 'setting' else plain(it.sub))

    # ---- input ----
    ARROWS = {Qt.Key.Key_Left: 'left', Qt.Key.Key_Right: 'right', Qt.Key.Key_Up: 'up',
              Qt.Key.Key_Down: 'down'}

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in self.ARROWS:
            self.move(self.ARROWS[k])
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            it = self.current()
            if it is not None:
                self.activated.emit(it)
        elif k == Qt.Key.Key_Menu:
            it = self.current()
            if it is not None:
                self.options.emit(it)
        elif k in (Qt.Key.Key_PageDown, Qt.Key.Key_BracketRight):
            self.cycle(1)                        # the keyboard's RB (Right opens a folder)
        elif k in (Qt.Key.Key_PageUp, Qt.Key.Key_BracketLeft):
            self.cycle(-1)
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(ev)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = ev.position()
        hit = next(((kind, i) for r, kind, i in self._hits if r.contains(pos)), None)
        if hit is None:
            return
        kind, i = hit
        if kind not in ('cat', 'label', 'item'):
            return                               # the rail, a progress bar: nothing to press
        if kind in ('cat', 'label'):
            cur = self.state.index(self.cats)
            if i != cur:
                self.select(self.cats[i].key)
                self.moved.emit(1 if i > cur else -1)
            return
        cid, items = column(self.state, self.cats)
        fi = clamp(self.state.focus.get(cid, 0), len(items))
        if i == fi:
            self.activated.emit(items[i])
        else:
            focus = {**self.state.focus, cid: i}
            self.state = RibbonState(self.state.cat, focus, self.state.path)
            self.last_move = 'item'
            self._sync_accessible()
            self._prefetch()
            self.update()
            self.moved.emit(1 if i > fi else -1)

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            self.move('up' if dy > 0 else 'down')

    # ---- art ----
    def _art_key(self, slug: str, w: int, h: int):
        """The ART key of the game's art at w x h (asking for it as needed);
        None while there is no file (a download is asked for)."""
        path, url = self.art_for(slug)
        if not path:
            if url and url not in self._fetching:
                self._fetching.add(url)
                ART.fetch(url, self._art_arrived)
            return None
        return ART.key(path, w, h)

    def _art_arrived(self, _path):
        self._fetching.clear()
        self.update()

    def _pixmap(self, slug: str, w: int, h: int):
        key = self._art_key(slug, w, h)
        if key is None or key in self._bad:
            return None                          # the glyph tile instead
        pm = ART.get(key)
        if pm is None:
            self._ask(key)
        return pm

    def _ask(self, key):
        """Decode `key` on the thread pool, once at a time; never again
        once it failed (a new file has a new key: its mtime is in it)."""
        if key not in self._asked and key not in self._bad:
            self._asked.add(key)
            ART.request_key(key, lambda pm, k=key: self._art_decoded(k, pm))

    def _art_decoded(self, key, pm):
        self._asked.discard(key)                 # a dropped one is asked again when painted
        if pm is None:
            self._bad.add(key)
        self.update()

    def _prefetch(self):
        """Decode the art either side of the focus ahead (while it fits the
        cache's budget), so Up/Down never shows an empty frame."""
        if not self.cats or not self.isVisible():
            return
        gm = geometry(self.width(), self.height(), self.scale)
        cid, items = column(self.state, self.cats)
        fi = clamp(self.state.focus.get(cid, 0), len(items))
        for it in items[max(0, fi - self.PREFETCH):fi + self.PREFETCH + 1]:
            if it.kind == 'game' and it.slug and ART.has_room():
                key = self._art_key(it.slug, *gm['art_on'])
                if key is not None and ART.get(key) is None:
                    self._ask(key)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._prefetch()

    # ---- painting ----
    def paintEvent(self, ev):
        if not self.cats:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        gm = geometry(self.width(), self.height(), self.scale)
        self._hits = []
        self._wash(p)
        self._bar(p, gm)
        if steps_in_place(self.state, self.cats):
            self._settings_group(p, gm)
        else:
            self._column(p, gm)
        p.end()

    def _wash(self, p):
        """A dark wash behind the column (w-145), so the ribbon and the art
        panel never fight the titles: the page colour, solid over the first
        part of its width, then fading out to the right."""
        w, h = self.width(), self.height()
        r = QRectF(0, h * WASH['top'], w * WASH['width'], h * (1 - WASH['top']))
        a = WASH['light'] if LOOK['light'] else WASH['dark']
        g = QLinearGradient(r.left(), 0.0, r.right(), 0.0)
        g.setColorAt(0.0, color('bg', a))
        g.setColorAt(WASH['solid'], color('bg', a))
        g.setColorAt(1.0, color('bg', 0.0))
        p.fillRect(r, g)

    def _glyph(self, p, key, cx, cy, px, c):
        p.setFont(glyph_font(px))
        p.setPen(c)
        p.drawText(QRectF(cx - px, cy - px, 2 * px, 2 * px), Qt.AlignmentFlag.AlignCenter,
                   G.get(key, G['library']))

    def _text(self, p, r: QRectF, s: str, f: QFont, c: QColor,
              flags=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) -> float:
        """Draw `s` elided to `r`; returns the x where it ends."""
        p.setFont(f)
        p.setPen(c)
        fm = QFontMetrics(f)
        text = fm.elidedText(s, Qt.TextElideMode.ElideRight, max(0, int(r.width())))
        p.drawText(r, flags, text)
        return r.x() + min(r.width(), fm.horizontalAdvance(text))

    def _span(self, x0: float, x1: float):
        """Widen the column's painted x range (for _col_rect)."""
        a, b = self._col_x or (x0, x1)
        self._col_x = (min(a, x0), max(b, x1))

    def _bar(self, p, gm):
        """The categories: an icon each with its quiet name under it; the
        focused one bigger, its name large with an accent underline and its
        tags (w-145)."""
        u, by = gm['u'], gm['by']
        ax = gm['ax'] + (0.0 if self._folder_slide else self._off.x())
        cur = self.state.index(self.cats)
        quiet = font('sm', self.scale)
        qh = QFontMetrics(quiet).height()
        for i, c in enumerate(self.cats):
            x = ax + (i - cur) * gm['gap']
            if x < -gm['gap'] or x > self.width() + gm['gap']:
                continue
            on = i == cur
            px = gm['icon_on'] if on else gm['icon_off']
            self._glyph(p, c.glyph, x, by, px, color('text') if on else color('muted', 0.75))
            self._hits.append((QRectF(x - gm['gap'] / 2, by - px, gm['gap'], 2 * px), 'cat', i))
            if c.live:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color('success'))
                p.drawEllipse(QPointF(x + px * 0.42, by - px * 0.42), 6 * u, 6 * u)
            if not on:
                lw = gm['gap'] - 16 * u
                lr = QRectF(x - lw / 2, by + px * 0.5 + 6 * u, lw, qh)
                p.save()
                p.setOpacity(0.8)
                self._text(p, lr, c.title, quiet, color('muted'), Qt.AlignmentFlag.AlignCenter)
                p.restore()
                self._hits.append((lr, 'label', i))
                continue
            f, fs = font('display', self.scale, QFont.Weight.DemiBold), font('sm', self.scale)
            lh, sh = QFontMetrics(f).height(), QFontMetrics(fs).height()
            ly = by + px * 0.5 + 8 * u
            half = min(300 * u, max(gm['gap'], x - 8))
            open_f = crumbs(self.state, self.cats)
            title = " › ".join([c.title] + [f.title for f in open_f])
            sub = open_f[-1].sub if open_f else c.sub
            tags = open_f[-1].tags if open_f else c.tags
            lr = QRectF(x - half, ly, 2 * half, lh)
            self._text(p, lr, title, f, color('text'), Qt.AlignmentFlag.AlignCenter)
            self._hits.append((QRectF(x - gm['gap'] / 2, ly, gm['gap'], lh), 'label', i))
            uy = ly + lh + 2 * u
            self._underline(p, QRectF(x - UNDERLINE[0] * u / 2, uy, UNDERLINE[0] * u,
                                      max(2.0, UNDERLINE[1] * u)), u)
            ty = uy + UNDERLINE[1] * u + 12 * u
            if tags:
                self._tags(p, tags, x, ty, u, fs, 2 * half, center=True)
            elif sub:
                self._text(p, QRectF(x - half, ty, 2 * half, sh), sub, fs,
                           color('muted'), Qt.AlignmentFlag.AlignCenter)

    def _underline(self, p, r: QRectF, u: float):
        """The focused category's short accent line; a soft glow round it
        unless Glow is off."""
        p.save()
        if LOOK['glow'] > 0:
            g = color('accent')
            p.setBrush(Qt.BrushStyle.NoBrush)
            for s in range(4, 0, -1):
                g.setAlphaF(min(1.0, 0.06 * (5 - s) * LOOK['glow']))
                p.setPen(QPen(g, s * 3 * u))
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color('accent'))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.restore()

    def _art_rect(self, it, on: bool, gm: dict, y: float) -> QRectF:
        if it.kind == 'game' and it.slug:
            w, h = gm['art_on'] if on else gm['art_off']
        else:
            w = h = gm['glyph_on'] if on else gm['glyph_off']
        dx = self._off.x() if self._folder_slide else 0.0
        return QRectF(gm['ax'] + dx - w / 2, y, w, h)

    def _column(self, p, gm):
        cid, items = column(self.state, self.cats)
        if not items:
            return
        fi = clamp(self.state.focus.get(cid, 0), len(items))
        u, dy, ht = gm['u'], self._off.y(), self.height()
        self._col_x = None
        self._rail(p, gm, fi, len(items))
        # Above the bar: up to two of the items you came from, dimmer further up.
        y = gm['by'] - gm['icon_on'] * 0.75 - 24 * u + dy
        for k, i in enumerate(range(fi - 1, max(-1, fi - 1 - self.PAINT_BEFORE), -1)):
            r = self._art_rect(items[i], False, gm, 0)
            r.moveBottom(y)
            if r.top() < 0:
                break
            self._item(p, items[i], r, False, gm, 0.55 - 0.2 * k)
            self._hits.append((QRectF(r.left(), r.top(), self.width() - r.left(), r.height()),
                               'item', i))
            y = r.top() - gm['step']
        y = gm['col_top'] + dy
        for i in range(fi, min(len(items), fi + self.PAINT_AFTER + 1)):
            on = i == fi
            r = self._art_rect(items[i], on, gm, y)
            if r.top() > ht:
                break
            fade = 1.0 if on else max(0.25, 1.0 - max(0.0, r.bottom() - ht * 0.8) / (ht * 0.2))
            bottom = self._item(p, items[i], r, on, gm, fade * (1.0 if on else 0.85))
            self._hits.append((QRectF(r.left(), r.top(), self.width() - r.left(), r.height()),
                               'item', i))
            # A small window: the focused text can run below its art.
            y = max(r.bottom(), bottom) + (gm['focus_gap'] if on else gm['step'])

    # ---- a settings group (w-145) ----
    SWITCH_CHOICES = ((False, "Off"), (True, "On"))

    def _settings_group(self, p, gm: dict):
        """Settings with a group open: the group's tile on the left, a thin
        accent rule, then its settings as rows. The focused row is a pill
        with its value between ‹ › (a switch for an on/off one), its help
        line and a dot per choice; the others fade with their distance."""
        cid, items = column(self.state, self.cats)
        if not items:
            return
        folder = crumbs(self.state, self.cats)[-1]
        fi = clamp(self.state.focus.get(cid, 0), len(items))
        u, top, ht = gm['u'], gm['col_top'], self.height()
        self._col_x = None
        # The group's tile, slid to the left.
        x0, tile = 64 * u, 120 * u
        r = QRectF(x0 + 50 * u, top, tile, tile)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glass())
        p.drawRoundedRect(r, R['card'], R['card'])
        p.restore()
        self._glyph(p, folder.glyph or 'settings', r.center().x(), r.center().y(), tile * 0.32,
                    color('text'))
        tf, sf = font('lg', self.scale, QFont.Weight.DemiBold), font('sm', self.scale)
        th = QFontMetrics(tf).height()
        tw = 220 * u
        self._text(p, QRectF(x0, r.bottom() + 10 * u, tw, th), folder.title, tf, color('text'),
                   Qt.AlignmentFlag.AlignCenter)
        self._text(p, QRectF(x0, r.bottom() + 10 * u + th, tw, QFontMetrics(sf).height()),
                   folder.sub, sf, color('muted'), Qt.AlignmentFlag.AlignCenter)
        self._span(x0, x0 + tw)
        # The rule between the group and its settings.
        rx = x0 + tw + 30 * u
        g = QLinearGradient(0.0, top - 20 * u, 0.0, ht - 40 * u)
        g.setColorAt(0.0, color('accent', 0.0))
        g.setColorAt(0.2, color('accent', 0.35))
        g.setColorAt(0.7, color('accent', 0.35))
        g.setColorAt(1.0, color('accent', 0.0))
        p.fillRect(QRectF(rx, top - 20 * u, max(1.5, 2 * u), ht - top - 20 * u), g)
        # The rows.
        lx = rx + 40 * u
        rw = max(200.0, min(self.width() - lx - 80 * u, 1080 * u))
        self._span(rx - 2, lx + rw + 30 * u)
        row_h, pill_h = 56 * u, 80 * u
        y = top + self._off.y() - 8 * u
        for i in range(max(0, fi - 2), len(items)):
            it = items[i]
            if y > ht:
                break
            alpha = max(0.35, 1.0 - 0.14 * abs(i - fi))
            if it.primary == 'none':
                alpha *= 0.5
            if i == fi:
                rr = QRectF(lx - 32 * u, y, rw + 64 * u, pill_h)
                bottom = self._setting_focused(p, it, rr, u, alpha)
                self._hits.append((rr, 'item', i))
                y = bottom + 16 * u
            else:
                rr = QRectF(lx, y, rw, row_h)
                self._setting_row(p, it, rr, u, alpha)
                self._hits.append((rr, 'item', i))
                y += row_h + 12 * u

    def _switch(self, p, at: QPointF, on: bool, u: float):
        """An on/off setting's switch, its right edge at `at`."""
        w, h = 56 * u, 32 * u
        r = QRectF(at.x() - w, at.y() - h / 2, w, h)
        p.save()
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color('accent'))
        else:
            p.setPen(QPen(color('overlay'), 2 * u))
            p.setBrush(glass('glass'))
        p.drawRoundedRect(r, h / 2, h / 2)
        d = (24 if on else 20) * u
        cx = r.right() - h / 2 if on else r.left() + h / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color('on_accent' if on else 'muted'))
        p.drawEllipse(QPointF(cx, r.center().y()), d / 2, d / 2)
        p.restore()
        return r.left()

    def _setting_row(self, p, it, r: QRectF, u: float, alpha: float):
        p.save()
        p.setOpacity(p.opacity() * alpha)
        f = font('lg', self.scale)
        if it.choices == self.SWITCH_CHOICES:
            left = self._switch(p, QPointF(r.right(), r.center().y()), bool(it.value), u)
        else:
            vw = min(r.width() * 0.45, QFontMetrics(f).horizontalAdvance(it.value_text) + 4)
            left = r.right() - vw
            self._text(p, QRectF(left, r.y(), vw, r.height()), it.value_text, f,
                       color('muted'), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._text(p, QRectF(r.x(), r.y(), max(0.0, left - r.x() - 24 * u), r.height()),
                   it.title, f, color('subtext'))
        p.restore()

    def _setting_focused(self, p, it, r: QRectF, u: float, alpha: float) -> float:
        """The focused setting: a pill, its help line, a dot per choice.
        Returns where it ends."""
        p.save()
        p.setOpacity(p.opacity() * alpha)
        if LOOK['glow'] > 0:
            g = color('accent')
            p.setBrush(Qt.BrushStyle.NoBrush)
            for s in range(5, 0, -1):
                g.setAlphaF(min(1.0, 0.03 * (6 - s) * LOOK['glow']))
                p.setPen(QPen(g, s * 3 * u))
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setBrush(glass())
        p.setPen(QPen(color('accent' if it.primary == 'step' else 'muted'), 2 * u))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        pad = 32 * u
        inner = r.adjusted(pad, 0, -pad, 0)
        if it.choices == self.SWITCH_CHOICES:
            left = self._switch(p, QPointF(inner.right(), inner.center().y()), bool(it.value), u)
        else:
            vf = font('lg', self.scale, QFont.Weight.DemiBold)
            fm = QFontMetrics(vf)
            arrows = it.primary == 'step'
            gap = 14 * u
            aw = fm.horizontalAdvance("‹") if arrows else 0
            text_w = min(inner.width() * 0.5, fm.horizontalAdvance(it.value_text) + 4)
            x = inner.right() - text_w - (aw + gap if arrows else 0)
            left = x - (aw + gap if arrows else 0)
            tint = color('accent' if arrows else 'muted')
            if arrows:
                faint = QColor(tint)
                faint.setAlphaF(0.7)
                self._text(p, QRectF(left, inner.y(), aw, inner.height()), "‹", vf, faint)
                self._text(p, QRectF(x + text_w + gap, inner.y(), aw, inner.height()), "›", vf,
                           faint)
            self._text(p, QRectF(x, inner.y(), text_w, inner.height()), it.value_text, vf, tint,
                       Qt.AlignmentFlag.AlignCenter)
        hf = font('display', self.scale, QFont.Weight.DemiBold)
        self._text(p, QRectF(inner.x(), inner.y(), max(0.0, left - inner.x() - 24 * u),
                             inner.height()), it.title, hf, color('text'))
        p.restore()
        # The help line under the pill, and the choices as dots at its right.
        sf = font('sm', self.scale)
        sh = QFontMetrics(sf).height()
        y = r.bottom() + 10 * u
        help_text = self.help.get(it.key[4:], '')
        n = len(it.choices)
        dots_w = (28 + 18 * (n - 1)) * u if 1 < n <= 12 else 0
        if help_text:
            self._text(p, QRectF(r.x() + pad, y, r.width() - 2 * pad - dots_w - 20 * u, sh),
                       help_text, sf, color('muted'))
        if dots_w:
            values = [c[0] for c in it.choices]
            cur = values.index(it.value) if it.value in values else 0
            x = r.right() - pad - dots_w
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            for k in range(n):
                w = (28 if k == cur else 10) * u
                p.setBrush(color('accent' if k == cur else 'overlay'))
                p.drawRoundedRect(QRectF(x, y + sh / 2 - 3 * u, w, 6 * u), 3 * u, 3 * u)
                x += w + 8 * u
            p.restore()
        return y + sh

    def _rail(self, p, gm: dict, i: int, n: int):
        """Your place in a long column (w-145): a thin track left of the
        art and a short accent thumb at the focused item's share of it. It
        sits inside the column's painted range, so a slide repaints it."""
        at = rail_pos(i, n)
        if at is None:
            return
        u = gm['u']
        w = max(2.0, RAIL['w'] * u)
        x = gm['ax'] - gm['art_on'][0] / 2 - RAIL['gap'] * u - w
        top = gm['col_top'] - 6 * u
        track = QRectF(x, top, w, max(0.0, self.height() - top - 60 * u))
        thumb_h = min(track.height(), RAIL['thumb'] * u)
        thumb = QRectF(x, top + at * (track.height() - thumb_h), w, thumb_h)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color('glass_hi'))
        p.drawRoundedRect(track, w / 2, w / 2)
        p.setBrush(color('accent'))
        p.drawRoundedRect(thumb, w / 2, w / 2)
        p.restore()
        self._span(x - 2, x + w + 2)
        self._hits.append((track, 'rail', -1))

    def _item(self, p, it, r: QRectF, on: bool, gm: dict, alpha: float) -> float:
        """One item; returns where its text ends (it can pass the art)."""
        u = gm['u']
        bottom = r.bottom()
        glow = 20 * u                            # the focus glow reaches this far out
        self._span(r.left() - glow, r.right() + glow)
        p.save()
        p.setOpacity(p.opacity() * alpha)
        rad = R['tile'] * (1.0 if on else 0.8)
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        if on and LOOK['glow'] > 0:
            g = QColor(P['accent'])
            p.setBrush(Qt.BrushStyle.NoBrush)
            for s in range(6, 0, -1):
                g.setAlphaF(min(1.0, 0.03 * (7 - s) * LOOK['glow']))
                p.setPen(QPen(g, s * 3 * u))
                p.drawRoundedRect(r.adjusted(-2, -2, 2, 2), rad + 2, rad + 2)
        art_w, art_h = gm['art_on']
        pm = self._pixmap(it.slug, art_w, art_h) if it.kind == 'game' and it.slug else None
        if pm is not None and not pm.isNull():
            p.save()
            p.setClipPath(path)
            p.drawPixmap(r, pm, QRectF(pm.rect()))
            p.restore()
        else:                                    # no art (yet): a tile with a glyph
            p.fillPath(path, color('surface_2', 0.9 if on else 0.6))
            self._glyph(p, it.glyph or 'library', r.center().x(), r.center().y(),
                        r.height() * (0.40 if it.kind != 'game' else 0.30),
                        color('text') if on else color('subtext', 0.85))
        if on:
            p.setPen(QPen(color('accent'), 3 * u))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        tx = r.right() + (36 if on else 24) * u
        tw = self.width() - tx - 40 * u
        if on:
            bottom = self._focused_text(p, it, r, tx, tw, u)
        else:
            base = font('base', self.scale)
            end = self._text(p, QRectF(tx, r.y(), tw, r.height()), it.title, base,
                             color('subtext'))
            if it.kind in ('setting', 'download'):
                vx = tx + min(tw * 0.6, QFontMetrics(base).horizontalAdvance(it.title) + 24 * u)
                end = self._text(p, QRectF(vx, r.y(), tx + tw - vx, r.height()),
                                 "·  " + (it.value_text if it.kind == 'setting' else it.figure),
                                 base, color('muted'))
            elif it.kind == 'folder':
                vx = tx + min(tw * 0.6, QFontMetrics(base).horizontalAdvance(it.title) + 24 * u)
                if it.tags:
                    sm = font('sm', self.scale)
                    th = self._tag_h(sm, u)
                    end, _ = self._tags(p, it.tags, vx, r.center().y() - th / 2, u, sm,
                                        tx + tw - vx, pad=10.0)
                else:
                    end = self._text(p, QRectF(vx, r.y(), tx + tw - vx, r.height()),
                                     "·  " + it.sub, base, color('muted'))
            self._span(tx, end + 2)
        p.restore()
        return bottom

    def _focused_text(self, p, it, r: QRectF, tx: float, tw: float, u: float) -> float:
        """Kicker, title, then the facts and the Play pill, or a setting's
        value and its help. Returns where it ends."""
        hf = font('headline', self.scale, QFont.Weight.DemiBold)
        if it.kind == 'game':                    # a game's name a size up (w-145)
            hf.setPixelSize(int(hf.pixelSize() * TITLE_ZOOM))
        sf = font('base', self.scale)
        hh, sh = QFontMetrics(hf).height(), QFontMetrics(sf).height()
        pf = font('sm', self.scale)
        prog_h = QFontMetrics(pf).height() + 14 * u if it.progress else 0
        pill = 46 * u if it.primary in PILLS else 0
        tf = self._track_font(u) if it.track else None
        track_h = 0.0
        if tf is not None:
            track_h = 10 * u + 8 * u + QFontMetrics(tf).height() + 16 * u
        tag_f = font('sm', self.scale, QFont.Weight.DemiBold)
        line_h = self._tag_h(tag_f, u) + 2 * u if it.tags else sh
        block = (26 * u if it.kicker else 0) + hh + 6 * u + line_h \
            + (16 * u + pill if pill else 0) + track_h + prog_h
        y = r.center().y() - block / 2 if r.height() > block else r.y()
        ends = [tx]
        if it.kicker:
            kf = font('xs', self.scale, QFont.Weight.DemiBold)
            kf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
            ends.append(self._text(p, QRectF(tx, y, tw, 22 * u), it.kicker, kf,
                                   color('success' if it.primary == 'resume' else 'accent')))
            y += 26 * u
        ends.append(self._text(p, QRectF(tx, y, tw, hh), it.title, hf, color('text')))
        y += hh + 6 * u
        if it.kind == 'setting':
            ends.append(self._value(p, it, QPointF(tx, y), u))
            y += 46 * u + 12 * u
            ends.append(self._text(p, QRectF(tx, y, tw, sh), self.help.get(it.key[4:], ''),
                                   font('sm', self.scale), color('muted')))
            self._span(tx, max(ends) + 2)
            return y + sh
        if it.tags:
            right, th = self._tags(p, it.tags, tx, y + 2 * u, u, tag_f, tw)
            ends.append(right)
            y += th + 2 * u + 16 * u
            if it.progress:
                y = self._progress(p, it.progress, tx, y - 6 * u, tw, u, pf, ends)
        elif it.sub:
            st = self._texts.get((it.sub, int(tw)))
            if st is None:
                st = QStaticText(it.sub)
                st.setTextFormat(Qt.TextFormat.RichText)
                st.setTextWidth(max(1.0, tw))
                self._texts[(it.sub, int(tw))] = st
            p.setFont(sf)
            p.setPen(color('subtext'))
            p.drawStaticText(QPointF(tx, y), st)
            ends.append(tx + tw)                 # wrapped rich text: its whole width
            y += max(sh, st.size().height()) + 16 * u
            if it.progress:
                y = self._progress(p, it.progress, tx, y - 6 * u, tw, u, pf, ends)
        if tf is not None:
            y = self._download_track(p, it, tf, tx, y, tw, u, ends)
        elif it.kind == 'download' and it.figure:
            ends.append(self._text(p, QRectF(tx, y, tw, sh), it.figure,
                                   font('headline', self.scale, QFont.Weight.DemiBold),
                                   color('accent')))
            y += hh
        if pill:
            ends.append(self._pill(p, PILLS[it.primary], QPointF(tx, y), u))
            y += pill
        self._span(tx, max(ends) + 2)
        return y

    def _progress(self, p, progress: tuple, x: float, y: float, tw: float, u: float, f: QFont,
                  ends: list) -> float:
        """How far through the main story (w-145): a thin aurora bar and its
        words beside it ("25 h of about 55 h to finish"). Returns the next y."""
        frac, words = progress
        fh = QFontMetrics(f).height()
        bw = min(PROGRESS['w'] * u, tw * 0.45)
        bh = max(3.0, PROGRESS['h'] * u)
        track = QRectF(x, y + (fh - bh) / 2, bw, bh)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color('glass_hi'))
        p.drawRoundedRect(track, bh / 2, bh / 2)
        fill = QRectF(track.x(), track.y(), max(bh, bw * max(0.0, min(1.0, frac))), bh)
        g = QLinearGradient(fill.topLeft(), fill.topRight())
        g.setColorAt(0, QColor(GRAD['aurora'][0]))
        g.setColorAt(1, QColor(GRAD['aurora'][1]))
        p.setBrush(g)
        p.drawRoundedRect(fill, bh / 2, bh / 2)
        p.restore()
        tx = x + bw + 16 * u
        ends.append(self._text(p, QRectF(tx, y, max(0.0, x + tw - tx), fh), words, f,
                               color('muted')))
        self._hits.append((QRectF(x, y, ends[-1] - x, fh), 'progress', -1))
        return y + fh + 14 * u

    _tag_h = staticmethod(tag_h)
    _tags = staticmethod(paint_tags)

    def _track_font(self, u: float) -> QFont:
        f = QFont(FONTS.mono)
        f.setPixelSize(max(1, int(FS['xs'] * self.scale * LOOK['scale'])))
        f.setWeight(QFont.Weight.Medium)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        return f

    def _download_track(self, p, it, tf: QFont, tx: float, y: float, tw: float, u: float,
                        ends: list) -> float:
        """A download's four stages as the pipeline paints them (no
        shimmer), its one big number to the right. Returns the next y."""
        from .pipeline_view import paint_track, track_labels, track_segments   # (a cycle at import)
        bar_h, gap = 10.0 * u, 4.0 * u
        fig = font('headline', self.scale, QFont.Weight.DemiBold)
        fw = float(QFontMetrics(fig).horizontalAdvance(it.figure)) if it.figure else 0.0
        bw = max(120.0, min(560.0 * u, tw - fw - 28 * u))
        segs = track_segments(list(it.track), bw, gap)
        labels = track_labels(segs, bw, gap, tf)
        p.save()
        p.translate(tx, 0.0)
        paint_track(p, segs, labels, bar_h, tf, bar_h + 8.0 * u, False, 0.0, y)
        p.restore()
        if it.figure:
            ends.append(self._text(p, QRectF(tx + bw + 20 * u, y + bar_h / 2 - QFontMetrics(fig).height() / 2,
                                             fw + 4, QFontMetrics(fig).height()),
                                   it.figure, fig, color('accent')))
        ends.append(tx + bw)
        return y + bar_h + 8 * u + QFontMetrics(tf).height() + 16 * u

    def _pill(self, p, label: str, at: QPointF, u: float) -> float:
        """Play / Resume: the one action that earns a pill. Returns its right."""
        f = font('base', self.scale, QFont.Weight.DemiBold)
        r = QRectF(at.x(), at.y(), QFontMetrics(f).horizontalAdvance(label) + 92 * u, 46 * u)
        g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0, QColor(GRAD['dusk'][0]))
        g.setColorAt(1, QColor(GRAD['dusk'][1]))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        self._glyph(p, 'play', r.x() + 30 * u, r.center().y(), 20 * u, color('on_accent'))
        self._text(p, r.adjusted(54 * u, 0, 0, 0), label, f, color('on_accent'))
        return r.right()

    def _value(self, p, it, at: QPointF, u: float) -> float:
        """A setting's value as a capsule (A steps it). Returns its right."""
        f = font('base', self.scale, QFont.Weight.DemiBold)
        s = it.value_text
        r = QRectF(at.x(), at.y(), QFontMetrics(f).horizontalAdvance(s) + 40 * u, 46 * u)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color('surface_2'))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(QPen(color('accent' if it.primary == 'step' else 'muted'), 2 * u))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        self._text(p, r, s, f, color('text' if it.primary == 'step' else 'muted'),
                   Qt.AlignmentFlag.AlignCenter)
        return r.right()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import anim_ms, ART  # noqa: E402
