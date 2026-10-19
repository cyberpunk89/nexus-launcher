"""The News page."""


from PyQt6.QtCore import pyqtSignal, QPoint, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QFontMetrics,
    QLinearGradient, QPainter, QPainterPath, QPixmap)
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .fmt import fmt_relative
from .theme import FONTS, FS, G, GRAD, P, PAL, R, SP


NEWS_STYLE = {'steam': ('accent', 'tide', 'steam'), 'proton': ('success', 'aurora', 'proton')}
NEWS_FEED_STYLE = ('info', 'dusk', 'news')


def news_style(item: dict) -> tuple[str, str, str]:
    """(text role, gradient, glyph) for a story's kind."""
    return NEWS_STYLE.get(item.get('kind'), NEWS_FEED_STYLE)


def news_group(item: dict) -> str:
    """The chip a story belongs to."""
    kind = item.get('kind')
    return 'Your games' if kind == 'steam' else 'Proton' if kind == 'proton' else \
        item.get('source') or 'Other'


def news_image(item: dict, games_by_appid: dict) -> tuple[str | None, str | None]:
    """(local path, url still to fetch) for a story's picture: the feed's own
    image, else — for news about a game you have — that game's banner art
    (already on disk, so no network)."""
    url = item.get('image')
    if url:
        return ART.cached(url), (None if ART.cached(url) else url)
    game = games_by_appid.get(item.get('appid')) if item.get('kind') == 'steam' else None
    if game:
        path, url, _k = art_lookup(game, ('wide', 'hero'))
        return path, url
    return None, None


def elide_lines(text: str, fm: QFontMetrics, width: int, lines: int) -> str:
    """Word-wrap `text` into at most `lines` lines of `width` px, the last
    one elided — so a fixed-size card never clips mid-line."""
    words, out, cur = (text or '').split(), [], ''
    i = 0
    while i < len(words) and len(out) < lines:
        trial = f"{cur} {words[i]}".strip()
        if fm.horizontalAdvance(trial) <= width or not cur:
            cur = trial
            i += 1
        else:
            out.append(cur)
            cur = ''
    if cur and len(out) < lines:
        out.append(cur)
    if i < len(words) and out:
        rest = ' '.join([out[-1]] + words[i:])
        out[-1] = fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return '\n'.join(out)


class NewsArt(QWidget):
    """A story's picture, painted: the image cover-cropped when it arrives,
    else a gradient in the story's colour with its glyph. Loading is lazy —
    the page calls load() only once the card is near the screen."""

    def __init__(self, item: dict, w: int, h: int, radius: float, images,
                 top_only: bool = False):
        super().__init__()
        self.item, self.radius, self._images = item, radius, images
        self.top_only = top_only               # a card's top: square bottom corners
        self.setFixedSize(w, h)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._pm: QPixmap | None = None
        self.requested = False

    def load(self):
        if self.requested:
            return
        self.requested = True
        path, url = self._images(self.item)
        if path:
            self._decode(path)
        elif url:
            ART.fetch(url, self._arrived)

    def _arrived(self, path):
        try:
            self._decode(path)
        except RuntimeError:                  # the card is gone
            pass

    def _decode(self, path):
        self._path = path
        w, h = self.width(), self.height()
        pm = ART.peek(path, w, h)
        if pm is not None:
            self._set(pm)
        else:
            ART.request(path, w, h, lambda pm, size=(w, h): self._set(pm, size))

    def _set(self, pm, size=None):
        if pm is not None and (size is None or size == (self.width(), self.height())):
            self._pm = pm
            self.update()

    def resize_to(self, w: int, h: int):
        """New card width: keep showing the old picture (scaled) until the
        one decoded at the new size arrives."""
        if (w, h) == (self.width(), self.height()):
            return
        self.setFixedSize(w, h)
        if getattr(self, '_path', None):
            self._decode(self._path)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        if self.radius:
            clip.addRoundedRect(r.adjusted(0, 0, 0, self.radius if self.top_only else 0),
                                self.radius, self.radius)
        else:
            clip.addRect(r)
        pt.setClipPath(clip)
        if self._pm is not None:
            if self._pm.size() == self.size():
                pt.drawPixmap(0, 0, self._pm)
            else:
                pt.drawPixmap(self.rect(), self._pm)
        else:
            _role, grad, glyph = news_style(self.item)
            a, b = (QColor(c) for c in GRAD[grad])
            a.setAlphaF(0.55)
            b.setAlphaF(0.30)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.fillRect(r, QColor(P['surface_2']))
            pt.fillRect(r, QBrush(g))
            f = QFont(FONTS.icon)
            f.setPixelSize(int(min(r.height() * 0.38, 64)))
            pt.setFont(f)
            c = QColor(P['on_art']); c.setAlphaF(0.75)
            pt.setPen(c)
            pt.drawText(r, Qt.AlignmentFlag.AlignCenter, G[glyph])
        pt.end()


