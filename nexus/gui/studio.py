"""Studio widgets: header, chip, card, timeline and game card (split out of
category.py, w-128)."""

import math

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from .. import categories, smart
from .art import screen_dpr
from .fmt import fmt_relative
from .theme import FS, is_light, LOOK, P, R, scaled, SP
from .tiles import GameTile
from .widgets import label
from .mosaic import _CachedLayer, _Mosaic, _played, _short, CoverWallHeader, studio_items, two_lines


class StudioHeader(CoverWallHeader):
    """A studio's header (w-114): its name big as a wordmark (no logos),
    "STUDIO · DEVELOPER", the studio's numbers (studio_items) and the same
    buttons, over three of its covers fanned out on the right in a gold
    glow. Painted once into the wall's cached pixmap like CoverWallHeader."""
    PICK = 3
    # Solid behind the text, then clear soon after: the fan stays crisp.
    FADE_STOPS = ((-1, 1.0), (0.0, 1.0), (0.12, 0.0), (1.0, 0.0))
    LOW_FADE = 0.90
    FAN_TILT = 9.0                  # degrees, the side covers
    FAN_SHIFT = 0.55                # the side covers' offset, in cover widths
    FAN_HALF = 1.15                 # the fan's half width, in cover widths
    FAN_OPACITY = 0.85
    TEXT_SHARES = (0.56, 0.68)      # compact: the numbers need the room, the fan narrows

    def __init__(self):
        super().__init__()
        self._studio: categories.StudioFacts | None = None
        self.setObjectName("StudioHeader")
        self.title.setObjectName("StudioWordmark")

    def set_category(self, cat: categories.Category, stats: categories.Stats | None = None,
                     lead: dict | None = None, studio: categories.StudioFacts | None = None):
        self._studio = studio
        super().set_category(cat, stats, lead)

    def _title_text(self, cat: categories.Category) -> str:
        return cat.title.upper()

    def _kind_text(self, cat: categories.Category) -> str:
        return f"STUDIO · {'DEVELOPER' if cat.kind == 'developer' else 'PUBLISHER'}"

    def _items(self, cat: categories.Category, st: categories.Stats) -> list[tuple[str, str]]:
        sf = self._studio
        if sf is None:
            n = len(cat.games)
            sf = categories.StudioFacts(n if cat.kind == 'developer' else 0,
                                        n if cat.kind == 'publisher' else 0)
        return studio_items(cat, st, sf)

    def _tint_role(self, cat: categories.Category | None = None) -> str:
        return 'col_gold'

    def fan_geometry(self, w: int, h: int) -> tuple[float, float, float, float]:
        """(cover width, cover height, centre x, centre y): as tall as 80 %
        of the header, narrower when the room right of the text is short,
        centred about three quarters across (never into the text)."""
        left = float(self._col_w + SP['4'])
        room = max(1.0, w - SP['6'] - left)
        cw = max(1.0, min(h * 0.80 / 1.5, room / (2 * self.FAN_HALF)))
        half = self.FAN_HALF * cw
        cx = min(w - SP['6'] - half, max(left + half, w * 0.76))
        return cw, cw * 1.5, cx, h / 2

    def _paint_wall(self, pt: QPainter, w: int, h: int):
        """Three covers fanned: the first upright on top, the second tilted
        left behind it, the third right."""
        cw, ch, cx, cy = self.fan_geometry(w, h)
        glow = QRadialGradient(QPointF(cx, cy), max(1.0, ch * 1.3))
        c0, c1 = QColor(P['col_gold']), QColor(P['col_gold'])
        c0.setAlphaF(0.12 if is_light() else 0.18)
        c1.setAlphaF(0.0)
        glow.setColorAt(0.0, c0)
        glow.setColorAt(1.0, c1)
        pt.fillRect(QRectF(0.0, 0.0, float(w), float(h)), glow)
        slugs = self._slugs[:3]
        place = {0: (0.0, 0.0), 1: (-self.FAN_SHIFT, -self.FAN_TILT),
                 2: (self.FAN_SHIFT, self.FAN_TILT)}
        if len(slugs) == 2:                             # a pair: centred, both leaning
            place = {0: (self.FAN_SHIFT / 2, self.FAN_TILT / 2),
                     1: (-self.FAN_SHIFT / 2, -self.FAN_TILT)}
        icw, ich = max(1, round(cw)), max(1, round(ch))
        shade = QColor(P['scrim'])
        shade.setAlphaF(0.45)
        for i in reversed(range(len(slugs))):           # the first painted last: on top
            dx, angle = place[i]
            pt.save()
            pt.setOpacity(1.0 if i == 0 else self.FAN_OPACITY)
            pt.translate(QPointF(cx + dx * cw, cy + (0.0 if i == 0 else ch * 0.04)))
            pt.rotate(float(angle))
            rect = QRectF(-cw / 2, -ch / 2, cw, ch)
            sp = QPainterPath()
            sp.addRoundedRect(rect.translated(0.0, float(SP['2'])), float(R['card']),
                              float(R['card']))
            pt.fillPath(sp, shade)
            cp = QPainterPath()
            cp.addRoundedRect(rect, float(R['card']), float(R['card']))
            pt.setClipPath(cp)
            pm = self._cover(slugs[i], icw, ich)
            pt.drawPixmap(rect, pm, QRectF(pm.rect()))
            pt.setClipping(False)
            pt.setPen(QPen(QColor(P['border']), 1.0))
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.drawPath(cp)
            pt.restore()


