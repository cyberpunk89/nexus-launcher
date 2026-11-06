"""The Home dashboard: its layout model and widgets."""

import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QEvent, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QLinearGradient, QPainter,
    QPainterPath, QPixmap, QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from nexus import gamedb, smart, storeinfo
from nexus.features import has

from .common import alive, COLLECTION_NAMES
from .fmt import fmt_ago, fmt_hours, fmt_relative, fmt_size
from .library import Dwell, FollowHero
from .moments import DISK_FULL
from .theme import FONTS, FS, G, GRAD, P, PILL_H, R, SP
from .theme import collection_look


DASH_SPAN = {'small': 1, 'wide': 2, 'full': 99, 'cinematic': 99, 'banner': 99}
# (id, sizes it allows, default size, hidden by default)
DASH_KINDS = [                       # default order packs evenly in 3 columns
    ('stage',      ('cinematic', 'wide', 'banner'), 'cinematic', False),
    ('friends',    ('wide', 'full'),           'full',  False),
    ('surprise',   ('full',),                  'full',  False),
    ('system',     ('small', 'wide'),          'wide',  False),
    ('news',       ('small', 'wide', 'full'),  'small', False),
    ('installed',  ('full',),                  'full',  False),
    ('smart',      ('full',),                  'full',  False),
    ('deals',      ('small', 'wide', 'full'),  'wide',  False),
    ('upcoming',   ('small', 'wide', 'full'),  'small', False),
    ('week',       ('small', 'wide'),          'small', False),
    ('favourites', ('full',),                  'full',  False),
    ('collections', ('full',),                 'full',  False),
    ('updates',    ('full',),                  'full',  False),
    ('owned',      ('full',),                  'full',  True),
]
DASH_SIZES = {k: sizes for k, sizes, _d, _h in DASH_KINDS}
DASH_DEFAULT_SIZE = {k: d for k, _s, d, _h in DASH_KINDS}
# Widgets folded into another: a saved layout's first one becomes the new
# widget in its place (so it doesn't jump to the end).
DASH_MERGED = {'activity': 'system', 'storage': 'system', 'health': 'system',
               'proton': 'system', 'jump': 'stage', 'playtime': 'week'}
# Merged widgets that are shown even when the old one was hidden: the Stage
# takes the hero's place, which was always there (w-85).
DASH_MERGED_SHOWN = {'stage'}
# Widgets added later that belong next to a related one rather than last.
DASH_PLACE_AFTER = {'upcoming': 'deals', 'surprise': 'stage', 'collections': 'favourites',
                    'smart': 'installed', 'friends': 'stage', 'week': 'upcoming'}
# Per-widget options checked like sizes: the first value is the default and
# a bad one falls back to it (w-85; more styles and backgrounds in w-90).
DASH_OPTIONS = {'stage': {'style': ('row',), 'bg': ('art',)},
                'week': {'style': ('bars', 'number')}}


def _dash_shelf(value) -> str:
    """The smart shelf the Home widget shows (an item's `shelf` key): a
    rule, or `user:<id>` for a shelf you built (w-51; checked when shown)."""
    ok = value in smart.WIDGET_RULES or (smart.is_user(value) and len(value) <= 40)
    return value if ok else smart.WIDGET_DEFAULT


def _user_shelf(hub, key) -> dict | None:
    """A shelf you built (w-51) by its `user:<id>` key, unless removed."""
    s = smart.find_user(getattr(getattr(hub, 'prefs', None), 'smart_shelves', ()), key)
    return s if s and not s['removed'] else None


def dash_shelf_label(hub, value) -> str:
    """The Home widget's shelf as Customise names it."""
    mine = _user_shelf(hub, value)
    if mine:
        return smart.title_of(mine)
    rule = _dash_shelf(value)
    return smart.label(f'smart:{smart.WIDGET_DEFAULT if smart.is_user(rule) else rule}')


def _dash_options(wid: str, it: dict) -> dict:
    """The widget's DASH_OPTIONS keys from a saved item, each checked."""
    return {key: (it.get(key) if it.get(key) in ok else ok[0])
            for key, ok in DASH_OPTIONS.get(wid, {}).items()}


def dash_default() -> list[dict]:
    return [{'id': k, 'size': d, 'hidden': h, **({'shelf': _dash_shelf(None)} if k == 'smart'
                                                 else {}), **_dash_options(k, {})}
            for k, _s, d, h in DASH_KINDS]


def stage_height(size: str, viewport_h: int) -> int:
    """The Stage's height for a size in a Home viewport this tall: tall but
    never the whole screen, so the next row always peeks."""
    if size == 'banner':
        return 250
    if size == 'wide':
        return max(320, min(420, viewport_h - 360))
    return max(480, min(610, viewport_h - 290))


DASH_VERSION = 2   # 2: the Stage moved to the top (w-86); renames to new kinds are not gated


def dash_normalize(items, version: int = DASH_VERSION) -> list[dict]:
    """A saved layout made safe: unknown widgets dropped, bad sizes fixed,
    merged widgets (DASH_MERGED) replaced in place by the one they became,
    and widgets added since it was saved put in (next to their DASH_PLACE_AFTER
    anchor, else at the end) with their defaults. A layout saved before version 2
    also gets the Stage moved to the top; every other widget keeps its place."""
    items = [it for it in (items if isinstance(items, list) else []) if isinstance(it, dict)]
    legacy: dict[str, list[dict]] = {}
    for it in items:
        if it.get('id') in DASH_MERGED:
            legacy.setdefault(DASH_MERGED[it['id']], []).append(it)
    out, seen = [], set()
    for it in items:
        wid = it.get('id')
        if wid in DASH_MERGED:
            wid = DASH_MERGED[wid]
            if wid in seen:
                continue
            olds = legacy[wid]
            it = {'id': wid, 'size': None, 'hidden': wid not in DASH_MERGED_SHOWN
                  and all(o.get('hidden') for o in olds)}
        if wid not in DASH_SIZES or wid in seen:
            continue
        seen.add(wid)
        size = it.get('size')
        out.append({'id': wid,
                    'size': size if size in DASH_SIZES[wid] else DASH_DEFAULT_SIZE[wid],
                    'hidden': bool(it.get('hidden'))})
        if wid == 'smart':
            out[-1]['shelf'] = _dash_shelf(it.get('shelf'))
        out[-1].update(_dash_options(wid, it))
    last: dict[str, str] = {}             # anchor -> the newest widget put after it
    for d in dash_default():
        if d['id'] in seen:
            continue
        anchor = DASH_PLACE_AFTER.get(d['id'])
        # After the anchor, and after the new ones already put there, so two
        # new widgets for one anchor keep their DASH_KINDS order.
        after = last.get(anchor, anchor)
        at = next((i for i, o in enumerate(out) if o['id'] == after), None)
        if at is None:
            out.append(d)
        else:
            out.insert(at + 1, d)
            last[anchor] = d['id']
        seen.add(d['id'])
    if version < 2:
        at = next((i for i, o in enumerate(out) if o['id'] == 'stage'), None)
        if at:
            out.insert(0, out.pop(at))
    return out


def system_badge(active: int, problems: int, fails: int) -> tuple[str, str]:
    """The System icon's badge: running jobs first (accent), else health
    problems (danger when anything failed, else warning), else none."""
    if active:
        return str(active), 'accent'
    if problems:
        return str(problems), 'danger' if fails else 'warning'
    return '', 'accent'