class NewsTile(QFrame):
    """One story in the grid: picture on top, source · time, a 3-line title.
    The page sets its width so the columns fill the row (set_width)."""
    W = 296                                    # starting width; MIN_W..: see NewsPage
    opened = pyqtSignal(int)

    def __init__(self, index: int, item: dict, images, width: int = W):
        super().__init__()
        self.index, self.item = index, item
        self.W = width
        self.IMG_H = int((width - 2) * 9 / 16)
        self.setObjectName("NewsTile")
        self.setFixedWidth(self.W)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, SP['3'])
        lay.setSpacing(SP['1_5'])
        self.art = NewsArt(item, self.W - 2, self.IMG_H, R['card'] - 1, images, top_only=True)
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        role = news_style(item)[0]
        meta = QHBoxLayout()
        meta.setContentsMargins(SP['3'], SP['1'], SP['3'], 0)
        meta.setSpacing(SP['1_5'])
        src = ElideLabel(item.get('source') or '', None)
        src.setStyleSheet(f"color: {P[role]}; font-size: {FS['xs']}px; font-weight: 600;")
        meta.addWidget(src, 1)
        when = label(fmt_relative(item.get('date')) or '', "Muted")
        meta.addWidget(when)
        lay.addLayout(meta)
        self.title = QLabel()
        self.title.setObjectName("NewsTitle")       # size/weight from the QSS
        f = QFont(FONTS.ui, -1, QFont.Weight.Medium)
        f.setPixelSize(FS['sm'])
        self._fm = QFontMetrics(f)
        self._set_title()
        self.title.setFixedHeight(self._fm.lineSpacing() * 3 + 2)
        self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.title.setContentsMargins(SP['3'], 0, SP['3'], 0)
        lay.addWidget(self.title)
        for w in (src, when, self.title):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip(item.get('title') or '')

    def _set_title(self):
        # Less the frame and a little slack, so the last glyph never clips.
        self.title.setText(elide_lines(self.item.get('title') or '', self._fm,
                                       self.W - 2 * SP['3'] - 8, 3))

    def set_width(self, w: int):
        if w == self.W:
            return
        self.W = w
        self.setFixedWidth(w)
        self.art.resize_to(w - 2, int((w - 2) * 9 / 16))
        self._set_title()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.opened.emit(self.index)
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.index)
        else:
            super().keyPressEvent(ev)


