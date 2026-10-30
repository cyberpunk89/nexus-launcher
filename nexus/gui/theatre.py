"""Theatre mode: full screen, controller first, for the TV."""

import random
import re
from datetime import datetime

from PyQt6.QtCore import (pyqtSignal, QEvent, QObject, QPoint, QRectF, Qt, QTimer, QUrl,
    QVariantAnimation)
from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QImage, QLinearGradient, QPainter,
    QPixmap, QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QProgressBar,
    QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from .common import alive, COLLECTION_NAMES, MAX_COLLECTION_NAME
from .theme import build_stylesheet, collection_look, FONTS, FS, G, P, SP
# The spotlight subclasses Home's carousel, so this one is needed up front
# (library.py imports nothing from here).
from .library import HeroCarousel


THEATRE_SCALE = 1.4


_SEARCH_JUNK = re.compile(r"[™®©:'’\-_.,!?]+")


def _search_norm(text: str) -> str:
    return ' '.join(_SEARCH_JUNK.sub(' ', (text or '').lower()).split())


def search_games(games: list[dict], query: str, limit: int = 18) -> list[dict]:
    """Games whose name holds every word of the query, best first: the name
    starting with it, then a word starting with it, then anywhere; installed
    before owned, then most played."""
    q = _search_norm(query)
    if not q:
        return []
    words = q.split()
    hits = []
    for g in games:
        name = _search_norm(g.get('display_name') or g.get('slug'))
        if not all(w in name for w in words):
            continue
        rank = 0 if name.startswith(q) else \
            1 if any(n.startswith(words[0]) for n in name.split()) else 2
        hits.append((rank, not is_installed(g), -(g.get('playtime_minutes') or 0),
                     name, g))
    hits.sort(key=lambda h: h[:4])
    return [h[-1] for h in hits[:limit]]


class TheatreSearch(QWidget):
    """Search for the couch: the query, an on-screen keyboard you drive with
    the D-pad (A types, X deletes, Y adds a space, B closes) and the matching
    games as big tiles under it. A real keyboard types straight in too."""
    picked = pyqtSignal(dict)
    closed = pyqtSignal()
    ROWS = ("1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm-:.")
    TILE = 200

    def __init__(self, parent):
        super().__init__(parent)
        self.games: list[dict] = []
        self.query = ''
        self._entry = None                 # set: naming something, not searching
        lay = QVBoxLayout(self)
        m = int(SP['8'] * 2)
        lay.setContentsMargins(m, SP['6'], m, SP['4'])
        lay.setSpacing(SP['4'])
        head = QHBoxLayout()
        head.addWidget(icon_label(G['search'], 34, P['accent']))
        self.text = label("", "SearchQuery")
        head.addWidget(self.text, 1)
        self.count = label("", "TheatreClock")
        head.addWidget(self.count)
        lay.addLayout(head)
        keys = QGridLayout()
        keys.setSpacing(SP['2'])
        self.keys: dict[str, QPushButton] = {}
        for r, row in enumerate(self.ROWS):
            for c, ch in enumerate(row):
                keys.addWidget(self._key(ch.upper(), lambda _=False, ch=ch: self.type(ch), ch),
                               r, c)
        r = len(self.ROWS)
        keys.addWidget(self._key("Space", lambda: self.type(' '), 'space'), r, 0, 1, 4)
        keys.addWidget(self._key("⌫  Delete", self.backspace, 'back'), r, 4, 1, 2)
        keys.addWidget(self._key("Clear", self.clear, 'clear'), r, 6, 1, 2)
        keys.addWidget(self._key("Done", self._done, 'done'), r, 8, 1, 2)
        kb = QWidget()
        kb.setLayout(keys)
        kb.setMaximumWidth(10 * 78 + 9 * SP['2'])
        lay.addWidget(kb, 0, Qt.AlignmentFlag.AlignHCenter)
        self.results = QScrollArea()
        self.results.setWidgetResizable(True)
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.flow = FlowLayout(host, margin=SP['2'], spacing=SP['4'])
        self.results.setWidget(host)
        lay.addWidget(self.results, 1)
        self.hint = label("", "TheatreClock")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)
        self.setVisible(False)
        parent.installEventFilter(self)                 # cover the parent as it resizes

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(P['bg']))
        glow = QRadialGradient(float(self.width() * 0.5), 0.0, float(max(self.width(), 1)))
        c = QColor(P['accent']); c.setAlphaF(0.10)
        end = QColor(P['accent']); end.setAlphaF(0.0)
        glow.setColorAt(0.0, c)
        glow.setColorAt(0.6, end)
        pt.fillRect(self.rect(), QBrush(glow))
        pt.end()

    def _key(self, text, fn, key):
        b = QPushButton(text)
        b.setObjectName("Key")
        b.setMinimumSize(60, 62)
        b.clicked.connect(fn)
        self.keys[key] = b
        return b

    # ---- open / close ----
    def open(self, games: list[dict]):
        self.games = games
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.setVisible(True)
        self._refresh()
        QTimer.singleShot(0, lambda: alive(self.keys['q'])
                          and self.keys['q'].setFocus(Qt.FocusReason.TabFocusReason))

    def open_entry(self, title: str, initial: str, on_done):
        """The keyboard for typing a name with a controller (a collection):
        Done hands the text to on_done, B drops it. No search results."""
        self._entry = on_done
        self.games = []
        self.query = initial
        self.results.setVisible(False)
        self.open([])
        self.hint.setText(f"{title} — Done saves, B cancels")

    def _done(self):
        fn, name = self._entry, self.query.strip()
        self.close_search()
        if fn is not None and name:
            fn(name[:1].upper() + name[1:])

    def close_search(self):
        if self.isVisible():
            self.setVisible(False)
            self._entry = None
            self.results.setVisible(True)
            self.closed.emit()

    def default_focus(self):
        return self.keys['q']

    # ---- typing ----
    def type(self, ch: str):
        if len(self.query) < (MAX_COLLECTION_NAME if self._entry else 60):
            self.query += ch
            self._refresh()

    def backspace(self):
        self.query = self.query[:-1]
        self._refresh()

    def clear(self):
        self.query = ''
        self._refresh()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_Menu or k == Qt.Key.Key_Backspace:     # X on a pad
            self.backspace()
        elif self._entry is not None and k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not isinstance(QApplication.focusWidget(), QPushButton):
            self._done()
        elif ev.text() and ev.text().isprintable() and not ev.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            self.type(ev.text().lower())
        else:
            super().keyPressEvent(ev)

    # ---- results ----
    def _refresh(self):
        self.text.setText((self.query or ("Type a name" if self._entry else
                                          "Type a game's name")) + "▏")
        self.text.setStyleSheet("" if self.query else f"color: {P['muted']};")
        if self._entry is not None:
            self.count.setText("")
            return
        while self.flow.count():
            w = self.flow.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        found = search_games(self.games, self.query)
        for g in found:
            t = GameTile(g, width=self.TILE, theatre=True)
            t.activated.connect(self.picked)
            t.selected.connect(lambda game, _m: self.picked.emit(game))
            self.flow.addWidget(t)
        self.count.setText(f"{len(found)} found" if self.query else "")
        self.hint.setText("" if found or not self.query else "Nothing matches yet")


