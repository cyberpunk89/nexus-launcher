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

from PyQt6.QtCore import pyqtSignal, QPoint, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from .. import categories
from .art import ART, art_lookup, COVER_KINDS, placeholder_pixmap, screen_dpr
from .common import alive
from .gameinfo import is_installed
from .library import _TileSignals, LibraryView, ListView, ShelfRow
from .theme import FONTS, G, is_light, LOOK, P, R, scaled, SP
from .tiles import GameTile
from .widgets import (clear_layout, FlowLayout, icon_button, icon_label, label,
    SegmentedControl, set_glyph_icon, set_icon_label)

# What the header's kicker calls each kind.
KIND_WORDS = {'smart': "Smart shelf", 'genre': "Genre", 'fav': "Yours",
              'collection': "Collection", 'shelf': "Your shelf", 'store': "Store"}
SORT_NAMES = (("Recently played", 'played'), ("Name", 'name'), ("Most played", 'most'),
              ("Recently added", 'added'), ("Size on disk", 'size'))
COVER_MAX = 6
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
    """'14 games · 228 h played · 5 finished · 426 GB on disk' (parts with
    nothing to say left out)."""
    parts = [f"{st.count} game{'s' if st.count != 1 else ''}"]
    if st.minutes >= 60:
        parts.append(f"{round(st.minutes / 60):,} h played")
    elif st.minutes:
        parts.append(f"{st.minutes} min played")
    if st.finished:
        parts.append(f"{st.finished} finished")
    if st.bytes:
        gb = st.bytes / 1e9
        parts.append(f"{gb:,.0f} GB on disk" if gb >= 10 else f"{gb:.1f} GB on disk")
    return "  ·  ".join(parts)


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
            self._slugs = [g['slug'] for g in categories.covers(cat, COVER_MAX)]
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
            ART.request_key(key, lambda _pm, w=self: alive(w) and w.update())
        name = g.get('display_name') or slug
        return pm if pm is not None else placeholder_pixmap(' '.join(name.split()[:2]), cw, ch)

    def _tint(self) -> QColor:
        color = self._cat.color if self._cat is not None else None
        return QColor(P.get(f'col_{color}', P['accent']) if color else P['accent'])

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
            a.setAlphaF(0.18 if is_light() else 0.28)
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


class MosaicHeader(QWidget, _Mosaic):
    """The category page's header: a crumb back to the Library, the kind,
    the title and a story line (games, hours, finished, size), then
    Continue <the game played last>, Pick for me, Pin and ⋮ — over the
    mosaic. In Theatre (`theatre`, w-100) it is `scale` times as tall and
    has no crumb (B goes back) and no ⋮."""
    back = pyqtSignal()
    play_lead = pyqtSignal(dict)
    pick = pyqtSignal()
    menu = pyqtSignal(QPoint)
    pin = pyqtSignal()                      # Pin / Pinned: toggle this category's tab

    TEXT_SHARE = 0.52

    def __init__(self, theatre: bool = False, scale: float = 1.0):
        super().__init__()
        self._mosaic_init()
        self.theatre = theatre
        self._lead: dict | None = None
        self._lead_name = ''                     # Continue's game, before _fit cuts it
        self._title = self._story = ''
        self._pin_state: str | None = None
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
        self.btn_back = QPushButton("Library")
        self.btn_back.setObjectName("Ghost")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setToolTip("Back to the Library (Esc)")
        set_glyph_icon(self.btn_back, G['chevron_left'], 15, 'subtext')
        self.btn_back.clicked.connect(self.back)
        col.addWidget(self.btn_back, 0, Qt.AlignmentFlag.AlignLeft)
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
        acts = QHBoxLayout()
        acts.setSpacing(SP['2'])
        self.btn_continue = QPushButton("")
        self.btn_continue.setObjectName("Primary")
        self.btn_continue.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn_continue, G['play'], px(15), 'on_accent', hover_role=None)
        self.btn_continue.clicked.connect(lambda: self._lead and self.play_lead.emit(self._lead))
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
        self.btn_more.clicked.connect(lambda: self.menu.emit(
            self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height()))))
        acts.addWidget(self.btn_more)
        acts.addStretch()
        col.addLayout(acts)
        # The text takes its share (_fit caps it), the covers the rest: with
        # Ignored labels the column would otherwise shrink to its buttons.
        self.col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.col, 1)
        lay.addStretch(1)

    def set_category(self, cat: categories.Category, stats: categories.Stats | None = None,
                     lead: dict | None = None):
        self._set_mosaic(cat)
        self._lead = lead
        self._title = cat.title
        self.kind_icon.setText(cat.glyph)
        self.kind.setText(KIND_WORDS.get(cat.kind, '').upper())
        self.title.setToolTip(cat.title)
        self._story = story(stats or categories.stats(cat, {}))
        self.story.setToolTip(cat.tip)
        self.btn_continue.setVisible(lead is not None)
        self._lead_name = ''
        if lead is not None:
            name = lead.get('display_name') or lead.get('slug') or ''
            self._lead_name = _short(name)
            self.btn_continue.setToolTip(f"Play {name}")
        self.btn_more.setVisible(cat.kind not in ('fav', 'store') and not self.theatre)
        self._fit()

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
        if self._lead_name:
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

    def __init__(self, cat: categories.Category):
        super().__init__()
        self._mosaic_init()
        self.key = cat.key
        self._look = None
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
        role = f'col_{cat.color}' if cat.color and f'col_{cat.color}' in P else 'accent'
        if (cat.glyph, role) != self._look:
            self._look = (cat.glyph, role)
            set_icon_label(self.glyph, cat.glyph, 20, role)
        self._title = cat.title
        n = len(cat.games)
        self.count.setText(f"{n} game{'s' if n != 1 else ''}")
        self._set_mosaic(cat)
        self._fit()

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