class NewsFeature(QFrame):
    """The lead story: a wide picture with the headline over a scrim."""
    H = 280
    opened = pyqtSignal(int)

    def __init__(self, images):
        super().__init__()
        self.setObjectName("NewsFeature")
        self._images = images
        self.index, self.item = -1, {}
        self._pm: QPixmap | None = None
        self._path = None
        self.setFixedHeight(self.H)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        col = QVBoxLayout()
        col.setSpacing(SP['1_5'])
        col.addStretch()
        self.kicker = label("", "HeroKicker")
        col.addWidget(self.kicker)
        self.headline = QLabel()
        self.headline.setObjectName("NewsHeadline")
        self.headline.setWordWrap(True)
        col.addWidget(self.headline)
        self.summary = QLabel()
        self.summary.setObjectName("NewsLede")
        self.summary.setWordWrap(True)
        col.addWidget(self.summary)
        lay.addLayout(col, 6)
        lay.addStretch(4)
        for w in (self.kicker, self.headline, self.summary):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_item(self, index: int, item: dict):
        self.index, self.item = index, item
        self._pm, self._path = None, None
        when = fmt_relative(item.get('date'))
        self.kicker.setText("  ·  ".join(x for x in ("FEATURED", item.get('source'), when) if x))
        self._relabel()
        path, url = self._images(item)
        if path:
            self._path = path
            self._decode()
        elif url:
            ART.fetch(url, self._arrived)
        self.update()

    def _arrived(self, path):
        try:
            self._path = path
            self._decode()
        except RuntimeError:
            pass

    def _size(self):
        # Decoded in 64 px steps, so a window resize doesn't decode every frame.
        return max(64, (self.width() + 63) // 64 * 64), self.H

    def _decode(self):
        if not self._path:
            return
        w, h = self._size()
        pm = ART.peek(self._path, w, h)
        if pm is not None:
            self._pm = pm
            self.update()
        else:
            want = self._path
            ART.request(self._path, w, h, lambda pm, p=want: self._decoded(p, pm))

    def _decoded(self, path, pm):
        if path == self._path and pm is not None:
            self._pm = pm
            self.update()

    def _relabel(self):
        w = max(200, int(self.width() * 0.58) - SP['5'])
        hf = QFont(FONTS.ui, -1, QFont.Weight.DemiBold)      # as #NewsHeadline / #NewsLede
        hf.setPixelSize(26)
        self.headline.setText(elide_lines(self.item.get('title') or '', QFontMetrics(hf), w, 2))
        sf = QFont(FONTS.ui)
        sf.setPixelSize(FS['sm'])
        self.summary.setText(elide_lines(self.item.get('summary') or '', QFontMetrics(sf), w, 3))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.item:
            self._relabel()
            if self._pm is not None and self._pm.width() != self._size()[0]:
                self._decode()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), R['card'], R['card'])
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        if self._pm is not None:
            x = (self.width() - self._pm.width()) // 2
            pt.drawPixmap(x, 0, self._pm)
        else:
            _role, grad, glyph = news_style(self.item)
            a, b = (QColor(c) for c in GRAD[grad])
            a.setAlphaF(0.6)
            b.setAlphaF(0.25)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.fillRect(r, QBrush(g))
            f = QFont(FONTS.icon)
            f.setPixelSize(140)
            pt.setFont(f)
            c = QColor(P['on_art']); c.setAlphaF(0.18)
            pt.setPen(c)
            pt.drawText(r.adjusted(r.width() * 0.55, 0, 0, 0), Qt.AlignmentFlag.AlignCenter,
                        G[glyph])
        # Scrim: dark on the left where the text sits, clear on the right.
        s = QLinearGradient(r.topLeft(), r.topRight())
        base = QColor(PAL['base'])
        for at, alpha in ((0.0, 0.92), (0.45, 0.72), (0.8, 0.10), (1.0, 0.0)):
            c = QColor(base); c.setAlphaF(alpha)
            s.setColorAt(at, c)
        pt.fillRect(r, QBrush(s))
        pt.end()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.index >= 0:
            self.opened.emit(self.index)
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self.index >= 0:
            self.opened.emit(self.index)
        else:
            super().keyPressEvent(ev)


