"""The Home dashboard: its layout model and widgets."""

import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QRectF, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath,
    QRadialGradient)
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from nexus import achievements as A, gamedb, smart
from nexus.features import has

from .common import alive, COLLECTION_NAMES
from .fmt import fmt_hours, fmt_relative, fmt_size
from .theme import FONTS, FS, G, GRAD, P, R, SP


DASH_SPAN = {'small': 1, 'wide': 2, 'full': 99}
# (id, sizes it allows, default size, hidden by default)
DASH_KINDS = [                       # default order packs evenly in 3 columns
    ('jump',       ('full',),                  'full',  False),
    ('surprise',   ('full',),                  'full',  False),
    ('system',     ('small', 'wide'),          'wide',  False),
    ('news',       ('small', 'wide', 'full'),  'small', False),
    ('installed',  ('full',),                  'full',  False),
    ('smart',      ('full',),                  'full',  False),
    ('deals',      ('small', 'wide', 'full'),  'wide',  False),
    ('upcoming',   ('small', 'wide', 'full'),  'small', False),
    ('favourites', ('full',),                  'full',  False),
    ('collections', ('full',),                 'full',  False),
    ('updates',    ('full',),                  'full',  False),
    ('playtime',   ('small', 'wide', 'full'),  'small', True),
    ('achievements', ('small', 'wide', 'full'), 'small', True),
    ('owned',      ('full',),                  'full',  True),
]
DASH_SIZES = {k: sizes for k, sizes, _d, _h in DASH_KINDS}
DASH_DEFAULT_SIZE = {k: d for k, _s, d, _h in DASH_KINDS}
# Widgets folded into another: a saved layout's first one becomes the new
# widget in its place (so it doesn't jump to the end).
DASH_MERGED = {'activity': 'system', 'storage': 'system', 'health': 'system',
               'proton': 'system'}
# Widgets added later that belong next to a related one rather than last.
DASH_PLACE_AFTER = {'upcoming': 'deals', 'surprise': 'jump', 'collections': 'favourites',
                    'smart': 'installed'}


def _dash_shelf(value) -> str:
    """The smart shelf the Home widget shows (an item's `shelf` key)."""
    return value if value in smart.WIDGET_RULES else smart.WIDGET_DEFAULT


def dash_default() -> list[dict]:
    return [{'id': k, 'size': d, 'hidden': h, **({'shelf': _dash_shelf(None)} if k == 'smart'
                                                 else {})}
            for k, _s, d, h in DASH_KINDS]


def dash_normalize(items) -> list[dict]:
    """A saved layout made safe: unknown widgets dropped, bad sizes fixed,
    merged widgets (DASH_MERGED) replaced in place by the one they became,
    and widgets added since it was saved put in (next to their DASH_PLACE_AFTER
    anchor, else at the end) with their defaults."""
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
            it = {'id': wid, 'size': None, 'hidden': all(o.get('hidden') for o in olds)}
        if wid not in DASH_SIZES or wid in seen:
            continue
        seen.add(wid)
        size = it.get('size')
        out.append({'id': wid,
                    'size': size if size in DASH_SIZES[wid] else DASH_DEFAULT_SIZE[wid],
                    'hidden': bool(it.get('hidden'))})
        if wid == 'smart':
            out[-1]['shelf'] = _dash_shelf(it.get('shelf'))
    for d in dash_default():
        if d['id'] in seen:
            continue
        anchor = DASH_PLACE_AFTER.get(d['id'])
        at = next((i for i, o in enumerate(out) if o['id'] == anchor), None)
        if at is None:
            out.append(d)
        else:
            out.insert(at + 1, d)
        seen.add(d['id'])
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
                  proton: dict | None) -> dict:
    """The Home System widget's four tiles and the one thing (if any) that
    needs attention, from plain data. `jobs` are {title, state, step,
    progress (pct|None), active}; `disks` as from --library; `checks` from
    --doctor; `proton` the Proton page's info ({} while loading, None if the
    check never ran)."""
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
                      'danger' if used > 0.9 else 'warning' if used > 0.8 else 'text'))
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
    if running:
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
# Dashboard — glanceable widgets under the Home hero, laid out by the user
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
            self.btn_shelf.setText(f"Shelf: {smart.label(shelf)}")
        self.btn_size.setText({'small': "Small", 'wide': "Wide", 'full': "Full width"}[size])
        self.setProperty('editing', on)
        self.style().unpolish(self)
        self.style().polish(self)

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
    """A row of game tiles (Jump back in, Recently installed, …)."""
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


