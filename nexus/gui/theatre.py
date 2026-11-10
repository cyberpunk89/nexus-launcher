"""Theatre mode: full screen, controller first, for the TV."""

import random
import re
from datetime import datetime

from PyQt6.QtCore import (pyqtSignal, QEvent, QObject, QPoint, QRectF, Qt, QTimer, QUrl,
    QVariantAnimation)
from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QImage, QLinearGradient, QPainter,
    QPixmap, QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from .common import alive, COLLECTION_NAMES, MAX_COLLECTION_NAME
from .theme import build_stylesheet, collection_look, FONTS, FS, G, P, SP
# The spotlight subclasses Home's carousel, so this one is needed up front
# (library.py imports nothing from here).
from .library import FollowHero


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
        self._max = MAX_COLLECTION_NAME    # the name's length limit while naming
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

    def open_entry(self, title: str, initial: str, on_done,
                   max_len: int = MAX_COLLECTION_NAME):
        """The keyboard for typing a name with a controller (for a modal's
        field): Done hands the text to on_done, B drops it. No search
        results. At most `max_len` characters."""
        self._entry = on_done
        self._max = max_len
        self.games = []
        self.query = initial[:max_len]
        self.results.setVisible(False)
        self.open([])
        self.hint.setText(f"{title} — Done to finish, B to cancel")

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
        if len(self.query) < (self._max if self._entry else 60):
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