def system_status(jobs: list[dict], disks: list[dict] | None, checks: list[dict] | None,
                  proton: dict | None, crash: dict | None = None) -> dict:
    """The Home System widget's four tiles and the one thing (if any) that
    needs attention, from plain data. `jobs` are {title, state, step,
    progress (pct|None), active}; `disks` as from --library; `checks` from
    --doctor; `proton` the Proton page's info ({} while loading, None if the
    check never ran); `crash` a --report-pending row. Only a native crash
    (Nexus itself went down last time) makes a row, and it comes first."""
    tiles, attention = [], None
    running = [j for j in jobs if j.get('active')]
    failed = [j for j in jobs if j.get('state') == 'failed']
    if running:
        tiles.append(('activity', 'activity', f"{len(running)} running",
                      running[0].get('title') or '', 'accent'))
    elif failed:
        tiles.append(('activity', 'activity', f"{len(failed)} failed",
                      failed[-1].get('title') or '', 'danger'))
    else:
        tiles.append(('activity', 'activity', "Idle", "Nothing running", 'muted'))
    real = [d for d in disks or [] if d.get('total')]
    fullest = max(real, key=lambda d: d['used'] / d['total']) if real else None
    if fullest:
        used = fullest['used'] / fullest['total']
        name = Path(fullest.get('path') or '/').name or fullest.get('path') or '/'
        tiles.append(('storage', 'storage', f"{fmt_size(fullest['free'])} free",
                      f"{name} · {int(used * 100)}% used",
                      'danger' if used > 0.9 else 'warning' if used > DISK_FULL else 'text'))
    else:
        tiles.append(('storage', 'storage', "Measuring…", "Disk space", 'muted'))
    if not checks:
        tiles.append(('health', 'health', "Checking…", "Health", 'muted'))
        bad = []
    else:
        bad = sorted((c for c in checks if c.get('status') in ('fail', 'warn')),
                     key=lambda c: c['status'] != 'fail')
        if bad:
            fails = any(c['status'] == 'fail' for c in bad)
            tiles.append(('health', 'health', f"{len(bad)} issue{'s' if len(bad) != 1 else ''}",
                          bad[0].get('title') or '', 'danger' if fails else 'warning'))
        else:
            tiles.append(('health', 'health', "All good", f"{len(checks)} checks passed",
                          'success'))
    ge = ((proton or {}).get('families') or {}).get('ge') or {}
    if not has('proton'):               # no Proton page on Windows
        ge = {}
    elif not proton:
        tiles.append(('proton', 'proton', "Checking…", "Proton", 'muted'))
    elif ge.get('update'):
        tiles.append(('proton', 'proton', "Update", f"{ge.get('latest')} is out", 'warning'))
    else:
        tiles.append(('proton', 'proton', "Up to date", proton.get('default') or 'Proton',
                      'success'))
    if crash and crash.get('kind') == 'native-crash' and crash.get('id'):
        n = int(crash.get('count') or 1)
        attention = {'key': 'report', 'glyph': 'fail', 'role': 'danger',
                     'text': "Nexus closed unexpectedly" + (f" ({n} times)" if n > 1 else ""),
                     'sub': "Report it: you see every line before anything is sent",
                     'crash': crash['id']}
    elif running:
        j = running[0]
        pct = j.get('progress')
        attention = {'key': 'activity', 'glyph': 'spinner', 'role': 'accent',
                     'text': j.get('title') or '',
                     'sub': " · ".join(x for x in (f"{pct}%" if pct is not None else '',
                                                   j.get('step') or '') if x)}
    elif fullest and fullest['used'] / fullest['total'] > 0.9:
        attention = {'key': 'storage', 'glyph': 'storage', 'role': 'danger',
                     'text': f"{tiles[1][3].split(' · ')[0]} is almost full",
                     'sub': f"{fmt_size(fullest['free'])} left — see what can go"}
    elif bad and bad[0]['status'] == 'fail':
        attention = {'key': 'health', 'glyph': 'fail', 'role': 'danger',
                     'text': bad[0].get('title') or '', 'sub': bad[0].get('fix') or ''}
    elif ge.get('update'):
        attention = {'key': 'proton', 'glyph': 'download', 'role': 'warning',
                     'text': f"{ge.get('latest')} is out",
                     'sub': "Installs next to your current builds", 'action': 'install-ge',
                     'latest': ge.get('latest')}
    return {'tiles': [dict(zip(('key', 'glyph', 'value', 'caption', 'role'), t))
                      for t in tiles],
            'attention': attention}


def dash_cols(width: int) -> int:
    # Three at most: cards stay readable and the default layout packs evenly.
    return 3 if width >= 1100 else 2 if width >= 700 else 1


def dash_pack(items, cols: int, skip=()) -> list[tuple[str, int, int, int]]:
    """Row-wise flow of the visible widgets: (id, row, col, span). A widget
    that doesn't fit what's left of a row starts the next one, so the order
    you set is the order you read."""
    out, row, col = [], 0, 0
    for it in items:
        if it.get('hidden') or it['id'] in skip:
            continue
        span = min(DASH_SPAN.get(it['size'], 1), cols)
        if col and col + span > cols:
            row, col = row + 1, 0
        out.append((it['id'], row, col, span))
        col += span
        if col >= cols:
            row, col = row + 1, 0
    return out


def dash_move(items, wid: str, direction: str, cols: int, skip=()) -> list[dict]:
    """Move one widget: left/right swap it with its visible neighbour;
    up/down move it to the row above/below, under the same column."""
    items = [dict(it) for it in items]
    vis = [i for i, it in enumerate(items) if not it.get('hidden') and it['id'] not in skip]
    pos = next((i for i in vis if items[i]['id'] == wid), None)
    if pos is None:
        return items
    k = vis.index(pos)
    if direction in ('left', 'right'):
        j = k - 1 if direction == 'left' else k + 1
        if 0 <= j < len(vis):
            a, b = vis[k], vis[j]
            items[a], items[b] = items[b], items[a]
        return items
    packed = dash_pack(items, cols, skip)
    where = {i: (r, c, s) for i, r, c, s in packed}
    r, c, _s = where[wid]
    target_row = r - 1 if direction == 'up' else r + 1
    row_items = [(i, cc, ss) for i, rr, cc, ss in packed if rr == target_row]
    if not row_items:
        return items
    # The widget in that row under our column (or the nearest one).
    tid = min(row_items, key=lambda t: 0 if t[1] <= c < t[1] + t[2] else
              min(abs(t[1] - c), abs(t[1] + t[2] - 1 - c)) + 1)[0]
    me = items.pop(pos)
    t = next(i for i, it in enumerate(items) if it['id'] == tid)
    items.insert(t if direction == 'up' else t + 1, me)
    return items


# --------------------------------------------------------------------------
# Dashboard — the Stage and glanceable widgets on Home, laid out by the user
# --------------------------------------------------------------------------