class JumpWidget(ShelfWidget):
    KIND, TITLE, KIND_TILE = 'jump', "Jump back in", 'wide'

    def games(self, hub):
        return sorted((g for g in _visible_games(hub) if g.get('last_played')),
                      key=lambda g: g['last_played'], reverse=True)[:10]


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
                row = view.make_shelf(name, items, self.KIND_TILE, (f'col:{name}', 'name'))
                self._tiles += row.tiles
                self.body.addWidget(row)


class SmartShelfWidget(ShelfWidget):
    """One smart shelf on Home (nexus/smart.py), "Unfinished" unless chosen
    otherwise in Customise. Gone while that shelf is empty."""
    KIND, TITLE = 'smart', "Smart shelf"

    def rule(self, hub) -> str:
        it = next((i for i in hub.prefs.dashboard if i['id'] == self.KIND), None)
        return _dash_shelf((it or {}).get('shelf'))

    def games(self, hub):
        get = getattr(hub, '_smart_facts', None)
        return smart.games_for(f'smart:{self.rule(hub)}', _visible_games(hub),
                               get() if get else {}, linux=has('proton'))[:24]

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
            key = f'smart:{self.rule(hub)}'
            row = view.make_shelf(smart.label(key), games, self.KIND_TILE, smart=key,
                                  tip=smart.tip(key))
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
                             getattr(hub.health, 'checks', []), proton)

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
            b.clicked.connect(lambda: hub.go(hub.SETTINGS))
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
    if sub:
        sl = label(sub, "Muted")
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
            right = [_pill("Pre-order", 'warning')] if preorder else []
            base = hub.dealspage._base()
            url = g.get('steamUrl') or (gamedb.detail_url(base, g['appid'])
                                        if base and g.get('appid') else '')
            row = game_row(g, [when] + right, lambda _=False, u=url: hub.dealspage._open(u),
                           sub=text)
            row.setToolTip(" · ".join(x for x in (g.get('releaseDateText'), text) if x))
            self.body.addWidget(row)
        if len(items) > n:
            self.body.addWidget(label(f"+{len(items) - n} more on the Deals page", "Muted"))


def _columns(body, n: int) -> list[QVBoxLayout]:
    """`n` side-by-side columns in a full-width card."""
    row = QHBoxLayout()
    row.setSpacing(SP['6'])
    cols = []
    for _ in range(n):
        c = QVBoxLayout()
        c.setSpacing(SP['1_5'])
        row.addLayout(c, 1)
        cols.append(c)
    body.addLayout(row)
    for c in cols:
        c.addStretch()
    return cols


def _add(lay, w):
    """Add above a column's closing stretch (or at the end of a plain body)."""
    i = lay.count() - 1
    if not (i >= 0 and lay.itemAt(i).spacerItem()):
        i = lay.count()
    if isinstance(w, QWidget):
        lay.insertWidget(i, w)
    else:
        lay.insertLayout(i, w)


def _bar_rows(lay, rows):
    """Name, value on the right, and a 4 px bar under it: rows of
    (name, text, value, most)."""
    for name, text, value, most in rows:
        row = QHBoxLayout()
        n = plain_label(name, None)
        n.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['xs']}px;")
        n.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(n, 1)
        row.addWidget(label(text, "Muted"))
        _add(lay, row)
        bar = QProgressBar()
        bar.setObjectName("CardProgress")
        bar.setTextVisible(False)
        bar.setFixedHeight(4)
        bar.setRange(0, 1000)
        bar.setValue(int(1000 * value / most) if most else 0)
        _add(lay, bar)


def _note_row(lay, title: str, right: str):
    """A title with a muted note on the right (an achievement and its game)."""
    row = QHBoxLayout()
    t = plain_label(title, None)
    t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
    t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    row.addWidget(t, 1)
    r = plain_label(right, "Muted")
    r.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    row.addWidget(r)
    _add(lay, row)


