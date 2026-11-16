"""Shared pieces of the category pages: the kind words and constants, the text
helpers, the cover mosaic and the header bases (split out of category.py, w-128)."""

from PyQt6.QtCore import pyqtSignal, QPoint, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from .. import categories
from .art import ART, art_lookup, COVER_KINDS, placeholder_pixmap, screen_dpr
from .common import alive
from .gameinfo import is_installed
from .theme import FONTS, G, is_light, LOOK, P, R, scaled, SP
from .widgets import clear_layout, icon_button, icon_label, label, set_glyph_icon, set_icon_label


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