class NewsReader(QFrame):
    """A story opened beside the grid: picture, headline, the summary, and
    the way out to the full article. Prev / Next walk the current filter."""
    W = 420
    closed = pyqtSignal()
    step = pyqtSignal(int)                    # -1 / +1
    show_game = pyqtSignal(dict)

    def __init__(self, images):
        super().__init__()
        self.setObjectName("NewsReader")
        self._images = images
        self.item: dict = {}
        self.setFixedWidth(self.W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['4'])
        lay.setSpacing(SP['2_5'])
        bar = QHBoxLayout()
        self.btn_prev = icon_button(G['chevron_up'], "Previous story", 30, "Ghost", round_=True)
        self.btn_prev.clicked.connect(lambda: self.step.emit(-1))
        self.btn_next = icon_button(G['chevron_down'], "Next story", 30, "Ghost", round_=True)
        self.btn_next.clicked.connect(lambda: self.step.emit(1))
        self.pos = label("", "Muted")
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.pos)
        bar.addStretch()
        close = icon_button(G['close'], "Close (Esc)", 30, "Ghost", round_=True)
        close.clicked.connect(self.closed)
        bar.addWidget(close)
        lay.addLayout(bar)
        self.art_host = QVBoxLayout()
        lay.addLayout(self.art_host)
        self.art = None
        self.meta = label("", None)
        lay.addWidget(self.meta)
        self.title = label("", "NewsReaderTitle", wrap=True)
        lay.addWidget(self.title)
        self.body = label("", None, wrap=True)
        self.body.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['sm']}px;")
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.body)
        lay.addStretch()
        acts = QHBoxLayout()
        acts.setSpacing(SP['2'])
        self.btn_open = QPushButton("Read the article")
        self.btn_open.setObjectName("Primary")
        set_glyph_icon(self.btn_open, G['external'], role='on_accent')
        self.btn_open.clicked.connect(self._open)
        self.btn_copy = icon_button(G['copy'], "Copy link", 34, None, round_=True)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_game = QPushButton("Show game")
        set_glyph_icon(self.btn_game, G['library'])
        self.btn_game.clicked.connect(lambda: self.show_game.emit(self.item))
        acts.addWidget(self.btn_open)
        acts.addWidget(self.btn_game)
        acts.addStretch()
        acts.addWidget(self.btn_copy)
        lay.addLayout(acts)

    def show_item(self, item: dict, pos: int, total: int, has_game: bool):
        self.item = item
        if self.art is not None:
            self.art.setParent(None)
            self.art.deleteLater()
        self.art = NewsArt(item, self.W - 2 * SP['4'], int((self.W - 2 * SP['4']) * 9 / 16),
                           R['control'], self._images)
        self.art_host.addWidget(self.art)
        self.art.load()
        role = news_style(item)[0]
        self.meta.setText(" · ".join(x for x in (item.get('source'),
                                                  fmt_relative(item.get('date'))) if x))
        self.meta.setStyleSheet(f"color: {P[role]}; font-size: {FS['xs']}px; font-weight: 600;")
        self.title.setText(soft_wrap(item.get('title') or ''))
        self.body.setText(item.get('summary') or "No summary — read it on the site.")
        self.pos.setText(f"{pos + 1} of {total}")
        self.btn_prev.setEnabled(pos > 0)
        self.btn_next.setEnabled(pos < total - 1)
        link = item.get('link')
        self.btn_open.setEnabled(bool(link))
        self.btn_copy.setEnabled(bool(link))
        self.btn_open.setToolTip(link or "This story has no link")
        self.btn_game.setVisible(has_game)

    def _open(self):
        if self.item.get('link'):
            QDesktopServices.openUrl(QUrl(self.item['link']))    # the browser renders it

    def _copy(self):
        if self.item.get('link'):
            QApplication.clipboard().setText(self.item['link'])


