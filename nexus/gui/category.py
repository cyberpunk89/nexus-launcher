"""Library category pages (w-98): one page per category of nexus/categories.py
(a smart rule, a genre, your favourites, a collection, a shelf you built, a
store). A cover-mosaic header, the smart cuts as chips, a sort, and Rows /
Grid / List of its games, then More like this.

The page is one of the Library's views (MainWindow.views): it answers to
the same calls as LibraryView and ListView (tiles, games, tile_for, reveal,
mark_selected, set_jobs) and emits the same tile signals, so selection, the
game page, jobs and focus need nothing of their own. Its header and chips
("top") scroll with the games: they are the grid's head (LibraryView
set_head, so the grid stays virtual), the first item of the Rows scroll, or
a strip over the List."""

import math

from PyQt6.QtCore import pyqtSignal, QPoint, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from .. import categories, smart
from .art import ART, art_lookup, COVER_KINDS, placeholder_pixmap, screen_dpr
from .common import alive
from .fmt import fmt_relative
from .gameinfo import is_installed
from .library import _TileSignals, LibraryView, ListView, ShelfRow
from .theme import FONTS, FS, G, is_light, LOOK, P, R, scaled, SP
from .tiles import GameTile
from .widgets import (clear_layout, FlowLayout, icon_button, icon_label, label,
    SegmentedControl, set_glyph_icon, set_icon_label)

# What the header's kicker calls each kind.
KIND_WORDS = {'smart': "Smart shelf", 'genre': "Genre", 'fav': "Yours",
              'collection': "Collection", 'shelf': "Your shelf", 'store': "Store",
              'developer': "Studio", 'publisher': "Studio"}
SORT_NAMES = (("Recently played", 'played'), ("Name", 'name'), ("Most played", 'most'),
              ("Recently added", 'added'), ("Size on disk", 'size'))
COVER_MAX = 6
WALL_COVERS = 12               # the wall header cycles this many covers
# The palette role a category tints its mosaic, glyph and tile with when it
# has no colour of its own (w-113: More like this, the hub).
KIND_TINT = {'genre': 'col_sky', 'smart': 'accent', 'shelf': 'col_teal',
             'collection': 'col_gold', 'fav': 'col_gold', 'store': 'subtext',
             'developer': 'col_gold', 'publisher': 'col_gold'}
CONTINUE_CHARS = 24            # "Continue <name>": the name is cut after this many
NO_GAMES = "No games here right now."
HOME_TILE = 150                # a row's tiles: as on the Shelves view (HomeView.SHELF_TILE)
# The pinned-tab strip's own tabs (never a category key; categories.HUB is
# the desktop's All categories): Browse, and Theatre's All games.
BROWSE_TAB, GAMES_TAB = 'browse', 'games'


def _short(name: str, n: int = CONTINUE_CHARS) -> str:
    name = ' '.join((name or '').split())
    return name if len(name) <= n else name[:n - 1].rstrip() + "…"


def story(st: categories.Stats) -> str:
    """'14 games · 228 h played · 5 finished · 426 GB on disk'."""
    return "  ·  ".join(f"{v} {l}" for v, l in stat_items(st))


def stat_items(st: categories.Stats) -> list[tuple[str, str]]:
    """The header's big numbers: [('14', 'games'), ('228 h', 'played'),
    ('5', 'finished'), ('426 GB', 'on disk')] (parts with nothing to say
    left out). story() is these joined."""
    out = [(f"{st.count}", "games" if st.count != 1 else "game")]
    if (played := _played(st.minutes)) is not None:
        out.append(played)
    if st.finished:
        out.append((f"{st.finished}", "finished"))
    if st.bytes:
        gb = st.bytes / 1e9
        out.append((f"{gb:,.0f} GB" if gb >= 10 else f"{gb:.1f} GB", "on disk"))
    return out


def _played(minutes: int) -> tuple[str, str] | None:
    if minutes >= 60:
        return f"{round(minutes / 60):,} h", "played"
    return (f"{minutes} min", "played") if minutes else None


def studio_items(cat: categories.Category, st: categories.Stats,
                 sf: categories.StudioFacts) -> list[tuple[str, str]]:
    """A studio header's numbers (w-114): the page's role first ('3',
    'games developed'), the other role when it has any ('2', 'also
    published'), hours played, the release years ('2021–2024')."""
    dev = cat.kind == 'developer'
    mine, other = (sf.developed, sf.published) if dev else (sf.published, sf.developed)
    verb, also = ("developed", "published") if dev else ("published", "developed")
    out = [(f"{mine}", f"{'games' if mine != 1 else 'game'} {verb}")]
    if other:
        out.append((f"{other}", f"also {also}"))
    if (played := _played(st.minutes)) is not None:
        out.append(played)
    if sf.years:
        a, b = sf.years
        out.append((f"{a}", "release year") if a == b else (f"{a}–{b}", "release years"))
    return out


def merged_line(names) -> str:
    """'Names merged: “A”, “B” and “C” count as one studio.' ('' for fewer
    than two spellings)."""
    names = [n for n in names or () if n]
    if len(names) < 2:
        return ''
    quoted = [f"“{n}”" for n in names]
    return f"Names merged: {', '.join(quoted[:-1])} and {quoted[-1]} count as one studio."


def story_lines(text: str, fm, width: int) -> str:
    """The story in `width` px: on one line, else broken between its parts
    ("3 games · 6 h played" / "1 finished · 12 GB on disk"), the second line
    elided if it still doesn't fit (a wider font, e.g. Windows')."""
    if fm.horizontalAdvance(text) <= width:
        return text
    sep = "  ·  "
    parts, first = text.split(sep), ''
    for i in range(len(parts), 0, -1):
        first = sep.join(parts[:i])
        if fm.horizontalAdvance(first) <= width:
            rest = sep.join(parts[i:])
            return first + "\n" + fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return fm.elidedText(text, Qt.TextElideMode.ElideRight, width)


def two_lines(text: str, fm, width: int) -> list[str]:
    """`text` in at most two lines of `width` px: the first breaks between
    words, the second takes the rest, elided. A first word too long for a
    line is elided on one line."""
    text = ' '.join((text or '').split())
    if fm.horizontalAdvance(text) <= width:
        return [text]
    words = text.split(' ')
    first = words[0]
    if fm.horizontalAdvance(first) > width:
        return [fm.elidedText(text, Qt.TextElideMode.ElideRight, width)]
    i = 1
    while i < len(words) and fm.horizontalAdvance(first + ' ' + words[i]) <= width:
        first += ' ' + words[i]
        i += 1
    rest = ' '.join(words[i:])
    return [first, fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)]