class AttractOverlay(QWidget):
    """Theatre's screensaver: after a while with nothing pressed, your games
    one after another — hero art drifting slowly, or (when the store has
    one) the game's trailer, muted. Video frames come through a QVideoSink
    and are painted like the art, so the name can sit on top. Any key or
    click ends it; A / Enter opens the game on screen."""
    open_game = pyqtSignal(dict)
    SLIDE_MS = 14000
    TRAILER_MS = 30000

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.games: list[dict] = []
        self.game: dict = {}
        self._pix: QPixmap | None = None
        self._frame: QImage | None = None
        self._recent: list[str] = []
        self._rng = random.Random()
        self.player = None
        self._zoom = QVariantAnimation(self)
        self._zoom.setStartValue(1.0)
        self._zoom.setEndValue(1.08)
        self._zoom.valueChanged.connect(self.update)
        self._next = QTimer(self)
        self._next.setSingleShot(True)
        self._next.timeout.connect(self._advance)
        parent.installEventFilter(self)
        self.hide()

    @property
    def running(self) -> bool:
        return not self.isHidden()

    def start(self, games: list[dict]):
        self.games = [g for g in games if is_installed(g) and not g.get('hidden')
                      and not g.get('dup_of')]
        if not self.games:
            return
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._advance()

    def stop(self):
        self._next.stop()
        self._zoom.stop()
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self._frame = None
        self.hide()

    def _advance(self):
        g = surprise_pick(self.games, self._rng, self._recent[-6:])
        if g is None:
            self.stop()
            return
        self._recent.append(g['slug'])
        self.game, self._pix, self._frame = g, None, None
        if self.player is not None:
            self.player.stop()
        path, url = art_source(g, HERO_KINDS)
        slug = g['slug']
        if path:
            self._set_pix(path, slug)
        elif url:
            ART.fetch(url, lambda p, slug=slug: p and self._set_pix(p, slug))
        # A trailer, when the store info is already known (cached on disk).
        cli_json(['--store-info', slug, '--json'],
                 lambda d, _e, slug=slug: self._maybe_trailer(d or {}, slug), 20000, ttl=3600)
        self._zoom.stop()
        self._zoom.setDuration(self.SLIDE_MS)
        self._zoom.start()
        self._next.start(self.SLIDE_MS)
        self.update()

    def _set_pix(self, path: str, slug: str):
        if slug == self.game.get('slug') and not self.isHidden():
            pm = screen_pixmap(path, self)
            if pm is not None and not pm.isNull():
                self._pix = pm
                self.update()

    def _maybe_trailer(self, info: dict, slug: str):
        if slug != self.game.get('slug') or self.isHidden():
            return
        trailers = ((info.get('store') or {}).get('trailers') or [])
        url = trailers[0]['url'] if trailers else ''
        if not url.startswith('https://') or not self._ensure_player():
            return
        self.player.setSource(QUrl(url))
        self.player.play()
        self._next.start(self.TRAILER_MS)

    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        except ImportError:
            return False
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)
        self.player.durationChanged.connect(self._skip_intro)
        return True

    def _skip_intro(self, ms: int):
        """Trailers open on rating cards and logos: start a little in."""
        if ms > 40000 and self.player.position() < 1000:
            self.player.setPosition(8000)

    def _on_frame(self, frame):
        if not self.isHidden() and frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self._frame = img
                self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pt.fillRect(self.rect(), QColor(0, 0, 0))
        w, h = self.width(), self.height()
        src = self._frame if self._frame is not None else self._pix
        if src is not None and not src.isNull():
            z = 1.0 if self._frame is not None else float(self._zoom.currentValue() or 1.0)
            sw, sh = src.width(), src.height()
            scale = max(w / sw, h / sh) * z
            tw, th = sw * scale, sh * scale
            target = QRectF((w - tw) / 2, (h - th) / 2, tw, th)
            if isinstance(src, QImage):
                pt.drawImage(target, src)
            else:
                pt.drawPixmap(target, src, QRectF(src.rect()))
        grad = QLinearGradient(0.0, h * 0.55, 0.0, float(h))
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, 200))
        pt.fillRect(self.rect(), QBrush(grad))
        pad = int(SP['8'] * THEATRE_SCALE)
        f = QFont(FONTS.ui, -1, QFont.Weight.Bold)
        f.setPixelSize(int(FS['display'] * THEATRE_SCALE * 1.6))
        pt.setFont(f)
        pt.setPen(QColor(255, 255, 255))
        pt.drawText(QRectF(pad, h - pad - 120, w - 2 * pad, 70),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                    self.game.get('display_name', ''))
        f.setWeight(QFont.Weight.Normal)
        f.setPixelSize(int(FS['base'] * THEATRE_SCALE))
        pt.setFont(f)
        pt.setPen(QColor(255, 255, 255, 190))
        pt.drawText(QRectF(pad, h - pad - 44, w - 2 * pad, 40),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    "Press A to open it · any button to go back")
        pt.end()

    def keyPressEvent(self, ev):
        g = self.game
        self.stop()
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and g:
            self.open_game.emit(g)

    def mousePressEvent(self, ev):
        self.stop()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and not self.isHidden():
            self.setGeometry(obj.rect())
        return False