class ClickRow(QFrame):
    """A focusable, clickable row with a real layout (a QPushButton ignores
    its child layout's height, so wrapped text in it collapses)."""
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("DashRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(ev)


class GlyphBadge(QWidget):
    """A widget's icon on a small gradient tile."""

    def __init__(self, glyph: str, grad: str, size: int = 28):
        super().__init__()
        self.glyph, self.grad = glyph, grad
        self.setFixedSize(size, size)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        a, b = (QColor(c) for c in GRAD[self.grad])
        g.setColorAt(0.0, a)
        g.setColorAt(1.0, b)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QBrush(g))
        pt.drawRoundedRect(r, R['sm'] + 2, R['sm'] + 2)
        f = QFont(FONTS.icon)
        f.setPixelSize(int(self.height() * 0.5))
        pt.setFont(f)
        pt.setPen(QColor(P['on_accent']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, self.glyph)
        pt.end()


class DashWidget(QFrame):
    """One dashboard card. Subclasses set the class attributes and fill
    `self.body` in fill(hub); `hub` is the main window (games, jobs, pages).
    Shelf widgets are unframed rows of tiles instead of cards."""
    KIND, TITLE, GLYPH, GRAD, LINK = '', '', '', 'dusk', None
    FRAMED = True
    go = pyqtSignal(str)                  # open a page
    edit = pyqtSignal(str, str)           # (kind, action) from the edit bar

    def __init__(self):
        super().__init__()
        self.setObjectName("DashCard" if self.FRAMED else "DashShelf")
        self.size_ = 'small'
        self.options: dict = {}           # its DASH_OPTIONS, set with the size by refresh
        lay = QVBoxLayout(self)
        pad = SP['4'] if self.FRAMED else 0
        lay.setContentsMargins(pad, pad - 2 if pad else 0, pad, pad)
        lay.setSpacing(SP['2_5'])
        self.editbar = self._edit_bar()
        self.editbar.setVisible(False)
        lay.addWidget(self.editbar)
        if self.FRAMED:
            head = QHBoxLayout()
            head.setSpacing(SP['2'])
            head.addWidget(GlyphBadge(self.GLYPH, self.GRAD))
            t = label(self.TITLE, "DashTitle")
            head.addWidget(t)
            head.addStretch()
            if self.LINK:
                more = QPushButton("Open")
                more.setObjectName("Ghost")
                more.setCursor(Qt.CursorShape.PointingHandCursor)
                more.clicked.connect(lambda: self.go.emit(self.LINK))
                head.addWidget(more)
            lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(SP['1_5'])
        lay.addLayout(self.body)
        if self.FRAMED:
            lay.addStretch()

    def _edit_bar(self):
        bar = QFrame()
        bar.setObjectName("DashEditBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(SP['2'], SP['1'], SP['1'], SP['1'])
        bl.setSpacing(SP['1'])
        self.edit_name = label(self.TITLE, "StatLabel")
        bl.addWidget(self.edit_name)
        bl.addStretch()
        for glyph, act, tip in ((G['arrow_left'], 'left', "Move left"), (G['arrow_up'], 'up', "Move up"),
                                (G['arrow_down'], 'down', "Move down"),
                                (G['arrow_right'], 'right', "Move right")):
            b = icon_button(glyph, tip, 28, "Ghost", round_=True)
            b.clicked.connect(lambda _=False, a=act: self.edit.emit(self.KIND, a))
            bl.addWidget(b)
        self.btn_size = QPushButton()
        self.btn_size.setObjectName("Ghost")
        self.btn_size.setToolTip("Change size")
        self.btn_size.clicked.connect(lambda: self.edit.emit(self.KIND, 'size'))
        bl.addWidget(self.btn_size)
        self.btn_shelf = QPushButton()            # the smart widget: which shelf it shows
        self.btn_shelf.setObjectName("Ghost")
        self.btn_shelf.setToolTip("Change which smart shelf this shows")
        self.btn_shelf.clicked.connect(lambda: self.edit.emit(self.KIND, 'shelf'))
        self.btn_shelf.setVisible(False)
        bl.addWidget(self.btn_shelf)
        hide = icon_button(G['eye_off'], "Hide this widget", 28, "Ghost", round_=True)
        hide.clicked.connect(lambda: self.edit.emit(self.KIND, 'hide'))
        bl.addWidget(hide)
        return bar

    def set_editing(self, on: bool, size: str, shelf: str | None = None):
        self.size_ = size
        self.editbar.setVisible(on)
        sizes = DASH_SIZES.get(self.KIND, ('small',))
        self.btn_size.setVisible(len(sizes) > 1)
        self.btn_shelf.setVisible(shelf is not None)
        if shelf is not None:
            self.btn_shelf.setText(f"Shelf: {shelf}")
        self.btn_size.setText({'small': "Small", 'wide': "Wide", 'full': "Full width",
                               'cinematic': "Cinematic", 'banner': "Banner"}[size])
        self.setProperty('editing', on)
        self.style().unpolish(self)
        self.style().polish(self)

    def bind(self, hub):
        """Wire to the window once, when the dashboard is built (the Stage
        hooks into Home's view); most widgets need nothing."""

    def has_content(self, hub) -> bool:
        """Shelf widgets with nothing to show stay out of the layout."""
        return True

    def fill(self, hub):
        pass

    def clear(self):
        clear_layout(self.body)

    def _error(self, message: str, retry) -> bool:
        """A failed load: say why, with Retry (instead of "Loading…" that
        never ends). False when there's no error to show."""
        if not message:
            return False
        self.body.addWidget(label(f"Couldn't load — {message}", "Muted", wrap=True))
        b = QPushButton("Retry")
        b.clicked.connect(retry)
        self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        return True

    # small row helpers shared by the card widgets
    def row(self, glyph: str, text: str, sub: str = '', color: str | None = None,
            on_click=None) -> QWidget:
        w = ClickRow() if on_click else QWidget()
        if on_click:
            w.clicked.connect(on_click)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2'])
        ic = icon_label(glyph, 13, color or P['muted'])
        ic.setFixedWidth(18)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = label(text, None, wrap=True)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        t.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        col.addWidget(t)
        if sub:
            sl = label(sub, "Muted", wrap=True)
            sl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            col.addWidget(sl)
        lay.addLayout(col, 1)
        return w

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self.FRAMED:
            return
        # A soft glow of the widget's gradient in its top-left corner.
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), R['card'], R['card'])
        pt.setClipPath(clip)
        g = QRadialGradient(0.0, 0.0, float(max(160, self.width() * 0.6)))
        c = QColor(GRAD[self.GRAD][0]); c.setAlphaF(0.10)
        e = QColor(c); e.setAlphaF(0.0)
        g.setColorAt(0.0, c)
        g.setColorAt(1.0, e)
        pt.fillRect(self.rect(), QBrush(g))
        pt.end()


class ShelfWidget(DashWidget):
    """A row of game tiles (Recently installed, Favourites, …)."""
    FRAMED = False
    KIND_TILE = 'cover'
    LINK_CHIP = None                      # (chip, sort) for "See all"

    def games(self, hub) -> list[dict]:
        return []

    def has_content(self, hub):
        return bool(self.games(hub))

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):           # forget the old row's tiles
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        games = self.games(hub)
        if games:
            row = view.make_shelf(self.TITLE, games, self.KIND_TILE, self.LINK_CHIP)
            self._tiles = row.tiles
            self.body.addWidget(row)


def _visible_games(hub):
    """What Home and its widgets are about: installed, not hidden, and one
    copy of a game you have in several stores."""
    return [g for g in hub._games if not g.get('hidden') and is_installed(g)
            and not g.get('dup_of')]


DUP_ORDER = ('nexus', 'steam', 'heroic', 'epic')


def _game_store_id(g: dict) -> int | None:
    if g.get('source') == 'steam' and str(g.get('appid', '')).isdigit():
        return int(g['appid'])
    # A store source (nexus/sources) that knows the game's Steam appid.
    if str(g.get('store_appid') or '').isdigit() and 0 < int(g['store_appid']) < 0x80000000:
        return int(g['store_appid'])
    v = str(g.get('umu_gameid') or '').removeprefix('umu-')
    return int(v) if v.isdigit() and 0 < int(v) < 0x80000000 else None


def mark_duplicates(games: list[dict], enabled: bool = True) -> None:
    """The same game from more than one source (a repack and a Steam copy,
    an Epic and a Steam one…) keeps one tile: installed beats owned, then
    repack > Steam > Heroic > Epic, then more play time. The others get
    `dup_of` (the kept slug); the kept one lists the others' sources in
    `dup_also`. Matched by Steam store id (a source's `store_appid` counts),
    else by name."""
    for g in games:
        g.pop('dup_of', None)
        g.pop('dup_also', None)
    if not enabled:
        return
    groups: dict = {}
    for g in games:
        sid = _game_store_id(g)
        name = _search_norm(g.get('store_name') or g.get('display_name') or '')
        key = ('id', sid) if sid else ('name', name) if name else None
        if key:
            groups.setdefault(key, []).append(g)
    # A store id and a name can describe the same game: fold name-only
    # groups into the id group with that name.
    by_name = {}
    for key, members in list(groups.items()):
        if key[0] == 'id':
            for m in members:
                by_name.setdefault(_search_norm(m.get('store_name') or m.get('display_name')
                                                or ''), key)
    for key in [k for k in groups if k[0] == 'name']:
        if key[1] in by_name and by_name[key[1]] in groups:
            groups[by_name[key[1]]] += groups.pop(key)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda g: (not is_installed(g),
                                    DUP_ORDER.index(g.get('source', 'nexus'))
                                    if g.get('source', 'nexus') in DUP_ORDER else 9,
                                    -(g.get('playtime_minutes') or 0)))
        keep = members[0]
        keep['dup_also'] = sorted({m.get('source', 'nexus') for m in members[1:]}
                                  - {keep.get('source', 'nexus')})
        for m in members[1:]:
            m['dup_of'] = keep['slug']