def _day_bars(period: dict, today: str, height: int, month: bool) -> "MinuteBars":
    """A week (weekday under each bar, today highlighted) or a month (every
    seventh day numbered, this week highlighted) from history.periods()."""
    days = period.get('days') or []
    bars, hi = [], set()
    try:
        now = datetime.fromisoformat(today).date()
    except (TypeError, ValueError):
        now = None
    for i, d in enumerate(days):
        try:
            day = datetime.fromisoformat(d['date']).date()
        except (KeyError, ValueError):
            day = None
        lab = ''
        if day:
            lab = (str(day.day) if day.day % 7 == 1 else '') if month else day.strftime('%a')
            if now and (day == now if not month
                        else now.isocalendar()[:2] == day.isocalendar()[:2]):
                hi.add(i)
        bars.append({'minutes': d.get('minutes') or 0, 'label': lab})
    chart = MinuteBars(height, labels_every=1)
    chart.set_bars(bars, hi)
    return chart


class PlaytimeWidget(DashWidget):
    """All-time play time, and from sessions.jsonl (nexus-cli --history)
    this week and month by day and by game."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'playtime', "Play time", G['clock'], 'dusk', None

    def __init__(self):
        super().__init__()
        self._hist: dict | None = None
        self._asked = 0.0
        self._total = None
        self._filling = False
        self._sig = None

    def fill(self, hub):
        games = [g for g in hub._games if g.get('playtime_minutes')]
        total = sum(g['playtime_minutes'] for g in games)
        self._filling = True
        try:
            # Ask first: a cached answer comes back right here, inside fill.
            if self._hist is None or total != self._total \
                    or time.monotonic() - self._asked > 300:
                fresh = self._total is not None and total != self._total
                self._asked, self._total = time.monotonic(), total
                cli_json(['--history', '--json'],
                         lambda d, e, hub=hub: self._arrived(hub, d, e), 30000,
                         ttl=0 if fresh else 60)
            sig = (self.size_, total, tuple((g.get('slug'), g['playtime_minutes'])
                                            for g in games),
                   repr((self._hist or {}).get('periods')), repr((self._hist or {}).get('weeks')))
            if sig == self._sig and self.body.count():
                return                  # nothing changed: keep the rows (and pad focus)
            self._sig = sig
            self.clear()
            self._build(games, total)
        finally:
            self._filling = False

    def _arrived(self, hub, data, err=''):
        if data is None:
            if self._hist is not None:
                return
            data = {}                        # no history: show the totals alone
        self._hist = data
        if not self._filling:
            self.fill(hub)

    def _build(self, games, total):
        hist = self._hist or {}
        per = hist.get('periods') or {}
        week, month = per.get('week') or {}, per.get('month') or {}
        if not total and not hist.get('since'):
            self.body.addWidget(label("No play time recorded yet.", "Muted"))
            return
        self.body.addWidget(label(fmt_hours(total) or '0 h', "DashBig"))
        facts = [f"across {len(games)} game{'s' if len(games) != 1 else ''}"]
        if week.get('minutes'):
            facts.append(f"{fmt_hours(week['minutes'])} this week")
        if self.size_ != 'small' and month.get('minutes'):
            facts.append(f"{fmt_hours(month['minutes'])} this month")
        self.body.addWidget(label(" · ".join(facts), "Muted", wrap=True))
        top = sorted(games, key=lambda g: g['playtime_minutes'], reverse=True)
        all_time = [(g.get('display_name', ''), fmt_hours(g['playtime_minutes']) or '',
                     g['playtime_minutes'], top[0]['playtime_minutes']) for g in top]
        month_games = month.get('games') or []
        this_month = [(m['name'], fmt_hours(m['minutes']) or '', m['minutes'],
                       month_games[0]['minutes']) for m in month_games]
        has_sessions = bool(hist.get('since'))
        today = per.get('today') or ''
        if self.size_ == 'small':
            if has_sessions:
                self.body.addWidget(_day_bars(week, today, 84, month=False))
            _bar_rows(self.body, all_time[:3])
            return
        if not has_sessions:
            self.body.addWidget(label("Sessions are recorded from now on; the week and "
                                      "month charts fill in as you play.", "Muted", wrap=True))
        if self.size_ == 'wide':
            if has_sessions:
                self.body.addWidget(_day_bars(month, today, 100, month=True))
            self.body.addWidget(label("This month" if this_month else "All time", "StatLabel"))
            _bar_rows(self.body, (this_month or all_time)[:5])
            return
        charts, by_month, by_all = _columns(self.body, 3)
        if has_sessions:
            _add(charts, label("This month by day", "StatLabel"))
            _add(charts, _day_bars(month, today, 110, month=True))
            weeks = WeekBars()
            weeks.setMinimumHeight(110)
            weeks.set_weeks(hist.get('weeks') or [])
            _add(charts, label("Last 12 weeks", "StatLabel"))
            _add(charts, weeks)
        _add(by_month, label("This month", "StatLabel"))
        if this_month:
            _bar_rows(by_month, this_month[:6])
        else:
            _add(by_month, label("Nothing played this month yet.", "Muted", wrap=True))
        _add(by_all, label("All time", "StatLabel"))
        _bar_rows(by_all, all_time[:6])


class AchievementsWidget(DashWidget):
    """Steam achievements: how many you've unlocked, the newest unlocks, the
    games closest to 100% (Steam's own stats cache) and, wide and full, your
    rarest unlocks (Steam's public global percentages, via the CLI)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'achievements', "Achievements", G['trophy'], 'dusk', None

    def __init__(self):
        super().__init__()
        self._recent: list | None = None
        self._rarest: list | None = None
        self._err = ''
        self._asked = 0.0
        self._asked_rare = 0.0
        self._filling = False

    def fill(self, hub):
        self._filling = True
        try:
            # Ask first: a cached answer comes back right here, inside fill,
            # and must not draw a second set of rows.
            if self._recent is None or time.monotonic() - self._asked > 300:
                self._asked = time.monotonic()
                cli_json(['--achievements', 'recent', '--json'],
                         lambda d, e, hub=hub: self._got_recent(hub, d, e), 30000, ttl=300)
            if self.size_ != 'small' and (self._rarest is None
                                          or time.monotonic() - self._asked_rare > 1800):
                self._asked_rare = time.monotonic()
                cli_json(['--achievements', 'rarest', '--json'],
                         lambda d, e, hub=hub: self._got_rarest(hub, d, e), 60000, ttl=1800)
            self.clear()
            self._build(hub)
        finally:
            self._filling = False

    def _build(self, hub):
        games = [g for g in hub._games if g.get('source') == 'steam' and g.get('achievements')]
        if not games:
            self.body.addWidget(label("Steam games you've played show their achievements "
                                      "here.", "Muted", wrap=True))
            return
        unlocked = sum(g['achievements'][0] for g in games)
        self.body.addWidget(label(f"{unlocked:,}", "DashBig"))
        done = sum(1 for g in games if g['achievements'][0] == g['achievements'][1])
        self.body.addWidget(label(f"unlocked across {len(games)} games"
                                  + (f" · {done} complete" if done else ""), "Muted"))
        n_recent, n_close, n_rare = {'small': (3, 2, 0), 'wide': (4, 3, 3),
                                     'full': (6, 6, 6)}[self.size_]
        if self.size_ == 'full':
            recent_col, close_col, rare_col = _columns(self.body, 3)
            _add(recent_col, label("Recent", "StatLabel"))
        else:
            recent_col = close_col = rare_col = self.body
        if self._err:
            _add(recent_col, label(f"Recent unlocks unavailable: {self._err}", "Muted",
                                   wrap=True))
        names = {g['appid']: g.get('display_name', '') for g in hub._games if g.get('appid')}
        for a in (self._recent or [])[:n_recent]:
            when = fmt_relative(datetime.fromtimestamp(a['time']).isoformat()) or ''
            _note_row(recent_col, a['title'],
                      f"{names.get(a['appid'], '')} · {when}".strip(' ·'))
        close = [games[i] for i in A.closest({i: g['achievements']
                                              for i, g in enumerate(games)}, n_close)]
        if close:
            _add(close_col, label("Closest to 100%", "StatLabel"))
            _bar_rows(close_col, [(g.get('display_name', ''),
                                   f"{g['achievements'][0]}/{g['achievements'][1]}",
                                   g['achievements'][0], g['achievements'][1]) for g in close])
        rare = (self._rarest or [])[:n_rare]
        if rare:                         # offline or nothing cached: the section stays out
            _add(rare_col, label("Rarest", "StatLabel"))
            for a in rare:
                _note_row(rare_col, a['title'],
                          f"{a['percent']:.1f}% · {names.get(a['appid'], '')}".strip(' ·'))

    def _got_recent(self, hub, data, err=''):
        if data is None:
            if self._err:
                return
            self._err = err or 'error'
            self._recent = self._recent or []
        else:
            new = data.get('recent') or []
            if new == self._recent and not self._err:
                return
            self._err, self._recent = '', new
        if not self._filling:
            self.fill(hub)

    def _got_rarest(self, hub, data, err=''):
        if data is None:                 # offline or failed: keep what we had
            new = self._rarest or []
        else:
            new = data.get('rarest') or []
        if new == self._rarest:
            return
        self._rarest = new
        if not self._filling:
            self.fill(hub)


DASH_WIDGETS = {cls.KIND: cls for cls in (
    JumpWidget, SurpriseWidget, SystemWidget, NewsWidget, InstalledWidget, SmartShelfWidget,
    DealsWidget,
    UpcomingWidget, FavouritesWidget, CollectionsWidget, UpdatesWidget, PlaytimeWidget,
    AchievementsWidget, OwnedWidget)}


class Dashboard(QWidget):
    """The widget grid under the hero. Columns follow the window width;
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
        head = QHBoxLayout()
        self.title = label("At a glance", "ShelfTitle")
        head.addWidget(self.title)
        head.addStretch()
        self.btn_reset = QPushButton("Reset layout")
        self.btn_reset.setObjectName("Ghost")
        self.btn_reset.clicked.connect(self._reset)
        self.btn_reset.setVisible(False)
        head.addWidget(self.btn_reset)
        self.btn_edit = QPushButton("Customise")
        self.btn_edit.setObjectName("Ghost")
        self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn_edit, G['customise'], 16)
        self.btn_edit.clicked.connect(lambda: self.set_editing(not self.editing))
        head.addWidget(self.btn_edit)
        lay.addLayout(head)
        self.add_row_host = QFrame()
        self.add_row_host.setObjectName("DashAdd")
        self.add_row = FlowLayout(self.add_row_host, margin=SP['3'], spacing=SP['1_5'])
        self.add_row_host.setVisible(False)
        lay.addWidget(self.add_row_host)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(SP['4'])
        self.grid.setVerticalSpacing(SP['5'])
        lay.addLayout(self.grid)
        self._cols = 0
        for kind, cls in DASH_WIDGETS.items():
            w = cls()
            w.setParent(self)                 # never a window of its own, even unplaced
            w.hide()
            w.go.connect(hub.go)
            w.edit.connect(self._edit)
            self.widgets[kind] = w

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
                if it and not it['hidden']:
                    w.fill(self.hub)
        if kinds is None or {'jump', 'installed', 'smart', 'favourites', 'collections',
                             'updates', 'upcoming', 'owned'} & set(kinds or ()):
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
        placed = dash_pack(self.items, cols, self._skip())
        on = {k for k, *_ in placed}
        for k, r, c, span in placed:
            it = next(i for i in self.items if i['id'] == k)
            self.widgets[k].set_editing(self.editing, it['size'], it.get('shelf'))
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
                rules = [r for r in smart.WIDGET_RULES if r != 'linux' or has('proton')]
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
from .deals import _pill  # noqa: E402
from .details import ElideLabel  # noqa: E402
from .gameinfo import is_installed  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .newspage import news_style, NewsArt  # noqa: E402
from .system import MinuteBars, WeekBars  # noqa: E402
from .theatre import _search_norm  # noqa: E402
from .widgets import (clear_layout, FlowLayout, icon_button, icon_label, label, plain_label,
    set_glyph_icon)  # noqa: E402