class TheatreSpotlight(FollowHero):
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
        self.btn_more.hide()                     # X opens the options sheet instead
        self.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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

    def _primary(self):
        (self.resume if self.is_pinned() else self.play).emit(self.game)

    def _secondary(self):
        (self.quit_game if self.is_pinned() else self.open).emit(self.game)

    def tick(self):
        """The minute changed (TheatreView's clock): "42 MIN" moves on."""
        if self.is_pinned():
            self.kicker.setText(live_kicker(self.game))
            self._fill_facts(self.game)

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
    together_asked = pyqtSignal(str)             # Play together… (w-83): the friends key in use
    download_action = pyqtSignal(str, dict)      # a Downloads row: its action key, its row
    moment_changed = pyqtSignal()                # the banner came or went (the hints follow)
    discover_shown = pyqtSignal()                # the Discover tab came up: the window fetches
    deal_asked = pyqtSignal(dict, str)           # A on a deal card: its item, its kind
    reader_options = pyqtSignal(dict)            # X on a story: open the article, copy the link
    open_settings = pyqtSignal()                 # the Game-db setup card
    hide_gamedb = pyqtSignal()                   # ... and X on it
    settings_sheet = pyqtSignal()                # the gear: the window opens Theatre's settings
    focus_game = pyqtSignal(object)              # the game focus rests on (dwell): the tint (w-65)
    # The Ribbon layout (w-108):
    ribbon_setting = pyqtSignal(str, object)     # A on a setting: its key, the new value
    settings_row = pyqtSignal(str)               # X on a setting: the sheet, on that row
    all_settings = pyqtSignal()                  # All settings (leaves Theatre)
    ribbon_moved = pyqtSignal(int)               # a move: the sound, the background, the hints

    TILE = 230
    WIDE = 440
    TINT_DWELL = 250                             # ms on a game before the background follows it

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
        # The Ribbon's open page (Discover, Downloads): its name by the wordmark.
        self.crumb = label("", "TheatreClock")
        self.crumb.hide()
        top.addWidget(self.crumb)
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
        # Theatre's settings (w-63): the quick menu has them too, but Steam
        # may keep the Guide button, and a generic pad has none.
        self.btn_settings = icon_button(G['settings'], "Settings", 52, "GlassIcon")
        self.btn_settings.clicked.connect(self.settings_sheet)
        top.addWidget(self.btn_settings)
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
        self._spot = Dwell(140, self._spot_follow, self)
        self.home_lay.addWidget(self.hero)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['6'])
        self.home_lay.addLayout(self.rows)
        # Your week (w-63): kept across set_games (taken out of the rows
        # before they are cleared), shown after "Jump back in".
        self.week_row = QWidget(host)
        wl = QVBoxLayout(self.week_row)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(SP['2'])
        wl.addWidget(label("Your week", "ShelfTitle"))
        self.week_card = WeekCard(self.WIDE * 2 + SP['3'], int(self.WIDE * 9 / 16) + TILE_LIFT,
                                  THEATRE_SCALE)
        self.week_card.open.connect(self._week_open)
        wl.addWidget(self.week_card, 0, Qt.AlignmentFlag.AlignLeft)
        self.week_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.week_row.hide()
        self._week_slug: str | None = None
        # Friends playing now (w-82): kept across set_games like Your week,
        # right after it. The shelf inside is rebuilt only when who plays
        # what changed; the minutes are updated in place.
        self.friends_row = QWidget(host)
        QVBoxLayout(self.friends_row).setContentsMargins(0, 0, 0, 0)
        self.friends_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.friends_row.hide()
        self.friends_shelf: ShelfRow | None = None
        self.friend_cards: list[TheatreCard] = []
        self._friends_sig = None
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
        self.facts: dict = {}                     # the facts index's games (Play together's modes)
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
        # The Ribbon layout (w-108): one painted page instead of the tabs.
        # Discover and Downloads open their tab's page over it (a sub-page).
        self.layout_mode = 'tabs'
        self.ribbon = RibbonView(THEATRE_SCALE)
        self.ribbon.art_for = self._ribbon_art
        self.ribbon.help = HELP
        self.ribbon.activated.connect(self._ribbon_activate)
        self.ribbon.options.connect(self._ribbon_options)
        self.ribbon.moved.connect(self._ribbon_moved)
        self.ribbon.category_changed.connect(self._ribbon_category)
        self.pages.addWidget(self.ribbon)
        self._ribbon_sub: str | None = None      # the tab page open over the Ribbon
        self._mirroring = False                  # the tabs follow the Ribbon (no page switch)
        self._ribbon_timer = QTimer(self)        # refreshes coalesced into one
        self._ribbon_timer.setSingleShot(True)
        self._ribbon_timer.setInterval(0)
        self._ribbon_timer.timeout.connect(self._ribbon_build)
        self._panels: dict = {}                  # the Ribbon's art panel, faded (paintEvent)
        self._panel_asked = None
        self._panel_bad: set = set()             # panel art keys that didn't decode
        self.settings_values = lambda: ({}, {})  # the window: (controls, appearance)
        self._by_slug: dict[str, dict] = {}
        self._friend_entries: list[dict] = []
        self._week_item: dict | None = None
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
        # The background's tint follows the focused game once focus rests
        # there (w-65): holding the D-pad doesn't strobe the colour.
        self._tint_slug: str | None = None
        self._tint = Dwell(self.TINT_DWELL, self._tint_settle, self)
        self._tint_timer = self._tint.timer
        self._masks: dict = {}                    # spotlight frame -> it faded out (paintEvent)
        QApplication.instance().focusChanged.connect(self._focus_changed)

    # ---- data ----
    def set_games(self, everything: list[dict]):
        games = [g for g in everything if is_installed(g)]
        owned = sorted((g for g in everything if not is_installed(g)),
                       key=lambda g: (not g.get('installing'), -(g.get('playtime_minutes') or 0),
                                      (g.get('display_name') or '').lower()))
        self.games = games
        self.everything = everything
        self._by_slug = {g['slug']: g for g in everything}
        self.hero.set_games(featured_games(games))
        self.hero.setVisible(bool(games))
        self._sync_art()
        self.rows.removeWidget(self.week_row)       # kept: clear_layout would delete it
        self.rows.removeWidget(self.friends_row)
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
            if title == "Jump back in":
                self.rows.addWidget(self.week_row)
        if self.rows.indexOf(self.week_row) < 0:     # nothing played: first
            self.rows.insertWidget(0, self.week_row)
        self.rows.insertWidget(self.rows.indexOf(self.week_row) + 1, self.friends_row)
        self._sync_week_game()
        self._fill_library()
        if self.details.isVisible():
            cur = next((g for g in everything
                        if g['slug'] == self.details.game.get('slug')), None)
            if cur:
                self.details.open(cur, refocus=False)

    # ---- Your week ----
    def set_week(self, week: dict | None, streak: int = 0, on: bool = True,
                 today: str | None = None):
        """`week`: history.periods()['week'] (None: not known). Hidden with no
        play this week or with the setting off; never a "0 h"."""
        show = bool(on and week and (week.get('minutes') or 0) > 0)
        self._week_item = None
        if show:
            self.week_card.set_week(week, streak, today)
            top = (week.get('games') or [{}])[0]
            self._week_slug = top.get('slug')
            self._sync_week_game()
            most = top.get('name') or top.get('slug')
            self._week_item = {'slug': self._week_slug, 'sub': "  ·  ".join(
                p for p in (self.week_card.total.text(), self.week_card.days.text(),
                            f"Most played: {most}" if most else '') if p)}
        elif self.week_card.hasFocus():              # focus first, then hide (w-60)
            nxt = self.default_focus()
            if nxt is not None and nxt is not self.week_card:
                focus_and_reveal(nxt)
        self.week_row.setVisible(show)
        self.refresh_ribbon()

    def _week_game(self) -> dict | None:
        slug = self._week_slug
        return next((g for g in self.everything if g.get('slug') == slug), None) \
            if slug else None

    def _sync_week_game(self):
        g = self._week_game()
        self.week_card.game_name = g.get('display_name') or g['slug'] if g else None

    def _week_open(self):
        """A on the card: the most played game's page (nothing if it has
        left the library)."""
        g = self._week_game()
        if g is not None:
            self.open_game(g)

    # ---- Friends playing now (w-82) ----
    def set_friends(self, entries: list[dict]):
        """friends.playing_now entries: a card per game (the window feeds
        it). Absent when nobody plays; focus leaves the row before it hides."""
        self._friend_entries = list(entries)
        self.refresh_ribbon()
        f = QApplication.focusWidget()
        inside = f is not None and self.friends_row.isAncestorOf(f)
        sig = tuple((e['appid'], e['slug'], e['title'],
                     tuple((r['steamid'], r.get('name'), r.get('avatar')) for r in e['people']))
                    for e in entries)
        if not entries:
            self._friends_sig = sig
            if inside:
                self.friends_row.hide()          # first: default_focus skips it
                nxt = self.default_focus()
                if nxt is not None:
                    focus_and_reveal(nxt)
            self.friends_row.hide()
            return
        if sig == self._friends_sig and self.friends_shelf is not None:
            for card, e in zip(self.friend_cards, entries):     # the minutes moved on
                card.item = e
                card.facts.setText(who_text(e))
            self.friends_row.show()
            return
        self._friends_sig = sig
        key = next((c.key for c in self.friend_cards if c is f), None)
        cards = []
        for e in entries:
            c = TheatreCard('friends', e, owned=bool(e.get('slug')))
            c.activated.connect(self._friend_activated)
            c.options.connect(self._friend_options)
            cards.append(c)
        h = max(c.sizeHint().height() for c in cards) + SP['2']
        shelf = ShelfRow("Friends playing now", TheatreCard.W, 'wide', tile_h=h)
        for c in cards:
            c.setFixedHeight(h)
            shelf.add(c)
        shelf.finish()
        lay = self.friends_row.layout()
        old = self.friends_shelf
        lay.addWidget(shelf)
        self.friends_shelf, self.friend_cards = shelf, cards
        self.friends_row.show()
        shelf.show()
        if inside:                               # the same game again, else the first
            focus_and_reveal(next((c for c in cards if c.key == key), cards[0]))
        if old is not None:
            lay.removeWidget(old)
            old.hide()
            old.deleteLater()

    def _friend_game(self, card) -> dict | None:
        slug = (card.item or {}).get('slug')
        return next((g for g in self.everything if g.get('slug') == slug), None) \
            if slug else None

    def _friend_activated(self, card):
        """A on a game friends play: its page when you have it, else the
        sheet (Open in Steam), as a Discover deal."""
        g = self._friend_game(card)
        if g is not None:
            self.open_game(g)
        else:
            self.deal_asked.emit(card.item, 'friends')

    def _friend_options(self, card):
        self.deal_asked.emit(card.item, 'friends')

    def refresh_friends(self):
        """New friends data: the game page's Friends and fact line, and the
        Library under Friends own (or its fallback once nobody owns)."""
        self.details.refresh_friends()
        show = str(self.lib_state.get('show') or '')
        if show == 'friends' or show.startswith(smart.FRIENDS_KEY):
            self._fill_library()

    def set_facts(self, facts: dict):
        """The facts index's games, for Play together's "More than one
        player" (the window feeds it with set_games)."""
        self.facts = facts or {}

    # ---- the Library tab ----
    SORTS = (('name', "A to Z"), ('played', "Recently played"), ('most', "Most played"),
             ('added', "Recently added"), ('collection', "By collection"))
    SHOWS = (('installed', "Installed"), ('missing', "Not installed"), ('all', "Everything"),
             ('friends', "Friends own"))

    def set_library_state(self, state: dict):
        """The saved sort and filter (prefs.theatre_library)."""
        self.lib_state = {'sort': state.get('sort', 'name'), 'show': state.get('show', 'installed')}
        self._fill_library()

    def _show(self) -> str:
        """The Show filter in use: Friends own falls back to Installed while
        no game has friends data."""
        show = self.lib_state['show']
        together = show.startswith(smart.FRIENDS_KEY)     # Play together (w-83)
        if (show == 'friends' or together) \
                and not any(g.get('friends') for g in self.everything):
            return 'installed'
        return show

    def _lib_games(self) -> list[dict]:
        show = self._show()
        if show.startswith(smart.FRIENDS_KEY):            # Play together (w-83)
            pool = smart.games_for(show, self.everything, self.facts,
                                   friends=FRIENDS.public_ids())
        else:
            pool = {'installed': self.games,
                    'missing': [g for g in self.everything if not is_installed(g)],
                    'all': self.everything,
                    'friends': [g for g in self.everything if g.get('friends')]}[show]
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
        sort = self.lib_state['sort']
        groups = self._collection_groups(games) if sort == 'collection' else [(None, games)]
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
        sort_t, show_t = self._library_labels()
        n = len(games)
        self.lib_chip.setText(f"{sort_t}  ·  {show_t}  ·  "
                              f"{n} game{'s' if n != 1 else ''}")
        self.refresh_ribbon()

    @staticmethod
    def _collection_groups(games: list[dict]) -> list[tuple[str, list[dict]]]:
        """By collection: a block per collection (a game in two is in both),
        the rest under "No collection", each A to Z; empty ones left out."""
        name = lambda g: (g.get('display_name') or '').lower()      # noqa: E731
        groups = [(n, [g for g in games if n in (g.get('collections') or ())])
                  for n in COLLECTION_NAMES]
        groups.append(("No collection", [g for g in games if not g.get('collections')]))
        return [(t, sorted(gs, key=name)) for t, gs in groups if gs]

    def _library_labels(self) -> tuple[str, str]:
        """The Library's sort and Show as words ("A to Z", "Installed")."""
        show = self._show()
        return dict(self.SORTS)[self.lib_state['sort']], \
            "Play together" if show.startswith(smart.FRIENDS_KEY) else dict(self.SHOWS)[show]

    def open_library_sheet(self, sheet, focus: int = 0):
        """Sort & filter in `sheet` (Theatre's ActionSheet): a pick updates
        the grid and the chip behind it and the sheet stays up."""
        def row(kind, key, text, i):
            on = self.lib_state[kind] == key
            return (text, G['check'] if on else G['list'],
                    lambda: self._pick_library(sheet, kind, key, i), on)
        # Friends own only once some game has friends data (or it is in use).
        shows = [(k, t) for k, t in self.SHOWS if k != 'friends' or self.lib_state['show'] == k
                 or any(g.get('friends') for g in self.everything)]
        items = ["Sort by"] + [row('sort', k, t, i) for i, (k, t) in enumerate(self.SORTS)] \
            + ["Show"] + [row('show', k, t, len(self.SORTS) + i)
                          for i, (k, t) in enumerate(shows)]
        # Play together… (w-83): the picker in Theatre's modal, ticked while
        # its filter is the one in use; only with a public friend to pick.
        cur = str(self.lib_state.get('show') or '')
        together = cur.startswith(smart.FRIENDS_KEY)
        if FRIENDS.public() or together:
            items.append(("Play together…", G['check'] if together else G['friends'],
                          self._ask_together, together))
        sheet.choose("Sort & filter", items, focus=focus, keep_open=True)

    def _ask_together(self):
        cur = str(self.lib_state.get('show') or '')
        self.together_asked.emit(cur if cur.startswith(smart.FRIENDS_KEY) else '')

    def use_show(self, key: str):
        """Play together's Show N games: the Library shows that friends
        filter (saved like a pick in Sort & filter)."""
        self.lib_state = {**self.lib_state, 'show': key}
        self._fill_library()
        self.library_changed.emit(dict(self.lib_state))

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
        on_library = self.ribbon.state.cat == 'library' and \
            self.pages.currentWidget() is self.ribbon if self.layout_mode == 'ribbon' \
            else self.tabs.current() == 'library'
        if on_library and not self.details.isVisible() \
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
        self._spot.push(tile.game)

    def _spot_follow(self, g):
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
        up with the page and fading out before the shelves. It fades to
        transparent, not to the page colour, so the window's background
        (and its art-tinted wash, w-65) shows through below it."""
        if self.layout_mode == 'ribbon':
            self._paint_panel()
            return
        if not self._art_on() or self.details.isVisible():
            return
        h = self.hero
        w, ht = self.width(), self.height()
        prev, cur = h._frame(h._prev, (w, ht)), h._frame(h._cur, (w, ht))
        if cur is None and prev is None:
            return
        pt = QPainter(self)
        y = -self.home.verticalScrollBar().value()
        if prev is not None and h._fade < 1.0:
            pt.drawPixmap(0, y, self._masked(prev, w, ht))
        if cur is not None:
            pt.setOpacity(h._fade if prev is not None else 1.0)
            pt.drawPixmap(0, y, self._masked(cur, w, ht))
            pt.setOpacity(1.0)
        pt.end()

    def _masked(self, frame: QPixmap, w: int, h: int) -> QPixmap:
        """The spotlight frame with its left scrim (the text side) and its
        fade at the bottom baked in, so a repaint (scrolling, the cross-fade)
        is one or two pixmap draws. Kept for the last few frames."""
        key = (frame.cacheKey(), w, h, P['scrim'])
        hit = self._masks.get(key)
        if hit is not None:
            return hit
        out = QPixmap(frame.size())
        out.setDevicePixelRatio(frame.devicePixelRatio())
        out.fill(Qt.GlobalColor.transparent)
        pt = QPainter(out)
        pt.drawPixmap(0, 0, frame)
        r = QRectF(0, 0, w, h)
        base = QColor(P['scrim'])
        g = QLinearGradient(0.0, 0.0, float(w), 0.0)
        for at, a in ((0.0, 0.90), (0.45, 0.45), (0.75, 0.05), (1.0, 0.0)):
            c = QColor(base); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))
        # Keep the art's alpha: opaque at the top, gone at the bottom.
        pt.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        g = QLinearGradient(0.0, float(h), 0.0, 0.0)
        for at, a in ((0.0, 0.0), (0.35, 0.45), (0.6, 1.0), (1.0, 1.0)):
            c = QColor(0, 0, 0); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))
        pt.end()
        if len(self._masks) >= 3:                 # a cross-fade needs two; a resize makes many
            self._masks.pop(next(iter(self._masks)))
        self._masks[key] = out
        return out

    # ---- the background's tint follows focus (w-65) ----
    def _game_at(self, w) -> dict | None:
        """The game a focused control stands for: a tile, the game page,
        the spotlight, Your week (its most played game)."""
        while w is not None and w is not self:
            if w is self.ribbon:
                return self._ribbon_game(self.ribbon.current())
            if isinstance(w, GameTile):
                return w.game
            if w is self.details:
                return self.details.game or None
            if w is self.hero:
                return self.hero.game or None
            if w is self.week_card:
                return self._week_game()
            if isinstance(w, TheatreCard) and w.kind == 'friends':
                return self._friend_game(w)
            w = w.parentWidget()
        return None

    def _focus_changed(self, _old, new):
        if new is None or not self.isVisible() or not self.isAncestorOf(new):
            return
        g = self._game_at(new)
        if g is None:
            return                               # a tab, a news card: the colour stays
        self._tint.push(g)

    def _tint_settle(self, g=None):
        g = g if g is not None else self._tint.value
        if g is None or g.get('slug') == self._tint_slug or not alive(self):
            return
        self._tint_slug = g.get('slug')
        self.focus_game.emit(g)

    def refresh_tint(self):
        """Entering Theatre or a new look: the tint of the game in focus,
        else the spotlight's, at once."""
        self._tint.cancel()
        f = QApplication.focusWidget()
        g = self._game_at(f) if f is not None and self.isAncestorOf(f) else None
        if g is None and self.hero.games:
            g = self.hero.game or None
        self._tint_slug = g.get('slug') if g else None
        self.focus_game.emit(g)

    # ---- the Ribbon layout (w-108) ----
    PAGE_TITLES = {'discover': "Discover", 'downloads': "Downloads"}

    def set_layout(self, mode: str):
        """Tabs (rows of tiles) or the Ribbon. The game in focus stays in
        focus. Focus is set on purpose, on a widget that stays visible, and
        before anything is hidden: hiding the focused widget would throw it
        somewhere else (w-60). A Discover or Downloads page stays open only
        when it is on screen in Theatre."""
        mode = 'ribbon' if mode == 'ribbon' else 'tabs'
        if mode == self.layout_mode:
            return
        f = QApplication.focusWidget()
        # Changed from Theatre's settings sheet: the sheet keeps focus, and
        # gets the new place to give it back to.
        holder = next((o for o in self.children() if isinstance(o, QWidget) and o.isVisible()
                       and hasattr(o, '_back_to') and f is not None and o.isAncestorOf(f)),
                      None)
        if holder is not None:
            f = holder._back_to
        shown = self.isVisible()
        inside = f is not None and shown and self._in_base(f)
        g = self._game_at(f) if inside else None
        page = self.pages.currentWidget()
        sub = next((k for k in self.PAGE_TITLES if self._pages[k] is page), None) \
            if shown else None
        self.layout_mode = mode
        if mode == 'ribbon':
            self._ribbon_sub = sub               # first: the tabs stay on that page
            self._ribbon_build()
            cat = self.tabs.current() if self.tabs.current() in ('home', 'library') else 'home'
            self.ribbon.select(sub or cat)
            if g is not None and not sub:
                key = f"game:{g['slug']}"
                for c in (cat, 'library' if cat == 'home' else 'home'):
                    if find(self.ribbon.cats, c, key) is not None:
                        self.ribbon.select(c, key)
                        break
            if sub:                              # the page stays, now over the Ribbon
                self.crumb.setText(self.PAGE_TITLES[sub])
                self.crumb.show()
            else:
                self.pages.setCurrentWidget(self.ribbon)
        else:
            self._ribbon_timer.stop()
            self._ribbon_sub = None
            self.crumb.hide()
            cat = self.ribbon.state.cat
            key = sub or (cat if cat in self._pages and not self.tabs.item(cat).isHidden()
                          else 'home')
            self.tabs.set_current(key)
            self.pages.setCurrentWidget(self._pages[key])
        hiding = (self.tabs, self.btn_search, self.btn_surprise) if mode == 'ribbon' else ()
        if inside:
            target = self._layout_target(f, g, sub, hiding)
            if holder is not None:
                holder._back_to = target
            elif target is not None:
                focus_and_reveal(target)         # first, then hide what had it
        for w in (self.tabs, self.btn_search, self.btn_surprise):
            w.setVisible(mode == 'tabs')
        if inside and holder is None:
            now = QApplication.focusWidget()
            if now is None or not now.isVisible():
                focus_and_reveal(self.default_focus())
        self._sync_art()

    def _layout_target(self, f, g, sub, hiding):
        """Where focus goes after a layout switch: the Ribbon, the game's
        tile, or (a page that stays open) where it was, never a widget that
        is hidden or about to be."""
        if sub is None:
            if self.layout_mode == 'ribbon':
                return self.ribbon
            page = self.pages.currentWidget()
            tile = next((t for t in page.findChildren(GameTile)
                         if g is not None and t.game.get('slug') == g['slug']
                         and not t.isHidden()), None)
            return tile or self.default_focus()
        stays = f is not None and f.isVisible() \
            and not any(w is f or w.isAncestorOf(f) for w in hiding)
        return f if stays else self.default_focus()

    def _in_base(self, w) -> bool:
        """`w` is on the page or the top bar (not on an overlay)."""
        return self.pages.isAncestorOf(w) or self.tabs.isAncestorOf(w) \
            or w in (self.btn_search, self.btn_surprise, self.btn_settings)

    def refresh_ribbon(self):
        """The Ribbon's data changed: rebuild it once the event loop runs
        (several feeds in a row make one rebuild)."""
        if self.layout_mode == 'ribbon':
            self._ribbon_timer.start()

    def _ribbon_flush(self):
        if self._ribbon_timer.isActive():
            self._ribbon_build()

    def _ribbon_build(self):
        self._ribbon_timer.stop()
        if self.layout_mode != 'ribbon':
            return
        self.ribbon.set_categories(ribbon_model.categories(self._ribbon_feed()))
        self.update()                            # the art panel follows the item

    def _ribbon_feed(self) -> 'ribbon_model.Feed':
        """What Theatre holds, for ribbon_model.categories."""
        controls, appearance = self.settings_values()
        settings = []
        for _grp, key, title, choices in ROWS:
            if key == 'theatre_layout':
                value = self.layout_mode
            else:
                value = (appearance if key in APPEARANCE_KEYS else controls).get(key,
                                                                                choices[0][0])
            enabled = not (key == 'volume' and not controls.get('sounds')) \
                and not (key == 'ribbon_motion' and self.layout_mode != 'ribbon')
            settings.append((key, title, choices, value, enabled))
        sort_t, show_t = self._library_labels()
        games = self._lib_games()
        groups = self._collection_groups(games) \
            if self.lib_state['sort'] == 'collection' else None
        run = now_playing(self.everything)
        kicker = "STARTING…"
        if run is not None and run.get('playing'):
            mins = session_minutes(run)
            kicker = "NOW PLAYING" + (f"  ·  {mins} MIN" if mins is not None else "")
        groups_n: dict[str, int] = {}
        for row in self.downloads_page.rows.values():
            groups_n[row.r['group']] = groups_n.get(row.r['group'], 0) + 1
        down = "  ·  ".join(t for t in (
            f"{groups_n['progress']} downloading" if groups_n.get('progress') else '',
            f"{groups_n['queue']} up next" if groups_n.get('queue') else '',
            f"{groups_n['ready']} ready to play" if groups_n.get('ready') else '') if t)
        return ribbon_model.Feed(
            games=self.games, library=games, lib_groups=groups,
            lib_label=f"{show_t}  ·  {sort_t}", lib_show_label=show_t,
            playing=run, playing_kicker=kicker, week=self._week_item,
            friends=[{'key': f"friend:{e['appid']}", 'title': e['title'], 'sub': who_text(e),
                      'slug': e.get('slug')} for e in self._friend_entries],
            settings=settings, discover=not self.tabs.item('discover').isHidden(),
            downloads=not self.tabs.item('downloads').isHidden(), downloads_sub=down,
            fact=fact_text, a_plays=self.a_plays)

    def ribbon_home(self):
        """Home's first item (Now playing while a game runs): entering
        Theatre with a game running, Back to Home on the session band."""
        if self.layout_mode != 'ribbon':
            return
        self._ribbon_flush()
        self._ribbon_close_page()
        home = next((c for c in self.ribbon.cats if c.key == 'home'), None)
        if home is not None and home.items:
            self.ribbon.select('home', home.items[0].key)
        self.update()

    def _ribbon_art(self, slug: str) -> tuple[str | None, str | None]:
        g = self._by_slug.get(slug)
        return art_source(g, WIDE_KINDS) if g else (None, None)

    def _ribbon_game(self, item) -> dict | None:
        """The game an item stands for: a game, Your week's most played, a
        game friends play that you have."""
        if item is None:
            return None
        if item.key == 'card:week':
            return self._week_game()
        return self._by_slug.get(item.slug) if item.slug else None

    def _friend_entry(self, key: str) -> dict | None:
        return next((e for e in self._friend_entries if f"friend:{e['appid']}" == key), None)

    def _ribbon_activate(self, item):
        """A on the focused item: what its `primary` says."""
        if item.kind == 'game':
            g = self._by_slug.get(item.slug)
            if g is None:
                return
            if item.primary == 'resume':
                self.action.emit('resume', g)
            else:
                self._activated(g)
        elif item.key == 'card:week':
            self._week_open()
        elif item.key.startswith('friend:'):
            e = self._friend_entry(item.key)
            g = self._by_slug.get(item.slug) if item.slug else None
            if g is not None:
                self.open_game(g)
            elif e is not None:
                self.deal_asked.emit(e, 'friends')
        elif item.primary.startswith('tab:'):
            self._ribbon_tab(item.primary[4:])
        elif item.kind == 'setting' and item.primary == 'step':
            self.ribbon_setting.emit(item.key[4:], ribbon_model.step_value(item))
        elif item.primary == 'sheet':
            self.library_sheet.emit()
        elif item.primary == 'all_settings':
            self.all_settings.emit()

    def _ribbon_options(self, item):
        """X: a game's options sheet; a setting's row in Theatre's settings."""
        if item.kind == 'game':
            g = self._by_slug.get(item.slug)
            if g is not None:
                self.menu.emit(g, QPoint())
        elif item.kind == 'setting':
            self.settings_row.emit(item.key[4:])
        elif item.key.startswith('friend:'):
            e = self._friend_entry(item.key)
            if e is not None:
                self.deal_asked.emit(e, 'friends')

    def _ribbon_moved(self, d: int):
        g = self._ribbon_game(self.ribbon.current())
        if g is not None:
            self._tint.push(g)                   # focus stays on the Ribbon: push it here
        self.update()
        self.ribbon_moved.emit(d)

    def _ribbon_category(self, key: str):
        """The tabs follow the Ribbon (Home's presence, the hints), without
        switching a page."""
        if self._ribbon_sub is not None:
            return
        self._mirror_tabs(key if key in ('home', 'library') else 'home')

    def _mirror_tabs(self, key: str):
        """Put the hidden tabs on `key` (their `changed` still goes out, for
        Home's presence) without switching a page."""
        if self.tabs.current() != key:
            self._mirroring = True
            try:
                self.tabs.set_current(key, animate=False, emit=True)
            finally:
                self._mirroring = False

    def _ribbon_tab(self, key: str):
        """A tab picked while in the Ribbon (Back to Home, View on a finished
        download, a tab that went away): its category, or its page."""
        if key in self.PAGE_TITLES:
            self._ribbon_sub = key               # first: the tabs then stay on this page
            self.ribbon.select(key)
            self._mirror_tabs(key)
            self.pages.setCurrentWidget(self._pages[key])
            self.crumb.setText(self.PAGE_TITLES[key])
            self.crumb.show()
            if key == 'discover':
                self.discover_shown.emit()
            self._sync_art()
            QTimer.singleShot(0, lambda: alive(self) and focus_and_reveal(self.default_focus()))
            return
        self._ribbon_close_page()
        self.ribbon.select(key)

    def _ribbon_close_page(self) -> bool:
        """B on Discover or Downloads opened from the Ribbon: back to it, on
        the same item. True if a page was open."""
        if self._ribbon_sub is None:
            return False
        self._ribbon_sub = None
        self.crumb.hide()
        self.pages.setCurrentWidget(self.ribbon)
        focus_and_reveal(self.ribbon)
        self._ribbon_category(self.ribbon.state.cat)
        self._sync_art()
        return True

    def _paint_panel(self):
        """The Ribbon's focused game, quiet, on the right: its hero art over
        60% of the width, faded in from the left and out at the bottom. Dark
        themes only (not on a light theme, nor High contrast)."""
        if self.details.isVisible() or self.pages.currentWidget() is not self.ribbon \
                or LOOK['light'] or LOOK['contrast']:
            return
        it = self.ribbon.current()
        g = self._by_slug.get(it.slug) if it is not None and it.kind == 'game' else None
        path = art_source(g, HERO_KINDS)[0] if g else None
        if not path:
            return
        w, h = self.width(), self.height()
        pw = int(w * 0.6)
        key = ART.key(path, pw, h)
        if key is None or key in self._panel_bad:
            return                               # no file, or it didn't decode: no panel
        pm = ART.get(key)
        if pm is None:
            if key != self._panel_asked:
                self._panel_asked = key
                ART.request_key(key, lambda pm, k=key: self._panel_decoded(k, pm))
            return
        pt = QPainter(self)
        pt.drawPixmap(w - pw, 0, self._panel(pm, pw, h))
        pt.end()

    def _panel_decoded(self, key, pm):
        self._panel_asked = None
        if pm is None:
            self._panel_bad.add(key)             # never asked again (a new file: a new key)
        self.update()

    def _panel(self, art: QPixmap, w: int, h: int) -> QPixmap:
        """The art with its fades baked in (kept for the last two)."""
        key = (art.cacheKey(), w, h)
        hit = self._panels.get(key)
        if hit is not None:
            return hit
        out = QPixmap(art.size())
        out.setDevicePixelRatio(art.devicePixelRatio())
        out.fill(Qt.GlobalColor.transparent)
        pt = QPainter(out)
        pt.drawPixmap(0, 0, art)
        pt.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        g = QLinearGradient(0.0, 0.0, float(w), 0.0)
        for at, a in ((0.0, 0), (0.55, 50), (1.0, 95)):
            g.setColorAt(at, QColor(0, 0, 0, a))
        pt.fillRect(QRectF(0, 0, w, h), QBrush(g))
        g = QLinearGradient(0.0, 0.0, 0.0, float(h))
        for at, a in ((0.0, 60), (0.18, 255), (0.60, 255), (1.0, 0)):   # quieter under the clock
            g.setColorAt(at, QColor(0, 0, 0, a))
        pt.fillRect(QRectF(0, 0, w, h), QBrush(g))
        pt.end()
        if len(self._panels) >= 2:
            self._panels.pop(next(iter(self._panels)))
        self._panels[key] = out
        return out

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
        if self.layout_mode == 'ribbon':
            if not self._mirroring:
                self._ribbon_tab(key)
            return
        self.pages.setCurrentWidget(self._pages[key])
        if key == 'discover':
            self.discover_shown.emit()
        self._sync_art()
        QTimer.singleShot(0, lambda: alive(self) and focus_and_reveal(self.default_focus()))

    def cycle_tab(self, d):
        """LB / RB: the next tab that is showing, wrapping round (the
        Ribbon: the next category)."""
        if self.layout_mode == 'ribbon':
            self._ribbon_close_page()
            self.ribbon.cycle(d)
            return
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
        self.refresh_ribbon()

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
        if self.layout_mode == 'ribbon':
            self.ribbon_home()
            return
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
            # The Ribbon has no search button (Y searches): back to the item.
            focus_and_reveal(self.btn_search if self.layout_mode == 'tabs'
                             else self.default_focus())

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
        if self.layout_mode == 'ribbon':
            if self._ribbon_close_page():
                return True
            if self.ribbon.state.cat != 'home':
                self.ribbon.select('home')
                self.ribbon_moved.emit(-1)
                return True
            return False
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
        if self.pages.currentWidget() is self.ribbon:
            return self.ribbon
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
        if self.details.isVisible():             # friends in it: minutes, or gone once stale
            self.details.refresh_friends()
        if self.hero.pinned is not None:         # "42 min" on the spotlight and tiles
            self.hero.tick()
            self.refresh_ribbon()
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
from .art import ART, art_source, HERO_KINDS, screen_pixmap, WIDE_KINDS  # noqa: E402
from .fmt import fmt_hours  # noqa: E402
from .gameinfo import (can_quick_play, is_installed, live_kicker, now_playing,  # noqa: E402
    played_minutes, session_minutes)
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .library import Dwell, featured_games, ShelfRow, surprise_pick  # noqa: E402
from .actions import TrailerOverlay  # noqa: E402
from .friends import FRIENDS, who_text  # noqa: E402
from nexus import smart  # noqa: E402
from .theatre_discover import TheatreCard, TheatreDiscover, TheatreReader  # noqa: E402
from .theatre_settings import APPEARANCE_KEYS, HELP, ROWS, WeekCard  # noqa: E402
from .theme import LOOK, TILE_LIFT  # noqa: E402
from . import ribbon_model  # noqa: E402
from .ribbon import RibbonView  # noqa: E402
from .ribbon_model import find  # noqa: E402
from .theatre_game import fact_line, fact_text, TheatreDetails  # noqa: E402
from .tiles import GameTile  # noqa: E402
from .widgets import (clear_layout, EmptyState, FlowLayout, glyph_pixmap, icon_button,  # noqa: E402
    icon_label, label, plain_label, SegmentedControl, set_glyph_icon)
from .topbar import Wordmark  # noqa: E402