class StageArt(FollowHero):
    """The Stage's art (w-85): one game's art, its logo, facts and Play,
    moved only by the Continue row (focus, or the mouse resting on a tile).
    A running or starting game is pinned with "Now playing". Its size sets
    the logo's height; Banner has no fact pills."""
    LOGO_H = {'cinematic': 96, 'wide': 96, 'banner': 80}

    def __init__(self):
        super().__init__()
        self.setObjectName("StageArt")
        self.setMinimumHeight(0)
        # Cinematic holds the Continue row too: less air above, so it all
        # fits the 480 px floor (stage_height) without squeezing the row.
        m = self.layout().contentsMargins()
        self.layout().setContentsMargins(m.left(), SP['5'], m.right(), SP['5'])
        self.size_ = 'cinematic'
        self.kicker.setTextFormat(Qt.TextFormat.RichText)   # live_kicker's &nbsp;

    def set_size(self, size: str):
        if size == self.size_:
            return
        self.size_ = size
        if self.games:
            self._show(animate=False)

    def _show(self, animate: bool):
        g = self.game
        # Disabling the focused Play (its game just started) would throw focus
        # somewhere else on the page: Details takes it first.
        if self.btn.hasFocus() and not play_enabled(g):
            self.btn_details.setFocus(Qt.FocusReason.OtherFocusReason)
        super()._show(animate)
        if self.is_pinned():
            self.kicker.setText(live_kicker(g))

    def tick(self):
        """The minute changed: "NOW PLAYING · 42 MIN" moves on."""
        if self.is_pinned():
            self.kicker.setText(live_kicker(self.game))

    def _fill_facts(self, g: dict):
        if self.size_ == 'banner':
            clear_layout(self.facts)
            return
        super()._fill_facts(g)

    def _logo(self, path):
        h = self.LOGO_H.get(self.size_, 96)
        try:
            key = (path, Path(path).stat().st_mtime, self.scale, h)
        except OSError:
            return QPixmap()
        if key not in self._logos:
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(h * 3, h, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            if len(self._logos) > 8:
                self._logos.clear()
            self._logos[key] = pm
        return self._logos[key]


class StageWidget(DashWidget):
    """The top of Home (w-85): one game's art at Cinematic, Wide or Banner
    size, and a Continue row of the most recently played games. Focus on a
    tile (pad, keyboard) or the mouse resting on one moves the stage there;
    nothing moves on its own. The row lies on the art at Cinematic and under
    it otherwise. It's kept across fills, so moving the stage only repaints
    the art and never takes focus off the row."""
    KIND, TITLE = 'stage', "Stage"
    FRAMED = False
    FOCUS_DWELL, HOVER_DWELL = 140, 350
    RECENT = 8

    def __init__(self):
        super().__init__()
        self.art = StageArt()
        self.body.addWidget(self.art)
        self.row: 'ShelfRow | None' = None
        self._view = None
        self._vh = 850                           # the Home viewport's height
        self._placed = None                      # the size the parts are laid out for
        self._dwell = Dwell(self.FOCUS_DWELL, self._follow, self)
        self._clock = QTimer(self)               # Now playing's minutes, while pinned
        self._clock.setInterval(20000)
        self._clock.timeout.connect(self.art.tick)

    def bind(self, hub):
        view = self._view = hub.homeview
        self.art.play.connect(view.play)
        self.art.open.connect(view.activated)
        self.art.menu.connect(view.menu)
        row = self.row = view.make_shelf("Continue", [], 'wide')
        self.art.layout().addWidget(row)         # parented before anything shows it
        row.focused.connect(self._row_focus)
        row.hovered.connect(self._row_hover)
        row.unhovered.connect(self._row_unhover)
        view.viewport().installEventFilter(self)
        self._place(self.size_ if self.size_ in DASH_SIZES['stage'] else 'cinematic')

    def has_content(self, hub):
        return bool(_visible_games(hub))

    def fill(self, hub):
        games = _visible_games(hub)
        self._place(self.size_)
        self.art.set_games(featured_games(games, max(1, len(games))))
        self.art.setVisible(bool(self.art.games))
        if self.art.is_pinned():
            if not self._clock.isActive():
                self._clock.start()
        else:
            self._clock.stop()
        if self.row is None:
            return
        recent = sorted((g for g in games if g.get('last_played')),
                        key=lambda g: g['last_played'], reverse=True)[:self.RECENT]
        view, row = hub.homeview, self.row
        view._untrack(row)
        row.set_games(recent, lambda g: view._tile(g, row.tile_w, row.kind))
        view._track(row)
        row.setVisible(bool(recent))

    def set_editing(self, on: bool, size: str, shelf: str | None = None):
        super().set_editing(on, size, shelf)
        self._place(size)

    # ---- size ----
    def _place(self, size: str):
        """The row on the art (Cinematic) or under it; the art's height."""
        self.size_ = size
        if size != self._placed and self.row is not None:
            self._placed = size
            row, on_art = self.row, size == 'cinematic'
            f = QApplication.focusWidget()
            had = f if f is not None and row.isAncestorOf(f) else None
            if row.parentWidget() is not (self.art if on_art else self):
                (self.body if on_art else self.art.layout()).removeWidget(row)
                (self.art.layout() if on_art else self.body).addWidget(row)
            row.layout().setContentsMargins(0, SP['3'] if on_art else SP['2'], 0, 0)
            for fade in row.fades.values():
                fade.role = 'scrim' if on_art else 'bg'
            for lab in (row.name_label, row.count_label):
                if lab is not None:
                    lab.style().unpolish(lab)
                    lab.style().polish(lab)
            if had is not None and alive(had):
                had.setFocus(Qt.FocusReason.OtherFocusReason)
        self.art.set_size(size)
        self._sync_height()

    def _sync_height(self):
        self.art.setFixedHeight(stage_height(self.size_, self._vh))

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and self._view is not None \
                and obj is self._view.viewport():
            self._vh = ev.size().height()
            self._sync_height()
        return False

    # ---- the row moves the stage ----
    def _row_focus(self, tile):
        self._dwell.push(tile.game, self.FOCUS_DWELL)

    def _row_hover(self, tile):
        self._dwell.push(tile.game, self.HOVER_DWELL)

    def _row_unhover(self, tile):
        if self._dwell.timer.isActive() and self._dwell.value is tile.game:
            self._dwell.cancel()

    def _follow(self, g):
        if g is not None and alive(self):
            self.art.show_game(g)


class InstalledWidget(ShelfWidget):
    KIND, TITLE, LINK_CHIP = 'installed', "Recently installed", ('all', 'added')

    def games(self, hub):
        return sorted((g for g in _visible_games(hub) if g.get('added_at')),
                      key=lambda g: g['added_at'], reverse=True)[:12]

    def fill(self, hub):
        super().fill(hub)
        for t in self._tiles:            # never played yet: what "Not played yet" was for
            t.mark_new = not t.game.get('last_played')
            t.update()


class FavouritesWidget(ShelfWidget):
    KIND, TITLE, LINK_CHIP = 'favourites', "Favourites", ('favorite', 'name')

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('favorite')]


class CollectionsWidget(ShelfWidget):
    """One row per collection that has an installed game in it."""
    KIND, TITLE = 'collections', "Collections"

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('collections')]

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        games = _visible_games(hub)
        for name in COLLECTION_NAMES:
            items = [g for g in games if name in (g.get('collections') or ())]
            if items:
                row = view.make_shelf(name, items, self.KIND_TILE, (f'col:{name}', 'name'),
                                      look=collection_look(name))
                self._tiles += row.tiles
                self.body.addWidget(row)