class TheatreSpotlight(HeroCarousel):
    """Theatre Home's spotlight: one game, full screen behind the top bar
    (kicker, logo, its line of facts, Play and Details). It follows focus
    in the first shelf, "Jump back in": no arrows, no dots, no rotation.
    TheatreView paints the art edge to edge; this widget paints nothing.

    While a game runs (or starts) it's pinned here: "NOW PLAYING · 42 MIN",
    when it started and the time played, and Resume / Quit game in place
    of Play / Details (`resume`, `quit_game`)."""
    art_changed = pyqtSignal()
    resume = pyqtSignal(dict)
    quit_game = pyqtSignal(dict)

    def __init__(self):
        super().__init__(scale=THEATRE_SCALE)
        self.timer.stop()
        self.pinned: dict | None = None          # the running game, while there is one
        for w in (self.btn_prev, self.btn_next, self.dots, self.btn_more):
            w.hide()
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.layout().setContentsMargins(0, 0, 0, SP['2'])
        self.kicker.setTextFormat(Qt.TextFormat.RichText)
        self.fact = fact_line()
        self.facts.addWidget(self.fact)
        self.btn.setFixedSize(336, 84)
        set_glyph_icon(self.btn, G['play'], 30, 'on_accent', hover_role=None)
        self.btn_details.setFixedHeight(84)
        self.btn_details.setMinimumWidth(200)
        # The carousel's own lambdas would always Play / open: these ask
        # whether the game shown is the pinned one first.
        self.btn.clicked.disconnect()
        self.btn.clicked.connect(self._primary)
        self.btn_details.clicked.disconnect()
        self.btn_details.clicked.connect(self._secondary)

    def set_games(self, games: list[dict]):
        """The game to show first: the one running or starting (pinned),
        else the fresh copy of the one shown, if it's still there; focus
        in "Jump back in" moves it from then on, unless one is pinned."""
        self.pinned = now_playing(games)
        keep = self.game.get('slug')
        cur = self.pinned or next((g for g in games if g.get('slug') == keep), None) or \
            (games[0] if games else None)
        moved = cur is not None and cur.get('slug') != keep and self._cur is not None
        self.games, self.i = ([cur] if cur else []), 0
        if self.games:
            self._show(animate=moved)

    def show_game(self, g: dict):
        if self.pinned is not None:
            return                               # the running game stays
        if g.get('slug') != self.game.get('slug'):
            self.games, self.i = [g], 0
            self._show(animate=True)

    def is_pinned(self) -> bool:
        return self.pinned is not None and self.pinned.get('slug') == self.game.get('slug')

    def _primary(self):
        (self.resume if self.is_pinned() else self.play).emit(self.game)

    def _secondary(self):
        (self.quit_game if self.is_pinned() else self.open).emit(self.game)

    def tick(self):
        """The minute changed (TheatreView's clock): "42 MIN" moves on."""
        if self.is_pinned():
            self.kicker.setText(live_kicker(self.game))
            self._fill_facts(self.game)

    def step(self, d: int):
        pass                                     # no rotation

    def wheelEvent(self, ev):
        ev.ignore()

    def _fill_facts(self, g: dict):
        t0 = g.get('session_start') if self.is_pinned() else None
        if t0 is None:
            self.fact.setText(fact_text(g))
            return
        hours = fmt_hours(played_minutes(g))
        self.fact.setText(f"Started at {t0:%H:%M}"
                          + (f"&nbsp;&nbsp;·&nbsp;&nbsp;{hours} played" if hours else ""))

    def _show(self, animate: bool):
        super()._show(animate)
        g, pinned = self.game, self.is_pinned()
        if pinned:
            playing = bool(g.get('playing'))
            self.kicker.setText(live_kicker(g))
            self.kicker.setProperty('live', playing)       # green once it really runs
            self.kicker.style().unpolish(self.kicker)
            self.kicker.style().polish(self.kicker)
            self.btn.setText("Resume" if playing else "Starting…")
            self.btn.setEnabled(playing)         # nothing to go back to yet
        self.btn_details.setText("Quit game" if pinned else "Details")
        if pinned:
            set_glyph_icon(self.btn_details, G['stop'], 26, 'on_art', hover_role=None)
        elif getattr(self.btn_details, '_glyph_spec', None):
            self.btn_details._glyph_spec = None
            self.btn_details.setIcon(QIcon())
        self.btn_details.setVisible(not pinned or bool(g.get('playing')))
        self.art_changed.emit()

    def _faded(self, v):
        super()._faded(v)
        self.art_changed.emit()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu and self.game:      # X: the options sheet
            self.menu.emit(self.game, QPoint())
        else:
            super().keyPressEvent(ev)

    def paintEvent(self, ev):
        pass


class TheatreDownloadRow(QFrame):
    """One line on the Downloads tab: a glyph, the title, one line of state, a
    bar and one big number. A runs the row's main action (or its options when
    it has none), X always its options."""
    action = pyqtSignal(str, dict)

    GLYPHS = {'torrent': 'download', 'job': 'download', 'game': 'play'}

    def __init__(self, r: dict):
        super().__init__()
        self.setObjectName("TheatreRow")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.r: dict = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['6'], SP['4'])
        lay.setSpacing(SP['5'])
        self.glyph = icon_label(G['download'], 40, P['on_art'])
        lay.addWidget(self.glyph)
        col = QVBoxLayout()
        col.setSpacing(SP['1'])
        self.title = plain_label("", "TheatreMeta")
        self.state = plain_label("", "TheatreSub")
        self.bar = QProgressBar()
        self.bar.setObjectName("CardProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(int(SP['2'] * 1.25))
        self.bar.setRange(0, 1000)
        for w in (self.title, self.state, self.bar):
            col.addWidget(w)
        lay.addLayout(col, 1)
        self.big = label("", "TheatreBig")
        lay.addWidget(self.big)
        self.set(r)

    def set(self, r: dict):
        self.r = r
        self.glyph.setPixmap(glyph_pixmap(G[self.GLYPHS.get(r['kind'], 'download')],
                                          P['on_art'], 40))
        self.title.setText(r['title'])
        self.state.setText(r['state'])
        self.big.setText(r['big'])
        self.bar.setVisible(r['frac'] is not None or r['group'] == 'progress')
        self.bar.setRange(0, 1000 if r['frac'] is not None else 0)   # 0..0: busy
        if r['frac'] is not None:
            self.bar.setValue(int(max(0.0, min(1.0, r['frac'])) * 1000))
        self.bar.setProperty('paused', bool(r['paused']))
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)

    def _run(self):
        self.action.emit(self.r['primary'] or 'options', self.r)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._run()
        elif ev.key() == Qt.Key.Key_Menu:
            self.action.emit('options', self.r)
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._run()
        super().mouseReleaseEvent(ev)