class _StudioButton(QPushButton, _Mosaic):
    """What the studio chip and card share: a key, `opened` on a click or
    Enter, a gold edge (the accent with focus)."""
    opened = pyqtSignal(str)

    def _open(self):
        self.opened.emit(self.key)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.opened.emit(self.key)          # Space already clicks a button
        else:
            super().keyPressEvent(ev)

    def _edge_pen(self) -> QPen:
        edge = QColor(P['col_gold'])
        edge.setAlphaF(0.18)
        if self.hasFocus() or self.underMouse():
            edge = QColor(P['accent'] if self.hasFocus() else P['col_gold'])
            edge.setAlphaF(1.0 if self.hasFocus() else 0.5)
        return QPen(edge, 2.0 if self.hasFocus() else 1.0)


class StudioChip(_StudioButton):
    """A studio behind the page's games (w-113, w-114 opens it): two covers,
    its name in gold and how many of your games it made. A click or Enter
    opens it (`opened`)."""
    PICK = 2
    COVER_W, COVER_H, COVER_STEP = 30, 45, 22

    def __init__(self, cat: categories.Category, shared: int):
        super().__init__()
        self._mosaic_init()
        self.key = cat.key
        self.setObjectName("StudioChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(cat.title)
        self.setAccessibleName(cat.title)
        self.setFixedHeight(scaled(64))
        self._set_mosaic(cat)
        lay = QHBoxLayout(self)
        n = self.cover_count()
        left = SP['3'] + (self.COVER_W + self.COVER_STEP * (n - 1) + SP['3'] if n else 0)
        lay.setContentsMargins(left, 0, SP['4'], 0)
        vb = QVBoxLayout()
        vb.setSpacing(SP['0_5'])
        vb.addStretch(1)
        self.name = label(_short(cat.title.upper(), 26), "StudioName")
        self.count = label(f"{shared} of your game{'s' if shared != 1 else ''}", "StudioCount")
        for lab in (self.name, self.count):
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            vb.addWidget(lab)
        vb.addStretch(1)
        lay.addLayout(vb)
        self._size_to_text()
        self.clicked.connect(self._open)

    def cover_count(self) -> int:
        return min(self.PICK, len(self._slugs))

    def _size_to_text(self):
        for lab in (self.name, self.count):
            lab.ensurePolished()
        m = self.layout().contentsMargins()
        w = m.left() + m.right() + max(self.name.sizeHint().width(),
                                       self.count.sizeHint().width())
        if w != self.width():
            self.setFixedWidth(max(1, w))

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange):
            self._size_to_text()

    def paintEvent(self, _ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = R['card']
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['surface'] if LOOK.get('contrast') else P['surface_2']))
        pt.drawRoundedRect(r, rad, rad)
        pt.setPen(self._edge_pen())
        pt.setBrush(Qt.BrushStyle.NoBrush)
        pt.drawRoundedRect(r, rad, rad)
        cw, ch = self.COVER_W, self.COVER_H
        y = (self.height() - ch) / 2
        for i in reversed(range(self.cover_count())):
            rect = QRectF(SP['3'] + i * self.COVER_STEP, y, float(cw), float(ch))
            cp = QPainterPath()
            cp.addRoundedRect(rect, 4.0, 4.0)
            pt.save()
            pt.setClipPath(cp)
            pt.drawPixmap(rect.topLeft(), self._cover(self._slugs[i], cw, ch))
            pt.restore()
            pt.setPen(QPen(QColor(P['surface_2']), 2.0))
            pt.drawPath(cp)
        pt.end()


