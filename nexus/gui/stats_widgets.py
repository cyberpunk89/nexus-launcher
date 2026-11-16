"""Home's Stats widgets (w-141): Backlog health, Where your space goes (a
treemap) and Library facts. Each one is a WidgetSpec in widget_spec.py plus
one InsightWidget here. The CLI did the work: the payload's `backlog`,
`space`, `space_more` and `facts` (nexus/insights.py), already without the
games you hid; the GUI never computes the backlog from its own game list.

A widget with nothing to say answers has_content() False and leaves the
layout. Words are kind: a game you have not played is "never played", the
pace is "at your pace", nothing is red. Names of games only, no paths.
Paint calls use the float overloads only (PyQt6 refuses ints mixed with
floats in one call)."""

from datetime import date

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .art import anim_ms
from .common import alive
from .daycard import _color, CardHost, CardText
from .fmt import fmt_bytes
from .insight_widgets import _day, InsightWidget
from .theme import contrast, FONTS, FS, G, P, scaled, SP
from .widgets import clear_layout, label, plain_label

CARD_DELAY_MS = 150
SPACE_HEIGHT = 200              # logical px of the treemap
GAP = 3                         # between blocks
BAR_HEIGHT = 14

# (key, colour token, the word in the legend and on the card)
BUCKETS = (('month', 'accent', "Played this month"),
           ('year', 'success', "Within a year"),
           ('over', 'warning', "Idle over a year"),
           ('never', 'overlay', "Never played"))
BUCKET = {k: (tok, words) for k, tok, words in BUCKETS}
BACKLOG_PARTS = (('done', 'accent', "done"), ('playing', 'success', "playing"),
                 ('started', 'warning', "started"), ('never', 'overlay', "never played"))
FACT_HEADS = {'library': "Your library", 'top_genre': "Top genre", 'most_played': "Most played",
              'oldest': "Oldest game", 'shortest_unplayed': "Shortest unplayed",
              'biggest': "Biggest install", 'top_studio': "Top studio"}


# ---- pure helpers (no Qt state) ----
def idle_bucket(days) -> str:
    """The legend bucket for a game last played `days` ago (None: never)."""
    if days is None:
        return 'never'
    return 'month' if days <= 30 else 'year' if days <= 365 else 'over'


def idle_words(days) -> str:
    return BUCKET[idle_bucket(days)][1]


def text_on(bg: str) -> str:
    """P['text'] or P['bg'], whichever reads better on the colour `bg`."""
    b = _color(bg).name()
    return max((P['text'], P['bg']), key=lambda t: contrast(_color(t).name(), b))


def _worst(row: list, side: float) -> float:
    s = sum(row)
    return max(side * side * max(row) / (s * s), s * s / (side * side * min(row)))


def squarify(values, rect) -> list:
    """Treemap `rect` (x, y, w, h) into one (x, y, w, h) per value, in the
    order given: areas proportional to the values, the rect filled, blocks
    near square (Bruls et al.). A value of 0 or less gets an empty block."""
    x, y, w, h = rect
    vals = [max(0.0, float(v or 0)) for v in values]
    total = sum(vals)
    out = [(x, y, 0.0, 0.0)] * len(vals)
    if not vals or total <= 0 or w <= 0 or h <= 0:
        return out
    scale = w * h / total
    order = sorted((i for i, v in enumerate(vals) if v > 0), key=lambda i: (-vals[i], i))
    areas = {i: vals[i] * scale for i in order}
    while order:
        side = min(w, h)
        row = [order[0]]
        rest = order[1:]
        while rest and _worst([areas[i] for i in row + [rest[0]]], side) <= \
                _worst([areas[i] for i in row], side):
            row.append(rest.pop(0))
        s = sum(areas[i] for i in row)
        if w >= h:                                 # a column on the left
            cw = s / h if h else 0.0
            cy = y
            for i in row:
                ch = areas[i] / cw if cw else 0.0
                out[i] = (x, cy, cw, ch)
                cy += ch
            x, w = x + cw, w - cw
        else:                                      # a strip along the top
            rh = s / w if w else 0.0
            cx = x
            for i in row:
                cw = areas[i] / rh if rh else 0.0
                out[i] = (cx, y, cw, rh)
                cx += cw
            y, h = y + rh, h - rh
        order = rest
    return out