class TheatreDownloads(QScrollArea):
    """The Downloads tab: Ready to play, In progress, Up next. Rows are kept
    by key, so a refresh every couple of seconds moves no focus."""
    action = pyqtSignal(str, dict)
    TITLES = {'ready': "READY TO PLAY", 'progress': "IN PROGRESS", 'queue': "UP NEXT"}

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.col = QVBoxLayout(host)
        self.col.setContentsMargins(0, SP['2'], 0, SP['8'])
        self.col.setSpacing(SP['3'])
        self.heads = {g: plain_label(t, "TheatreLabel") for g, t in self.TITLES.items()}
        for head in self.heads.values():
            head.setParent(host)               # never a window of its own while unplaced
        self.rows: dict[str, TheatreDownloadRow] = {}
        self.empty = EmptyState(G['download'], "Nothing downloading",
                                "Repacks you download and games Steam is fetching show up here")
        self.empty.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.col.addWidget(self.empty)
        self.col.addStretch()
        self.setWidget(host)

    def set_downloads(self, rows: list[dict]):
        """`rows` from downloads.theatre_rows, in order."""
        for key in [k for k in self.rows if k not in {r['key'] for r in rows}]:
            w = self.rows.pop(key)
            f = QApplication.focusWidget()
            if f is w:                         # move focus first, then hide
                nxt = next((x for x in self.rows.values() if x is not w and not x.isHidden()),
                           self.empty)
                focus_and_reveal(nxt)
            w.hide()
            w.deleteLater()
        while self.col.count() > 1:            # re-laid in order below; the stretch stays last
            self.col.takeAt(0)
        groups = {r['group'] for r in rows}
        for g, head in self.heads.items():
            head.setVisible(g in groups)
        group = None
        for r in rows:
            w = self.rows.get(r['key'])
            if w is None:
                w = self.rows[r['key']] = TheatreDownloadRow(r)
                w.action.connect(self.action)
            else:
                w.set(r)
            if r['group'] != group:
                group = r['group']
                self.col.insertWidget(self.col.count() - 1, self.heads[group])
            self.col.insertWidget(self.col.count() - 1, w)
            w.setVisible(True)
        self.empty.setVisible(not rows)
        if not rows:
            self.col.insertWidget(0, self.empty)

    def first_row(self):
        return next((self.col.itemAt(i).widget() for i in range(self.col.count())
                     if isinstance(self.col.itemAt(i).widget(), TheatreDownloadRow)), None)


class TheatreMoment(QFrame):
    """A banner for something that just finished (a download, an install):
    under the clock for ten seconds, and it never takes focus, so an A in
    flight can't act on it. View acts on it (TheatreView.view_pressed)."""
    changed = pyqtSignal()
    TIMEOUT_MS = 10_000

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("TheatreMoment")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['3'], SP['6'], SP['3'])
        lay.setSpacing(SP['3'])
        lay.addWidget(icon_label(G['check'], 26, P['success']))
        self.text = plain_label("", "TheatreMeta")
        lay.addWidget(self.text)
        self.game: dict | None = None
        self.row_key: str | None = None
        self.name = ''
        self._timer = QTimer(self)               # a child: it goes with the banner
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self.hide()

    def show_moment(self, text: str, game: dict | None, row_key: str | None):
        self.text.setText(text)
        self.game, self.row_key = game, row_key
        self.name = (game or {}).get('display_name') or ''
        self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(self.TIMEOUT_MS)
        self.changed.emit()

    def dismiss(self):
        if self.isVisible():
            self.hide()
            self._timer.stop()
            self.changed.emit()