class NewsPage(QWidget):
    """Linux gaming news, Proton releases and Steam news for your installed
    games, as a magazine: a featured story, then a grid of picture cards,
    and a reader beside it. Built for a few hundred stories: cards are made
    once per load (36 at a time), filters only show and hide them, and a
    picture is fetched and decoded only when its card nears the screen."""
    toast = pyqtSignal(str, str)
    loaded = pyqtSignal()
    show_game = pyqtSignal(str)               # slug
    PAGE = 36
    MAX = 120
    MIN_W = 270                               # cards grow from here to fill their row
    GAP = SP['3']

    def __init__(self):
        super().__init__()
        self.data: dict = {}
        self.items: list[dict] = []
        self.games_by_appid: dict = {}
        self._src = 'all'
        self._view: list[int] = []            # item indexes in the current filter
        self._limit = self.PAGE
        self.tiles: dict[int, NewsTile] = {}
        self._reading = -1
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        main = QWidget()
        outer = QVBoxLayout(main)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        outer.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.addWidget(label("News", "PageTitle"))
        head.addWidget(_pill("experimental", 'warning'))
        head.addStretch()
        self.status = label("", "Muted")
        head.addWidget(self.status)
        self.btn_refresh = icon_button(G['refresh'], "Refresh (bypasses the 1-hour cache)", 32)
        self.btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        head.addWidget(self.btn_refresh)
        outer.addLayout(head)
        self.chips_host = QWidget()
        self.chips = QHBoxLayout(self.chips_host)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(SP['1_5'])
        outer.addWidget(self.chips_host)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.host = host
        col = QVBoxLayout(host)
        col.setContentsMargins(0, SP['1'], 0, SP['4'])
        col.setSpacing(SP['4'])
        self.feature = NewsFeature(self._image_for)
        self.feature.opened.connect(self.open_index)
        self.feature.setVisible(False)
        col.addWidget(self.feature)
        self.grid_host = QWidget()
        self.grid = FlowLayout(self.grid_host, spacing=self.GAP)
        col.addWidget(self.grid_host)
        self.empty_host = QVBoxLayout()
        col.addLayout(self.empty_host)
        self.btn_more = QPushButton("Show more")
        self.btn_more.clicked.connect(self._more)
        col.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignHCenter)
        self.btn_more.setVisible(False)
        col.addWidget(self._feeds_box())
        col.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)
        root.addWidget(main, 1)
        self.reader = NewsReader(self._image_for)
        self.reader.closed.connect(self.close_reader)
        self.reader.step.connect(self._step)
        self.reader.show_game.connect(self._show_game)
        self.reader.setVisible(False)
        root.addWidget(self.reader)
        # Pictures load for what's on (or near) the screen, a beat after it
        # stops moving.
        self._art_timer = QTimer(self)
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(60)
        self._art_timer.timeout.connect(self._load_visible)
        # A bound method, not a lambda: PyQt drops it when the page is
        # destroyed (a lambda here segfaulted on teardown, mid-resize).
        self.scroll.verticalScrollBar().valueChanged.connect(self._scrolled)
        self._loaded = False
        self.error = ''                          # the last load's failure, if any

    def _feeds_box(self):
        body = QWidget()
        feeds = QHBoxLayout(body)
        feeds.setContentsMargins(0, 0, 0, 0)
        self.feed_url = QLineEdit()
        self.feed_url.setPlaceholderText("Add a feed: https://example.com/rss")
        add = QPushButton("Add feed")
        add.clicked.connect(self._add_feed)
        self.feed_url.returnPressed.connect(self._add_feed)
        self.user_feeds = NoWheelCombo()
        self.user_feeds.setToolTip("Feeds you added")
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove_feed)
        self.btn_remove_feed = rm
        feeds.addWidget(self.feed_url, 1)
        feeds.addWidget(add)
        feeds.addWidget(self.user_feeds)
        feeds.addWidget(rm)
        self.feeds_row = feeds
        self.feeds_box = Disclosure('news-feeds', "Manage feeds", G['news'], body)
        return self.feeds_box

    # ---- data ----
    def set_games(self, games: list[dict]):
        """Library games, so news about one can show its own art."""
        self.games_by_appid = {g['appid']: g for g in games
                               if g.get('source') == 'steam' and g.get('appid')}

    def _image_for(self, item):
        return news_image(item, self.games_by_appid)

    def refresh(self, force=False):
        # One fetch at a time: the dashboard's News card and opening the page
        # both ask until the first answer lands.
        if (self._loaded and not force) or getattr(self, '_busy', False):
            return
        self._busy = True
        self.error = ''
        self.btn_refresh.setEnabled(False)
        self.status.setText("Loading…")

        def done(data, err):
            self._busy = False
            try:
                self.btn_refresh.setEnabled(True)
            except RuntimeError:
                return
            if data is None:
                self.error = err or 'unknown error'
                self.status.setText(f"Couldn't load news: {self.error}")
                self.loaded.emit()               # the Home card shows it, with Retry
                return
            self.error = ''
            self._loaded = True
            self.set_data(data)
            self.loaded.emit()
        cli_json(['--news', '--json'] + (['--refresh'] if force else []), done, 120000)

    def set_data(self, data: dict):
        self.data = data
        self.items = (data.get('items') or [])[:self.MAX]
        bad = [s['name'] for s in data.get('sources', []) if s.get('error')]
        self.status.setText(f"{len(data.get('items', []))} stories"
                            + (f" · unavailable: {', '.join(bad)}" if bad else ""))
        for t in self.tiles.values():
            t.setParent(None)
            t.deleteLater()
        self.tiles = {}
        while self.grid.count():
            self.grid.takeAt(0)
        if self._src != 'all' and self._src not in {news_group(i) for i in self.items}:
            self._src = 'all'
        self.close_reader()
        self._fill_chips()
        self._fill()
        self.user_feeds.clear()
        for f in data.get('feeds', []):
            if f.get('user'):
                self.user_feeds.addItem(f['name'], f['url'])
        self.user_feeds.setVisible(self.user_feeds.count() > 0)
        self.btn_remove_feed.setVisible(self.user_feeds.count() > 0)

    # ---- filter ----
    def _fill_chips(self):
        clear_layout(self.chips)
        counts: dict[str, int] = {}
        for i in self.items:
            k = news_group(i)
            counts[k] = counts.get(k, 0) + 1
        order = sorted(counts, key=lambda k: (k != 'Your games', k != 'Proton', -counts[k]))
        for key in ['all', *order]:
            text = (f"All ({len(self.items)})" if key == 'all' else f"{key} ({counts[key]})")
            c = QPushButton(text)
            c.setObjectName("Chip")
            c.setCheckable(True)
            c.setChecked(key == self._src)
            c.clicked.connect(lambda _=False, k=key: self._pick(k))
            self.chips.addWidget(c)
        self.chips.addStretch()

    def _pick(self, key):
        self._src = key
        for i in range(self.chips.count()):
            w = self.chips.itemAt(i).widget()
            if isinstance(w, QPushButton):
                w.setChecked(w.text().startswith("All (") if key == 'all'
                             else w.text().startswith(f"{key} ("))
        self._limit = self.PAGE
        self.close_reader()
        self._fill()
        self.scroll.verticalScrollBar().setValue(0)

    def _fill(self):
        """Show the current filter: reuse cards, make only missing ones,
        and lay them out in story order."""
        view = [n for n, i in enumerate(self.items)
                if self._src == 'all' or news_group(i) == self._src]
        self._view = view
        lead = next((n for n in view[:12] if self.items[n].get('image')
                     or (self.items[n].get('kind') == 'steam'
                         and self.items[n].get('appid') in self.games_by_appid)),
                    view[0] if view else None)
        if lead is not None:
            self.feature.set_item(lead, self.items[lead])
        self.feature.setVisible(lead is not None)
        rest = [n for n in view if n != lead]
        shown = rest[:self._limit]
        keep = set(shown)
        while self.grid.count():
            self.grid.takeAt(0)
        for n, t in self.tiles.items():
            if n not in keep:
                t.setVisible(False)
        for n in shown:
            t = self.tiles.get(n)
            if t is None:
                t = self.tiles[n] = NewsTile(n, self.items[n], self._image_for,
                                             self._tile_w())
                t.opened.connect(self.open_index)
            self.grid.addWidget(t)
            t.setVisible(True)
        self.grid.invalidate()
        self.btn_more.setVisible(len(rest) > len(shown))
        self.btn_more.setText(f"Show more ({len(rest) - len(shown)})")
        clear_layout(self.empty_host)
        if not view:
            self.empty_host.addWidget(EmptyState(G['news'], "No stories",
                                                 "Nothing in these feeds yet — try another "
                                                 "source or refresh."))
        self._art_timer.start()

    def _more(self):
        self._limit += self.PAGE
        self._fill()

    def _tile_w(self) -> int:
        """Card width that fills the grid's row exactly."""
        avail = self.scroll.viewport().width() - 2
        cols = max(1, (avail + self.GAP) // (self.MIN_W + self.GAP))
        return max(self.MIN_W if avail >= self.MIN_W else avail,
                   (avail - (cols - 1) * self.GAP) // cols)

    def _fit_tiles(self) -> bool:
        """Resize the cards to the grid; True if anything changed (positions
        are then stale until the layout runs)."""
        w = self._tile_w()
        changed = [t for t in self.tiles.values() if t.W != w]
        for t in changed:
            t.set_width(w)
        if changed:
            self.grid.invalidate()
        return bool(changed)

    # ---- lazy pictures ----
    def _scrolled(self, _value):
        self._art_timer.start()

    def _load_visible(self):
        if not self.isVisible():
            return
        if self._fit_tiles():
            self._art_timer.start()          # load once the new layout has run
            return
        vh = self.scroll.viewport().height()
        top = self.scroll.verticalScrollBar().value()
        lo, hi = top - vh // 2, top + 2 * vh          # what's on screen, and the next screen
        for t in self.tiles.values():
            if not t.isVisible() or t.art.requested:
                continue
            y = t.mapTo(self.host, QPoint(0, 0)).y()
            if y + t.height() >= lo and y <= hi:
                t.art.load()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._art_timer.start()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._art_timer.start()

    # ---- reader ----
    def open_index(self, n: int):
        if not (0 <= n < len(self.items)):
            return
        self._reading = n
        it = self.items[n]
        pos = self._view.index(n) if n in self._view else 0
        has_game = it.get('kind') == 'steam' and it.get('appid') in self.games_by_appid
        self.reader.show_item(it, pos, max(1, len(self._view)), has_game)
        self.reader.setVisible(True)
        for i, t in self.tiles.items():
            t.setProperty('reading', i == n)
            t.style().unpolish(t)
            t.style().polish(t)
        self._art_timer.start()

    def open_item(self, item: dict):
        """Open a story by content (from the Home widget)."""
        key = item.get('link') or item.get('title')
        n = next((n for n, i in enumerate(self.items)
                  if (i.get('link') or i.get('title')) == key), None)
        if n is not None:
            if self._src != 'all' and n not in self._view:
                self._pick('all')
            self.open_index(n)
            self.reader.btn_open.setFocus()

    def reader_open(self) -> bool:
        return self.reader.isVisible()

    def close_reader(self):
        if not self.reader.isVisible():
            return
        n, self._reading = self._reading, -1
        self.reader.setVisible(False)
        self._art_timer.start()              # the grid got wider
        for t in self.tiles.values():
            if t.property('reading'):
                t.setProperty('reading', False)
                t.style().unpolish(t)
                t.style().polish(t)
        t = self.tiles.get(n)
        if t is not None and t.isVisible():
            t.setFocus()
            self.scroll.ensureWidgetVisible(t)

    def _step(self, d: int):
        if self._reading not in self._view:
            return
        k = self._view.index(self._reading) + d
        if 0 <= k < len(self._view):
            n = self._view[k]
            if n not in self.tiles or not self.tiles[n].isVisible():
                self._more()                 # past the loaded batch: grow the grid
            self.open_index(n)
            t = self.tiles.get(n)
            if t is not None and t.isVisible():
                self.scroll.ensureWidgetVisible(t)

    def _show_game(self, item):
        game = self.games_by_appid.get(item.get('appid'))
        if game:
            self.show_game.emit(game['slug'])

    # ---- feeds ----
    def _add_feed(self):
        url = self.feed_url.text().strip()
        if not url.startswith('https://'):
            self.toast.emit("Enter a feed address starting with https://", 'warn')
            return

        def done(out, err):
            if out is None:
                self.toast.emit(f"Couldn't add the feed: {err}", 'error')
                return
            self.feed_url.clear()
            self.toast.emit("Feed added.", 'success')
            self.refresh(force=True)
        CliQuery(['--news-add-feed', url], done)

    def _remove_feed(self):
        url = self.user_feeds.currentData()
        if not url:
            return
        CliQuery(['--news-remove-feed', url], lambda out, err: self.refresh(force=True))


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .app import main  # noqa: E402
from .art import ART, art_lookup  # noqa: E402
from .deals import _pill  # noqa: E402
from .details import Disclosure, ElideLabel  # noqa: E402
from .jobs import cli_json, CliQuery  # noqa: E402
from .widgets import (clear_layout, EmptyState, FlowLayout, icon_button, label, NoWheelCombo,
    set_glyph_icon, soft_wrap)  # noqa: E402