class SmartShelfWidget(ShelfWidget):
    """One smart shelf on Home (nexus/smart.py), "Unfinished" unless chosen
    otherwise in Customise. Gone while that shelf is empty."""
    KIND, TITLE = 'smart', "Smart shelf"

    def key(self, hub) -> str:
        """`smart:<rule>`, or `user:<id>` while that shelf of yours exists."""
        it = next((i for i in hub.prefs.dashboard if i['id'] == self.KIND), None)
        shelf = _dash_shelf((it or {}).get('shelf'))
        if smart.is_user(shelf):
            if _user_shelf(hub, shelf):
                return shelf
            shelf = smart.WIDGET_DEFAULT
        return f'smart:{shelf}'

    def games(self, hub):
        get = getattr(hub, '_smart_facts', None)
        facts, key = get() if get else {}, self.key(hub)
        if smart.is_user(key):
            return smart.user_games(_user_shelf(hub, key), _visible_games(hub), facts,
                                    friends=FRIENDS.public_ids())[:24]
        return smart.games_for(key, _visible_games(hub), facts, linux=has('proton'))[:24]

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        games = self.games(hub)
        if games:
            key = self.key(hub)
            mine = _user_shelf(hub, key)
            row = view.make_shelf(smart.title_of(mine) if mine else smart.label(key), games,
                                  self.KIND_TILE, smart=key,
                                  tip=smart.describe(mine, names=FRIENDS.names()) if mine
                                  else smart.tip(key))
            row.btn_menu.hide()                # hide/save live in the Library; Customise here
            self._tiles = row.tiles
            self.body.addWidget(row)


class SurpriseWidget(DashWidget):
    """"Can't decide?" — one button that picks a game for you."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'surprise', "Surprise me", G['dice'], 'dawn', None

    def has_content(self, hub):
        return bool(_visible_games(hub))

    def fill(self, hub):
        self.clear()
        row = QHBoxLayout()
        row.setSpacing(SP['3'])
        t = label("Can't decide? Nexus picks one of your games — ones you haven't played "
                  "in a while and favourites come up more often.", "Muted", wrap=True)
        row.addWidget(t, 1)
        b = QPushButton("Pick a game")
        b.setObjectName("Primary")
        set_glyph_icon(b, G['dice'], 16, 'on_accent', hover_role=None)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda: hub.surprise())
        row.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(row)


class UpdatesWidget(ShelfWidget):
    KIND, TITLE = 'updates', "Updates ready"

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('update_pending')]


class OwnedWidget(ShelfWidget):
    """Games you own on Steam / Epic / GOG but haven't installed: the ones
    downloading first, then the ones you've played most."""
    KIND, TITLE, LINK_CHIP = 'owned', "In your library", ('owned', 'most')

    def games(self, hub):
        owned = [g for g in hub._games if not g.get('hidden') and not is_installed(g)]
        owned.sort(key=lambda g: (not g.get('installing'), -(g.get('playtime_minutes') or 0)))
        return owned[:24]


def job_facts(jobs) -> list[dict]:
    """JobManager jobs as the plain dicts system_status() reads."""
    out = []
    for j in jobs:
        prog = getattr(j, 'progress', None)
        pct = (int(100 * min(prog[0], prog[1]) / prog[1])
               if j.state == 'running' and prog and prog[1] else None)
        out.append({'title': j.title, 'state': j.state, 'step': j.step, 'progress': pct,
                    'active': j.active, 'job': j})
    return out


class SysTile(ClickRow):
    """One clickable status tile in the System widget: icon, value, caption."""

    def __init__(self, t: dict):
        super().__init__()
        self.setObjectName("SysTile")
        self.key = t['key']
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['2_5'], SP['2'], SP['2_5'], SP['2'])
        lay.setSpacing(SP['2_5'])
        role = t['role']
        color = P['muted'] if role in ('muted', 'text') else P[role]
        ic = icon_label(G[t['glyph']], 18, color)
        ic.setFixedWidth(22)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.value = ElideLabel(t['value'], "StatValue")
        if role not in ('muted', 'text'):
            self.value.setStyleSheet(f"color: {P[role]};")
        col.addWidget(self.value)
        self.caption = ElideLabel(t['caption'], "StatLabel")
        col.addWidget(self.caption)
        lay.addLayout(col, 1)
        for w in (ic, self.value, self.caption):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip(f"{t['value']} — {t['caption']}")