class _Mosaic:
    """The cover mosaic the header and the More like this tiles share: a
    tint of the category's colour, its covers fanned in from the right
    edge (the first on top, each a little lower), fading out towards the
    text. Covers come from the disk only and live in ART (keys, not
    pixmaps); the slugs are picked when a category is set and kept while
    it stays the same one, so the header doesn't reshuffle as you play."""
    INSET = SP['5']
    SHARE = 0.48               # the covers keep to this much of the width
    FADE = (0.50, 0.62)        # covers gone left of the first, whole right of the second
    RADIUS = R['panel']
    PICK = COVER_MAX           # how many covers a category keeps
    TINT_BY_KIND = False       # a category without a colour: its kind's (KIND_TINT)
    TINT_ALPHA = (0.28, 0.18)  # the tint's strength: dark, light theme

    def _mosaic_init(self):
        self._cat: categories.Category | None = None
        self._slugs: list[str] = []
        self._by: dict[str, dict] = {}
        self._keys: dict[tuple, object] = {}        # (slug, w, h) -> ART key or None
        self._asked: set = set()

    def _set_mosaic(self, cat: categories.Category):
        by = {g.get('slug'): g for g in cat.games}
        if self._cat is not None and self._cat.key == cat.key:
            self._slugs = [s for s in self._slugs if s in by]    # fixed while open
        else:
            self._slugs = [g['slug'] for g in categories.covers(cat, self.PICK)]
            self._keys, self._asked = {}, set()
        self._cat, self._by = cat, by
        self.update()

    def _cover_geometry(self) -> tuple[int, int, int]:
        ch = max(1, self.height() - 2 * self.INSET)
        cw = round(ch * 2 / 3)
        return cw, ch, max(1, round(cw * 0.62))

    def cover_count(self) -> int:
        """How many covers show at this width: one for a category of 1-3
        games, none for an empty one."""
        if self._cat is None or not self._slugs:
            return 0
        if len(self._cat.games) <= 3:
            return 1
        cw, _ch, step = self._cover_geometry()
        n = 1 + int((self.width() * self.SHARE - cw - 2 * self.INSET) // step)
        return max(1, min(COVER_MAX, n, len(self._slugs)))

    def shown_slugs(self) -> list[str]:
        return self._slugs[:self.cover_count()]

    def glyph_shown(self) -> bool:
        return self._cat is not None and not self._cat.games

    def _cover(self, slug: str, cw: int, ch: int) -> QPixmap:
        g = self._by.get(slug) or {}
        k = (slug, cw, ch, screen_dpr())
        if k not in self._keys:
            path = art_lookup(g, COVER_KINDS)[0]
            self._keys[k] = ART.key(path, cw, ch, not is_installed(g)) if path else None
        key = self._keys[k]
        pm = ART.get(key) if key is not None else None
        if pm is None and key is not None and key not in self._asked:
            self._asked.add(key)                    # once: a bad file stays initials
            ART.request_key(key, lambda _pm, w=self: alive(w) and w._cover_arrived())
        name = g.get('display_name') or slug
        return pm if pm is not None else placeholder_pixmap(' '.join(name.split()[:2]), cw, ch)

    def _cover_arrived(self):
        self.update()

    def _tint_role(self, cat: categories.Category | None = None) -> str:
        cat = cat or self._cat
        color = cat.color if cat is not None else None
        if color and f'col_{color}' in P:
            return f'col_{color}'
        if self.TINT_BY_KIND and cat is not None:
            return KIND_TINT.get(cat.kind, 'accent')
        return 'accent'

    def _tint(self) -> QColor:
        return QColor(P[self._tint_role()])

    def _paint_mosaic(self, pt: QPainter):
        r = QRectF(self.rect())
        rad = min(self.RADIUS, r.height() / 2)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        contrast = LOOK.get('contrast')
        pt.save()
        pt.setClipPath(path)
        if contrast:
            pt.fillRect(r, QColor(P['surface']))
        else:
            pt.fillRect(r, QColor(P['surface_2']))
            tint = self._tint()
            a, b = QColor(tint), QColor(tint)
            a.setAlphaF(self.TINT_ALPHA[1] if is_light() else self.TINT_ALPHA[0])
            b.setAlphaF(0.0)
            grad = QLinearGradient(0, 0, r.width(), r.height())
            grad.setColorAt(0, a)
            grad.setColorAt(1, b)
            pt.fillRect(r, grad)
        if self.glyph_shown():
            self._paint_glyph(pt, r)
        elif self._slugs:
            pt.drawPixmap(0, 0, self._covers_layer())
        pt.restore()
        pen = QPen(QColor(P['border']), 2.0 if contrast else 1.0)
        pt.setPen(pen)
        pt.setBrush(Qt.BrushStyle.NoBrush)
        pt.drawPath(path)

    def _covers_layer(self) -> QPixmap:
        """The fanned covers on a transparent layer, faded out to the left
        so the background shows through (whatever its tint)."""
        dpr = screen_dpr()
        w, h = self.width(), self.height()
        layer = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        layer.setDevicePixelRatio(dpr)
        layer.fill(Qt.GlobalColor.transparent)
        cw, ch, step = self._cover_geometry()
        slugs = self.shown_slugs()
        n = len(slugs)
        x0 = w - self.INSET - cw - step * (n - 1)
        pt = QPainter(layer)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        shade = QColor(P['scrim'])                 # the darkest role, on every theme
        shade.setAlphaF(0.35)
        drop = SP['1']                                # each a little lower, the fan centred
        top = self.INSET - drop * (n - 1) / 2
        for i in reversed(range(n)):                  # the first painted last: on top
            rect = QRectF(x0 + step * i, top + i * drop, cw, ch)
            sp = QPainterPath()
            sp.addRoundedRect(rect.translated(0, 2), R['sm'], R['sm'])
            pt.fillPath(sp, shade)
            cp = QPainterPath()
            cp.addRoundedRect(rect, R['sm'], R['sm'])
            pt.save()
            pt.setClipPath(cp)
            pt.drawPixmap(rect.topLeft(), self._cover(slugs[i], cw, ch))
            pt.restore()
            pt.setPen(QPen(QColor(P['border']), 1.0))
            pt.drawPath(cp)
        fade = QLinearGradient(0, 0, w, 0)
        fade.setColorAt(0, QColor(0, 0, 0, 0))
        fade.setColorAt(self.FADE[0], QColor(0, 0, 0, 0))
        fade.setColorAt(self.FADE[1], QColor(0, 0, 0, 255))
        fade.setColorAt(1, QColor(0, 0, 0, 255))
        pt.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        pt.fillRect(QRectF(0, 0, w, h), fade)
        pt.end()
        return layer

    def _paint_glyph(self, pt: QPainter, r: QRectF):
        px = scaled(56)
        f = QFont(FONTS.icon)
        f.setPixelSize(px)
        pt.setFont(f)
        pt.setPen(QColor(P['subtext']))
        box = QRectF(r.width() * (1 - self.SHARE), 0, r.width() * self.SHARE - self.INSET,
                     r.height())
        pt.drawText(box, Qt.AlignmentFlag.AlignCenter, self._cat.glyph)


class _HeaderActions(QWidget):
    """What the two headers share (the desktop's CoverWallHeader and
    Theatre's MosaicHeader): the Continue / Pick for me / Pin / ⋮ buttons,
    their signals and the pin states. The window and Theatre only ever
    touch these: back, play_lead, pick, menu, pin and btn_continue,
    btn_pick, btn_pin, btn_more, btn_back, title."""
    back = pyqtSignal()
    play_lead = pyqtSignal(dict)
    pick = pyqtSignal()
    menu = pyqtSignal(QPoint)
    pin = pyqtSignal()                      # Pin / Pinned: toggle this category's tab

    def _actions_init(self):
        self._lead: dict | None = None
        self._lead_name = ''                     # Continue's game, before _fit cuts it
        self._pin_state: str | None = None

    def _build_back(self) -> QPushButton:
        self.btn_back = QPushButton("Library")
        self.btn_back.setObjectName("Ghost")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setToolTip("Back to the Library (Esc)")
        set_glyph_icon(self.btn_back, G['chevron_left'], 15, 'subtext')
        self.btn_back.clicked.connect(self.back)
        return self.btn_back

    def _build_actions(self, px) -> QHBoxLayout:
        """The buttons in a row (`px`: glyph size by the header's scale)."""
        acts = QHBoxLayout()
        acts.setSpacing(SP['2'])
        self.btn_continue = QPushButton("")
        self.btn_continue.setObjectName("Primary")
        self.btn_continue.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn_continue, G['play'], px(15), 'on_accent', hover_role=None)
        self.btn_continue.clicked.connect(self._play_lead)
        acts.addWidget(self.btn_continue)
        self.btn_pick = QPushButton("Pick for me")
        self.btn_pick.setObjectName("Ghost")
        self.btn_pick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick.setToolTip("A game from this category, picked for you")
        set_glyph_icon(self.btn_pick, G['dice'], px(15), 'subtext')
        self.btn_pick.clicked.connect(self.pick)
        acts.addWidget(self.btn_pick)
        # Pin (w-100): this category as a tab in the Library.
        self.btn_pin = QPushButton("Pin")
        self.btn_pin.setObjectName("Ghost")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn_pin, G['pin'], px(15), 'subtext', on_role='accent')
        self.btn_pin.clicked.connect(self._pin_clicked)
        self.btn_pin.hide()
        acts.addWidget(self.btn_pin)
        self.btn_more = icon_button(G['more'], "More for this category", 32, "Ghost",
                                    round_=True)
        self.btn_more.clicked.connect(self._more_clicked)
        acts.addWidget(self.btn_more)
        acts.addStretch()
        return acts

    def _play_lead(self):
        if self._lead:
            self.play_lead.emit(self._lead)

    def _more_clicked(self):
        self.menu.emit(self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height())))

    def _set_lead(self, lead: dict | None):
        self._lead = lead
        self.btn_continue.setVisible(lead is not None)
        self._lead_name = ''
        if lead is not None:
            name = lead.get('display_name') or lead.get('slug') or ''
            self._lead_name = _short(name)
            self.btn_continue.setToolTip(f"Play {name}")

    def set_pin_state(self, state: str):
        """categories.pin_state(): 'on' (Pinned), 'off' (Pin), 'full' (Pin,
        greyed out: no room). The button shows from the first call."""
        self._pin_state = state
        self.btn_pin.show()
        on = state == 'on'
        self.btn_pin.setChecked(on)
        self.btn_pin.setText("Pinned" if on else "Pin")
        self.btn_pin.setEnabled(state != 'full')
        self.btn_pin.setToolTip(
            "Unpin this category from the Library's tabs" if on else
            f"{categories.PIN_MAX} pinned, unpin one first" if state == 'full' else
            "Pin this category as a tab in the Library")
        self._fit()                              # Continue's room changed

    def _pin_clicked(self):
        self.btn_pin.setChecked(self._pin_state == 'on')     # the window says what it is
        self.pin.emit()

    def _fit_continue(self, w: int):
        """Continue's game name is cut to leave room, in `w` px, for the
        other buttons."""
        if not self._lead_name:
            return
        b = self.btn_continue
        b.ensurePolished()
        gap = SP['2']
        others = sum(o.sizeHint().width() + gap for o in (self.btn_pick, self.btn_pin,
                                                          self.btn_more)
                     if not o.isHidden())
        fm = b.fontMetrics()
        head = "Continue "
        pad = b.sizeHint().width() - fm.horizontalAdvance(b.text().replace('&&', '&'))
        room = w - others - pad - fm.horizontalAdvance(head)
        name = fm.elidedText(self._lead_name, Qt.TextElideMode.ElideRight, max(40, room))
        text = (head + name).replace('&', '&&')
        if b.text() != text:
            b.setText(text)

    def _fit(self):
        pass