class StudioCard(_StudioButton):
    """Another studio you play (w-114, a studio page's foot): its name as a
    small wordmark, "N games · H h", three of its covers in a strip, a gold
    wash. A click or Enter opens it (`opened`); the page sizes its width."""
    PICK = 3
    COVER_W, COVER_H, COVER_STEP = 40, 60, 30

    def __init__(self, cat: categories.Category, minutes: int):
        super().__init__()
        self._mosaic_init()
        self.key = cat.key
        self._title = cat.title.upper()
        self.setObjectName("StudioCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(cat.tip)
        self.setAccessibleName(cat.title)
        self.setFixedSize(scaled(200), scaled(150))
        self._set_mosaic(cat)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        lay.setSpacing(SP['0_5'])
        self.name = label("", "StudioCardName")
        n = len(cat.games)
        played = _played(minutes)
        count = f"{n} game{'s' if n != 1 else ''}" + (f" · {played[0]}" if played else '')
        self.count = label(count, "StudioCardCount")
        for lab in (self.name, self.count):
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            lay.addWidget(lab)
        lay.addStretch(1)
        self.clicked.connect(self._open)

    def cover_count(self) -> int:
        return min(self.PICK, len(self._slugs))

    def _fit(self):
        self.name.ensurePolished()
        w = max(1, self.width() - 2 * SP['4'])
        self.name.setText('\n'.join(two_lines(self._title, self.name.fontMetrics(), w)))

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange):
            self._fit()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def paintEvent(self, _ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = float(R['card'])
        base = QColor(P['surface'] if LOOK.get('contrast') else P['surface_2'])
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(base)
        pt.drawRoundedRect(r, rad, rad)
        if not LOOK.get('contrast'):
            wash = QLinearGradient(0.0, 0.0, 0.0, r.height() * 0.6)
            a, b = QColor(P['col_gold']), QColor(P['col_gold'])
            a.setAlphaF(0.10)
            b.setAlphaF(0.0)
            wash.setColorAt(0.0, a)
            wash.setColorAt(1.0, b)
            pt.setBrush(wash)
            pt.drawRoundedRect(r, rad, rad)
        pt.setPen(self._edge_pen())
        pt.setBrush(Qt.BrushStyle.NoBrush)
        pt.drawRoundedRect(r, rad, rad)
        cw, ch = self.COVER_W, self.COVER_H
        y = float(self.height() - SP['4'] - ch)
        for i in reversed(range(self.cover_count())):
            rect = QRectF(float(SP['4'] + i * self.COVER_STEP), y, float(cw), float(ch))
            cp = QPainterPath()
            cp.addRoundedRect(rect, 5.0, 5.0)
            pt.save()
            pt.setClipPath(cp)
            pt.drawPixmap(rect.topLeft(), self._cover(self._slugs[i], cw, ch))
            pt.restore()
            pt.setPen(QPen(base, 2.0))
            pt.drawPath(cp)
        pt.end()


def _caption(g: dict) -> str:
    name = ' '.join((g.get('display_name') or g.get('slug') or '').split())
    played = _played(g.get('playtime_minutes') or 0)
    return f"{name} · {played[0]}" if played else name


def timeline_layout(years, w: int, h: int):
    """Where the studio timeline paints (pure, no painting): `years` is
    categories.studio_years(). Returns (covers, axis, axis_y): covers as
    [(cover QRectF, slug, caption, caption QRectF)], axis as [(x, year,
    has games)]. Every year of the span shows when it is 12 or fewer,
    else only the years with games; a year's games stand side by side,
    each cover's width between 56 and 112 px (scaled, at most 80 % of its
    share of the column) by the square root of its hours over the most
    played one's."""
    if not years or w < 2 or h < 2:
        return [], [], 0.0
    first, last = years[0][0], years[-1][0]
    has = {y for y, _gs in years}
    cols = list(range(first, last + 1)) if last - first + 1 <= 12 else [y for y, _gs in years]
    pad = float(scaled(40))
    colw = max(1.0, (w - 2 * pad) / len(cols))
    axis_y = float(h - scaled(52))
    cap_h = float(scaled(18))
    bottom = axis_y - scaled(16) - cap_h - SP['1_5']
    top = float(SP['5'])
    tall = max(1.0, bottom - top)
    most = max((g.get('playtime_minutes') or 0 for _y, gs in years for g in gs), default=0)
    lo_w, gap = float(scaled(56)), float(SP['2'])
    by = dict(years)
    covers, axis = [], []
    for i, y in enumerate(cols):
        x = pad + colw * (i + 0.5)
        axis.append((x, y, y in has))
        gs = by.get(y) or []
        if not gs:
            continue
        share = (colw - gap * (len(gs) - 1)) / len(gs)
        hi = max(1.0, min(0.8 * share, float(scaled(112)), tall / 1.5))
        lo = min(lo_w, hi)
        sizes = []
        for g in gs:
            t = math.sqrt((g.get('playtime_minutes') or 0) / most) if most else 0.0
            cw = lo + (hi - lo) * t
            sizes.append((cw, cw * 1.5))
        row_w = sum(cw for cw, _ch in sizes) + gap * (len(sizes) - 1)
        cx = x - row_w / 2
        for g, (cw, ch) in zip(gs, sizes):
            rect = QRectF(cx, bottom - ch, cw, ch)
            cap_w = max(cw, share)
            cap = QRectF(cx + cw / 2 - cap_w / 2, bottom + SP['1_5'], cap_w, cap_h)
            covers.append((rect, g.get('slug'), _caption(g), cap))
            cx += cw + gap
    return covers, axis, axis_y


class StudioTimeline(_CachedLayer, _Mosaic, QWidget):
    """A studio's games along its release years (w-114): a gold axis with
    a dot per year, each game's cover over its year, sized by your hours
    (timeline_layout), with "Name · 23 h" under it. Painted once into a
    cached pixmap; covers arriving and resizes coalesce (`builds` counts)."""
    PICK = 0

    def __init__(self):
        super().__init__()
        self._mosaic_init()
        self._layer_init()
        self.builds = 0
        self._years: list = []
        self.setObjectName("StudioTimeline")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(scaled(300))

    def set_years(self, cat: categories.Category, years):
        """`years`: categories.studio_years(cat, facts)."""
        if self._cat is None or self._cat.key != cat.key:
            self._layer_drop()
            self._keys, self._asked = {}, set()
        self._cat = cat
        self._years = [(y, list(gs)) for y, gs in years]
        self._by = {g.get('slug'): g for _y, gs in self._years for g in gs}
        self._slugs = list(self._by)
        self.update()

    def has_years(self) -> bool:
        return bool(self._years)

    def layout_now(self):
        return timeline_layout(self._years, self.width(), self.height())

    def _cover_arrived(self):
        self._layer_stale()

    def _key(self, w, h, dpr):
        sig = tuple((y, tuple((g.get('slug'), g.get('playtime_minutes') or 0,
                               g.get('display_name')) for g in gs)) for y, gs in self._years)
        return (sig, w, h, dpr, P['bg'], P['surface_2'], P['col_gold'], is_light(),
                bool(LOOK.get('contrast')))

    def paintEvent(self, _ev):
        w, h = self.width(), self.height()
        if not self._years or w < 2 or h < 2:
            return
        dpr = screen_dpr()
        pm = self._layer(self._key(w, h, dpr), lambda: self._build(w, h, dpr))
        if pm is None:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pt.drawPixmap(QRectF(self.rect()), pm, QRectF(pm.rect()))    # scaled while stale
        pt.end()

    def _build(self, w: int, h: int, dpr: float) -> QPixmap:
        self.builds += 1
        pm = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        contrast = bool(LOOK.get('contrast'))
        pt = QPainter(pm)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(0.0, 0.0, float(w), float(h)).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = float(R['panel'])
        pt.setPen(QPen(QColor(P['border']), 2.0 if contrast else 1.0))
        pt.setBrush(QColor(P['surface'] if contrast else P['surface_2']))
        pt.drawRoundedRect(r, rad, rad)
        covers, axis, axis_y = self.layout_now()
        gold = QColor(P['col_gold'])
        line = QColor(gold)
        line.setAlphaF(0.35)
        pad = float(scaled(40))
        pt.fillRect(QRectF(pad, axis_y - 1.0, max(1.0, w - 2 * pad), 2.0), line)
        f = QFont(self.font())
        f.setPixelSize(max(1, scaled(FS['sm'])))
        f.setWeight(QFont.Weight.DemiBold)
        dot = float(scaled(10))
        for x, year, hit in axis:
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(gold if hit else QColor(P['overlay']))
            pt.drawEllipse(QRectF(x - dot / 2, axis_y - dot / 2, dot, dot))
            pt.setFont(f)
            pt.setPen(gold if hit else QColor(P['muted']))
            pt.drawText(QRectF(x - 40.0, axis_y + dot, 80.0, float(scaled(24))),
                        int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), str(year))
        shade = QColor(P['scrim'])
        shade.setAlphaF(0.40)
        fc = QFont(self.font())
        fc.setPixelSize(max(1, scaled(FS['xs'])))
        fc.setWeight(QFont.Weight.Medium)
        pt.setFont(fc)
        fm = pt.fontMetrics()
        for rect, slug, caption, cap in covers:
            sp = QPainterPath()
            sp.addRoundedRect(rect.translated(0.0, 3.0), float(R['sm']), float(R['sm']))
            pt.fillPath(sp, shade)
            cp = QPainterPath()
            cp.addRoundedRect(rect, float(R['sm']), float(R['sm']))
            pt.save()
            pt.setClipPath(cp)
            cw = max(8, int(round(rect.width() / 8)) * 8)      # an 8 px step: a drag-resize
            cover = self._cover(slug, cw, round(cw * 1.5))       # asks ART for few sizes
            pt.drawPixmap(rect, cover, QRectF(cover.rect()))
            pt.restore()
            pt.setPen(QPen(QColor(P['border']), 1.0))
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.drawPath(cp)
            pt.setPen(QColor(P['subtext']))
            text = fm.elidedText(caption, Qt.TextElideMode.ElideRight, max(1, int(cap.width())))
            pt.drawText(cap, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), text)
        pt.end()
        return pm