def backlog_lines(b: dict) -> dict:
    """The words of Backlog health: `counts` [(n, word, colour token)], `left`
    ("310 h", "of main stories left") or None, `pace` and `idle` (or "")."""
    counts = [(int(b.get(k) or 0), word, tok) for k, tok, word in BACKLOG_PARTS]
    left = float(b.get('main_hours_left') or 0)
    pace = float(b.get('pace_hours_week') or 0)
    if pace <= 0:
        pace_text = "Your pace shows here once you play a little"
    elif left <= 0:
        pace_text = ""
    else:
        weeks = max(1, round(left / pace))
        pace_text = f"About {weeks} week{'s' if weeks != 1 else ''} at your pace"
    idle = int(b.get('idle_year_bytes') or 0)
    return {'counts': counts,
            'left': (f"{left:,.0f} h", "of main stories left") if left > 0 else None,
            'pace': pace_text,
            'idle': f"{fmt_bytes(idle)} is held by games idle for a year" if idle > 0 else ""}


def _games(n: int) -> str:
    return f"{n:,} game{'s' if n != 1 else ''}"


def fact_text(f: dict) -> tuple[str, str, str]:
    """(heading, the big line, the muted line) for one payload fact."""
    kind, name, n = f.get('kind'), f.get('name') or "", f.get('n') or 0
    head = FACT_HEADS.get(kind, "A fact")
    if kind == 'library':
        hours = float(f.get('hours') or 0)
        line = f"About {hours:,.0f} h played in all" if hours >= 1 else "Nothing played yet, and that is fine"
        return head, _games(int(n)), line
    if kind == 'top_genre':
        return head, name, f"{_games(int(n))} in your library"
    if kind == 'most_played':
        return head, name, f"{n:g} h with it so far"
    if kind == 'oldest':
        return head, name, f"Released in {int(n)}"
    if kind == 'shortest_unplayed':
        return head, name, f"About {n:g} h to finish, and still waiting for you"
    if kind == 'biggest':
        return head, name, f"{fmt_bytes(n)} on your disk"
    if kind == 'top_studio':
        return head, name, f"{_games(int(n))} from them"
    return head, name, ""


def fact_index(today: date, n: int) -> int:
    """Which fact is the fact of the day: the same all day, the next at midnight."""
    return today.toordinal() % n if n > 0 else 0


