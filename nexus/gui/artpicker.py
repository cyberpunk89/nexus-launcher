"""The artwork picker: a game's cover, banner, hero, logo and icon."""


from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget)

from .theme import FONTS, G, GRAD, P, R, SP


ART_KIND_LABEL = {'cover': "Cover", 'wide': "Banner", 'hero': "Hero", 'logo': "Logo",
                  'icon': "Icon"}
ART_THUMB = {'cover': (120, 180), 'wide': (230, 108), 'hero': (320, 103),
             'logo': (220, 96), 'icon': (80, 80)}


class ArtThumb(QFrame):
    """One SteamGridDB image to pick. Focusable; Enter/A or a click picks it."""
    picked = pyqtSignal(dict)

    def __init__(self, item: dict, kind: str, current: bool):
        super().__init__()
        self.item, self.kind, self.current = item, kind, current
        w, h = ART_THUMB[kind]
        self.setFixedSize(w, h)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        tip = [f"{item.get('width')}×{item.get('height')}"]
        if item.get('style'):
            tip.append(item['style'])
        if item.get('author'):
            tip.append(f"by {item['author']}")
        self.setToolTip(" · ".join(tip))
        self._pm = None
        cached = ART.cached(item['thumb'])
        if cached:
            self._load(cached)
        else:
            ART.fetch(item['thumb'], self._arrived)

    def _arrived(self, path):
        try:
            self._load(path)
        except RuntimeError:
            pass

    def _load(self, path):
        pm = QPixmap(path)
        if pm.isNull():
            return
        w, h = self.width(), self.height()
        if self.kind in ('logo', 'icon'):
            self._pm = pm.scaled(w - 16, h - 16, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        else:
            self._pm = cover_pixmap(path, w, h)
        self.update()

    def set_current(self, on: bool):
        self.current = on
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.item)
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.picked.emit(self.item)
        else:
            super().keyPressEvent(ev)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(r, R['tile'], R['tile'])
        pt.setClipPath(clip)
        if self.kind in ('logo', 'icon'):
            # A checkerboard, so dark logos are as readable as light ones.
            pt.fillRect(r, QColor('#3a3650'))
            sq, alt = 10, QColor('#2a273d')
            for yy in range(0, self.height(), sq):
                for xx in range((yy // sq) % 2 * sq, self.width(), sq * 2):
                    pt.fillRect(xx, yy, sq, sq, alt)
        else:
            pt.fillRect(r, QColor(P['surface_2']))
        if self._pm is not None:
            x = (self.width() - self._pm.width()) / 2
            y = (self.height() - self._pm.height()) / 2
            pt.drawPixmap(QPointF(x, y), self._pm)
        pt.setClipping(False)
        pt.setBrush(Qt.BrushStyle.NoBrush)
        if self.current or self.underMouse():
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            a, b = (QColor(c) for c in GRAD['dusk'])
            if not self.current:
                a.setAlphaF(0.7); b.setAlphaF(0.7)
            g.setColorAt(0.0, a); g.setColorAt(1.0, b)
            pt.setPen(QPen(QBrush(g), 3.0 if self.current else 2.0))
            pt.drawRoundedRect(r.adjusted(1, 1, -1, -1), R['tile'] - 1, R['tile'] - 1)
        else:
            hair = QColor(P['on_art']); hair.setAlphaF(0.06)
            pt.setPen(QPen(hair, 1.0))
            pt.drawRoundedRect(r, R['tile'], R['tile'])
        if self.current:
            c = QPointF(self.width() - 16.0, 16.0)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(P['accent']))
            pt.drawEllipse(c, 10.0, 10.0)
            f = QFont(FONTS.icon)
            f.setPixelSize(11)
            pt.setFont(f)
            pt.setPen(QColor(P['on_accent']))
            pt.drawText(QRectF(c.x() - 10, c.y() - 10, 20, 20), Qt.AlignmentFlag.AlignCenter,
                        G['check'])
        pt.end()


class ArtPickerPage(QWidget):
    """Every SteamGridDB image for one game and one kind, as a grid to pick
    from. Picking runs `--set-art` as a job; Steam's own files are never
    touched (a repack's Steam shortcut gets the new art too)."""
    request = pyqtSignal(list, str, bool, str)
    back = pyqtSignal()
    open_settings = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.game: dict = {}
        self.kind = 'hero'
        self.page = 0
        self.items: list[dict] = []
        self.current_url = None
        self._query = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        outer.setSpacing(SP['3'])
        head = QHBoxLayout()
        back = QPushButton("Back")
        back.setObjectName("Ghost")
        set_glyph_icon(back, G['arrow_left'], 16)
        back.clicked.connect(self.back)
        head.addWidget(back)
        self.title = label("Artwork", "PageTitle")
        head.addWidget(self.title)
        head.addStretch()
        self.btn_reset = QPushButton("Reset to default")
        self.btn_reset.clicked.connect(self._reset)
        head.addWidget(self.btn_reset)
        self.btn_site = QPushButton("Open on SteamGridDB")
        self.btn_site.setObjectName("Ghost")
        self.btn_site.clicked.connect(self._open_site)
        head.addWidget(self.btn_site)
        self.btn_match = QPushButton("Wrong game?")
        self.btn_match.setObjectName("Ghost")
        self.btn_match.clicked.connect(self._toggle_match)
        head.addWidget(self.btn_match)
        outer.addLayout(head)
        # Choose which SteamGridDB game this one fetches art from.
        self.match_box = QFrame()
        self.match_box.setObjectName("Card")
        ml = QVBoxLayout(self.match_box)
        ml.setContentsMargins(SP['3'], SP['3'], SP['3'], SP['3'])
        ml.setSpacing(SP['2'])
        self.match_note = label("", "Muted", wrap=True)
        ml.addWidget(self.match_note)
        srow = QHBoxLayout()
        self.match_ed = QLineEdit()
        self.match_ed.setPlaceholderText("Search SteamGridDB for the right game")
        self.match_ed.returnPressed.connect(self._match_search)
        srow.addWidget(self.match_ed, 1)
        go = QPushButton("Search")
        go.clicked.connect(self._match_search)
        srow.addWidget(go)
        self.btn_auto = QPushButton("Use automatic match")
        self.btn_auto.setObjectName("Ghost")
        self.btn_auto.clicked.connect(self._match_reset)
        srow.addWidget(self.btn_auto)
        ml.addLayout(srow)
        self.match_rows = QVBoxLayout()
        self.match_rows.setSpacing(SP['1'])
        ml.addLayout(self.match_rows)
        self.match_box.setVisible(False)
        outer.addWidget(self.match_box)
        row = QHBoxLayout()
        self.kinds = SegmentedControl('pill', 34)
        for k in ('cover', 'wide', 'hero', 'logo', 'icon'):
            self.kinds.add(k, ART_KIND_LABEL[k])
        self.kinds.changed.connect(self._pick_kind)
        row.addWidget(self.kinds)
        row.addSpacing(SP['3'])
        self.status = label("", "Muted")
        row.addWidget(self.status)
        row.addStretch()
        outer.addLayout(row)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, SP['1'], 0, SP['4'])
        self.flow_host = QWidget()
        self.flow = FlowLayout(self.flow_host, margin=SP['1'], spacing=SP['3'], justify=True)
        hl.addWidget(self.flow_host)
        self.empty = EmptyState(G['image'], "")
        self.empty.action.connect(self.open_settings)
        self.empty.setVisible(False)
        hl.addWidget(self.empty)
        self.btn_more = QPushButton("Load more")
        self.btn_more.clicked.connect(lambda: self._load(self.page + 1))
        hl.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignHCenter)
        hl.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)

    def open(self, game: dict, kind: str = 'hero'):
        self.game = game
        self.title.setText(f"Artwork · {game.get('display_name', '')}")
        self.btn_site.setVisible(False)
        self.match_box.setVisible(False)
        self._match_q = 0
        self.kinds.set_current(kind, animate=False)
        self._pick_kind(kind)

    def reload(self):
        """Fetch the current kind again (after the SteamGridDB match changed)."""
        self._pick_kind(self.kind)

    def _toggle_match(self):
        show = not self.match_box.isVisible()
        self.match_box.setVisible(show)
        if not show:
            return
        self.match_ed.setText(self.game.get('display_name', ''))
        self.match_note.setText(
            "SteamGridDB matched this game "
            + ("by its Steam id." if self.game.get('source') == 'steam' else "by name.")
            + " Pick the right entry and the artwork is fetched from it instead. "
              "Art you chose for the old match is cleared.")
        self.match_ed.setFocus()
        self._match_search()

    def _match_search(self):
        term = self.match_ed.text().strip()
        if not term:
            return
        self._match_q += 1
        q = self._match_q
        clear_layout(self.match_rows)
        self.match_rows.addWidget(label("Searching…", "Muted"))

        def done(data, err, q=q):
            if q != self._match_q:
                return
            clear_layout(self.match_rows)
            items = (data or {}).get('items') or []
            if not items:
                msg = {'no-key': "Add a SteamGridDB API key in Settings.",
                       'auth': "SteamGridDB rejected the API key.",
                       'rate': "SteamGridDB is rate-limiting; try again in a minute."
                       }.get((data or {}).get('error'), "No games found.")
                self.match_rows.addWidget(label(msg, "Muted"))
                return
            for it in items[:8]:
                text = it['name'] + (f" ({it['year']})" if it.get('year') else "")
                b = QPushButton(text)
                b.setObjectName("Ghost")
                b.setStyleSheet("text-align: left;")
                b.clicked.connect(lambda _=False, it=it: self._match_pick(it))
                self.match_rows.addWidget(b)
        cli_json(['--sgdb-search', term, '--json'], done, 30000)

    def _match_pick(self, item):
        self.match_box.setVisible(False)
        self.request.emit(['--set-sgdb', self.game['slug'], '--id', str(item['id']),
                           '--sgdb-name', item['name']],
                          f"Using “{item['name']}” for {self.game.get('display_name', '')}'s art",
                          False, 'task')

    def _match_reset(self):
        self.match_box.setVisible(False)
        self.request.emit(['--set-sgdb', self.game['slug'], '--reset'],
                          f"Matching {self.game.get('display_name', '')}'s art automatically",
                          False, 'task')

    def _pick_kind(self, kind):
        self.kind = kind
        self.items = []
        self._clear()
        self._load(0)

    def _clear(self):
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()

    def _load(self, page):
        self.page = page
        self._query += 1
        q = self._query
        self.status.setText("Looking on SteamGridDB…")
        self.btn_more.setVisible(False)
        self.empty.setVisible(False)
        slug, kind = self.game.get('slug'), self.kind

        def done(data, err, q=q, kind=kind):
            if q != self._query:
                return                       # a newer request (other kind/page) won
            try:
                self._fill(data or {}, err)
            except RuntimeError:
                pass
        cli_json(['--art-variants', slug, '--kind', kind, '--page', str(page), '--json'],
                 done, 60000)

    def _fill(self, data, err):
        error = data.get('error') or (err and 'net')
        new = data.get('items') or []
        self.current_url = data.get('current')
        gid = data.get('game_id')
        self.btn_site.setVisible(bool(gid))
        self._gid = gid
        for it in new:
            t = ArtThumb(it, self.kind, it['url'] == self.current_url)
            t.picked.connect(self._picked)
            self.flow.addWidget(t)
        self.items += new
        n = len(self.items)
        label_ = ART_KIND_LABEL[self.kind].lower()
        if n:
            self.status.setText(f"{n} {label_} image{'s' if n != 1 else ''} · pick one to use it")
        else:
            self.status.setText("")
            title, hint, action = {
                'no-key': ("Add a SteamGridDB API key",
                           "Settings → SteamGridDB API key (free at steamgriddb.com).",
                           "Open Settings"),
                'auth': ("SteamGridDB rejected the API key", "Check it in Settings.",
                         "Open Settings"),
                'rate': ("SteamGridDB is rate-limiting", "Try again in a minute.", ''),
                'net': ("Couldn't reach SteamGridDB", "Check your connection.", ''),
            }.get(error, (f"No {label_} art for this game yet",
                          "Someone may upload one to SteamGridDB.", ''))
            self.empty.set(title, hint)
            self.empty.button.setText(action)
            self.empty.button.setVisible(bool(action))
            self.empty.setVisible(True)
        # SteamGridDB pages are 50 images.
        self.btn_more.setVisible(len(new) >= 50)
        first = next((t for t in self.flow_host.findChildren(ArtThumb) if not t.isHidden()), None)
        if first is not None and self.isVisible() and QApplication.focusWidget() is None:
            first.setFocus()

    def _picked(self, item):
        for t in self.flow_host.findChildren(ArtThumb):
            t.set_current(t.item is item)
        self.current_url = item['url']
        name = self.game.get('display_name', '')
        self.request.emit(['--set-art', self.game['slug'], '--kind', self.kind,
                           '--url', item['url']],
                          f"Setting the {ART_KIND_LABEL[self.kind].lower()} for {name}",
                          False, 'task')

    def _reset(self):
        for t in self.flow_host.findChildren(ArtThumb):
            t.set_current(False)
        self.current_url = None
        name = self.game.get('display_name', '')
        self.request.emit(['--set-art', self.game['slug'], '--kind', self.kind, '--reset'],
                          f"Resetting the {ART_KIND_LABEL[self.kind].lower()} for {name}",
                          False, 'task')

    def _open_site(self):
        if getattr(self, '_gid', None):
            QDesktopServices.openUrl(QUrl(f"https://www.steamgriddb.com/game/{self._gid}"))


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import ART, cover_pixmap  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .widgets import (clear_layout, EmptyState, FlowLayout, label, SegmentedControl,
    set_glyph_icon)  # noqa: E402