class _Progress(QWidget):
    """A thin bar: how far into the main story (0-1)."""

    def __init__(self):
        super().__init__()
        self.value = 0.0
        self.setFixedHeight(4)

    def set_value(self, v: float):
        self.value = max(0.0, min(1.0, float(v)))
        self.update()

    def paintEvent(self, _ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        rad = r.height() / 2
        track = QColor(P['border'])
        pt.setBrush(track)
        pt.drawRoundedRect(r, rad, rad)
        if self.value > 0:
            pt.setBrush(QColor(P['col_gold']))
            pt.drawRoundedRect(QRectF(r.x(), r.y(), max(r.height(), r.width() * self.value),
                                      r.height()), rad, rad)
        pt.end()


def game_meta(g: dict, fact: dict | None) -> str:
    """A studio page game card's line: "Yesterday · 25 h · 46 %" (parts
    with nothing to say left out; "Not played yet" for none)."""
    parts = []
    when = fmt_relative(g.get('last_played'))
    if when:
        parts.append(when[:1].upper() + when[1:])
    played = _played(g.get('playtime_minutes') or 0)
    if played:
        parts.append(played[0])
    prog = smart.progress(g, fact)
    if prog is not None and played:
        parts.append(f"{round(100 * min(1.0, prog))} %")
    return "  ·  ".join(parts) if parts else "Not played yet"


class StudioGameCard(QFrame):
    """A game on a studio page (w-114): its tile (a real GameTile, so
    selection, jobs and the menu work as anywhere), name, credit line, when
    and how long you played, and a gold bar of the main story. A click on
    the card is the tile's click, a double click its double click."""

    def __init__(self, g: dict, fact: dict | None, tile: GameTile):
        super().__init__()
        self.game = g
        self.tile = tile
        self.setObjectName("StudioGameCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['3'], SP['3'], SP['3'], SP['3'])
        lay.setSpacing(SP['4'])
        lay.addWidget(tile, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, SP['1'], 0, SP['1'])
        col.setSpacing(SP['1'])
        self._name = ' '.join((g.get('display_name') or g.get('slug') or '').split())
        self._credit = categories.credit_line(fact)
        self.name = label("", "StudioGameName")
        self.credit = label("", "StudioGameCredit")
        self.meta = label(game_meta(g, fact), "StudioGameMeta")
        for lab in (self.name, self.credit, self.meta):
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.credit.setToolTip(self._credit)
        self.credit.setVisible(bool(self._credit))
        col.addWidget(self.name)
        col.addWidget(self.credit)
        col.addStretch(1)
        col.addWidget(self.meta)
        self.bar = _Progress()
        prog = smart.progress(g, fact)
        self.bar.set_value(prog or 0.0)
        self.bar.setVisible(prog is not None and bool(g.get('playtime_minutes')))
        col.addWidget(self.bar)
        lay.addLayout(col, 1)
        self.setFixedHeight(tile.height() + 2 * SP['3'])

    def _fit(self):
        w = max(1, self.width() - self.tile.width() - 2 * SP['3'] - SP['4'])
        for lab in (self.name, self.credit):
            lab.ensurePolished()
        self.name.setText('\n'.join(two_lines(self._name, self.name.fontMetrics(), w)))
        self.credit.setText(self.credit.fontMetrics().elidedText(
            self._credit, Qt.TextElideMode.ElideRight, w))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange):
            self._fit()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()                          # the release comes here: the click
        else:
            super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.tile.selected.emit(self.game, ev.modifiers())
        else:
            super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        """As the tile's own right click: select it, then its menu."""
        self.tile.selected.emit(self.game, Qt.KeyboardModifier.NoModifier)
        self.tile.menu.emit(self.game, ev.globalPos())

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.tile.double.emit(self.game)
        else:
            super().mouseDoubleClickEvent(ev)