class SystemWidget(DashWidget):
    """Jobs, disk space, health and Proton at a glance — four tiles, each
    opening its part of the System page (Proton: the Proton page). Wide adds
    the one thing that needs you, if anything does."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'system', "System", G['system'], 'tide', 'system'

    def status(self, hub) -> dict:
        proton = getattr(getattr(hub, 'protonpage', None), 'info', None)
        return system_status(job_facts(hub.jobs.jobs), hub._disks,
                             getattr(hub.health, 'checks', []), proton,
                             getattr(hub, '_native_crash', None))

    def fill(self, hub):
        st = self.status(hub)
        sig = (self.size_, repr([(t['value'], t['caption'], t['role']) for t in st['tiles']]),
               repr({k: v for k, v in (st['attention'] or {}).items() if k != 'job'}))
        if sig == getattr(self, '_sig', None):
            return                  # unchanged: keep the widgets (and pad focus) as they are
        self._sig = sig
        self.clear()
        grid = QGridLayout()
        grid.setSpacing(SP['2'])
        cols = 4 if self.size_ == 'wide' else 2
        self.tiles = []
        for i, t in enumerate(st['tiles']):
            tile = SysTile(t)
            tile.clicked.connect(lambda k=t['key']: hub.go(k))
            self.tiles.append(tile)
            grid.addWidget(tile, i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        self.body.addLayout(grid)
        att = st['attention']
        if att and self.size_ == 'wide':
            if att['key'] == 'activity':
                job = next((j['job'] for j in job_facts(hub.jobs.jobs) if j['active']), None)
                go = (lambda _=False, j=job: hub._open_job(j)) if job else None
            elif att['key'] == 'report':
                go = lambda _=False, c=att['crash']: hub.open_report(c)
            else:
                go = lambda _=False, k=att['key']: hub.go(k)
            line = QHBoxLayout()
            line.addWidget(self.row(G[att['glyph']], att['text'], att['sub'], P[att['role']],
                                    go), 1)
            if att.get('action') == 'install-ge':
                b = QPushButton(f"Install {att['latest']}")
                b.setObjectName("Primary")
                b.clicked.connect(lambda: hub.protonpage._install('latest'))
                line.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
            self.body.addLayout(line)


class NewsWidget(DashWidget):
    KIND, TITLE, GLYPH, GRAD, LINK = 'news', "News", G['news'], 'aurora', 'news'

    def fill(self, hub):
        self.clear()
        if not hub.config.get('news', True):
            self.body.addWidget(label("The News page is off (Settings).", "Muted", wrap=True))
            return
        items = (getattr(hub.newspage, 'data', {}) or {}).get('items', [])
        if not items:
            if not self._error(hub.newspage.error, lambda: hub.newspage.refresh(force=True)):
                self.body.addWidget(label("Loading headlines…", "Muted"))
                hub.newspage.refresh()            # once; cached by the CLI
            return
        n = {'small': 3, 'wide': 4, 'full': 6}[self.size_]
        for it in items[:n]:
            self.body.addWidget(self._story_row(it, hub))

    def _story_row(self, it, hub):
        """Thumbnail, headline, source · time; opens the story in the News
        page's reader."""
        w = ClickRow()
        w.clicked.connect(lambda: (hub.go(hub.NEWS), hub.newspage.open_item(it)))
        lay = QHBoxLayout(w)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2_5'])
        art = NewsArt(it, 64, 36, R['sm'] - 2, hub.newspage._image_for)
        art.load()                       # a handful of rows: fine to load now
        lay.addWidget(art, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = plain_label(it.get('title', ''), None, wrap=True)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        col.addWidget(t)
        role = news_style(it)[0]
        sub = label(" · ".join(x for x in (it.get('source'), fmt_relative(it.get('date')))
                               if x), None)
        sub.setStyleSheet(f"color: {P[role]}; font-size: {FS['2xs']}px;")
        col.addWidget(sub)
        lay.addLayout(col, 1)
        for x in (t, sub):
            x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        return w


class DealsWidget(DashWidget):
    KIND, TITLE, GLYPH, GRAD, LINK = 'deals', "Wishlist deals", G['deals'], 'dawn', 'deals'

    def fill(self, hub):
        self.clear()
        if not hub.config.get('gamedb_enabled', False):
            self.body.addWidget(label("Connect Game-db in Settings to see deals on your "
                                      "Steam wishlist.", "Muted", wrap=True))
            b = QPushButton("Open Settings")
            b.clicked.connect(lambda: hub._open_settings('accounts', 'deals'))
            self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
            return
        d = hub.dealspage._data()
        items = [g for g in d.get('wishlist') or [] if (g.get('price') or {}).get('onSale')]
        if not d:
            if not self._error(hub.dealspage.error, hub.dealspage.retry):
                self.body.addWidget(label("Loading deals…", "Muted"))
                hub.dealspage.refresh()
            return
        if not items:
            self.body.addWidget(label("Nothing on your wishlist is on sale right now.",
                                      "Muted", wrap=True))
            return
        items.sort(key=lambda g: -((g.get('price') or {}).get('cutPct') or 0))
        n = {'small': 3, 'wide': 4, 'full': 6}[self.size_]
        for g in items[:n]:
            price = g.get('price') or {}
            cur = label(price.get('currentStr') or '', None)
            cur.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px; font-weight: 600;")
            right = [cur] + ([_pill(f"-{price['cutPct']}%", 'accent')]
                             if price.get('cutPct') else [])
            self.body.addWidget(game_row(g, right, lambda: hub.go(hub.DEALS)))
        radar = d.get('radar') or {}
        if radar.get('onSaleCount'):
            self.body.addWidget(label(f"{radar['onSaleCount']} on sale · "
                                      f"save ~{radar.get('totalSavings', 0):.0f} "
                                      f"{d.get('currency') or ''}", "Muted"))


def game_row(g: dict, right: list, on_click, sub: str = '') -> ClickRow:
    """A dashboard row for a store game: header thumbnail (fetched once,
    cached), title (and an optional muted line), then `right` widgets."""
    w = ClickRow()
    w.clicked.connect(on_click)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
    lay.setSpacing(SP['2_5'])
    thumb = QLabel()
    thumb.setFixedSize(64, 30)
    thumb.setStyleSheet(f"background: {P['surface_2']}; border-radius: {R['sm'] - 2}px;")
    url = gamedb.safe_url(g.get('headerImage'))

    def show(pm, thumb=thumb):
        try:
            thumb.setPixmap(pm)
            thumb.setStyleSheet("")
        except RuntimeError:                     # the row is gone
            pass

    def put(path):
        ART.pixmap_async(path, 64, 30, R['sm'] - 2, show)
    if url:
        cached = ART.cached(url)
        if cached:
            put(cached)
        else:
            ART.fetch(url, put)
    lay.addWidget(thumb)
    col = QVBoxLayout()
    col.setSpacing(0)
    t = plain_label(g.get('title') or '', None)
    t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
    t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    col.addWidget(t)
    parts = [thumb, t]
    w.sub_label = None
    if sub:
        sl = w.sub_label = label(sub, "Muted")
        sl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(sl)
        parts.append(sl)
    lay.addLayout(col, 1)
    for x in right:
        lay.addWidget(x)
        parts.append(x)
    for x in parts:
        x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return w


class PresenceCard(ClickRow):
    """One game friends are in right now (Friends playing now, full size):
    the Steam header art with their avatars over its corner, the game and
    an "In your library" pill, then who and how long."""
    W = 248

    def __init__(self, entry: dict):
        super().__init__()
        self.appid = entry['appid']
        self.setFixedWidth(self.W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['1_5'], SP['1_5'], SP['1_5'], SP['2'])
        lay.setSpacing(SP['1'])
        aw = self.W - 2 * SP['1_5']
        ah = int(aw * 215 / 460)                   # Steam's header is 460 x 215
        self.art = DealArt(aw, ah, R['sm'])
        self.art.round_all = True
        self.art.set_url(entry.get('headerImage'))
        lay.addWidget(self.art)
        d = 30
        self.stack = AvatarStack(entry['people'], d, 3, ring='surface', parent=self.art)
        self.stack.move(SP['2'], ah - d - SP['2'])
        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        self.title = plain_label(entry['title'], None)
        self.title.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px; "
                                 f"font-weight: 600;")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setFixedHeight(PILL_H)             # the same with or without the pill
        row.addWidget(self.title, 1)
        self.pill = _pill("In your library", 'accent') if entry.get('slug') else None
        if self.pill is not None:
            row.addWidget(self.pill)
        lay.addLayout(row)
        self.sub_label = label(who_text(entry), "Muted")
        self.sub_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.sub_label)
        for x in (self.title, self.sub_label, *([self.pill] if self.pill else [])):
            x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip("Open its page" if entry.get('slug') else
                        "Not in your library · opens its Steam page ↗")


class FriendsWidget(DashWidget):
    """Friends playing now (w-82): one card per game a Steam friend is in,
    the most friends first (full: a strip of cards, wide: rows). In the
    layout only while somebody plays (or Steam refused the key); Customise
    shows its other states: set up, looking, nobody in a game, all
    private. Presence is asked by the window (_sync_presence); this only
    reads FRIENDS."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'friends', "Friends playing now", G['friends'], 'aurora', None
    WIDE_ROWS = 4
    PRIVATE_TIP = ("Private: their game details aren't public on Steam. “Friends only” "
                   "counts as private for apps.")

    def __init__(self):
        super().__init__()
        self._sig = None
        self._filling = False
        self.state = ''
        self.cards: list[ClickRow] = []
        self._subs: dict[int, QLabel] = {}
        self.footer: QLabel | None = None

    @staticmethod
    def _entries(hub, limit: int | None = 0):
        """playing_now over the library; limit 0: MAX_CARDS, None: all."""
        return playing_now(getattr(hub, '_games', None) or (),
                           limit=MAX_CARDS if limit == 0 else limit)

    def has_content(self, hub):
        if not hub.config.get('friends_enabled') or not FRIENDS.usable():
            return False
        return FRIENDS.error_kind() == 'bad-key' or bool(self._entries(hub))

    def _state_of(self, hub) -> str:
        if not hub.config.get('friends_enabled') or (
                FRIENDS.data is not None and not FRIENDS.usable()):
            return 'setup'
        if FRIENDS.data is None:
            return 'loading'
        if FRIENDS.error_kind() == 'bad-key':
            return 'bad-key'
        if not FRIENDS.friends():
            return 'none'
        if not FRIENDS.public():
            return 'private'
        return 'live'

    def fill(self, hub):
        if self._filling:
            return
        self._filling = True
        try:
            self._fill(hub)
        finally:
            self._filling = False

    def _fill(self, hub):
        state = self._state_of(hub)
        entries = self._entries(hub, None if self.size_ == 'wide' else MAX_CARDS) \
            if state == 'live' else []
        sig = (self.size_, state, len(FRIENDS.private()),
               tuple((e['appid'], e['slug'], e['title'],
                      tuple((r['steamid'], r.get('name'), r.get('avatar'))
                            for r in e['people'])) for e in entries))
        if sig == self._sig:
            for e in entries:                       # only the minutes moved on
                sub = self._subs.get(e['appid'])
                if sub is not None:
                    sub.setText(who_text(e))
            self._set_footer()
            return
        self._sig = sig
        f = QApplication.focusWidget()
        focus_appid = next((c.appid for c in self.cards if c is f), None)
        self.clear()
        self.cards, self._subs, self.footer, self.state = [], {}, None, state
        if state == 'setup':
            self.body.addWidget(label("See what your Steam friends are playing and who owns "
                                      "your games. Nexus only reads what Steam shows; it "
                                      "shares nothing.", "Muted", wrap=True))
            b = QPushButton("Set up in Settings")
            b.clicked.connect(lambda: hub._open_settings('accounts', 'friends'))
            self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
            return
        if state == 'loading':
            self.body.addWidget(label("Looking for your friends…", "Muted"))
            return
        if state == 'bad-key':
            self._error("Steam refused the key", hub._reload_friends)
            return
        if state == 'none':
            self.body.addWidget(label("No friends on this Steam account.", "Muted"))
            return
        if state == 'private':
            private = FRIENDS.private()
            self.body.addWidget(AvatarStack(private, 28, 5, ring='surface'))
            n = len(private)
            self.body.addWidget(label(
                f"None of your {n} friend{'s' if n != 1 else ''} show{'s' if n == 1 else ''} "
                "their game details on Steam, so Nexus can't see what they play.",
                "Muted", wrap=True))
            self.body.addWidget(label("A friend can change it under Steam › Edit Profile › "
                                      "Privacy Settings › Game details: Public.", "Muted",
                                      wrap=True))
            return
        if not entries:
            self.body.addWidget(label("No friends are in a game right now.", "Muted"))
            self._set_footer(new=True)
            return
        if self.size_ == 'full':
            host = QWidget()
            flow = FlowLayout(host, margin=0, spacing=SP['3'])
            for e in entries:
                card = PresenceCard(e)
                card.clicked.connect(lambda e=e: self._open(hub, e))
                flow.addWidget(card)
                self.cards.append(card)
                self._subs[e['appid']] = card.sub_label
            self.body.addWidget(host)
        else:
            for e in entries[:self.WIDE_ROWS]:
                right = [AvatarStack(e['people'], 22, 3, ring='surface')]
                if e['slug']:
                    right.append(_pill("In your library", 'accent'))
                row = game_row({'title': e['title'], 'headerImage': e['headerImage']}, right,
                               lambda e=e: self._open(hub, e), sub=who_text(e))
                row.appid = e['appid']
                row.setToolTip("Open its page" if e['slug'] else
                               "Not in your library · opens its Steam page ↗")
                self.body.addWidget(row)
                self.cards.append(row)
                self._subs[e['appid']] = row.sub_label
            if len(entries) > self.WIDE_ROWS:
                self.body.addWidget(label(f"+{len(entries) - self.WIDE_ROWS} more", "Muted"))
        self._set_footer(new=True)
        card = next((c for c in self.cards if c.appid == focus_appid), None) \
            if focus_appid is not None else None
        if card is not None:
            QTimer.singleShot(0, lambda c=card: alive(c) and focus_and_reveal(c))

    def footer_text(self) -> str:
        """'3 friends are private on Steam · updated 1 min ago'; 'as of
        14:02' when Steam isn't answering (the cards are its last word)."""
        n = len(FRIENDS.private())
        parts = [f"{n} friend{'s are' if n != 1 else ' is'} private on Steam"] if n else []
        t = _ts((FRIENDS.data or {}).get('updated'))
        if t is not None:
            if FRIENDS.error_kind() in ('offline', 'rate-limited', 'private-list'):
                parts.append(f"as of {datetime.fromtimestamp(t).strftime('%H:%M')}")
            else:
                parts.append(f"updated {fmt_ago(t)}")
        text = " · ".join(parts)
        return text[:1].upper() + text[1:]

    def _set_footer(self, new: bool = False):
        if new:
            self.footer = label("", "Muted")
            self.footer.setToolTip(self.PRIVATE_TIP)
            self.body.addWidget(self.footer)
        if self.footer is not None:
            self.footer.setText(self.footer_text())

    @staticmethod
    def _open(hub, entry: dict):
        """A game you own: its page. One you don't: its Steam store page."""
        if entry.get('slug'):
            hub._reveal_game(entry['slug'])
        else:
            open_url(storeinfo.STORE_PAGE.format(appid=entry['appid']))