class TheatreView(QWidget):
    """Theatre mode: one big hero, rows of big tiles, a clock, and nothing
    that needs a mouse. LB/RB switch Home and Library, A opens a game, B goes
    back, Start opens the quick menu (the window's). Styled with the same
    tokens at THEATRE_SCALE."""
    play = pyqtSignal(dict)
    action = pyqtSignal(str, dict)
    menu = pyqtSignal(dict, QPoint)
    exit = pyqtSignal()
    surprise = pyqtSignal()
    library_sheet = pyqtSignal()                 # View on Library: the window opens Sort & filter
    library_changed = pyqtSignal(dict)           # a pick in that sheet: save it
    download_action = pyqtSignal(str, dict)      # a Downloads row: its action key, its row
    moment_changed = pyqtSignal()                # the banner came or went (the hints follow)
    discover_shown = pyqtSignal()                # the Discover tab came up: the window fetches
    deal_asked = pyqtSignal(dict, str)           # A on a deal card: its item, its kind
    reader_options = pyqtSignal(dict)            # X on a story: open the article, copy the link
    open_settings = pyqtSignal()                 # the Game-db setup card
    hide_gamedb = pyqtSignal()                   # ... and X on it

    TILE = 230
    WIDE = 440

    def __init__(self):
        super().__init__()
        self.setObjectName("Theatre")
        self.setStyleSheet(build_stylesheet(THEATRE_SCALE))
        self.games: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['8'] * 2, SP['6'], SP['8'] * 2, 0)
        lay.setSpacing(SP['4'])
        top = QHBoxLayout()
        top.addWidget(Wordmark("Nexus", 30))
        top.addSpacing(SP['6'])
        self.tabs = SegmentedControl('underline', 56)
        self.tabs.add('home', "Home")
        self.tabs.add('library', "Library")
        self.tabs.add('discover', "Discover")
        self.tabs.add('downloads', "Downloads")
        self.tabs.changed.connect(self._tab)
        top.addWidget(self.tabs)
        top.addStretch()
        self.btn_search = icon_button(G['search'], "Search (Y)", 52, "GlassIcon")
        self.btn_search.clicked.connect(self.open_search)
        top.addWidget(self.btn_search)
        self.btn_surprise = icon_button(G['dice'], "Surprise me — pick a game for me", 52,
                                        "GlassIcon")
        self.btn_surprise.clicked.connect(self.surprise)
        top.addWidget(self.btn_surprise)
        top.addSpacing(SP['4'])
        self.clock = label("", "TheatreClock")
        top.addWidget(self.clock)
        lay.addLayout(top)
        self.pages = QStackedWidget()
        lay.addWidget(self.pages, 1)
        # Home: hero + rows
        self.home = QScrollArea()
        self.home.setWidgetResizable(True)
        self.home.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.home.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.home_lay = QVBoxLayout(host)
        self.home_lay.setContentsMargins(0, 0, 0, SP['8'])
        self.home_lay.setSpacing(SP['6'])
        self.hero = TheatreSpotlight()
        self.hero.play.connect(self.play)
        self.hero.open.connect(self.open_game)
        self.hero.menu.connect(self.menu)
        self.hero.resume.connect(lambda g: self.action.emit('resume', g))
        self.hero.quit_game.connect(lambda g: self.action.emit('quit-game', g))
        self.hero.art_changed.connect(self.update)
        self.home.verticalScrollBar().valueChanged.connect(self._home_scrolled)
        # Focus in "Jump back in" moves the spotlight (once it settles there).
        self._spot_next: dict | None = None
        self._spot_timer = QTimer(self)
        self._spot_timer.setSingleShot(True)
        self._spot_timer.timeout.connect(self._spot_follow)
        self.home_lay.addWidget(self.hero)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['6'])
        self.home_lay.addLayout(self.rows)
        self.home_lay.addStretch()
        self.home.setWidget(host)
        self.pages.addWidget(self.home)
        # Library: your games, big tiles, under a chip that opens Sort & filter
        self.lib_page = QWidget()
        lp = QVBoxLayout(self.lib_page)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(SP['3'])
        chip_row = QHBoxLayout()
        self.lib_chip = QPushButton()
        self.lib_chip.setObjectName("StateChip")
        self.lib_chip.clicked.connect(self.library_sheet)
        chip_row.addWidget(self.lib_chip)
        chip_row.addStretch()
        lp.addLayout(chip_row)
        self.lib = QScrollArea()
        self.lib.setWidgetResizable(True)
        self.lib.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lhost = QWidget()
        self.lib_col = QVBoxLayout(lhost)         # the plain grid, or one block per collection
        self.lib_col.setContentsMargins(0, 0, 0, 0)
        self.lib_col.setSpacing(SP['6'])
        self.lib_plain = QWidget()
        self.lib_flow = FlowLayout(self.lib_plain, margin=SP['2'], spacing=SP['5'],
                                   justify=True)
        self.lib_col.addWidget(self.lib_plain)
        self.lib_col.addStretch()
        self._lib_groups: list[QWidget] = []
        self.lib_state = {'sort': 'name', 'show': 'installed'}
        self.lib.setWidget(lhost)
        lp.addWidget(self.lib, 1)
        self.pages.addWidget(self.lib_page)
        # Discover and Downloads: stubs until their cards (w-62, w-61) fill them.
        self.discover = self.discover_page = TheatreDiscover()
        self.discover.news_opened.connect(self._open_reader)
        self.discover.news_options.connect(self.reader_options)
        self.discover.deal_asked.connect(self.deal_asked)
        self.discover.open_settings.connect(self.open_settings)
        self.discover.hide_gamedb.connect(self.hide_gamedb)
        self.pages.addWidget(self.discover_page)
        self.downloads_page = TheatreDownloads()
        self.downloads_page.action.connect(self.download_action)
        self.pages.addWidget(self.downloads_page)
        self._pages = {'home': self.home, 'library': self.lib_page,
                       'discover': self.discover_page, 'downloads': self.downloads_page}
        self.reader = TheatreReader(self)
        self.reader.options.connect(self.reader_options)
        self.reader.show_game.connect(self._reader_show_game)
        self.moment_banner = TheatreMoment(self)
        self.moment_banner.changed.connect(self.moment_changed)
        self.search = TheatreSearch(self)
        self.search.picked.connect(self._search_picked)
        self.search.closed.connect(self._search_closed)
        self.everything: list[dict] = []
        self.details = TheatreDetails(self)
        self._tips: dict[str, dict] = {}
        self._launch: dict[str, dict] = {}
        self._asked: set[str] = set()
        self.news_items = lambda: []          # MainWindow hands over the News page's items
        self.details.play.connect(self.play)
        self.details.action.connect(self._detail_action)
        self.details.back_asked.connect(self.back)
        self.details.trailer.connect(self._play_trailer)
        self.details.home_asked.connect(self.go_home)
        self._store: dict[str, dict] = {}
        # Trailers from the game page's Media play here, over Theatre (the
        # desktop's overlay lives in the hidden desk shell).
        self.trailer = TrailerOverlay(self)
        self.trailer.closed.connect(self._trailer_closed)
        self._trailer_from = None
        self._clock = QTimer(self)
        self._clock.setInterval(20000)
        self._clock.timeout.connect(self._tick)
        self._tick()

    # ---- data ----
    def set_games(self, everything: list[dict]):
        games = [g for g in everything if is_installed(g)]
        owned = sorted((g for g in everything if not is_installed(g)),
                       key=lambda g: (not g.get('installing'), -(g.get('playtime_minutes') or 0),
                                      (g.get('display_name') or '').lower()))
        self.games = games
        self.everything = everything
        self.hero.set_games(featured_games(games))
        self.hero.setVisible(bool(games))
        self._sync_art()
        clear_layout(self.rows)
        played = sorted((g for g in games if g.get('last_played')),
                        key=lambda g: g['last_played'], reverse=True)
        added = sorted((g for g in games if g.get('added_at')),
                       key=lambda g: g['added_at'], reverse=True)
        looks = {n: collection_look(n) for n in COLLECTION_NAMES}
        cols = [(n, [g for g in games if n in (g.get('collections') or ())], 'cover',
                 (*looks[n], int(16 * THEATRE_SCALE)) if looks[n] else None)
                for n in COLLECTION_NAMES]
        for title, items, kind, look in (("Jump back in", played[:12], 'wide', None),
                                         ("Favourites", [g for g in games if g.get('favorite')],
                                          'cover', None),
                                         *cols,
                                         ("Recently installed", added[:14], 'cover', None),
                                         ("Not played yet",
                                          [g for g in games if not g.get('last_played')],
                                          'cover', None),
                                         ("Ready to install", owned[:20], 'cover', None)):
            if not items:
                continue
            w = self.WIDE if kind == 'wide' else self.TILE
            row = ShelfRow(title, w, kind, look=look)
            for g in items:
                row.add(self._tile(g, w, kind))
            row.finish()
            if title == "Jump back in":
                row.focused.connect(self._spot_focus)
            self.rows.addWidget(row)
        self._fill_library()
        if self.details.isVisible():
            cur = next((g for g in everything
                        if g['slug'] == self.details.game.get('slug')), None)
            if cur:
                self.details.open(cur, refocus=False)

    # ---- the Library tab ----
    SORTS = (('name', "A to Z"), ('played', "Recently played"), ('most', "Most played"),
             ('added', "Recently added"), ('collection', "By collection"))
    SHOWS = (('installed', "Installed"), ('missing', "Not installed"), ('all', "Everything"))

    def set_library_state(self, state: dict):
        """The saved sort and filter (prefs.theatre_library)."""
        self.lib_state = {'sort': state.get('sort', 'name'), 'show': state.get('show', 'installed')}
        self._fill_library()

    def _lib_games(self) -> list[dict]:
        pool = {'installed': self.games,
                'missing': [g for g in self.everything if not is_installed(g)],
                'all': self.everything}[self.lib_state['show']]
        name = lambda g: (g.get('display_name') or '').lower()      # noqa: E731
        sort = self.lib_state['sort']
        by_name = sorted(pool, key=name)                 # stable: ties keep A to Z
        if sort == 'played':
            return sorted(by_name, key=lambda g: g.get('last_played') or '', reverse=True)
        if sort == 'most':
            return sorted(by_name, key=lambda g: g.get('playtime_minutes') or 0, reverse=True)
        if sort == 'added':
            return sorted(by_name, key=lambda g: g.get('added_at') or g.get('installed_at') or '',
                          reverse=True)
        return by_name

    def _fill_library(self):
        """The Library grid for the saved sort and filter, reusing tiles so a
        refresh keeps focus. By collection: a block per collection (a game in
        two shows in both), the rest last."""
        games = self._lib_games()
        sort, show = self.lib_state['sort'], self.lib_state['show']
        name = lambda g: (g.get('display_name') or '').lower()      # noqa: E731
        if sort == 'collection':
            groups = [(n, [g for g in games if n in (g.get('collections') or ())])
                      for n in COLLECTION_NAMES]
            groups.append(("No collection", [g for g in games if not g.get('collections')]))
            groups = [(t, sorted(gs, key=name)) for t, gs in groups if gs]
        else:
            groups = [(None, games)]
        spare: dict[str, list[GameTile]] = {}
        flows = [self.lib_flow] + [w.flow for w in self._lib_groups]
        for flow in flows:
            while flow.count():
                w = flow.takeAt(0).widget()
                if isinstance(w, GameTile):
                    spare.setdefault(w.game.get('slug'), []).append(w)
        old, self._lib_groups = self._lib_groups, []

        def tile(g):
            t = spare.get(g['slug'], []).pop() if spare.get(g['slug']) else None
            if t is not None:
                t.set_game(g)
                return t
            return self._tile(g, self.TILE, 'cover')
        if groups and groups[0][0] is None:
            for g in groups[0][1]:
                self.lib_flow.addWidget(tile(g))
            self.lib_plain.setVisible(True)
        else:
            self.lib_plain.setVisible(False)
            for title, gs in groups:
                grp = QWidget()
                v = QVBoxLayout(grp)
                v.setContentsMargins(0, 0, 0, 0)
                v.setSpacing(SP['2'])
                v.addWidget(plain_label(title.upper(), "TheatreLabel"))
                host = QWidget()
                grp.flow = FlowLayout(host, margin=SP['2'], spacing=SP['5'], justify=True)
                v.addWidget(host)
                self.lib_col.insertWidget(self.lib_col.count() - 1, grp)
                for g in gs:
                    grp.flow.addWidget(tile(g))
                self._lib_groups.append(grp)
        for w in old:
            w.hide()
            w.deleteLater()
        for tiles in spare.values():
            for w in tiles:
                w.hide()
                w.deleteLater()
        sort_t = dict(self.SORTS)[sort]
        n = len(games)
        self.lib_chip.setText(f"{sort_t}  ·  {dict(self.SHOWS)[show]}  ·  "
                              f"{n} game{'s' if n != 1 else ''}")

    def open_library_sheet(self, sheet, focus: int = 0):
        """Sort & filter in `sheet` (Theatre's ActionSheet): a pick updates
        the grid and the chip behind it and the sheet stays up."""
        def row(kind, key, text, i):
            on = self.lib_state[kind] == key
            return (text, G['check'] if on else G['list'],
                    lambda: self._pick_library(sheet, kind, key, i), on)
        items = ["Sort by"] + [row('sort', k, t, i) for i, (k, t) in enumerate(self.SORTS)] \
            + ["Show"] + [row('show', k, t, len(self.SORTS) + i)
                          for i, (k, t) in enumerate(self.SHOWS)]
        sheet.choose("Sort & filter", items, focus=focus, keep_open=True)

    def _pick_library(self, sheet, kind: str, key: str, index: int):
        self.lib_state = {**self.lib_state, kind: key}
        self._fill_library()
        self.library_changed.emit(dict(self.lib_state))
        self.open_library_sheet(sheet, index)

    def moment(self, text: str, game: dict | None = None, row_key: str | None = None):
        """A finished download or install: the banner (never a toast on the
        TV). View shows `game`'s page, or the Downloads row `row_key`."""
        self.moment_banner.show_moment(text, game, row_key)
        self._place_moment()

    def _place_moment(self):
        m = self.moment_banner
        m.adjustSize()
        m.move(max(0, self.width() - m.width() - SP['8'] * 2), SP['6'] + 44)
        m.raise_()

    def view_pressed(self) -> bool:
        """The View button, as a context button: with a banner up it shows
        what finished; on Library it opens Sort & filter. True if it did
        something."""
        m = self.moment_banner
        if m.isVisible() and not self.search.isVisible() and not self.trailer.isVisible():
            g, key = m.game, m.row_key
            m.dismiss()
            if g is not None:
                self.open_game(g)                      # Play is focused there
            elif key:
                self.tabs.set_current('downloads', emit=True)
                QTimer.singleShot(0, lambda: self._focus_download(key))
            return True
        if self.tabs.current() == 'library' and not self.details.isVisible() \
                and not self.search.isVisible() and not self.trailer.isVisible():
            self.library_sheet.emit()
            return True
        return False

    # Set by the main window from Settings → Controls: whether A (and a
    # double click) play a game straight away instead of opening its page.
    a_plays = False
    double_plays = True

    def _activated(self, g):
        if self.a_plays and can_quick_play(g):
            self.play.emit(g)
        else:
            self.open_game(g)

    def _double(self, g):
        if self.double_plays and can_quick_play(g):
            self.play.emit(g)
        else:
            self.open_game(g)

    def _tile(self, g, w, kind):
        t = GameTile(g, width=w, kind=kind, theatre=True)
        t.selected.connect(lambda game, _m: self.open_game(game))
        t.activated.connect(self._activated)
        t.double.connect(self._double)
        t.play.connect(self.play)
        t.menu.connect(self.menu)
        return t

    # ---- the spotlight ----
    def _spot_focus(self, tile):
        self._spot_next = tile.game
        self._spot_timer.start(anim_ms(140))

    def _spot_follow(self):
        g = self._spot_next
        if g is not None and not self.hero.isHidden():
            self.hero.show_game(g)

    def _home_scrolled(self, _v):
        if self._art_on():
            self.update()

    def _art_on(self) -> bool:
        return self.pages.currentWidget() is self.home and not self.hero.isHidden() \
            and self.hero._cur is not None

    def _sync_art(self):
        """Over the spotlight's art the top bar's tabs are on_art (theme.py)."""
        on = self._art_on()
        if self.property('art') != on:
            self.setProperty('art', on)
            for b in self.tabs._items.values():
                b.style().unpolish(b)
                b.style().polish(b)
        self.update()

    def paintEvent(self, ev):
        """Home: the spotlight's art edge to edge, behind the top bar, moving
        up with the page and fading into it before the shelves."""
        if not self._art_on() or self.details.isVisible():
            return
        h = self.hero
        w, ht = self.width(), self.height()
        prev, cur = h._frame(h._prev, (w, ht)), h._frame(h._cur, (w, ht))
        if cur is None and prev is None:
            return
        pt = QPainter(self)
        y = -self.home.verticalScrollBar().value()
        r = QRectF(0, y, w, ht)
        if prev is not None and h._fade < 1.0:
            pt.drawPixmap(0, y, prev)
        if cur is not None:
            pt.setOpacity(h._fade if prev is not None else 1.0)
            pt.drawPixmap(0, y, cur)
            pt.setOpacity(1.0)
        base, bg = QColor(P['scrim']), QColor(P['bg'])
        g = QLinearGradient(0.0, 0.0, float(w), 0.0)
        for at, a in ((0.0, 0.90), (0.45, 0.45), (0.75, 0.05), (1.0, 0.0)):
            c = QColor(base); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))
        g = QLinearGradient(0.0, r.bottom(), 0.0, r.top())
        for at, a in ((0.0, 1.0), (0.35, 0.55), (0.6, 0.0)):
            c = QColor(bg); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))
        if r.bottom() < ht:
            pt.fillRect(QRectF(0, r.bottom(), w, ht - r.bottom()), bg)
        pt.end()

    # ---- navigation ----
    def _open_reader(self, items: list, index: int, card):
        self.reader.open(items, index, card, self._news_game_name)

    def _news_game(self, item: dict) -> dict | None:
        appid = item.get('appid')
        if item.get('kind') != 'steam' or not appid:
            return None
        return next((g for g in self.everything if g.get('appid') == appid), None)

    def _news_game_name(self, item: dict) -> str | None:
        g = self._news_game(item)
        return g.get('display_name') if g else None

    def show_game_for(self, appid) -> bool:
        """Open the game page of the library game with this Steam appid."""
        g = next((x for x in self.everything if appid and x.get('appid') == appid), None)
        if g is None:
            return False
        self.reader.close_reader()
        self.open_game(g)
        return True

    def _reader_show_game(self, appid):
        self.show_game_for(appid)

    def _tab(self, key):
        self.pages.setCurrentWidget(self._pages[key])
        if key == 'discover':
            self.discover_shown.emit()
        self._sync_art()
        QTimer.singleShot(0, lambda: alive(self) and focus_and_reveal(self.default_focus()))

    def cycle_tab(self, d):
        """LB / RB: the next tab that is showing, wrapping round."""
        keys = [k for k in self.tabs.keys() if not self.tabs.item(k).isHidden()]
        cur = self.tabs.current()
        i = (keys.index(cur) + d) % len(keys) if cur in keys else 0
        self.tabs.set_current(keys[i], emit=True)

    def sync_tabs(self, discover: bool, downloads: bool):
        """Which of Discover and Downloads show (Settings: news and Game-db
        for Discover, repack features for Downloads). A tab that goes away
        under you sends you Home."""
        self.tabs.set_item_visible('discover', discover)
        self.tabs.set_item_visible('downloads', downloads)
        if self.tabs.current() not in [k for k in self.tabs.keys()
                                       if not self.tabs.item(k).isHidden()]:
            self.tabs.set_current('home', emit=True)

    def _focus_download(self, key: str):
        row = self.downloads_page.rows.get(key)
        if alive(self) and row is not None and row.isVisible() and not self.details.isVisible():
            focus_and_reveal(row)

    def open_game(self, g):
        # What's behind the game page is out of reach (D-pad and Tab alike).
        self.pages.setEnabled(False)
        self.tabs.setEnabled(False)
        self.details.open(g)
        if self.moment_banner.isVisible():
            self.moment_banner.raise_()
        self._load_info(g)

    def show_session(self, g, summary: dict):
        """A game just closed: its page with the session band (w-60)."""
        if self.trailer.isVisible():
            self.trailer.stop()
        self.search.setVisible(False)
        self._from_search = False
        self.open_game(g)
        self.details.show_session(summary)

    def go_home(self):
        """The session band's Back to Home (and B on it)."""
        self.details.clear_session()
        if self.details.isVisible():
            self.close_game()
        self.tabs.set_current('home', emit=True)

    def _load_info(self, g):
        """The drawer's data: cached per game for the session (the CLI caches
        ProtonDB / Game-db / the store itself), fetched in the background."""
        slug = g['slug']
        self.details.set_info(self._tips.get(slug), self._launch.get(slug), self.news_items(),
                              self._store.get(slug))
        if slug not in self._asked:
            self._asked.add(slug)
            cli_json(['--game-tips', slug, '--json'],
                     lambda info, _e, slug=slug: self._got(self._tips, slug, info or {}),
                     ttl=300)
            cli_json(['--store-info', slug, '--json'],
                     lambda info, _e, slug=slug: self._got(self._store, slug, info or {}),
                     60000, ttl=600)
            if g.get('source', 'nexus') == 'nexus':
                cli_json(['--show-launch', slug, '--json'],
                         lambda info, _e, slug=slug: self._got(self._launch, slug, info or {}))

    def _got(self, store, slug, info):
        store[slug] = info
        try:
            if self.details.isVisible() and self.details.game.get('slug') == slug:
                self.details.set_info(self._tips.get(slug), self._launch.get(slug),
                                      store=self._store.get(slug))
        except RuntimeError:
            pass

    def _play_trailer(self, t: dict):
        self._trailer_from = QApplication.focusWidget()
        self.trailer.play(t, self.details.game.get('display_name', ''))

    def _trailer_closed(self):
        f = self._trailer_from
        if alive(f) and f.isVisible():
            focus_and_reveal(f)

    def news_changed(self):
        if self.details.isVisible():
            self.details.set_info(news=self.news_items())

    def close_game(self):
        self._from_search = False
        slug = self.details.game.get('slug')
        self.details.setVisible(False)
        self.pages.setEnabled(True)
        self.tabs.setEnabled(True)
        tile = next((t for t in self.findChildren(GameTile)
                     if t.game.get('slug') == slug and not t.isHidden()
                     and self.pages.currentWidget().isAncestorOf(t)), None)
        focus_and_reveal(tile or self.default_focus())

    # ---- search ----
    def open_search(self):
        self.pages.setEnabled(False)
        self.tabs.setEnabled(False)
        self.search.open(self.everything or self.games)

    def _search_picked(self, g):
        self.search.setVisible(False)          # the game page replaces it
        self._from_search = True
        self.open_game(g)

    def _search_closed(self):
        if not self.details.isVisible():
            self.pages.setEnabled(True)
            self.tabs.setEnabled(True)
            focus_and_reveal(self.btn_search)

    def back(self) -> bool:
        """B, one step at a time: a trailer, the game page's reader, its
        drawer, the page (back to the search it came from), the search;
        otherwise go to Home. Never out of Theatre. True if handled."""
        if self.trailer.isVisible():
            self.trailer.stop()
            return True
        if self.reader.isVisible():
            self.reader.close_reader()
            return True
        if self.search.isVisible():
            self.search.close_search()
            return True
        if self.details.isVisible():
            if self.details.session_shown() and not self.details.expanded():
                self.go_home()                  # B on the session band: Home
                return True
            if self.details.back():
                return True
            if getattr(self, '_from_search', False):
                # Back from a game found by search: to the search, as it was.
                self._from_search = False
                self.details.setVisible(False)
                self.search.open(self.everything or self.games)
            else:
                self.close_game()
            return True
        if self.tabs.current() != 'home':
            self.tabs.set_current('home', emit=True)
            return True
        return False

    def _detail_action(self, act, g):
        if act == 'options':
            self.menu.emit(g, QPoint())
        else:
            self.action.emit(act, g)    # a favourite comes back through set_games

    def default_focus(self):
        if self.trailer.isVisible():
            return self.trailer
        if self.reader.isVisible():
            return self.reader.btn_game if self.reader.btn_game.isVisible() else self.reader.body
        if self.search.isVisible():
            return self.search.default_focus()
        if self.details.isVisible():
            return self.details.default_focus()
        if self.pages.currentWidget() is self.home:
            if not self.hero.isHidden() and self.hero.btn.isEnabled():
                return self.hero.btn
        page = self.pages.currentWidget()
        if page is self.downloads_page:
            return page.first_row() or page.empty
        if page is self.discover_page:
            return page.first_focus()
        tile = next((t for t in page.findChildren(GameTile) if not t.isHidden()), None)
        if tile is None and page is self.lib_page:
            return self.lib_chip
        return tile or (page if page.focusPolicy() != Qt.FocusPolicy.NoFocus else None)

    def _tick(self):
        now = datetime.now().strftime('%H:%M')
        self.clock.setText(now)
        self.details.clock.setText(now)
        if self.hero.pinned is not None:         # "42 min" on the spotlight and tiles
            self.hero.tick()
            for t in self.findChildren(GameTile):
                if t.game.get('playing'):
                    t.update()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._tick()
        self._clock.start()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._clock.stop()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.hero.setFixedHeight(int(max(400, min(680, self.height() * 0.48))))
        if self.details.isVisible():
            self.details.setGeometry(self.rect())
        if self.search.isVisible():
            self.search.setGeometry(self.rect())
        if self.moment_banner.isVisible():
            self._place_moment()