class MosaicHeader(_HeaderActions, _Mosaic):
    """Theatre's category header (the desktop page has CoverWallHeader): the
    kind, the title and a story line (games, hours, finished, size), then
    Continue <the game played last>, Pick for me and Pin — over the mosaic.
    It is `scale` times as tall and has no crumb (B goes back) and no ⋮."""
    TEXT_SHARE = 0.52

    def __init__(self, theatre: bool = False, scale: float = 1.0):
        super().__init__()
        self._mosaic_init()
        self._actions_init()
        self.theatre = theatre
        self._title = self._story = ''
        self.setObjectName("MosaicHeader")
        self.setFixedHeight(round(scaled(208) * scale))
        px = lambda n: round(n * scale)          # noqa: E731  (glyph sizes)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['3'], SP['5'], SP['5'])
        lay.setSpacing(0)
        self.col = QWidget()
        col = QVBoxLayout(self.col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SP['1_5'])
        col.addWidget(self._build_back(), 0, Qt.AlignmentFlag.AlignLeft)
        self.btn_back.setVisible(not theatre)
        col.addStretch(1)
        kind = QHBoxLayout()
        kind.setSpacing(SP['2'])
        self.kind_icon = icon_label('', px(14), P['subtext'])
        kind.addWidget(self.kind_icon)
        self.kind = label("", "CatKind")
        kind.addWidget(self.kind)
        kind.addStretch()
        col.addLayout(kind)
        self.title = label("", "CatTitle")
        self.story = label("", "CatStory")
        for lab in (self.title, self.story):     # elided to fit (_fit): no minimum width
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            col.addWidget(lab)
        col.addSpacing(SP['1'])
        col.addLayout(self._build_actions(px))
        # The text takes its share (_fit caps it), the covers the rest: with
        # Ignored labels the column would otherwise shrink to its buttons.
        self.col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.col, 1)
        lay.addStretch(1)

    def set_category(self, cat: categories.Category, stats: categories.Stats | None = None,
                     lead: dict | None = None):
        self._set_mosaic(cat)
        self._title = cat.title
        self.kind_icon.setText(cat.glyph)
        self.kind.setText(KIND_WORDS.get(cat.kind, '').upper())
        self.title.setToolTip(cat.title)
        self._story = story(stats or categories.stats(cat, {}))
        self.story.setToolTip(cat.tip)
        self._set_lead(lead)
        self.btn_more.setVisible(cat.kind not in ('fav', 'store') and not self.theatre)
        self._fit()

    def _fit(self):
        """The text keeps to the left part, the title on one line, and
        Continue's game name is cut to leave room for the other buttons."""
        m = self.layout().contentsMargins()
        w = max(120, int(self.width() * self.TEXT_SHARE) - m.left())
        self.col.setMaximumWidth(w)
        for lab, text in ((self.title, self._title), (self.story, self._story)):
            lab.ensurePolished()                 # the theme's font, not the default
            lab.setText(story_lines(text, lab.fontMetrics(), w) if lab is self.story else
                        lab.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, w))
        self._fit_continue(w)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange):
            self._fit()                        # a new size or font: elide again

    def paintEvent(self, _ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_mosaic(pt)
        pt.end()



class StatBlock(QWidget):
    """The header's numbers, big, each over its label with a hairline
    between: [('14', 'games'), ('228 h', 'played'), ...] (set_items).
    fit(width) puts them on one row, or as many a row as fit when one row
    would be cut (Size 1.5 at 900 px; one a row with Windows' fonts): never
    clipped."""

    def __init__(self):
        super().__init__()
        self._items: list[tuple[str, str]] = []
        self._boxes: list[QWidget] = []
        self._rules: list[QFrame] = []
        self._per_row = 0
        self._width = 0
        self._lay = QGridLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(SP['5'])
        self._lay.setVerticalSpacing(SP['3'])

    def set_items(self, items):
        items = [(str(v), str(lab)) for v, lab in items]
        if items == self._items:
            return
        self._items = items
        for w in self._boxes + self._rules:       # rules out of the grid too: no leak
            self._lay.removeWidget(w)
            w.hide()
            w.deleteLater()
        clear_layout(self._lay)
        self._boxes, self._rules = [], []
        for value, text in items:
            box = QWidget()
            vb = QVBoxLayout(box)
            vb.setContentsMargins(0, 0, 0, 0)
            vb.setSpacing(SP['0_5'])
            vb.addWidget(label(value, "CatStatValue"))
            vb.addWidget(label(text, "CatStatLabel"))
            self._boxes.append(box)
        for _ in range(max(0, len(items) - 1)):
            rule = QFrame()
            rule.setObjectName("CatStatRule")
            rule.setFixedWidth(1)
            self._rules.append(rule)
        self._per_row = 0
        self._place(self._choose(self._width))

    def _one_row_width(self) -> int:
        sp = self._lay.horizontalSpacing()
        n = len(self._boxes)
        return (sum(b.sizeHint().width() for b in self._boxes)
                + max(0, n - 1) * (1 + 2 * sp))

    def _row_width(self, per_row: int) -> int:
        """The grid's width with `per_row` boxes a row: each column as wide
        as its widest box, a hairline and two gaps between columns."""
        sp = self._lay.horizontalSpacing()
        cols = [max(b.sizeHint().width() for b in self._boxes[c::per_row])
                for c in range(min(per_row, len(self._boxes)))]
        return sum(cols) + max(0, len(cols) - 1) * (1 + 2 * sp)

    def _choose(self, width: int) -> int:
        """As many a row as fit (one row when it does), never fewer than one:
        Windows' wider fonts at Size 1.5 don't fit two a row either."""
        n = len(self._boxes)
        if n <= 1 or width <= 0 or self._one_row_width() <= width:
            return max(1, n)
        return next((k for k in range(n - 1, 0, -1) if self._row_width(k) <= width), 1)

    def _place(self, per_row: int):
        if per_row == self._per_row:
            return
        self._per_row = per_row
        for w in self._boxes + self._rules:
            self._lay.removeWidget(w)
        for c in range(self._lay.columnCount()):
            self._lay.setColumnStretch(c, 0)
        r = 0
        for i, box in enumerate(self._boxes):
            row, k = divmod(i, per_row)
            col = 2 * k
            self._lay.addWidget(box, row, col)
            if k < per_row - 1 and i < len(self._boxes) - 1:
                rule = self._rules[r]
                r += 1
                self._lay.addWidget(rule, row, col + 1)
                rule.show()
            box.show()
        for rule in self._rules[r:]:
            rule.hide()
        self._lay.setColumnStretch(2 * per_row, 1)

    def fit(self, width: int):
        """Lay the numbers out for this width (the header's text column)."""
        self._width = width
        self._place(self._choose(width))

    def rows(self) -> int:
        n = len(self._boxes)
        return 0 if not n else -(-n // max(1, self._per_row))

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)


class _CachedLayer:
    """A pixmap painted once and kept while its key holds (the wall header,
    the studio timeline). A new key while one is kept (a resize, a cover
    arriving) paints the stale one, scaled, until a 60 ms child timer fires:
    a burst of covers or a drag-resize is one build, never one per event."""
    LAYER_DELAY = 60

    def _layer_init(self):
        self._layer_pm: QPixmap | None = None
        self._layer_key = None
        self._layer_now = False             # the timer fired: build it again on this paint
        self._layer_timer = QTimer(self)    # a child: it dies with the widget
        self._layer_timer.setSingleShot(True)
        self._layer_timer.setInterval(self.LAYER_DELAY)
        self._layer_timer.timeout.connect(self._layer_rebuild)

    def _layer_drop(self):
        """Never paint the kept pixmap again (another category)."""
        self._layer_pm = self._layer_key = None

    def _layer_stale(self):
        """Something in it changed (a cover arrived): built again soon."""
        self._layer_key = None
        if not self._layer_timer.isActive():
            self._layer_timer.start()

    def _layer_rebuild(self):
        self._layer_now = True
        self.update()

    def _layer(self, key, build) -> QPixmap | None:
        """The pixmap for `key`: the kept one when the key holds, the stale
        one while the timer runs, else `build()` (a QPixmap or None)."""
        if key == self._layer_key and self._layer_pm is not None:
            return self._layer_pm
        if self._layer_pm is not None and not self._layer_now:
            if not self._layer_timer.isActive():
                self._layer_timer.start()
            return self._layer_pm           # the last one, until the timer fires
        self._layer_now = False
        pm = build()
        self._layer_pm, self._layer_key = pm, (key if pm is not None else None)
        return pm


class CoverWallHeader(_CachedLayer, _HeaderActions, _Mosaic):
    """The desktop category page's header (w-113): a panel with a wall of
    covers, tilted, behind the text — the kind, the title big, the numbers
    (StatBlock), then Continue / Pick for me / Pin / ⋮. The wall (covers,
    tint and the fades into the page) is painted once into a pixmap and
    kept until the category, size, theme or a cover changes
    (`wall_builds` counts the builds)."""
    PICK = WALL_COVERS
    SHARE = 0.40                    # the glyph of an empty category keeps to this
    TINT_BY_KIND = True
    ROWS = 4
    ROW_OFFSETS = (0, -70, -20)     # each row's shift, at the 372 px design height
    DESIGN_H = 372
    COMPACT_H = 280                      # a floor: at Size 1.5 the content decides
    TEXT_SHARES = (0.56, 0.60)      # of the width: wide, compact
    # The page colour over the wall, left to right: (where, alpha), where 0
    # is just right of the text (_solid_stop), 1 the right edge, -1 the left.
    FADE_STOPS = ((-1, 1.0), (0.0, 1.0), (0.27, 0.72), (0.70, 0.15), (1.0, 0.35))
    LOW_FADE = 0.65                 # the page colour rises from the bottom to this height

    def __init__(self):
        super().__init__()
        self._mosaic_init()
        self._actions_init()
        self.theatre = False
        self._title = ''
        self._compact: bool | None = None
        self._col_w = 0
        self._kind_css = ''
        self._layer_init()
        self.wall_builds = 0
        self.setObjectName("CoverWall")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(scaled(self.DESIGN_H))
        px = lambda n: n                          # noqa: E731
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['6'], SP['4'], SP['6'], SP['5'])
        lay.setSpacing(0)
        self.col = QWidget()
        col = QVBoxLayout(self.col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self._build_back(), 0, Qt.AlignmentFlag.AlignLeft)
        col.addSpacing(SP['6'])
        kind = QHBoxLayout()
        kind.setSpacing(SP['2'])
        self.kind_icon = icon_label('', 16, P['subtext'])
        kind.addWidget(self.kind_icon)
        self.kind = label("", "CatKind")
        kind.addWidget(self.kind)
        kind.addStretch()
        col.addLayout(kind)
        self.title = label("", "CatWallTitle")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.title)
        col.addSpacing(SP['4'])
        self.stats = StatBlock()
        col.addWidget(self.stats)
        col.addSpacing(SP['5'])
        col.addLayout(self._build_actions(px))
        col.addStretch(1)                  # spare height under the actions, as in the mock
        self.col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.col, 1)
        lay.addStretch(1)

    def set_category(self, cat: categories.Category, stats: categories.Stats | None = None,
                     lead: dict | None = None):
        if self._cat is None or self._cat.key != cat.key:
            self._layer_drop()                            # never another category's wall
        self._set_mosaic(cat)
        self._title = self._title_text(cat)
        self.kind_icon.setText(cat.glyph)
        self.kind.setText(self._kind_text(cat))
        self.title.setToolTip(cat.title)
        self.stats.set_items(self._items(cat, stats or categories.stats(cat, {})))
        self.stats.setToolTip(cat.tip)
        self._set_lead(lead)
        self.btn_more.setVisible(cat.kind not in ('fav', 'store'))
        self._fit()

    def _title_text(self, cat: categories.Category) -> str:
        return cat.title

    def _kind_text(self, cat: categories.Category) -> str:
        return KIND_WORDS.get(cat.kind, '').upper()

    def _items(self, cat: categories.Category, st: categories.Stats) -> list[tuple[str, str]]:
        return stat_items(st)

    # ---- fitting ----
    TITLE_MIN = 0.6                  # the smallest a long title shrinks to, of its size

    def _shrink_title(self, w: int):
        """A title wider than its column gets a smaller font (down to
        TITLE_MIN) before it is elided: Windows' wider fonts cut "SQUARE
        ENIX" to "SQUARE E…" on a studio page at 1388 px."""
        css = ""
        if self.title.styleSheet():
            self.title.setStyleSheet("")          # measure at the theme's size
        self.title.ensurePolished()
        f = self.title.font()
        px = f.pixelSize() if f.pixelSize() > 0 else round(f.pointSizeF() * 96 / 72)
        adv = QFontMetrics(f).horizontalAdvance(self._title)
        if adv > w and px > 0:
            css = f"font-size: {max(int(px * self.TITLE_MIN), int(px * w / adv))}px;"
        if css:
            self.title.setStyleSheet(css)

    def _fit(self):
        """Compact below ~1100 px (a smaller title, the text a little
        wider); the title on one line; Continue's name cut; the header as
        tall as its text needs (never under the design height)."""
        compact = self.width() < scaled(1100)
        if compact != self._compact:
            self._compact = compact
            self.title.setProperty('compact', 'true' if compact else 'false')
            self.stats.layout().setHorizontalSpacing(SP['3'] if compact else SP['5'])
            st = self.title.style()
            st.unpolish(self.title)
            st.polish(self.title)
        m = self.layout().contentsMargins()
        w = max(120, int(self.width() * self.TEXT_SHARES[1 if compact else 0]) - m.left())
        self._col_w = w + m.left()
        self.col.setMaximumWidth(w)
        self._shrink_title(w)
        self.title.setText(self.title.fontMetrics().elidedText(
            self._title, Qt.TextElideMode.ElideRight, w))
        role = self._tint_role()
        css = f"color: {P[role]};"
        if css != self._kind_css:
            self._kind_css = css
            self.kind.setStyleSheet(css)
        self.stats.fit(w)
        self._fit_continue(w)
        self.col.layout().invalidate()
        need = self.col.sizeHint().height() + m.top() + m.bottom()
        h = max(scaled(self.COMPACT_H if compact else self.DESIGN_H), need)
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange):
            self._fit()

    # ---- the wall ----
    def _cover_arrived(self):
        self._layer_stale()                # built again soon (one build for a burst)

    def _rebuild_wall(self):
        self._layer_rebuild()

    def _solid_stop(self) -> float:
        """Where the page colour stops being solid: just right of the text."""
        w = max(1, self.width())
        return min(0.85, max(0.2, (self._col_w + SP['4']) / w))

    def _wall_pixmap(self) -> QPixmap | None:
        w, h = self.width(), self.height()
        if w < 2 or h < 2 or self._cat is None:
            return None
        dpr = screen_dpr()
        stop = round(self._solid_stop(), 3)
        contrast = bool(LOOK.get('contrast'))
        key = (self._cat.key, tuple(self._slugs), w, h, dpr, P['bg'], P['surface'],
               is_light(), contrast, stop, self._tint().name())
        return self._layer(key, lambda: self._build_wall(w, h, dpr, stop, contrast))

    def _build_wall(self, w: int, h: int, dpr: float, stop: float, contrast: bool) -> QPixmap:
        self.wall_builds += 1
        layer = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        layer.setDevicePixelRatio(dpr)
        base = QColor(P['surface'] if contrast else P['bg'])
        layer.fill(base)
        pt = QPainter(layer)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not contrast:
            tint = QColor(self._tint())
            tint.setAlphaF(self.TINT_ALPHA[1] if is_light() else self.TINT_ALPHA[0])
            clear = QColor(tint)
            clear.setAlphaF(0.0)
            grad = QLinearGradient(0.0, 0.0, float(w), float(h))
            grad.setColorAt(0.0, tint)
            grad.setColorAt(1.0, clear)
            pt.fillRect(QRectF(0.0, 0.0, float(w), float(h)), grad)
        if self._slugs:
            self._paint_wall(pt, w, h)
        fade = QLinearGradient(0.0, 0.0, float(w), 0.0)
        for at, alpha in self.FADE_STOPS:      # at: 0 = the solid stop, 1 = the right edge
            c = QColor(base)
            c.setAlphaF(alpha)
            fade.setColorAt(0.0 if at < 0 else min(1.0, stop + (1 - stop) * at), c)
        pt.fillRect(QRectF(0.0, 0.0, float(w), float(h)), fade)
        low = QLinearGradient(0.0, float(h), 0.0, h * self.LOW_FADE)
        c0, c1 = QColor(base), QColor(base)
        c1.setAlphaF(0.0)
        low.setColorAt(0.0, c0)
        low.setColorAt(1.0, c1)
        pt.fillRect(QRectF(0.0, 0.0, float(w), float(h)), low)
        pt.end()
        return layer

    def _paint_wall(self, pt: QPainter, w: int, h: int):
        """Rows of covers, tilted: the wall behind the text."""
        k = h / self.DESIGN_H
        cw = max(1, round(h * 118 / self.DESIGN_H))
        ch = max(1, round(cw * 1.5))
        gap = cw * 14 / 118
        step = cw + gap
        cols = int(w * 0.9 / step) + 3
        slugs = self._slugs
        n = len(slugs)
        pt.save()
        pt.translate(QPointF(w * 0.33, -90.0 * k))
        pt.rotate(-9.0)
        pt.setOpacity(0.75)
        for i in range(self.ROWS):
            y = i * (ch + gap)
            x0 = self.ROW_OFFSETS[i % len(self.ROW_OFFSETS)] * k
            for c in range(cols):
                rect = QRectF(x0 + c * step, y, float(cw), float(ch))
                cp = QPainterPath()
                cp.addRoundedRect(rect, R['sm'], R['sm'])
                pt.save()
                pt.setClipPath(cp)
                pt.drawPixmap(rect.topLeft(), self._cover(slugs[(c + 2 * i) % n], cw, ch))
                pt.restore()
        pt.restore()

    def paintEvent(self, _ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        rad = min(self.RADIUS, r.height() / 2)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        pt.save()
        pt.setClipPath(path)
        wall = self._wall_pixmap()
        if wall is not None:
            pt.drawPixmap(r, wall, QRectF(wall.rect()))     # scaled while it is stale
        else:
            pt.fillRect(r, QColor(P['bg']))
        if self.glyph_shown():
            self._paint_glyph(pt, r)
        pt.restore()
        pt.setPen(QPen(QColor(P['border']), 2.0 if LOOK.get('contrast') else 1.0))
        pt.setBrush(Qt.BrushStyle.NoBrush)
        pt.drawPath(path)
        pt.end()


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


class MosaicTile(QPushButton, _Mosaic):
    """A category as a tile (More like this, the All categories hub): the
    glyph, title and count over its mosaic. Focusable; a click or Enter
    opens it (`opened`). set_category re-points it (the hub keeps its
    tiles across a refresh)."""
    opened = pyqtSignal(str)

    INSET = SP['3']
    SHARE = 0.55               # the text keeps the left half
    FADE = (0.45, 0.58)
    RADIUS = R['card']
    TINT_BY_KIND = True        # w-113: a genre sky, a smart shelf the accent, ...
    TINT_ALPHA = (0.30, 0.20)

    def __init__(self, cat: categories.Category):
        super().__init__()
        self._mosaic_init()
        self.key = cat.key
        self._look = None
        self._note: str | None = None
        self.setObjectName("MosaicTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(scaled(300), scaled(128))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        lay.setSpacing(SP['0_5'])
        self.glyph = icon_label('', 20, P['accent'])
        self.glyph.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.glyph)
        lay.addStretch()
        self._title = ''
        title = self.title = label("", "MosaicTitle")
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(title)
        self.count = label("", "MosaicCount")
        self.count.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.count)
        self.clicked.connect(lambda: self.opened.emit(self.key))
        self.set_category(cat)

    def set_category(self, cat: categories.Category):
        """Show `cat` (this tile's category refreshed, or another one)."""
        self.key = cat.key
        self.setToolTip(cat.tip)
        self.setAccessibleName(cat.title)
        role = self._tint_role(cat)
        if (cat.glyph, role) != self._look:
            self._look = (cat.glyph, role)
            set_icon_label(self.glyph, cat.glyph, 20, role)
        self._title = cat.title
        self._set_mosaic(cat)
        self._set_count()
        self._fit()

    def set_note(self, note: str | None):
        """The line under the title in place of "N games" ("8 games · 5
        shared" in More like this); None puts the count back."""
        self._note = note
        self._set_count()

    def _set_count(self):
        n = len(self._cat.games)
        self.count.setText(self._note or f"{n} game{'s' if n != 1 else ''}")

    def _fit(self):
        """The title in the width left of the cover fan, on up to two lines
        (broken between words), the second elided. Measured once the
        theme's font is on the label (after polish), and again when the
        size, style or font changes."""
        self.title.ensurePolished()
        w = self.text_width()
        self.title.setText('\n'.join(two_lines(self._title, self.title.fontMetrics(), w)))

    def text_width(self) -> int:
        """From the text's left edge to just before the first cover (the
        whole inner width with no cover)."""
        left = self.layout().contentsMargins().left()
        n = self.cover_count()
        if not n:
            return max(1, self.width() - 2 * left)
        cw, _ch, step = self._cover_geometry()
        x0 = self.width() - self.INSET - cw - step * (n - 1)
        return max(1, x0 - SP['2'] - left)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange, ev.Type.PolishRequest):
            self._fit()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._fit()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()                            # the hub sizes its tiles to the columns

    def paintEvent(self, _ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_mosaic(pt)
        if self.hasFocus() or self.underMouse():
            rad = min(self.RADIUS, self.height() / 2)
            pt.setPen(QPen(QColor(P['accent'] if self.hasFocus() else P['overlay']), 2.0))
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), rad, rad)
        pt.end()