class CategoryPage(QWidget, _TileSignals):
    """One category's page in the Library (see the module docstring). The
    window fills it (set_category) from nexus/categories.py and refills it
    as the library changes; the sort and view are kept per category in
    `prefs` (category_sort, category_view).

    `theatre` (w-100): Theatre's page, the header `scale` times as big and
    the grid only, of `tile`-wide Theatre tiles; no cuts, view switch or
    More like this. Its sort is the saved per-category sort (Theatre's
    View sheet sets it); the view is never saved."""
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
        self.header = MosaicHeader(theatre, scale)
        self.header.back.connect(self.back)
        self.header.play_lead.connect(self.play_lead)
        self.header.pick.connect(self.pick)
        self.header.menu.connect(lambda pos: self.cat and self.cat_menu.emit(self.cat.key, pos))
        self.header.pin.connect(self._pin)
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
        # Never taller than it needs: a short grid in a tall window would
        # otherwise hand the spare height to the head (a gap over the games).
        self.top.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        # More like this, under the games (Rows and Grid).
        self.foot = QWidget()
        self.foot.setObjectName("CategoryFoot")
        fl = QVBoxLayout(self.foot)
        fl.setContentsMargins(SP['5'], SP['5'], SP['5'], SP['6'])
        fl.setSpacing(SP['3'])
        kick = label("MORE LIKE THIS", "SectionKicker")
        fl.addWidget(kick)
        self.related_box = QWidget()
        self._related = FlowLayout(self.related_box, margin=0, spacing=SP['4'])
        fl.addWidget(self.related_box)
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

    # ---- filling ----
    def set_category(self, cat: categories.Category, stats: categories.Stats | None = None,
                     lead: dict | None = None, cuts=(), cut_games: dict | None = None,
                     related=(), empty: str = '', default_sort: str | None = None):
        """Show `cat` (a new one, or the same one refreshed). A new category
        starts on All with its own saved sort and view; a refresh keeps the
        cut while it still has games. `default_sort`: the sort to start a
        new category on when none was chosen for it (a store row's See all
        asks for Name, as the row was)."""
        new = self.cat is None or self.cat.key != cat.key
        self.cat = cat
        self._cuts = list(cuts)
        self._cut_games = dict(cut_games or {})
        self._empty = empty
        if new or self.cut not in self._cut_games:
            self.cut = None
        self.header.set_category(cat, stats, lead)
        self._fill_chips()
        self._fill_related(related)
        if new:
            sort = self.prefs.category_sort.get(cat.key) or (
                default_sort if default_sort in categories.SORTS else self.DEFAULT_SORT)
            i = self.sort.findData(sort)
            self.sort.blockSignals(True)
            self.sort.setCurrentIndex(max(0, i))
            self.sort.blockSignals(False)
            self._show_view('grid' if self.theatre else
                            self.prefs.category_view.get(cat.key, self.DEFAULT_VIEW))
        self._apply()

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
        b.setObjectName("Chip")
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b._cut = key
        if glyph:
            set_glyph_icon(b, glyph, 14, 'subtext', on_role='accent')
        b.clicked.connect(lambda _=False, k=key: self.set_cut(k))
        return b

    def chip_buttons(self) -> list[QPushButton]:
        return [self._chips.itemAt(i).widget() for i in range(self._chips.count())
                if self._chips.itemAt(i).widget() is not None]

    def _fill_related(self, related):
        if self.theatre:
            related = ()
        keys = tuple((c.key, len(c.games)) for c in related)
        if keys == self._related_keys:
            return
        self._related_keys = keys
        clear_layout(self._related)
        tiles = []
        for c in related:
            t = MosaicTile(c)
            t.opened.connect(self.open_key)
            tiles.append(t)
        self._related.add_widgets(tiles)
        self.foot.setVisible(bool(tiles))

    # ---- cut, sort, view ----
    def set_cut(self, key: str | None):
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
        if view not in categories.VIEWS or self.theatre:
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
        self.chips_box.setVisible(self.view != 'rows')
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
        self.foot.setVisible(self._related.count() > 0)
        if had is not None and alive(had):
            had.setFocus(Qt.FocusReason.OtherFocusReason)

    def _apply(self):
        """Fill the body on show with the cut, sorted."""
        if self.cat is None:
            return
        sort = self.sort.currentData() or self.DEFAULT_SORT
        pool = self._cut_games.get(self.cut, self.cat.games) if self.cut else self.cat.games
        self._shown = categories.sort_games(pool, sort)
        empty = self._empty or NO_GAMES
        if self.view == 'grid':
            self.grid.set_games(self._shown, empty)
            self._after_fill(self.grid)
        elif self.view == 'list':
            self.listview.set_games(self._shown, empty)
            self._after_fill(self.listview)
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
        if self._jobs is not None:
            self.set_jobs(self._jobs)

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
        rows = self.rows()
        return next((r.tiles[0] for r in rows if r.tiles), None) if not slug else None

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
