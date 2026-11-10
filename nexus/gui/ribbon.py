"""Theatre's Ribbon layout (w-108): categories on one line, the focused
category's items in a column under it, and the ribbon of light behind them.

`RibbonView` is one focusable widget that paints the bar and the column
itself and keeps every arrow key (NATIVE_ARROWS = 'all'): the content moves,
focus stays put. What it shows comes from ribbon_model. `paint_ribbon` is
the background's ribbon: an original sheet of thin curves (sine waves with
a drift), drawn additively on dark themes; GradientBackground paints it."""

import math
import re
from html import unescape

from PyQt6.QtCore import pyqtSignal, QEasingCurve, QPointF, QRect, QRectF, Qt, QVariantAnimation
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen, QStaticText)
from PyQt6.QtWidgets import QWidget

from .ribbon_model import clamp, current, find, nav, RibbonState
from .theme import FONTS, FS, G, GRAD, LOOK, P, R


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
    label = QFontMetrics(font('base', scale)).height() + QFontMetrics(font('sm', scale)).height()
    # The focused art never runs off the left edge (a narrow window).
    return dict(u=u, ax=max(int(214 * u), int(w * 0.17)), by=by, gap=int(210 * u),
                icon_on=int(72 * u), icon_off=int(46 * u),
                art_on=(int(380 * u), int(178 * u)), art_off=(int(200 * u), int(94 * u)),
                glyph_on=int(112 * u), glyph_off=int(64 * u),
                col_top=by + int(36 * u) + label + int(46 * u),
                step=int(18 * u), focus_gap=int(34 * u))


def color(key: str, alpha: float = 1.0) -> QColor:
    c = QColor(P[key])
    c.setAlphaF(c.alphaF() * alpha)
    return c


def plain(text: str) -> str:
    """Rich text as plain words (for the accessible description)."""
    return ' '.join(unescape(re.sub(r'<[^>]+>', '', text or '')).split())


# What A does on the focused item, as a pill: only Play and Resume earn one,
# the hint bar says the rest.
PILLS = {'play': "Play", 'resume': "Resume"}


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
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._anim.valueChanged.connect(self._slide)

    # ---- data ----
    def set_categories(self, cats: list):
        """New categories; the category and each one's focused item stay
        (by key), falling back to Home when a category went away."""
        keys = {c.key: c.items[self.state.item_index(c)].key for c in self.cats if c.items}
        old_cat = self.state.cat
        focus = {}
        for c in cats:
            i = find(cats, c.key, keys.get(c.key))
            focus[c.key] = i if i is not None else \
                clamp(self.state.focus.get(c.key, 0), len(c.items))
        cat = old_cat if any(c.key == old_cat for c in cats) else 'home'
        self.cats = cats
        self.state = RibbonState(cat, focus)
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
        self.state = RibbonState(cat_key, focus)
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
        """left, right, up, down, next, prev or home (ribbon_model.nav).
        False when nothing moved (an end)."""
        if not self.cats:
            return False
        old = self.state
        new = nav(old, self.cats, action)
        i0, i1 = old.index(self.cats), new.index(self.cats)
        c1 = self.cats[i1]
        j0 = old.item_index(c1) if i0 == i1 else None
        j1 = new.item_index(c1)
        if i0 == i1 and j0 == j1:
            return False
        self.state = new
        gm = geometry(self.width(), self.height(), self.scale)
        off = QPointF(self._off)
        if i0 != i1:
            off.setX(off.x() + (i1 - i0) * gm['gap'])
            off.setY(0)
        else:
            off.setY(off.y() + (j1 - j0) * (gm['art_off'][1] + gm['step']))
        self._start_slide(off)
        self._sync_accessible()
        self._prefetch()
        self.update()
        self.moved.emit(1 if (i1 > i0 or (i0 == i1 and j1 > j0)) else -1)
        if i0 != i1:
            self.category_changed.emit(new.cat)
        return True

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
        if self._col_x is None:
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
        self.setAccessibleName(f"{c.title}: {it.title}")
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
        if kind == 'cat':
            cur = self.state.index(self.cats)
            if i != cur:
                self.select(self.cats[i].key)
                self.moved.emit(1 if i > cur else -1)
            return
        c = self.category()
        fi = self.state.item_index(c)
        if i == fi:
            self.activated.emit(c.items[i])
        else:
            focus = {**self.state.focus, c.key: i}
            self.state = RibbonState(c.key, focus)
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
        c = self.category()
        if c is None or not self.isVisible():
            return
        gm = geometry(self.width(), self.height(), self.scale)
        fi = self.state.item_index(c)
        for it in c.items[max(0, fi - self.PREFETCH):fi + self.PREFETCH + 1]:
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
        self._bar(p, gm)
        self._column(p, gm)
        p.end()

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
        u, ax, by = gm['u'], gm['ax'] + self._off.x(), gm['by']
        cur = self.state.index(self.cats)
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
                continue
            f, fs = font('base', self.scale, QFont.Weight.Medium), font('sm', self.scale)
            lh, sh = QFontMetrics(f).height(), QFontMetrics(fs).height()
            ly = by + px * 0.5 + 8 * u
            half = min(300 * u, max(gm['gap'], x - 8))
            self._text(p, QRectF(x - half, ly, 2 * half, lh), c.title, f, color('text'),
                       Qt.AlignmentFlag.AlignCenter)
            if c.sub:
                self._text(p, QRectF(x - half, ly + lh, 2 * half, sh), c.sub, fs,
                           color('muted'), Qt.AlignmentFlag.AlignCenter)

    def _art_rect(self, it, on: bool, gm: dict, y: float) -> QRectF:
        if it.kind == 'game' and it.slug:
            w, h = gm['art_on'] if on else gm['art_off']
        else:
            w = h = gm['glyph_on'] if on else gm['glyph_off']
        return QRectF(gm['ax'] - w / 2, y, w, h)

    def _column(self, p, gm):
        c = self.category()
        items = c.items
        if not items:
            return
        fi = self.state.item_index(c)
        u, dy, ht = gm['u'], self._off.y(), self.height()
        self._col_x = None
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
            if it.kind == 'setting':
                vx = tx + min(tw * 0.6, QFontMetrics(base).horizontalAdvance(it.title) + 24 * u)
                end = self._text(p, QRectF(vx, r.y(), tx + tw - vx, r.height()),
                                 "·  " + it.value_text, base, color('muted'))
            self._span(tx, end + 2)
        p.restore()
        return bottom

    def _focused_text(self, p, it, r: QRectF, tx: float, tw: float, u: float) -> float:
        """Kicker, title, then the facts and the Play pill, or a setting's
        value and its help. Returns where it ends."""
        hf = font('headline', self.scale, QFont.Weight.DemiBold)
        sf = font('base', self.scale)
        hh, sh = QFontMetrics(hf).height(), QFontMetrics(sf).height()
        pill = 46 * u if it.primary in PILLS else 0
        block = (26 * u if it.kicker else 0) + hh + 6 * u + sh + (16 * u + pill if pill else 0)
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
        if it.sub:
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
        if pill:
            ends.append(self._pill(p, PILLS[it.primary], QPointF(tx, y), u))
            y += pill
        self._span(tx, max(ends) + 2)
        return y

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