class _WidthBox(QWidget):
    """A box that says when its width changes (the studio page sizes its
    cards to the columns that fit, scroll bar or not)."""
    width_changed = pyqtSignal(int)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if ev.size().width() != ev.oldSize().width():
            self.width_changed.emit(ev.size().width())


def _section_head(kicker: QLabel, note) -> QHBoxLayout:
    """A section's kicker with a muted note after it, on one baseline."""
    head = QHBoxLayout()
    head.setContentsMargins(0, 0, 0, 0)
    head.setSpacing(SP['3'])
    head.addWidget(kicker, 0, Qt.AlignmentFlag.AlignBottom)
    head.addWidget(note if isinstance(note, QLabel) else label(note, "StudioNote"), 0,
                   Qt.AlignmentFlag.AlignBottom)
    head.addStretch(1)
    return head


class CategoryPage(QWidget, _TileSignals):
    """One category's page in the Library (see the module docstring). The
    window fills it (set_category) from nexus/categories.py and refills it
    as the library changes; the sort and view are kept per category in
    `prefs` (category_sort, category_view).

    `theatre` (w-100): Theatre's page, the header `scale` times as big and
    the grid only, of `tile`-wide Theatre tiles; no cuts, view switch or
    More like this. Its sort is the saved per-category sort (Theatre's
    View sheet sets it); the view is never saved.

    A studio (w-114, a developer or publisher, never in Theatre) gets its
    own page in the same place: StudioHeader for the wall, a Developed /
    Published switch with the merged spellings, the timeline of its years,
    its games as cards (real GameTiles, so the window's calls work as in
    Rows) and Other studios you play. No cuts, sort or view switch; its
    view is never saved."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)
    back = pyqtSignal()                     # the crumb
    open_key = pyqtSignal(str)              # More like this
    play_lead = pyqtSignal(dict)            # Continue <game>
    pick = pyqtSignal()                     # Pick for me
    cat_menu = pyqtSignal(str, QPoint)      # ⋮ (key, where)
    shown_changed = pyqtSignal()            # a cut, sort or view changed what is shown
    pin_asked = pyqtSignal(str)             # Pin / Pinned (the category's key)

    DEFAULT_VIEW = 'rows'
    DEFAULT_SORT = 'played'

    def __init__(self, prefs, theatre: bool = False, scale: float = 1.0,
                 tile: int | None = None):
        super().__init__()
        self.prefs = prefs
        self.theatre = theatre
        self.cat: categories.Category | None = None
        self.cut: str | None = None
        self.view = self.DEFAULT_VIEW
        self._cuts: list[categories.Cut] = []
        self._cut_games: dict[str, list[dict]] = {}
        self._related_keys: tuple = ()
        self._studio_keys: tuple = ()
        self._facts: dict = {}
        self._chip_sig = None
        self._empty = ''
        self._shown: list[dict] = []
        self._rows: dict[str | None, ShelfRow] = {}        # cut key (None: All games) -> row
        self._row_tiles: dict[str, list[GameTile]] = {}
        self._sel: set = set()
        self._jobs = None
        self.setObjectName("CategoryPage")
        # The header and the cuts / sort / view bar: they move between the
        # bodies with the view (_place_top).
        self.top = QWidget()
        self.top.setObjectName("CategoryTop")
        tl = QVBoxLayout(self.top)
        tl.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['2'])
        tl.setSpacing(SP['3'])
        self.header = MosaicHeader(theatre, scale) if theatre else CoverWallHeader()
        self._wall_header = self.header
        self.studio_header: StudioHeader | None = None      # made on the first studio
        self._studio_mode = False
        self._others_keys: tuple = ()
        self._cards_sig = None
        self._connect_header(self.header)
        tl.addWidget(self.header)
        self.bar = QWidget()                    # cuts, sort, view (not in Theatre)
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(SP['2'])
        self.chips_box = QWidget()
        self._chips = FlowLayout(self.chips_box, margin=0, spacing=SP['1_5'])
        bar.addWidget(self.chips_box, 1)
        bar.addStretch(0)
        self.sort = QComboBox()
        for text, key in SORT_NAMES:
            self.sort.addItem(text, key)
        self.sort.setAccessibleName("Sort")
        self.sort.currentIndexChanged.connect(lambda _i: self.set_sort(self.sort.currentData()))
        bar.addWidget(self.sort, 0, Qt.AlignmentFlag.AlignTop)
        self.view_seg = SegmentedControl('pill', 34)
        for key, glyph, tip in (('rows', G['shelves'], "Rows — one per smart cut"),
                                ('grid', G['grid'], "Grid"), ('list', G['list'], "List")):
            self.view_seg.add(key, glyph=glyph, tip=tip)
        self.view_seg.changed.connect(self.set_view)
        bar.addWidget(self.view_seg, 0, Qt.AlignmentFlag.AlignTop)
        tl.addWidget(self.bar)
        self.bar.setVisible(not theatre)
        # A studio's switch and timeline (w-114), shown in studio mode only.
        self.studio_bar = QWidget()
        sb = QHBoxLayout(self.studio_bar)
        sb.setContentsMargins(0, SP['2'], 0, 0)
        sb.setSpacing(SP['3'])
        self.role_seg = SegmentedControl('pill', 34)
        self.role_seg.add('dev', "Developed")
        self.role_seg.add('pub', "Published")
        self.role_seg.setAccessibleName("Credit")
        self.role_seg.changed.connect(self._role_picked)
        sb.addWidget(self.role_seg, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.merged = label("", "StudioMerged", wrap=True)
        sb.addWidget(self.merged, 1, Qt.AlignmentFlag.AlignVCenter)
        sb.addStretch(0)
        tl.addWidget(self.studio_bar)
        self.studio_bar.hide()
        self.timeline_wrap = QWidget()
        tw = QVBoxLayout(self.timeline_wrap)
        tw.setContentsMargins(0, SP['5'], 0, 0)
        tw.setSpacing(SP['3'])
        self.timeline_title = label("", "SectionKicker")
        tw.addLayout(_section_head(self.timeline_title, "By release date, sized by hours played"))
        self.timeline = StudioTimeline()
        tw.addWidget(self.timeline)
        tl.addWidget(self.timeline_wrap)
        self.timeline_wrap.hide()
        # Never taller than it needs: a short grid in a tall window would
        # otherwise hand the spare height to the head (a gap over the games).
        self.top.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        # More like this, under the games (Rows and Grid).
        self.foot = QWidget()
        self.foot.setObjectName("CategoryFoot")
        fl = QVBoxLayout(self.foot)
        fl.setContentsMargins(SP['5'], SP['5'], SP['5'], SP['6'])
        fl.setSpacing(SP['3'])
        # Other studios you play (w-114, a studio's page).
        self.others_wrap = QWidget()
        ow = QVBoxLayout(self.others_wrap)
        ow.setContentsMargins(0, 0, 0, 0)
        ow.setSpacing(SP['3'])
        ow.addLayout(_section_head(label("OTHER STUDIOS YOU PLAY", "SectionKicker"),
                                   "Ranked by your hours"))
        self.others_box = _WidthBox()
        self._others = FlowLayout(self.others_box, margin=0, spacing=SP['3'])
        self.others_box.width_changed.connect(self._size_others)
        ow.addWidget(self.others_box)
        fl.addWidget(self.others_wrap)
        self.others_wrap.hide()
        # The studios behind the games (w-113; their group comes with w-112).
        self.studios_wrap = QWidget()
        sw = QVBoxLayout(self.studios_wrap)
        sw.setContentsMargins(0, 0, 0, SP['3'])
        sw.setSpacing(SP['3'])
        sw.addWidget(label("STUDIOS BEHIND THESE GAMES", "SectionKicker"))
        self.studios_box = QWidget()
        self._studios = FlowLayout(self.studios_box, margin=0, spacing=SP['3'])
        sw.addWidget(self.studios_box)
        fl.addWidget(self.studios_wrap)
        self.studios_wrap.hide()
        self.related_wrap = QWidget()
        rw = QVBoxLayout(self.related_wrap)
        rw.setContentsMargins(0, 0, 0, 0)
        rw.setSpacing(SP['3'])
        rw.addWidget(label("MORE LIKE THIS", "SectionKicker"))
        self.related_box = QWidget()
        self._related = FlowLayout(self.related_box, margin=0, spacing=SP['4'])
        rw.addWidget(self.related_box)
        fl.addWidget(self.related_wrap)
        self.related_wrap.hide()
        self.foot.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        # The bodies.
        self.grid = LibraryView()
        self.grid.theatre = theatre
        self.grid.tile_width = tile or getattr(prefs, 'tile', self.grid.tile_width)
        self.listview = ListView()
        for v in (self.grid, self.listview):
            for name in ('clicked', 'play', 'menu', 'activated', 'double', 'pending_clicked'):
                getattr(v, name).connect(getattr(self, name))
        self.rows_view = QScrollArea()
        self.rows_view.setWidgetResizable(True)
        self.rows_view.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_host = QWidget()
        self._rows_lay = QVBoxLayout(self.rows_host)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(0)
        rows_box = QWidget()
        self._row_list = QVBoxLayout(rows_box)
        self._row_list.setContentsMargins(SP['5'], SP['3'], SP['5'], 0)
        self._row_list.setSpacing(SP['5'])
        self._rows_lay.addWidget(rows_box)
        self._rows_empty = label("", "Muted", wrap=True)
        self._row_list.addWidget(self._rows_empty)
        self._rows_empty.hide()
        # A studio's games as cards (w-114), in place of the rows.
        self.studio_games = QWidget()
        gw = QVBoxLayout(self.studio_games)
        gw.setContentsMargins(0, SP['2'], 0, 0)
        gw.setSpacing(SP['3'])
        self.games_note = label("", "StudioNote")
        gw.addLayout(_section_head(label("GAMES", "SectionKicker"), self.games_note))
        self.cards_box = _WidthBox()
        self._cards = FlowLayout(self.cards_box, margin=0, spacing=SP['4'])
        self.cards_box.width_changed.connect(self._size_cards)
        gw.addWidget(self.cards_box)
        self._row_list.addWidget(self.studio_games)
        self.studio_games.hide()
        self._rows_lay.addStretch(1)
        self.rows_view.setWidget(self.rows_host)
        self.list_top = QWidget()               # the strip over the List
        self._list_lay = QVBoxLayout(self.list_top)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        for w in (self.rows_view, self.grid, self.listview):
            self.stack.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.list_foot = QWidget()              # More like this under the List
        self._list_foot_lay = QVBoxLayout(self.list_foot)
        self._list_foot_lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.list_top)
        lay.addWidget(self.stack, 1)
        lay.addWidget(self.list_foot)
        self.list_top.hide()
        self.list_foot.hide()
        self._place_top()

    def _connect_header(self, h):
        h.back.connect(self.back)
        h.play_lead.connect(self.play_lead)
        h.pick.connect(self.pick)
        h.menu.connect(self._header_menu)
        h.pin.connect(self._pin)

    def _header_menu(self, pos: QPoint):
        if self.cat is not None:
            self.cat_menu.emit(self.cat.key, pos)

    # ---- filling ----
    def set_category(self, cat: categories.Category, stats: categories.Stats | None = None,
                     lead: dict | None = None, cuts=(), cut_games: dict | None = None,
                     related=(), empty: str = '', default_sort: str | None = None,
                     facts: dict | None = None, studios=(),
                     studio: categories.StudioFacts | None = None, others=()):
        """Show `cat` (a new one, or the same one refreshed). A new category
        starts on All with its own saved sort and view; a refresh keeps the
        cut while it still has games. `default_sort`: the sort to start a
        new category on when none was chosen for it (a store row's See all
        asks for Name, as the row was). `facts` (slug -> fact) give a smart
        row's covers their line ("2 h left"); `studios`: [(category,
        shared)] for the strip over More like this. A studio (not in
        Theatre) shows as a studio page: `studio` its numbers
        (categories.studio_facts), `others` [(studio, minutes)]
        (categories.other_studios)."""
        new = self.cat is None or self.cat.key != cat.key
        studio_mode = cat.kind in categories.STUDIO_KINDS and not self.theatre
        switched = studio_mode != self._studio_mode
        self.cat = cat
        self._cuts = list(cuts)
        self._cut_games = dict(cut_games or {})
        self._empty = empty
        self._facts = facts or {}
        if new or self.cut not in self._cut_games:
            self.cut = None
        self._set_studio_mode(studio_mode)
        if studio_mode:
            self.header.set_category(cat, stats, lead, studio)
        else:
            self.header.set_category(cat, stats, lead)
        self._fill_chips()
        self._fill_related(() if studio_mode else related)
        self._fill_studios(() if studio_mode else studios)
        self._fill_others(others if studio_mode else ())
        if studio_mode:
            self._fill_studio_top(cat, studio)
        self.foot.setVisible(self._foot_wanted())
        if new or switched:
            sort = self.prefs.category_sort.get(cat.key) or (
                default_sort if default_sort in categories.SORTS else self.DEFAULT_SORT)
            i = self.sort.findData(sort)
            self.sort.blockSignals(True)
            self.sort.setCurrentIndex(max(0, i))
            self.sort.blockSignals(False)
            self._show_view('grid' if self.theatre else 'rows' if studio_mode else
                            self.prefs.category_view.get(cat.key, self.DEFAULT_VIEW))
        self._apply()

    # ---- a studio's page (w-114) ----
    def _set_studio_mode(self, on: bool):
        """Swap the wall header for the studio one (or back), the cuts bar
        for the studio switch. The studio header is made on the first studio,
        in the layout before it shows; the focus moves to the header shown
        before the other hides."""
        if on == self._studio_mode:
            return
        self._studio_mode = on
        if on and self.studio_header is None:
            h = StudioHeader()
            self._connect_header(h)
            tl = self.top.layout()
            tl.insertWidget(tl.indexOf(self._wall_header) + 1, h)
            h.hide()
            self.studio_header = h
        old, new = ((self._wall_header, self.studio_header) if on
                    else (self.studio_header, self._wall_header))
        old_bar, new_bar = (self.bar, self.studio_bar) if on else (self.studio_bar, self.bar)
        new.show()
        new_bar.show()
        f = QApplication.focusWidget()
        if f is not None and (old.isAncestorOf(f) or old_bar.isAncestorOf(f)):
            new.btn_back.setFocus(Qt.FocusReason.OtherFocusReason)    # focus first, then hide
        old.hide()
        old_bar.hide()
        self.header = new
        if not on:
            self.timeline_wrap.hide()
            self._drop_cards()

    def _fill_studio_top(self, cat: categories.Category, sf: categories.StudioFacts | None):
        """The Developed N / Published N switch (a side with fewer than
        STUDIO_MIN games greyed out), the merged names, the timeline."""
        if sf is None:
            n = len(cat.games)
            sf = categories.StudioFacts(n if cat.kind == 'developer' else 0,
                                        n if cat.kind == 'publisher' else 0)
        mine = 'dev' if cat.kind == 'developer' else 'pub'
        for k, word, n in (('dev', "Developed", sf.developed), ('pub', "Published", sf.published)):
            b = self.role_seg.item(k)
            text = f"{word} {n}"
            if b.text() != text:
                b.setText(text)
            ok = k == mine or n >= categories.STUDIO_MIN
            b.setEnabled(ok)
            b.setToolTip("" if ok else f"Fewer than {categories.STUDIO_MIN} of your games")
        if self.role_seg.current() != mine:
            self.role_seg.set_current(mine, animate=False)
        line = merged_line(sf.names)
        self.merged.setText(line)
        self.merged.setVisible(bool(line))
        years = categories.studio_years(cat, self._facts)
        self.timeline.set_years(cat, years)
        self.timeline_title.setText(f"YOUR {cat.title.upper()} YEARS")
        self.timeline_wrap.setVisible(bool(years))

    def _role_picked(self, key: str):
        """The switch: the same studio's other role (the window opens it,
        Back returns). The switch shows this page's role until it does."""
        if self.cat is None:
            return
        mine = 'dev' if self.cat.kind == 'developer' else 'pub'
        if key == mine:
            return
        self.role_seg.set_current(mine, animate=False)
        other = categories.studio_switch(self.cat.key)
        if other:
            self.open_key.emit(other)

    def _fill_others(self, others):
        """Other studios you play, as cards (hidden when none)."""
        others = list(others)
        keys = tuple((c.key, c.title, m, tuple(g.get('slug') for g in c.games))
                     for c, m in others)
        if keys == self._others_keys:
            return
        self._others_keys = keys
        f = QApplication.focusWidget()
        had = f.key if isinstance(f, StudioCard) and self.others_box.isAncestorOf(f) else None
        cards = []
        for c, m in others:
            card = StudioCard(c, m)
            card.opened.connect(self.open_key)
            cards.append(card)
        old = [self._others.itemAt(i).widget() for i in range(self._others.count())]
        while self._others.count():
            self._others.takeAt(0)
        self._size_others(cards=cards)
        self._others.add_widgets(cards)
        again = next((c for c in cards if c.key == had), None)
        if again is None and had is not None:
            again = cards[0] if cards else self.header.btn_back
        if again is not None:
            again.setFocus(Qt.FocusReason.OtherFocusReason)    # before the old ones hide
        for w in old:
            if w is not None:
                w.hide()
                w.deleteLater()
        self.others_wrap.setVisible(bool(cards))

    def _size_others(self, _w=None, cards=None):
        """As many columns as fit cards of 180 px (scaled), up to 6."""
        cards = cards if cards is not None else [
            self._others.itemAt(i).widget() for i in range(self._others.count())]
        gap, least = SP['3'], scaled(180)
        avail = max(1, self.others_box.width())
        cols = max(1, min(6, (avail + gap) // (least + gap)))
        w = max(1, (avail - gap * (cols - 1)) // cols)
        for c in cards:
            if c is not None and c.width() != w:
                c.setFixedWidth(w)

    def _drop_cards(self):
        while self._cards.count():
            w = self._cards.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._cards_sig = None
        self.studio_games.hide()

    def studio_cards(self) -> list:
        return [self._cards.itemAt(i).widget() for i in range(self._cards.count())]

    def _fill_studio_games(self):
        """The studio's games as cards, recently played first. Kept (and
        re-pointed at the fresh game dicts) while what they show is the
        same; else made again, the focus kept on the same game."""
        for row in self._rows.values():
            self._row_list.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows = {}
        games = self._shown
        facts = self._facts
        sig = tuple((g.get('slug'), g.get('display_name'), g.get('playtime_minutes'),
                     g.get('last_played'), categories.credit_line(facts.get(g.get('slug'))),
                     smart.progress(g, facts.get(g.get('slug')))) for g in games)
        if sig == self._cards_sig:
            for card, g in zip(self.studio_cards(), games):
                card.game = g
                card.tile.set_game(g)
        else:
            self._cards_sig = sig
            f = QApplication.focusWidget()
            had = f.game.get('slug') if isinstance(f, GameTile) and \
                self.cards_box.isAncestorOf(f) else None
            old = self.studio_cards()
            while self._cards.count():
                self._cards.takeAt(0)
            cards = [StudioGameCard(g, facts.get(g.get('slug')),
                                    self._tile(g, scaled(96))) for g in games]
            self._size_cards(cards=cards)
            self._cards.add_widgets(cards)
            again = next((c.tile for c in cards if c.game.get('slug') == had), None)
            if again is None and had is not None:
                again = cards[0].tile if cards else self.header.btn_back
            if again is not None:
                again.setFocus(Qt.FocusReason.OtherFocusReason)    # before the old ones hide
            for w in old:
                if w is not None:
                    w.hide()
                    w.deleteLater()
        self._row_tiles = {}
        for card in self.studio_cards():
            self._row_tiles.setdefault(card.game.get('slug'), []).append(card.tile)
            card.tile.set_selected(card.game.get('slug') in self._sel)
        n = len(games)
        self.games_note.setText(f"{n}  ·  recently played first")
        self.studio_games.setVisible(bool(games))
        self._rows_empty.setText(self._empty or NO_GAMES)
        self._rows_empty.setVisible(not games)
        if self._jobs is not None:
            self.set_jobs(self._jobs)

    def _size_cards(self, _w=None, cards=None):
        """One to three columns of cards at least 380 px (scaled) wide."""
        cards = cards if cards is not None else self.studio_cards()
        gap = SP['4']
        avail = max(1, self.cards_box.width())
        cols = max(1, min(3, (avail + gap) // (scaled(380) + gap)))
        w = max(1, (avail - gap * (cols - 1)) // cols)
        for c in cards:
            if c is not None and c.width() != w:
                c.setFixedWidth(w)

    def set_pin_state(self, state: str):
        """The header's Pin (categories.pin_state)."""
        self.header.set_pin_state(state)

    def _pin(self):
        if self.cat is not None:
            self.pin_asked.emit(self.cat.key)

    def _fill_chips(self):
        sig = (self.cat.key, len(self.cat.games), tuple((c.key, c.count) for c in self._cuts))
        if sig != self._chip_sig:
            self._chip_sig = sig
            clear_layout(self._chips)
            made = [self._chip(None, "All", None, len(self.cat.games))]
            made += [self._chip(c.key, c.title, c.glyph, c.count) for c in self._cuts]
            self._chips.add_widgets(made)
        for b in self.chip_buttons():
            b.setChecked(b._cut == self.cut)

    def _chip(self, key, title, glyph, count) -> QPushButton:
        b = QPushButton(f"{title}  {count}".replace('&', '&&'))
        b.setObjectName("CatCut")
        b.setCheckable(True)
        b.setFixedHeight(scaled(38))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b._cut = key
        if glyph:
            set_glyph_icon(b, glyph, 14, 'subtext', on_role='on_accent')
        b.clicked.connect(lambda _=False, k=key: self.set_cut(k))
        return b

    def chip_buttons(self) -> list[QPushButton]:
        return [self._chips.itemAt(i).widget() for i in range(self._chips.count())
                if self._chips.itemAt(i).widget() is not None]

    def _fill_related(self, related):
        if self.theatre:
            related = ()
        mine = {g.get('slug') for g in self.cat.games} if self.cat is not None else set()
        shared = {c.key: len(mine & {g.get('slug') for g in c.games}) for c in related}
        keys = tuple((c.key, len(c.games), shared[c.key]) for c in related)
        if keys == self._related_keys:
            return
        self._related_keys = keys
        clear_layout(self._related)
        tiles = []
        for c in related:
            t = MosaicTile(c)
            n = len(c.games)
            t.set_note(f"{n} game{'s' if n != 1 else ''} · {shared[c.key]} shared")
            t.opened.connect(self.open_key)
            tiles.append(t)
        self._related.add_widgets(tiles)
        self.related_wrap.setVisible(bool(tiles))

    def _fill_studios(self, studios):
        """The strip of studios behind these games (hidden when none)."""
        if self.theatre:
            studios = ()
        studios = list(studios)
        keys = tuple((c.key, len(c.games), n) for c, n in studios)
        if keys == self._studio_keys:
            return
        self._studio_keys = keys
        clear_layout(self._studios)
        chips = []
        for c, n in studios:
            chip = StudioChip(c, n)
            chip.opened.connect(self.open_key)
            chips.append(chip)
        self._studios.add_widgets(chips)
        self.studios_wrap.setVisible(bool(chips))

    def _foot_wanted(self) -> bool:
        return self._related.count() > 0 or self._studios.count() > 0 \
            or self._others.count() > 0

    # ---- cut, sort, view ----
    def set_cut(self, key: str | None):
        """A cut button. In Rows every cut is already a row: its button
        opens that cut's Grid (as the row's See all does)."""
        if self.view == 'rows':
            if key in self._cut_games:
                self._see_cut(key)
            else:
                self._fill_chips()                   # All: where Rows already is
            return
        self.cut = key if key in self._cut_games else None
        for b in self.chip_buttons():
            b.setChecked(b._cut == self.cut)
        self._apply()

    def set_sort(self, sort: str):
        if sort not in categories.SORTS:
            return
        if self.sort.currentData() != sort:
            self.sort.blockSignals(True)
            self.sort.setCurrentIndex(self.sort.findData(sort))
            self.sort.blockSignals(False)
        if self.cat is not None:
            self._remember(self.prefs.category_sort, sort)
        self._apply()

    def set_view(self, view: str):
        if view not in categories.VIEWS or self.theatre or self._studio_mode:
            return
        if self.cat is not None:
            self._remember(self.prefs.category_view, view)
        if view != self.view:
            self._show_view(view)
            self._apply()

    def _remember(self, store: dict, value: str):
        key = self.cat.key
        if store.get(key) != value:
            store.pop(key, None)                     # the newest last (the cap keeps those)
            store[key] = value
            self.prefs.save()

    def _show_view(self, view: str):
        self.view = 'grid' if self.theatre else \
            view if view in categories.VIEWS else self.DEFAULT_VIEW
        if self.view_seg.current() != self.view:
            self.view_seg.set_current(self.view, animate=False)
        self._place_top()
        self.stack.setCurrentWidget({'rows': self.rows_view, 'grid': self.grid,
                                     'list': self.listview}[self.view])
        if self.view == 'rows':
            self.cut = None

    def _place_top(self):
        """Put the header and bar (and More like this) into the body on
        show. Added to the new layout before they are shown: a widget shown
        while it has no parent is a window."""
        f = QApplication.focusWidget()
        had = f if f is not None and self.top.isAncestorOf(f) else None
        for lay in (self._rows_lay, self._list_lay, self._list_foot_lay):
            for w in (self.top, self.foot):
                if lay.indexOf(w) >= 0:
                    lay.removeWidget(w)
        self.grid.set_head(None)
        self.grid.set_foot(None)
        if self.view == 'grid':
            self.grid.set_head(self.top)
            self.grid.set_foot(self.foot)
        elif self.view == 'rows':
            self._rows_lay.insertWidget(0, self.top)
            self._rows_lay.insertWidget(self._rows_lay.count() - 1, self.foot)
        else:
            self._list_lay.addWidget(self.top)
            self._list_foot_lay.addWidget(self.foot)
        self.list_top.setVisible(self.view == 'list')
        self.list_foot.setVisible(self.view == 'list')
        self.top.show()
        self.foot.setVisible(self._foot_wanted())
        if had is not None and alive(had):
            had.setFocus(Qt.FocusReason.OtherFocusReason)

    def _apply(self):
        """Fill the body on show with the cut, sorted."""
        if self.cat is None:
            return
        sort = 'played' if self._studio_mode else self.sort.currentData() or self.DEFAULT_SORT
        pool = self._cut_games.get(self.cut, self.cat.games) if self.cut else self.cat.games
        self._shown = categories.sort_games(pool, sort)
        empty = self._empty or NO_GAMES
        if self.view == 'grid':
            self.grid.set_games(self._shown, empty)
            self._after_fill(self.grid)
        elif self.view == 'list':
            self.listview.set_games(self._shown, empty)
            self._after_fill(self.listview)
        elif self._studio_mode:
            self._fill_studio_games()
        else:
            self._fill_rows(sort)
        self.shown_changed.emit()

    def _after_fill(self, view):
        view.mark_selected(self._sel)
        if self._jobs is not None:
            view.set_jobs(self._jobs)
        if isinstance(view, LibraryView):
            view.set_friends_badge(self.friends_badge)

    def _fill_rows(self, sort: str):
        plan = [(c.key, c.title, self._cut_games.get(c.key, [])) for c in self._cuts]
        if self.cat.games:
            plan.append((None, "All games", list(self.cat.games)))
        old, self._rows = self._rows, {}
        for row in old.values():
            self._row_list.removeWidget(row)
        self._row_tiles = {}
        for key, title, games in plan:
            items = categories.sort_games(games, sort)[:categories.ROW_CAP]
            row = old.pop(key, None)
            if row is not None and row.title == title:
                row.set_games(items, lambda g, r=row: self._tile(g, r.tile_w))
                if row.count_label is not None:
                    row.count_label.setText(str(len(games)))
            else:
                if row is not None:
                    old[('old', key)] = row
                row = ShelfRow(title, HOME_TILE, count=len(games), see_all=True)
                row.see_all.connect(lambda k=key: self._see_cut(k))
                for g in items:
                    row.add(self._tile(g, HOME_TILE))
                row.finish()
            self._row_list.addWidget(row)
            row.show()
            self._rows[key] = row
            for t in row.tiles:
                self._row_tiles.setdefault(t.game.get('slug'), []).append(t)
                t.set_selected(t.game.get('slug') in self._sel)
        for row in old.values():
            row.hide()
            row.deleteLater()
        self._rows_empty.setText(self._empty or NO_GAMES)
        self._rows_empty.setVisible(not self.cat.games)
        self._tag_rows()
        if self._jobs is not None:
            self.set_jobs(self._jobs)

    def _tag_rows(self):
        """A smart row's covers carry a line: "2 h left" in Almost
        finished, "~12 h story" in Never played (the All games row, in the
        category of that rule)."""
        for key, row in self._rows.items():
            rule = key or self.cat.key
            for t in row.tiles:
                t.set_tag(categories.cover_tag(rule, t.game, self._facts.get(t.game.get('slug'))))

    def _tile(self, g: dict, w: int) -> GameTile:
        t = GameTile(g, width=w)
        self._wire(t)
        return t

    def _see_cut(self, key: str | None):
        """A row's See all (or its title): the grid on that cut."""
        self._show_view('grid')                  # this visit only: nothing saved
        self.cut = key if key in self._cut_games else None
        self._fill_chips()
        self._apply()

    # ---- what the window asks of a Library view ----
    def shown_games(self) -> list[dict]:
        """What Pick for me picks from: the cut in Grid and List, the whole
        category in Rows."""
        return list(self._shown)

    def row_titles(self) -> list[str]:
        return [r.title for r in self.rows()]

    def rows(self) -> list[ShelfRow]:
        return [self._row_list.itemAt(i).widget() for i in range(self._row_list.count())
                if isinstance(self._row_list.itemAt(i).widget(), ShelfRow)]

    def body_widget(self) -> QWidget:
        """The scrolled content of the body on show (renders, tests)."""
        return {'rows': self.rows_host, 'grid': self.grid.container,
                'list': self.listview}[self.view]

    @property
    def tiles(self) -> dict:
        if self.view == 'grid':
            return self.grid.tiles
        return self._row_tiles if self.view == 'rows' else {}

    @property
    def games(self) -> dict:
        if self.view == 'grid':
            return self.grid.games
        if self.view == 'list':
            return self.listview.games
        return {g['slug']: i for i, g in enumerate(self._shown)}

    def tile_for(self, slug: str | None = None):
        if self.view == 'grid':
            return self.grid.tile_for(slug)
        if self.view == 'list':
            return self.listview if self.listview.rowCount() else None
        ts = self._row_tiles.get(slug) if slug else None
        if ts:
            return ts[0]
        if slug:
            return None
        rows = self.rows()
        first = next((r.tiles[0] for r in rows if r.tiles), None)
        if first is None and self._studio_mode:
            first = next((c.tile for c in self.studio_cards()), None)
        return first

    def reveal(self, slug: str):
        if self.view == 'grid':
            self.grid.reveal(slug)
        elif self.view == 'list':
            self.listview.reveal(slug)
        else:
            t = self.tile_for(slug)
            if t is not None:
                self.rows_view.ensureWidgetVisible(t)

    def mark_selected(self, slugs):
        self._sel = set(slugs)
        self.grid.mark_selected(self._sel)
        self.listview.mark_selected(self._sel)
        for s, ts in self._row_tiles.items():
            for t in ts:
                t.set_selected(s in self._sel)

    def set_jobs(self, jobs):
        self._jobs = jobs
        self.grid.set_jobs(jobs)
        by = jobs.tile_jobs()
        for s, ts in self._row_tiles.items():
            for t in ts:
                t.set_job(by.get(s))

    def _each_tile(self):
        for s, ts in self._row_tiles.items():
            for t in ts:
                yield s, t

    def set_friends_badge(self, on: bool):
        _TileSignals.set_friends_badge(self, on)
        self.grid.set_friends_badge(on)

    def refresh_friends(self):
        _TileSignals.refresh_friends(self)
        self.grid.refresh_friends()


class CategoryTabs(QWidget):
    """Pinned categories as underline tabs (w-100): Browse (or All games),
    each pin, All categories. `set_tabs` rebuilds the tabs only when they
    change (the same ones just move `current`); `picked(key)` is a click
    or A on one. Titles are cut at TITLE_CHARS, and the pins' shortened
    further, the long ones first, when the strip is too narrow for them
    all (the first and last tab, the way back, stay whole)."""
    picked = pyqtSignal(str)

    TITLE_CHARS = 18
    MIN_TEXT = 40                           # px a squeezed tab keeps for its text

    def __init__(self, height: int = 36, margins: tuple = (0, 0, 0, 0), parent=None):
        super().__init__(parent)
        self.setObjectName("CategoryTabs")
        self._h = height
        self._sig = None
        self._divider_after: str | None = None
        self._titles: dict[str, str] = {}
        self.seg: SegmentedControl | None = None
        self.divider: QFrame | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*margins)
        lay.setSpacing(0)
        # The tabs that don't fit, behind "More" (w-118: a narrow window or
        # a big Size folds the page tabs first).
        self.folded: list[str] = []
        self.more = QPushButton("More")
        self.more.setObjectName("TabItem")
        self.more.setCheckable(True)
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.setFixedHeight(height)
        set_glyph_icon(self.more, G['chevron_down'], 14,
                       'muted', on_role='text')
        self.more.clicked.connect(self._more_menu)
        self.more.hide()
        lay.addWidget(self.more)
        lay.addStretch(1)
        # As wide as the row gives it: the titles fit that (_fit), not the
        # other way round.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height + margins[1] + margins[3])

    def set_tabs(self, tabs, current: str | None, divider_after: str | None = None):
        """`tabs`: [(key, title)] in order; a thin divider follows the tab
        `divider_after` (w-116: Browse · All games │ pins). Never emits
        `picked`."""
        tabs = [(k, t) for k, t in tabs]
        sig = (tuple(tabs), divider_after)
        if sig != self._sig:
            self._sig = sig
            self._divider_after = divider_after
            self._rebuild(tabs, current)
        elif self.seg is not None and self.seg.current() != (current or ''):
            self.seg.set_current(current if current in self._titles else None,
                                 animate=self.isVisible())

    def _rebuild(self, tabs, current):
        old = self.seg
        f = QApplication.focusWidget()
        had = old is not None and f is not None and old.isAncestorOf(f)
        seg = SegmentedControl('underline', self._h, self)
        self._titles = {}
        for key, title in tabs:
            short = _short(title, self.TITLE_CHARS)
            b = seg.add(key, short.replace('&', '&&'))
            b.setToolTip(title)
            b.setAccessibleName(title)
            self._titles[key] = short
        self.divider = None
        keys = [k for k, _ in tabs]
        after = self._divider_after
        if after in keys and keys.index(after) < len(keys) - 1:
            div = QFrame(seg)
            div.setObjectName("TopSep")               # the theme's hairline
            div.setFixedSize(1, max(12, self._h // 2))
            seg.layout().insertWidget(keys.index(after) + 1, div, 0,
                                      Qt.AlignmentFlag.AlignVCenter)
            self.divider = div
        seg.set_current(current if current in self._titles else None, animate=False)
        seg.changed.connect(self.picked)
        lay = self.layout()
        lay.insertWidget(0, seg)                 # in the layout before it shows
        self.seg = seg                           # before show: a resize then fits this one
        self.folded = []                         # a new strip: everything shows until _fit
        self.more.hide()
        seg.show()
        self._fit()
        if had and tabs:                         # focus first, then hide (w-60)
            b = seg.item(current) if current in self._titles else seg.item(tabs[0][0])
            b.setFocus(Qt.FocusReason.OtherFocusReason)
        if old is not None:
            lay.removeWidget(old)
            old.hide()
            old.deleteLater()

    def _fit(self):
        """The titles in the width there is: the shortest stay whole, the
        longer ones share what is left equally, elided. When even short
        titles don't fit, the last tabs (never Browse, All games or the
        current one) fold into More, the rightmost first."""
        seg = self.seg
        if seg is None or not self._titles:
            return
        keys = list(self._titles)
        buttons = {k: seg.item(k) for k in keys}
        if None in buttons.values():
            return
        for b in buttons.values():
            b.ensurePolished()
        b0 = buttons[keys[0]]
        fm = b0.fontMetrics()
        pad = b0.sizeHint().width() - fm.horizontalAdvance(b0.text())
        lay = seg.layout()
        m = lay.contentsMargins()
        om = self.layout().contentsMargins()
        room = self.width() - om.left() - om.right() - m.left() - m.right() \
            - ((self.divider.width() + lay.spacing()) if self.divider is not None else 0)
        textw = {k: fm.horizontalAdvance(self._titles[k]) for k in keys}
        cur = seg.current()
        keep = set(keys[:2]) | {cur}
        shown = list(keys)

        def least(ks):                      # every tab at its shortest (the kept ones whole)
            return sum(pad + (textw[k] if k in keep else min(textw[k], self.MIN_TEXT))
                       + lay.spacing() for k in ks)
        more_w = self.more.sizeHint().width()
        folded = []
        for k in reversed(keys):
            if least(shown) + (more_w if folded else 0) <= room:
                break
            if k in keep:
                continue
            shown.remove(k)
            folded.append(k)
        folded.reverse()
        if folded != self.folded:
            self.folded = folded
            for k in keys:
                seg.set_item_visible(k, k not in folded)
            self.more.setVisible(bool(folded))
        self.more.setChecked(cur in folded)
        avail = room - (more_w if folded else 0) - lay.spacing() * (len(shown) - 1) \
            - pad * len(shown)
        ends = {shown[0], shown[-1]} if len(shown) > 2 and not folded else set()
        ends |= keep & set(shown)                # Browse, All games, the current: whole
        avail -= sum(textw[k] for k in ends)
        need = sorted(textw[k] for k in shown if k not in ends)
        cap = None
        if sum(need) > avail:
            left, n = avail, len(need)           # water-fill: whole while under the cap
            for i, w in enumerate(need):
                if w * (n - i) > left:
                    cap = max(self.MIN_TEXT, left // (n - i))
                    break
                left -= w
        for key in keys:
            b = buttons[key]
            text = self._titles[key]
            if cap is not None and key not in ends and textw[key] > cap:
                text = fm.elidedText(text, Qt.TextElideMode.ElideRight, cap)
            text = text.replace('&', '&&')
            if b.text() != text:
                b.setText(text)

    def _more_menu(self):
        """The folded tabs as a menu; picking one is a tab pick."""
        self.more.setChecked(self.seg is not None and self.seg.current() in self.folded)
        if not self.folded:
            return
        m = QMenu(self)
        for k in self.folded:
            a = m.addAction(self._titles[k].replace('&', '&&'))
            a.setCheckable(True)
            a.setChecked(self.seg is not None and self.seg.current() == k)
            a.triggered.connect(lambda _=False, k=k: self.picked.emit(k))
        m.exec(self.more.mapToGlobal(QPoint(0, self.more.height())))
        m.deleteLater()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.StyleChange, ev.Type.FontChange):
            self._fit()

    # ---- what callers ask ----
    def current(self) -> str:
        return self.seg.current() if self.seg is not None else ''

    def keys(self) -> list[str]:
        return list(self._titles)

    def button(self, key: str):
        return self.seg.item(key) if self.seg is not None else None

    def buttons(self) -> list:
        return self.seg.buttons() if self.seg is not None else []


class CategoryHub(QScrollArea):
    """All categories (w-99): every category as a MosaicTile, grouped under
    the same kickers as Browse, rows you hid from Browse included. One of
    the Library's views like CategoryPage (the window routes it as the
    place key categories.HUB); it has no game tiles, so the view calls are
    no-ops and tile_for gives the tile to focus."""
    back = pyqtSignal()                     # the crumb
    open_key = pyqtSignal(str)              # a tile

    TILE_MIN = 260                          # a tile's narrowest (scaled); columns share the rest

    def __init__(self):
        super().__init__()
        self.setObjectName("CategoryHub")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        lay = self._lay = QVBoxLayout(host)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['6'])
        lay.setSpacing(SP['3'])
        self.btn_back = QPushButton("Library")
        self.btn_back.setObjectName("Ghost")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setToolTip("Back to the Library (Esc)")
        set_glyph_icon(self.btn_back, G['chevron_left'], 15, 'subtext')
        self.btn_back.clicked.connect(self.back)
        lay.addWidget(self.btn_back, 0, Qt.AlignmentFlag.AlignLeft)
        self.title = label("All categories", "CatTitle")
        lay.addWidget(self.title)
        self.sections_lay = QVBoxLayout()
        self.sections_lay.setContentsMargins(0, 0, 0, 0)
        self.sections_lay.setSpacing(SP['5'])
        lay.addLayout(self.sections_lay)
        lay.addStretch(1)
        self.setWidget(host)
        self._sig = None
        self._tiles: dict[str, MosaicTile] = {}
        self._sections: dict[str, tuple[QWidget, FlowLayout]] = {}
        self._order: list[str] = []                 # group keys, as shown
        self._last: str | None = None               # the tile opened last (Back lands there)
        self._jump: str | None = None

    # ---- filling ----
    def set_groups(self, groups):
        """Show `groups` (categories.categories(), uncapped). Nothing is
        touched when no tile's text, look or games changed (one game out and
        another in changes the covers); otherwise tiles are kept by key and
        re-pointed, and only new ones are made."""
        sig = tuple((g.key, g.title, tuple((c.key, c.title, c.tip, c.glyph, c.color,
                                            tuple(x.get('slug') for x in c.games))
                                           for c in g.cats))
                    for g in groups)
        if sig == self._sig:
            return
        self._sig = sig
        old_tiles, self._tiles = self._tiles, {}
        old_secs, self._sections = self._sections, {}
        for gkey in self._order:
            if gkey in old_secs:
                self.sections_lay.removeWidget(old_secs[gkey][0])
        for g in groups:
            sec = old_secs.pop(g.key, None)
            if sec is None:
                sec = self._section(g.title)
            box, flow = sec
            inner = flow.parentWidget()
            while flow.count():
                flow.takeAt(0)                       # the tiles stay children of the box
            tiles = []
            for c in g.cats:
                t = old_tiles.pop(c.key, None)
                if t is None:
                    t = MosaicTile(c)
                    t.opened.connect(self._opened)
                else:
                    t.set_category(c)
                if t.parentWidget() is not inner:
                    t.setParent(inner)
                tiles.append(t)
                self._tiles[c.key] = t
            self._size_tiles(tiles)
            flow.add_widgets(tiles)
            self.sections_lay.addWidget(box)
            box.show()
            self._sections[g.key] = sec
        self._order = [g.key for g in groups]
        for t in old_tiles.values():
            t.hide()
            t.deleteLater()
        for box, _flow in old_secs.values():
            box.hide()
            box.deleteLater()

    def tile_width(self) -> int:
        """As many columns as fit tiles of TILE_MIN, sharing the width."""
        m = self._lay.contentsMargins()
        avail = max(1, self.viewport().width() - m.left() - m.right())
        gap, least = SP['4'], scaled(self.TILE_MIN)
        cols = max(1, (avail + gap) // (least + gap))
        return max(least if cols > 1 else 1, (avail - gap * (cols - 1)) // cols)

    def _size_tiles(self, tiles):
        w, h = self.tile_width(), scaled(128)
        for t in tiles:
            if t.width() != w or t.height() != h:
                t.setFixedSize(w, h)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._size_tiles(self._tiles.values())

    def _section(self, title: str) -> tuple[QWidget, FlowLayout]:
        box = QWidget(self.widget())
        box.setObjectName("HubSection")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, SP['2'], 0, 0)
        lay.setSpacing(SP['2'])
        lay.addWidget(label(title.upper(), "SectionKicker"))
        tiles = QWidget()
        flow = FlowLayout(tiles, margin=0, spacing=SP['4'])
        lay.addWidget(tiles)
        return box, flow

    def _opened(self, key: str):
        self._last = key
        self.open_key.emit(key)

    # ---- where to go ----
    def group_keys(self) -> list[str]:
        return list(self._order)

    def tiles_in(self, gkey: str) -> list[MosaicTile]:
        sec = self._sections.get(gkey)
        if sec is None:
            return []
        flow = sec[1]
        return [flow.itemAt(i).widget() for i in range(flow.count())]

    def tile(self, key: str) -> MosaicTile | None:
        return self._tiles.get(key)

    def jump_to(self, gkey: str):
        """Scroll to a group (All genres): now, and once more after the next
        layout pass (a child timer on a bound method, so it dies with the
        hub). The group's first tile becomes tile_for(), where the window
        puts the focus (keys or pad, no overlay up); the hub never moves
        the focus itself, least of all from a timer."""
        tiles = self.tiles_in(gkey)
        if not tiles:
            return
        self._jump = gkey
        self._last = tiles[0].key                   # Back and the focus land here
        self._scroll_to_jump()
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(self._scroll_to_jump)
        t.timeout.connect(t.deleteLater)
        t.start(0)

    def _scroll_to_jump(self):
        sec = self._sections.get(self._jump or '')
        if sec is None:
            return
        self.widget().layout().activate()
        y = sec[0].mapTo(self.widget(), QPoint(0, 0)).y()
        self.verticalScrollBar().setValue(max(0, y - SP['2']))

    # ---- what the window asks of a Library view (no game tiles here) ----
    @property
    def tiles(self) -> dict:
        return {}

    @property
    def games(self) -> dict:
        return {}

    def tile_for(self, slug: str | None = None):
        """The tile to focus: the one opened last, else the first."""
        if slug:
            return None
        t = self._tiles.get(self._last) if self._last else None
        if t is not None:
            return t
        for gkey in self._order:
            ts = self.tiles_in(gkey)
            if ts:
                return ts[0]
        return None

    def shown_games(self) -> list[dict]:
        return []

    def reveal(self, _slug):
        pass

    def mark_selected(self, _slugs):
        pass

    def set_jobs(self, _jobs):
        pass

    def set_friends_badge(self, _on):
        pass

    def refresh_friends(self):
        pass