# ---- small painted bits ----
class Dot(QWidget):
    """A small rounded square of a theme colour (the legend's key)."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token
        self.setFixedSize(scaled(10), scaled(10))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(_color(P[self.token]))
        r = float(scaled(3))
        pt.drawRoundedRect(QRectF(0.0, 0.0, float(self.width()), float(self.height())), r, r)
        pt.end()


class SegmentBar(QWidget):
    """One bar split by counts; the ends are rounded (radius at most half
    the height), a 2 px gap between segments. Its accessible name says the
    counts, so colour is never the only way to read it."""

    def __init__(self):
        super().__init__()
        self.parts: list[tuple[int, str]] = []
        self.setFixedHeight(scaled(BAR_HEIGHT))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(scaled(60))

    def set_parts(self, parts: list[tuple[int, str, str]]):
        """parts: (count, colour token, word)."""
        self.parts = [(n, tok) for n, tok, _w in parts]
        self.setAccessibleName(", ".join(f"{n} {w}" for n, _t, w in parts))
        self.update()

    def segments(self) -> list[tuple[float, float, str]]:
        """(x, width, token) of every non-empty part, gaps taken out."""
        live = [(n, tok) for n, tok in self.parts if n > 0]
        gap = float(scaled(2))
        room = max(0.0, self.width() - gap * max(0, len(live) - 1))
        total = sum(n for n, _t in live) or 1
        out, x = [], 0.0
        for n, tok in live:
            wd = room * n / total
            out.append((x, wd, tok))
            x += wd + gap
        return out

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = float(self.height())
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.0, 0.0, float(self.width()), h), h / 2, h / 2)
        pt.setClipPath(path)
        pt.setPen(Qt.PenStyle.NoPen)
        for x, wd, tok in self.segments():
            pt.fillRect(QRectF(x, 0.0, wd, h), _color(P[tok]))
        pt.end()


# ---- Backlog health ----
class BacklogWidget(InsightWidget):
    """What is done, being played, started and waiting, as one bar, with the
    hours of main story left and about how long that is at your pace."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'backlog', "Backlog health", G['shelves'], 'ember', None

    def _b(self, hub) -> dict | None:
        b = (self.data(hub) or {}).get('backlog')
        if not isinstance(b, dict):
            return None
        return b if sum(int(b.get(k) or 0) for k, _t, _w in BACKLOG_PARTS) > 0 else None

    def has_content(self, hub):
        return self._b(hub) is not None

    def signature(self, hub):
        return repr(self._b(hub))

    def build(self, hub):
        b = self._b(hub)
        if b is None:
            return
        lines = backlog_lines(b)
        bar = SegmentBar()
        bar.set_parts([(n, tok, word) for n, word, tok in lines['counts']])
        self.bar = bar
        self.body.addWidget(bar)
        grid = QVBoxLayout()
        grid.setSpacing(SP['1'])
        for pair in (lines['counts'][:2], lines['counts'][2:]):
            row = QHBoxLayout()
            row.setSpacing(SP['4'])
            for n, word, tok in pair:
                cell = QHBoxLayout()
                cell.setSpacing(SP['1_5'])
                cell.addWidget(Dot(tok))
                cell.addWidget(label(f"{n:,}", "StatValue"))
                cell.addWidget(label(word, "Muted"))
                cell.addStretch()
                row.addLayout(cell, 1)
            grid.addLayout(row)
        self.body.addLayout(grid)
        if lines['left']:
            row = QHBoxLayout()
            row.setSpacing(SP['2'])
            row.addWidget(label(lines['left'][0], "DashBig"))
            row.addWidget(label(lines['left'][1], "Muted"))
            row.addStretch()
            self.body.addSpacing(SP['1'])
            self.body.addLayout(row)
        if lines['pace']:
            self.body.addWidget(label(lines['pace'], None, wrap=True))
        if lines['idle']:
            self.body.addWidget(label(lines['idle'], "Muted", wrap=True))