def open_url(url: str):
    """The browser (a function of its own, so tests can stand in for it)."""
    QDesktopServices.openUrl(QUrl(url))


class UpcomingWidget(DashWidget):
    """Unreleased games on your wishlist, soonest first, with the cheapest
    current price (keyshops included) — from the Game-db summary the Deals
    page already holds."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'upcoming', "Upcoming", G['calendar'], 'aurora', 'upcoming'

    def has_content(self, hub):
        # Off without Game-db: the Deals widget already says how to connect it.
        if not hub.config.get('gamedb_enabled', False):
            return False
        return not hub.dealspage._loaded or bool(gamedb.upcoming(hub.dealspage._data()))

    def fill(self, hub):
        self.clear()
        d = hub.dealspage._data()
        if not d:
            if not self._error(hub.dealspage.error, hub.dealspage.retry):
                self.body.addWidget(label("Loading releases…", "Muted"))
                hub.dealspage.refresh()
            return
        items = gamedb.upcoming(d)
        if not items:
            self.body.addWidget(label("Nothing unreleased on your wishlist.", "Muted",
                                      wrap=True))
            return
        n = {'small': 3, 'wide': 4, 'full': 6}[self.size_]
        for g in items[:n]:
            text, preorder = gamedb.price_line(g)
            when = label(gamedb.release_label(g), None)
            when.setStyleSheet(f"color: {P['accent']}; font-size: {FS['xs']}px; "
                               f"font-weight: 600;")
            chips = gamedb.release_chips(g)
            if any(t == "Out now" for t, _r in chips):
                when.setVisible(False)                  # the chip says it
            right = [_pill(t, role) for t, role in chips[:1 if self.size_ == 'small' else 2]]
            right += [_pill("Pre-order", 'warning')] if preorder else []
            base = hub.dealspage._base()
            url = g.get('steamUrl') or (gamedb.detail_url(base, g['appid'])
                                        if base and g.get('appid') else '')
            row = game_row(g, [when] + right, lambda _=False, u=url: hub.dealspage._open(u),
                           sub=text)
            row.setToolTip(" · ".join(x for x in (g.get('releaseDateText'), text,
                                                  *(t for t, _r in chips)) if x))
            if gamedb.is_cancelled(g):
                dim(row)
            self.body.addWidget(row)
        if len(items) > n:
            self.body.addWidget(label(f"+{len(items) - n} more on the Deals page", "Muted"))


class WeekWidget(DashWidget):
    """Your week (w-87): this week's play time, how many games and Monday to
    Sunday as bars, today bright; wide adds the week's three most played.
    Style 'number' leaves the chart out. The big charts are on Play history
    (Open). From sessions.jsonl via nexus-cli --history."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'week', "Your week", G['clock'], 'dusk', 'history'

    def __init__(self):
        super().__init__()
        self._hist: dict | None = None
        self._asked = None                   # monotonic; None: never
        self._filling = False
        self._sig = None

    def fill(self, hub):
        self._filling = True
        try:
            # Ask first: a cached answer comes back right here, inside fill.
            if self._hist is None or time.monotonic() - self._asked > 300:
                self._asked = time.monotonic()
                cli_json(['--history', '--json'],
                         lambda d, e, hub=hub: self._arrived(hub, d, e), 30000, ttl=60)
            per = (self._hist or {}).get('periods') or {}
            style = self.options.get('style', 'bars')
            sig = (self.size_, style, repr(per.get('week')), per.get('today'),
                   bool((self._hist or {}).get('since')))
            if sig == self._sig and self.body.count():
                return                  # nothing changed: keep the rows (and pad focus)
            self._sig = sig
            self.clear()
            self._build(style)
        finally:
            self._filling = False

    def _arrived(self, hub, data, err=''):
        if data is None:
            if self._hist is not None:
                return
            data = {}                        # no history: show the empty week
        self._hist = data
        if not self._filling:
            self.fill(hub)

    def _build(self, style: str):
        hist = self._hist or {}
        per = hist.get('periods') or {}
        week = per.get('week') or {}
        games = week.get('games') or []
        wide = self.size_ == 'wide'
        if wide:
            left, right = _columns(self.body, 2)
        else:
            left = right = self.body
        _add(left, label(fmt_hours(week.get('minutes') or 0) or '0 h', "DashBig"))
        if not hist.get('since'):
            _add(left, label("Your play time shows here as you play.", "Muted", wrap=True))
            return
        n = len(games)
        _add(left, label(f"{n} game{'s' if n != 1 else ''} this week" if n
                         else "Nothing played yet this week", "Muted", wrap=True))
        if style == 'bars':
            _add(left, _day_bars(week, per.get('today') or '', 84, month=False))
        if wide and games:
            _add(right, label("Most played", "StatLabel"))
            _bar_rows(right, play_rows([], hist)['week'][:3])