class CursorHider(QObject):
    """Theatre mode hides the pointer until the mouse actually moves, and
    again after a few idle seconds."""

    def __init__(self, parent):
        super().__init__(parent)
        self.active = False
        self.hidden = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.hide)

    def start(self):
        self.active = True
        QApplication.instance().installEventFilter(self)
        self.hide()

    def stop(self):
        self.active = False
        QApplication.instance().removeEventFilter(self)
        self.timer.stop()
        self.show()

    def hide(self):
        if self.active and not self.hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self.hidden = True

    def show(self):
        if self.hidden:
            QApplication.restoreOverrideCursor()
            self.hidden = False

    def eventFilter(self, obj, ev):
        if not self.active:
            return False
        if ev.type() == QEvent.Type.ToolTip:
            return True                          # Theatre: no tooltips
        if ev.type() == QEvent.Type.MouseMove:
            self.show()
            self.timer.start()
        return False


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import anim_ms, ART, art_source, HERO_KINDS, screen_pixmap  # noqa: E402
from .fmt import fmt_hours  # noqa: E402
from .gameinfo import (can_quick_play, is_installed, live_kicker, now_playing,  # noqa: E402
    played_minutes)
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .library import featured_games, ShelfRow, surprise_pick  # noqa: E402
from .actions import TrailerOverlay  # noqa: E402
from .theatre_discover import TheatreDiscover, TheatreReader  # noqa: E402
from .theatre_game import fact_line, fact_text, TheatreDetails  # noqa: E402
from .tiles import GameTile  # noqa: E402
from .widgets import (clear_layout, EmptyState, FlowLayout, glyph_pixmap, icon_button,  # noqa: E402
    icon_label, label, plain_label, SegmentedControl, set_glyph_icon)
from .topbar import Wordmark  # noqa: E402