# ---- Where your space goes ----
class SpaceMap(CardHost, QWidget):
    """A treemap of the installed games by size; the colour says how long
    since you played (the legend names each). Hover or the keyboard puts a
    ring on a block and a painted card beside it; arrows move to the nearest
    block, Enter opens the game, Escape goes back to Open."""

    OWN_FOCUS_RING = True       # the map rings its own block (input.FocusRing skips it)
    NATIVE_ARROWS = 'all'       # arrows move the block, never the window's focus
    activated = pyqtSignal(str)         # Enter on a game block (its slug)
    leave_requested = pyqtSignal()      # Escape or the pad's B

    def __init__(self):
        super().__init__()
        self.init_card_host()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Where your space goes, installed games by size")
        self.setMouseTracking(True)
        self.items: list[dict] = []         # {slug, name, bytes, idle_days}
        self.more: dict = {'n': 0, 'bytes': 0}
        self.hovered: int | None = None     # index into blocks()
        self.keyboard = False
        self.setMinimumHeight(scaled(SPACE_HEIGHT))
        self.setMaximumHeight(scaled(SPACE_HEIGHT))
        self.setMinimumWidth(scaled(160))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(scaled(480), scaled(SPACE_HEIGHT))

    def set_data(self, items: list[dict], more: dict | None = None):
        self.items = list(items)
        self.more = more if isinstance(more, dict) else {'n': 0, 'bytes': 0}
        self.hovered = None
        self._hide_card()
        self.setAccessibleDescription(
            "; ".join(f"{i['name']}, {fmt_bytes(i['bytes'])}, {idle_words(i.get('idle_days'))}"
                      for i in self.items))
        self.update()

    def blocks(self) -> list[dict]:
        """Every block with its rect: one per game, then "+ N more"."""
        rows = [{'slug': i['slug'], 'name': i['name'], 'bytes': i['bytes'],
                 'key': idle_bucket(i.get('idle_days')), 'more': False} for i in self.items]
        if self.more.get('n') and self.more.get('bytes'):
            rows.append({'slug': None, 'name': f"+ {self.more['n']} more", 'bytes': self.more['bytes'],
                         'key': 'more', 'more': True})
        rects = squarify([r['bytes'] for r in rows], (0.0, 0.0, float(self.width()),
                                                      float(self.height())))
        half = GAP / 2
        for r, (x, y, w, h) in zip(rows, rects):
            r['rect'] = QRectF(x + half, y + half, max(0.0, w - GAP), max(0.0, h - GAP))
        return rows

    def block_at(self, pos) -> int | None:
        p = QPointF(pos)
        for i, b in enumerate(self.blocks()):
            if b['rect'].contains(p):
                return i
        return None

    def card_text(self, b: dict) -> CardText:
        if b['more']:
            return CardText(head=b['name'] + " games", total=fmt_bytes(b['bytes']),
                            line2="The smaller installs")
        return CardText(head=b['name'], total=fmt_bytes(b['bytes']), line2=idle_words(
            next((i.get('idle_days') for i in self.items if i['slug'] == b['slug']), None)))

    # ---- card ----
    def _show_card(self, i: int):
        bl = self.blocks()
        if not 0 <= i < len(bl):
            return self._hide_card()
        self.show_card_at(bl[i]['rect'], self.card_text(bl[i]), {})

    def _timed_show(self):
        if self.hovered is not None:
            self._show_card(self.hovered)

    def _card_for_hover(self):
        i = self.hovered
        if i is None:
            return self._hide_card()
        if self.card is not None and alive(self.card) and self.card.isVisible():
            return self._show_card(i)                  # moving along the map: no new delay
        delay = anim_ms(CARD_DELAY_MS)
        if delay:
            self.show_timer.start(delay)
        else:
            self._show_card(i)

    # ---- keyboard and pad ----
    def _move_to(self, i: int | None):
        bl = self.blocks()
        if i is None or not 0 <= i < len(bl):
            return
        self.hovered = i
        self.setAccessibleDescription(self.card_text(bl[i]).text())
        self.update()
        self._show_card(i)

    def _nearest(self, cur: int, key) -> int | None:
        """The block nearest by centre in the key's direction, else None."""
        bl = self.blocks()
        c = bl[cur]['rect'].center()
        best, best_d = None, None
        for i, b in enumerate(bl):
            if i == cur or b['rect'].isEmpty():
                continue
            p = b['rect'].center()
            dx, dy = p.x() - c.x(), p.y() - c.y()
            ahead, side = {Qt.Key.Key_Right: (dx, dy), Qt.Key.Key_Left: (-dx, dy),
                           Qt.Key.Key_Down: (dy, dx), Qt.Key.Key_Up: (-dy, dx)}[key]
            if ahead <= 1.0:
                continue
            d = ahead * ahead + 4 * side * side         # straight ahead beats sideways
            if best_d is None or d < best_d:
                best, best_d = i, d
        return best

    def keyPressEvent(self, ev):
        key = ev.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            if self.blocks():
                cur = self.hovered if self.hovered is not None else 0
                if self.hovered is None:
                    self._move_to(0)
                else:
                    nxt = self._nearest(cur, key)
                    if nxt is not None:
                        self._move_to(nxt)
            ev.accept()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            bl = self.blocks()
            if self.hovered is not None and self.hovered < len(bl) and bl[self.hovered]['slug']:
                self.activated.emit(bl[self.hovered]['slug'])
            ev.accept()
        elif key == Qt.Key.Key_Escape:
            self.leave_requested.emit()
            ev.accept()
        else:
            super().keyPressEvent(ev)

    def focusInEvent(self, ev):
        self.keyboard = True
        if ev.reason() != Qt.FocusReason.MouseFocusReason or self.hovered is None:
            self._move_to(self.hovered if self.hovered is not None else 0)
        self.update()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        self.keyboard = False
        self.hovered = None
        self._hide_card()
        self.update()
        super().focusOutEvent(ev)

    def mouseMoveEvent(self, ev):
        i = self.block_at(ev.position().toPoint())
        if i != self.hovered:
            self.hovered = i
            self.update()
            self._card_for_hover()
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        bl = self.blocks()
        i = self.block_at(ev.position().toPoint())
        if i is not None and bl[i]['slug']:
            self.activated.emit(bl[i]['slug'])
        super().mousePressEvent(ev)

    def leaveEvent(self, ev):
        if not self.keyboard:
            if self.hovered is not None:
                self.hovered = None
                self.update()
            self._hide_card()
        if ev is not None:
            super().leaveEvent(ev)

    def hideEvent(self, ev):
        self._hide_card()
        super().hideEvent(ev)

    def resizeEvent(self, ev):
        self._hide_card()
        super().resizeEvent(ev)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(FONTS.ui)
        f.setPixelSize(FS['xs'])
        small = QFont(FONTS.ui)
        small.setPixelSize(FS['2xs'])
        radius = float(scaled(8))
        pad = float(scaled(8))
        for i, b in enumerate(self.blocks()):
            r = b['rect']
            if r.width() < 2 or r.height() < 2:
                continue
            if b['more']:
                pen = QPen(_color(P['muted']), 1.0)
                pen.setStyle(Qt.PenStyle.DashLine)
                pt.setPen(pen)
                pt.setBrush(Qt.BrushStyle.NoBrush)
                pt.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
                fg = P['subtext']
            else:
                bg = P[BUCKET[b['key']][0]]
                pt.setPen(Qt.PenStyle.NoPen)
                pt.setBrush(_color(bg))
                pt.drawRoundedRect(r, radius, radius)
                fg = text_on(bg)
            inner = r.adjusted(pad, pad * 0.75, -pad, -pad * 0.75)
            for font, text, bold in ((f, b['name'], True), (small, fmt_bytes(b['bytes']), False)):
                fm = QFontMetricsF(font)
                if inner.width() < fm.averageCharWidth() * 3 or inner.height() < fm.height():
                    break
                font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
                pt.setFont(font)
                pt.setPen(_color(fg))
                row = QRectF(inner.left(), inner.top(), inner.width(), fm.height())
                pt.drawText(row, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                            fm.elidedText(text, Qt.TextElideMode.ElideRight, inner.width()))
                inner = inner.adjusted(0.0, fm.height() + 1.0, 0.0, 0.0)
            if i == self.hovered:
                pt.setBrush(Qt.BrushStyle.NoBrush)
                if self.keyboard:
                    pt.setPen(QPen(_color(P['text']), 2.0))
                    pt.drawRoundedRect(r.adjusted(1.0, 1.0, -1.0, -1.0), radius, radius)
                else:
                    pt.setPen(QPen(_color(P['text']), 1.5))
                    pt.drawRoundedRect(r.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        pt.end()


class SpaceWidget(InsightWidget):
    """Where your space goes: the installed games as a treemap, the biggest
    first, idle ones marked by colour. Leaves the layout without sizes (the
    disk size can be missing, on Windows for one)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'spacemap', "Where your space goes", G['storage'], 'tide', 'storage'

    def __init__(self):
        super().__init__()
        self.map: SpaceMap | None = None

    def _items(self, hub) -> list[dict]:
        rows = (self.data(hub) or {}).get('space')
        shown = self.shown(hub)
        out = []
        for r in rows if isinstance(rows, list) else ():
            if isinstance(r, dict) and r.get('slug') in shown and (r.get('bytes') or 0) > 0:
                g = shown[r['slug']]
                out.append({'slug': r['slug'], 'bytes': int(r['bytes']), 'idle_days': r.get('idle_days'),
                            'name': g.get('display_name') or g.get('name') or r['slug']})
        return out

    def _more(self, hub) -> dict:
        m = (self.data(hub) or {}).get('space_more')
        return m if isinstance(m, dict) else {'n': 0, 'bytes': 0}

    def has_content(self, hub):
        return bool(self._items(hub))

    def signature(self, hub):
        return (repr(self._items(hub)), repr(self._more(hub)))

    def clear(self):
        self.map = None
        super().clear()

    def build(self, hub):
        items = self._items(hub)
        if not items:
            return
        more = self._more(hub)
        total = sum(i['bytes'] for i in items) + int(more.get('bytes') or 0)
        self.body.addWidget(label(f"{fmt_bytes(total)} in your installed games", "Muted"))
        self.map = SpaceMap()
        self.map.set_data(items, more)
        self.map.activated.connect(lambda slug: self._open(hub, slug))
        self.map.leave_requested.connect(self._leave_map)
        self.body.addWidget(self.map)
        legend = QHBoxLayout()
        legend.setSpacing(SP['4'])
        present = {idle_bucket(i['idle_days']) for i in items}
        for key, tok, words in BUCKETS:
            if key in present:
                cell = QHBoxLayout()
                cell.setSpacing(SP['1_5'])
                cell.addWidget(Dot(tok))
                cell.addWidget(label(words, "Muted"))
                legend.addLayout(cell)
        legend.addStretch()
        self.body.addLayout(legend)

    def _open(self, hub, slug: str):
        g = self.shown(hub).get(slug)
        if g is not None:
            self.open_page(hub, g)

    def _leave_map(self):
        btn = getattr(self, 'open_btn', None)
        if btn is not None and alive(btn):
            btn.setFocus(Qt.FocusReason.TabFocusReason)


# ---- Library facts ----
class FactsWidget(InsightWidget):
    """One fact about your library, the same all day (a new one at midnight);
    "Next fact" steps through the rest (not saved)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'facts', "Library facts", G['info'], 'dawn', None

    def __init__(self):
        super().__init__()
        self._shift = 0
        self.slot: QVBoxLayout | None = None

    def _items(self, hub) -> list[dict]:
        rows = (self.data(hub) or {}).get('facts')
        shown = self.shown(hub)
        return [f for f in (rows if isinstance(rows, list) else []) if isinstance(f, dict)
                and f.get('kind') in FACT_HEADS and (f.get('slug') is None or f['slug'] in shown)]

    def _today(self, hub) -> date:
        return _day((self.data(hub) or {}).get('today')) or date.today()

    def has_content(self, hub):
        return bool(self._items(hub))

    def signature(self, hub):
        return (self._today(hub).isoformat(), repr(self._items(hub)))

    def index(self, hub) -> int:
        items = self._items(hub)
        return (fact_index(self._today(hub), len(items)) + self._shift) % len(items) if items else 0

    def clear(self):
        self.slot = None
        super().clear()

    def build(self, hub):
        if not self._items(hub):
            return
        self.slot = QVBoxLayout()
        self.slot.setSpacing(SP['1_5'])
        self.body.addLayout(self.slot)
        self._paint_fact(hub)
        row = QHBoxLayout()
        nxt = QPushButton("Next fact")
        nxt.setObjectName("Ghost")
        nxt.setCursor(Qt.CursorShape.PointingHandCursor)
        nxt.clicked.connect(lambda: self._next(hub))
        row.addWidget(nxt)
        row.addStretch()
        self.body.addLayout(row)

    def _paint_fact(self, hub):
        """The fact itself, in the slot above the button (which keeps focus)."""
        items = self._items(hub)
        if self.slot is None or not items:
            return
        clear_layout(self.slot)
        i = self.index(hub)
        f = items[i]
        head, big, line = fact_text(f)
        top = QHBoxLayout()
        top.addWidget(label(head, "StatLabel"))
        top.addStretch()
        top.addWidget(label(f"{i + 1} of {len(items)}", "Muted"))
        self.slot.addLayout(top)
        g = self.shown(hub).get(f.get('slug') or '')
        shown_name = (g.get('display_name') or big) if g is not None else big
        title = plain_label(shown_name, "DashTitle", wrap=True)
        if g is not None:
            self.slot.addWidget(self.link_block([title], lambda g=g: self.open_page(hub, g)))
        else:
            self.slot.addWidget(title)
        if line:
            self.slot.addWidget(label(line, "Muted", wrap=True))

    def _next(self, hub):
        self._shift += 1
        self._paint_fact(hub)


STATS_WIDGETS = (BacklogWidget, SpaceWidget, FactsWidget)