DASH_WIDGETS = {cls.KIND: cls for cls in (
    StageWidget, FriendsWidget, SurpriseWidget, SystemWidget, NewsWidget, InstalledWidget, SmartShelfWidget,
    DealsWidget,
    UpcomingWidget, WeekWidget, FavouritesWidget, CollectionsWidget, UpdatesWidget,
    OwnedWidget)}


class Dashboard(QWidget):
    """Home's widget grid, the Stage first. Columns follow the window width;
    "Customise" turns on per-widget edit bars (move ←↑↓→, size, hide) and an
    "Add widgets" row for hidden ones — all buttons, so a controller can do
    it too. The layout is saved in library.json."""

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.editing = False
        self.widgets: dict[str, DashWidget] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['3'])
        # Customise and Reset sit in a slim row under the grid (w-85): the
        # Stage is the top of Home, with no title above it.
        foot = QHBoxLayout()
        foot.addStretch()
        self.btn_reset = QPushButton("Reset layout")
        self.btn_reset.setObjectName("Ghost")
        self.btn_reset.clicked.connect(self._reset)
        self.btn_reset.setVisible(False)
        foot.addWidget(self.btn_reset)
        self.btn_edit = QPushButton("Customise")
        self.btn_edit.setObjectName("Ghost")
        self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn_edit, G['customise'], 16)
        self.btn_edit.clicked.connect(lambda: self.set_editing(not self.editing))
        foot.addWidget(self.btn_edit)
        self.add_row_host = QFrame()
        self.add_row_host.setObjectName("DashAdd")
        self.add_row = FlowLayout(self.add_row_host, margin=SP['3'], spacing=SP['1_5'])
        self.add_row_host.setVisible(False)
        lay.addWidget(self.add_row_host)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(SP['4'])
        self.grid.setVerticalSpacing(SP['5'])
        lay.addLayout(self.grid)
        lay.addLayout(foot)
        self._cols = 0
        self._skipped: set | None = None          # what the last relayout left out
        for kind, cls in DASH_WIDGETS.items():
            w = cls()
            w.setParent(self)                 # never a window of its own, even unplaced
            w.hide()
            w.go.connect(hub.go)
            w.edit.connect(self._edit)
            self.widgets[kind] = w
        for w in self.widgets.values():
            w.bind(hub)

    @property
    def stage(self) -> StageWidget:
        return self.widgets['stage']

    @property
    def items(self):
        return self.hub.prefs.dashboard

    def _skip(self):
        """Shelf widgets with nothing in them stay out of the layout (except
        while editing, so they can still be arranged)."""
        if self.editing:
            return set()
        return {k for k, w in self.widgets.items() if not w.has_content(self.hub)}

    # ---- data ----
    def refresh(self, kinds=None):
        for k, w in self.widgets.items():
            if kinds is None or k in kinds:
                it = next((i for i in self.items if i['id'] == k), None)
                w.size_ = it['size'] if it else 'small'
                w.options = _dash_options(k, it or {})
                if it and not it['hidden']:
                    w.fill(self.hub)
        if kinds is not None and set(kinds) <= {'friends', 'stage'}:
            # Presence ticks and every page or focus change ask for Friends
            # playing now, a game starting or quitting for the Stage: lay
            # out again only when one comes or goes.
            if self._skip() != self._skipped:
                self.relayout(force=True)
        elif kinds is None or {'stage', 'friends', 'installed', 'smart', 'favourites',
                               'collections', 'updates', 'upcoming', 'owned'} & set(kinds):
            self.relayout(force=True)

    # ---- layout ----
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if dash_cols(self.width()) != self._cols:
            self.relayout()

    def relayout(self, force=False):
        cols = dash_cols(max(self.width(), 1))
        while self.grid.count():
            self.grid.takeAt(0)
        for c in range(6):
            self.grid.setColumnStretch(c, 0)
        self._cols = cols
        self._skipped = self._skip()
        placed = dash_pack(self.items, cols, self._skipped)
        on = {k for k, *_ in placed}
        for k, r, c, span in placed:
            it = next(i for i in self.items if i['id'] == k)
            self.widgets[k].set_editing(self.editing, it['size'], None if 'shelf' not in it
                                        else dash_shelf_label(self.hub, it['shelf']))
            self.grid.addWidget(self.widgets[k], r, c, 1, span)
        for k, w in self.widgets.items():
            w.setVisible(k in on)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        self._fill_add_row()

    def _fill_add_row(self):
        while self.add_row.count():
            it = self.add_row.takeAt(0)
            if it.widget():
                it.widget().hide()             # gone now, not at the next event loop
                it.widget().deleteLater()
        hidden = [i for i in self.items if i['hidden']]
        self.add_row_host.setVisible(self.editing)
        if not self.editing:
            return
        if not hidden:
            self.add_row.addWidget(label("Every widget is on the dashboard. Hide one with its"
                                         " eye button to remove it.", "Muted"))
        for it in hidden:
            b = QPushButton(f"＋  {DASH_WIDGETS[it['id']].TITLE}")
            b.setObjectName("Chip")
            b.clicked.connect(lambda _=False, k=it['id']: self._edit(k, 'show'))
            self.add_row.addWidget(b)

    # ---- editing ----
    def set_editing(self, on: bool):
        self.editing = on
        self.btn_edit.setText("Done" if on else "Customise")
        self.btn_edit.setObjectName("Primary" if on else "Ghost")
        set_glyph_icon(self.btn_edit, G['check'] if on else G['customise'], 16,
                       'on_accent' if on else 'subtext', hover_role=None if on else 'text')
        self.btn_edit.style().unpolish(self.btn_edit)
        self.btn_edit.style().polish(self.btn_edit)
        self.btn_reset.setVisible(on)
        self.relayout(force=True)
        if not on:
            self.hub.prefs.save()

    def _edit(self, kind, action):
        items = self.items
        if action in ('left', 'right', 'up', 'down'):
            new = dash_move(items, kind, action, self._cols, self._skip())
        else:
            new = [dict(i) for i in items]
            it = next(i for i in new if i['id'] == kind)
            if action == 'size':
                sizes = DASH_SIZES[kind]
                it['size'] = sizes[(sizes.index(it['size']) + 1) % len(sizes)]
            elif action == 'shelf':
                rules = [r for r in smart.WIDGET_RULES if r != 'linux' or has('proton')] + \
                    [smart.USER + s['id'] for s in self.hub.prefs.user_shelves()]   # w-51
                cur = _dash_shelf(it.get('shelf'))
                it['shelf'] = rules[(rules.index(cur) + 1) % len(rules)] if cur in rules \
                    else rules[0]
            elif action == 'hide':
                it['hidden'] = True
            elif action == 'show':
                it['hidden'] = False
        self.hub.prefs.dashboard = new
        self.refresh([kind]) if action in ('size', 'show', 'shelf') else None
        self.relayout(force=True)
        self.hub.prefs.save()
        # Keep the controller's focus on the widget that moved.
        w = self.widgets.get(kind)
        if w is not None and w.isVisible() and action in ('left', 'right', 'up', 'down'):
            btn = next((b for b in w.editbar.findChildren(QToolButton)
                        if b.toolTip().lower().endswith(action)), None)
            QTimer.singleShot(0, lambda: btn and alive(btn) and focus_and_reveal(btn))

    def _reset(self):
        self.hub.prefs.dashboard = dash_default()
        self.refresh()
        self.hub.prefs.save()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import ART  # noqa: E402
from .deals import _pill, DealArt, dim  # noqa: E402
from .friends import (_ts, AvatarStack, FRIENDS, MAX_CARDS, playing_now,  # noqa: E402
    who_text)
from .details import ElideLabel  # noqa: E402
from .gameinfo import is_installed, live_kicker, play_enabled  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .library import featured_games, ShelfRow  # noqa: E402
from .newspage import news_style, NewsArt  # noqa: E402
from .system import _add, _bar_rows, _columns, _day_bars, play_rows  # noqa: E402
from .theatre import _search_norm  # noqa: E402
from .widgets import (clear_layout, FlowLayout, icon_button, icon_label, label, plain_label,
    set_glyph_icon)  # noqa: E402
