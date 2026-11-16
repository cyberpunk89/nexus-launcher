"""DealSpotlight: one big deal at a time over the game's hero art. It
left the desktop Deals page in w-146 (For you and the shelves took its
place); it stays for the controller hints and anything that shows a
single deal large."""

import html

from PyQt6.QtCore import pyqtSignal, QPoint, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
    QPainterPath, QPixmap)
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout

from nexus import gamedb, hypervisor

from .deals import _pill, deal_links, deal_meta, deal_was, meta_html, steam_hero_url
from .theme import FONTS, FS, G, GRAD, P, R, SP


class DealSpotlight(QFrame):
    """The best deals right now, one at a time: the game's Steam hero art
    with the price over a scrim, Open deal / Grab / Steam buttons, and
    ‹ › to step through the top few."""
    H = 236
    activated = pyqtSignal(str)
    repack_action = pyqtSignal(str, dict, dict)

    def __init__(self):
        super().__init__()
        self.setObjectName("DealSpotlight")
        self.setFixedHeight(self.H)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.items: list[dict] = []
        self.repacks: dict = {}
        self.base = None
        self.index = 0
        self.primary = None
        self._pm: QPixmap | None = None
        self._path = self._want = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['4'], SP['4'])
        col = QVBoxLayout()
        col.setSpacing(SP['1_5'])
        col.addStretch()
        self.kicker = label("", "HeroKicker")
        col.addWidget(self.kicker)
        self.headline = QLabel()
        self.headline.setObjectName("NewsHeadline")
        col.addWidget(self.headline)
        prow = QHBoxLayout()
        prow.setSpacing(SP['2'])
        self.cut = _pill("", 'accent')
        prow.addWidget(self.cut)
        self.price = label("", None)
        self.price.setStyleSheet(f"color: {P['on_art']}; font-size: 22px; font-weight: 600;")
        prow.addWidget(self.price)
        self.was = label("", None)
        self.was.setTextFormat(Qt.TextFormat.RichText)
        self.was.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['sm']}px;")
        prow.addWidget(self.was, 0, Qt.AlignmentFlag.AlignBottom)
        prow.addStretch()
        col.addLayout(prow)
        self.meta = label("", "NewsLede")
        self.meta.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(self.meta)
        brow = QHBoxLayout()
        brow.setSpacing(SP['2'])
        self.btn_open = QPushButton("Open deal")
        self.btn_open.setObjectName("Primary")
        self.btn_open.clicked.connect(self.activate)
        self.btn_grab = QPushButton("Grab from FitGirl")
        self.btn_grab.setObjectName("Glass")
        set_glyph_icon(self.btn_grab, G['download'], 15, 'on_art', hover_role=None)
        self.btn_grab.clicked.connect(self._grab)
        self.btn_steam = QPushButton("Steam page")
        self.btn_steam.setObjectName("Glass")
        self.btn_steam.clicked.connect(self._steam)
        for b in (self.btn_open, self.btn_grab, self.btn_steam):
            b.setFixedHeight(34)
            brow.addWidget(b)
        brow.addStretch()
        col.addSpacing(SP['1'])
        col.addLayout(brow)
        lay.addLayout(col, 6)
        lay.addStretch(3)
        nav = QVBoxLayout()
        nav.addStretch()
        nrow = QHBoxLayout()
        nrow.setSpacing(SP['1'])
        self.btn_prev = icon_button(G['chevron_left'], "Previous deal", 30, "GlassIcon")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.counter = label("", "HeroKicker")
        self.counter.setContentsMargins(SP['2'], 0, SP['2'], 0)   # inside its backing
        self.btn_next = icon_button(G['chevron_right'], "Next deal", 30, "GlassIcon")
        self.btn_next.clicked.connect(lambda: self.step(1))
        nrow.addWidget(self.btn_prev)
        nrow.addWidget(self.counter)
        nrow.addWidget(self.btn_next)
        nav.addLayout(nrow)
        lay.addLayout(nav)
        for w in (self.kicker, self.headline, self.price, self.was, self.meta, self.cut,
                  self.counter):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_items(self, items: list[dict], repacks: dict, base):
        keep = self.items[self.index]['appid'] if self.items else None
        self.items, self.repacks, self.base = items, repacks, base
        self.index = next((i for i, g in enumerate(items) if g.get('appid') == keep), 0)
        if items:
            self._show()

    def step(self, d: int):
        if len(self.items) > 1:
            self.index = (self.index + d) % len(self.items)
            self._show()

    def current(self) -> dict:
        return self.items[self.index] if self.items else {}

    def _show(self):
        g = self.current()
        price = g.get('price') or {}
        self.primary, _lab, self.links = deal_links(g, 'deal', self.base, set())
        bits = ["BEST DEAL RIGHT NOW" if self.index == 0 else f"TOP DEAL #{self.index + 1}"]
        if price.get('atHistoricalLow'):
            bits.append("ALL-TIME LOW")
        elif price.get('historicalLowStr'):
            bits.append(f"LOWEST EVER {price['historicalLowStr']}")
        self.kicker.setText("  ·  ".join(bits))
        self.cut.setText(f"-{price['cutPct']}%" if price.get('cutPct') else '')
        self.cut.setVisible(bool(price.get('cutPct')))
        self.price.setText(price.get('currentStr') or '')
        was = deal_was(price)
        self.was.setText(f"<s>{html.escape(was)}</s>" if was else '')
        meta = deal_meta(g)
        rv = g.get('reviews') or {}
        if rv.get('steamDesc'):
            meta.insert(1 if meta and g.get('protonTier') else 0, (rv['steamDesc'], None))
        self.meta.setText(meta_html(meta))
        self.btn_open.setVisible(bool(self.primary))
        m = self.repacks.get(str(g.get('appid')))
        self.btn_grab.setVisible(m is not None)
        hv = hypervisor.badge((m or {}).get('hv'))
        self.btn_grab.setText(f"Grab from FitGirl ({hv})" if hv else "Grab from FitGirl")
        self.btn_grab.setToolTip(hypervisor.note(m['hv']) if hv else '')
        self.btn_steam.setVisible(bool(gamedb.safe_url(g.get('steamUrl'))))
        many = len(self.items) > 1
        for w in (self.btn_prev, self.btn_next, self.counter):
            w.setVisible(many)
        self.counter.setText(f"{self.index + 1} / {len(self.items)}")
        self._relabel()
        self._pm = self._path = None
        hero, header = steam_hero_url(g.get('appid')), gamedb.safe_url(g.get('headerImage'))
        self._want = g.get('appid')
        for url in (header, hero):              # the header now, the hero once it's here
            if not url:
                continue
            path = ART.cached(url)
            if path:
                self._arrived(path, g.get('appid'), url == hero)
            else:
                ART.fetch(url, lambda p, a=g.get('appid'), h=(url == hero):
                          self._arrived(p, a, h))
        self.update()

    def _arrived(self, path, appid, is_hero):
        try:
            if appid != self._want or (not is_hero and self._path and 'hero' in self._kind):
                return
        except RuntimeError:
            return
        self._path, self._kind = path, ('hero' if is_hero else 'header')
        self._decode()

    _kind = ''

    def _size(self):
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
            want = (self._path, w, h)
            ART.request(self._path, w, h, lambda pm, k=want: self._decoded(k, pm))

    def _decoded(self, key, pm):
        try:
            if key[0] == self._path and pm is not None:
                self._pm = pm                  # better than nothing while we re-ask
                self.update()
                if key[1:] != self._size():    # resized while it decoded: resizeEvent
                    self._decode()             # only re-asks once there's a pixmap
        except RuntimeError:
            pass

    def _relabel(self):
        w = max(200, int(self.width() * 0.62) - SP['5'])
        hf = QFont(FONTS.ui, -1, QFont.Weight.DemiBold)      # as #NewsHeadline
        hf.setPixelSize(26)
        self.headline.setText(elide_lines(self.current().get('title') or '',
                                          QFontMetrics(hf), w, 1))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.items:
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
            pt.drawPixmap((self.width() - self._pm.width()) // 2, 0, self._pm)
        else:
            a, b = (QColor(c) for c in GRAD['dawn'])
            a.setAlphaF(0.5)
            b.setAlphaF(0.2)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.fillRect(r, QBrush(g))
        s = QLinearGradient(r.topLeft(), r.topRight())
        base = QColor(P['scrim'])       # dark under the white text on a light theme too
        for at, alpha in ((0.0, 0.94), (0.45, 0.75), (0.8, 0.15), (1.0, 0.05)):
            c = QColor(base); c.setAlphaF(alpha)
            s.setColorAt(at, c)
        pt.fillRect(r, QBrush(s))
        if self.counter.isVisible():
            # "2 / 5" sits where the scrim has faded out: over light art, or
            # a light theme's light fallback, its white text needs a backing
            # of its own (dark like the glass ‹ › beside it, in every theme).
            g, h = self.counter.geometry(), self.btn_prev.height()
            box = QRectF(g.x(), g.center().y() + 0.5 - h / 2, g.width(), h)
            bg = QColor(P['scrim'])
            bg.setAlphaF(0.78)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(bg)
            pt.drawRoundedRect(box, h / 2, h / 2)
        pt.end()

    def activate(self):
        if self.primary:
            self.activated.emit(self.primary)

    def _grab(self):
        g = self.current()
        m = self.repacks.get(str(g.get('appid')))
        if m:
            self.repack_action.emit('grab', g, m)

    def _steam(self):
        u = gamedb.safe_url(self.current().get('steamUrl'))
        if u:
            self.activated.emit(u)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                ev.position().toPoint()):
            self.activate()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate()
        elif ev.key() == Qt.Key.Key_BracketRight:
            self.step(1)
        elif ev.key() == Qt.Key.Key_BracketLeft:
            self.step(-1)
        elif ev.key() == Qt.Key.Key_Menu:
            self._menu(self.mapToGlobal(QPoint(self.width() // 3, self.height() // 2)))
        else:
            super().keyPressEvent(ev)

    def contextMenuEvent(self, ev):
        self._menu(ev.globalPos())

    def menu_items(self) -> list[tuple]:
        """What the buttons do, plus the deal's links: (text, glyph, fn)."""
        g = self.current()
        if not g:
            return []
        items = []
        if self.primary:
            items.append(("Open deal", G['external'], self.activate))
        if self.repacks.get(str(g.get('appid'))):
            items.append(("Grab from FitGirl", G['download'], self._grab))
        steam = gamedb.safe_url(g.get('steamUrl'))
        if steam:
            items.append(("Steam page", G['external'], self._steam))
        seen = {self.primary, steam}
        for text, url in self.links:
            if url not in seen:
                seen.add(url)
                items.append((text, G['external'], lambda u=url: self.activated.emit(u)))
        if len(self.items) > 1:
            items += [("Next deal", G['chevron_right'], lambda: self.step(1)),
                      ("Previous deal", G['chevron_left'], lambda: self.step(-1))]
        return items

    def _menu(self, pos):
        items = self.menu_items()
        if not items:
            return
        sheet = getattr(self.window(), 'sheet', None)
        if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
            # A controller gets the sheet, never a desktop menu.
            sheet.choose(self.current().get('title') or "Deal", items)
            return
        m = QMenu(self)
        for text, _glyph, fn in items:
            m.addAction(text, fn)
        m.exec(pos)
        m.deleteLater()


def late_imports():
    """Sibling imports, run by deals.py at its end (they import it back)."""
    global ActionSheet, ART, elide_lines, icon_button, input_mode, label, set_glyph_icon
    from .actions import ActionSheet
    from .art import ART
    from .input import input_mode
    from .newspage import elide_lines
    from .widgets import icon_button, label, set_glyph_icon
