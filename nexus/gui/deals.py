"""The Deals page (Game-db): wishlist deals, upcoming releases, backlog."""

import html
import re
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QEvent, QPoint, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QFontMetrics,
    QLinearGradient, QPainter, QPainterPath, QPixmap)
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from nexus import gamedb, hypervisor
from nexus.features import has

from .fmt import fmt_ago
from .theme import FONTS, FS, G, GRAD, P, PILL_H, pill_qss, R, SP


def _pill(text: str, role: str) -> QLabel:
    lab = label(text, None)
    tint = {'success': P['success_18'], 'warning': P['warning_18'], 'danger': P['danger_18'],
            'accent': P['accent_22']}.get(role, P['surface_2'])
    lab.setStyleSheet(pill_qss(tint, P.get(role, P['subtext'])))
    lab.setFixedHeight(PILL_H)
    return lab


def deal_was(price: dict) -> str | None:
    """The price before the discount, in the same format as currentStr
    ("€10.03" at -44% -> "€17.91")."""
    cur, cut = price.get('current'), price.get('cutPct')
    text = price.get('currentStr') or ''
    if not cur or not cut or not 0 < cut < 100 or not re.search(r'\d', text):
        return None
    was = cur / (1 - cut / 100)
    sep = ',' if re.search(r'\d,\d{2}(?!\d)', text) else '.'
    return re.sub(r'\d[\d.,]*', f"{was:.2f}".replace('.', sep), text, count=1)


def deal_sort_key(sort: str):
    """A sort key for wishlist items. 'best' = on sale, at the all-time low,
    the biggest cut, then the best reviews."""
    def p(g):
        return g.get('price') or {}

    def rev(g):
        return (g.get('reviews') or {}).get('steamPercent') or 0
    if sort == 'discount':
        return lambda g: (-(p(g).get('cutPct') or 0), -rev(g))
    if sort == 'price':
        return lambda g: (p(g).get('current') is None, p(g).get('current') or 0)
    if sort == 'reviews':
        return lambda g: (-rev(g), -((g.get('reviews') or {}).get('metacritic') or 0))
    if sort == 'added':
        return lambda g: [-ord(c) for c in (g.get('dateAdded') or '')]
    if sort == 'name':
        return lambda g: (g.get('title') or '').casefold()
    return lambda g: (not p(g).get('onSale'), not p(g).get('atHistoricalLow'),
                      -(p(g).get('cutPct') or 0), -rev(g))


def deal_meta(item: dict) -> list[tuple[str, str | None]]:
    """(text, colour) bits for a deal's facts line: Proton tier, reviews,
    time to beat."""
    out = []
    tier = item.get('protonTier')
    if tier:
        out.append((tier.capitalize(), TIER_COLORS.get(tier, P['muted'])))
    rv = item.get('reviews') or {}
    if rv.get('steamPercent'):
        out.append((f"★ {rv['steamPercent']}%", None))
    elif rv.get('metacritic'):
        out.append((f"MC {rv['metacritic']}", None))
    hl = item.get('hltb') or {}
    if hl.get('main'):
        out.append((f"{hl['main']:g} h", None))
    return out


def meta_html(bits) -> str:
    dot = f" <span style='color:{P['overlay']}'>·</span> "
    return dot.join(f"<span style='color:{c}'>{html.escape(t)}</span>" if c else html.escape(t)
                    for t, c in bits)


def deal_links(item: dict, kind: str, base: str | None, installed: set):
    """(primary url, primary label, [(label, url)]) for a Game-db item.
    Only https links (or the configured local Game-db) are kept."""
    appid = item.get('appid')
    price = item.get('price') or {}
    best = price.get('best') or {}
    steam_page = gamedb.safe_url(item.get('steamUrl'))
    links: list[tuple[str, str]] = []
    if kind == 'tonight' and isinstance(appid, int):      # an id, never a path
        if appid in installed:
            primary, primary_label = f"steam://rungameid/{appid}", "Play"
        else:
            primary, primary_label = f"steam://install/{appid}", "Install in Steam"
        links.append((primary_label, primary))
    else:
        deal = gamedb.safe_url(best.get('url'))
        if deal:
            links.append((f"Open best deal ({best.get('shop') or 'keyshop'})", deal))
        gg = gamedb.safe_url(price.get('ggUrl'))
        if gg and gg != deal:
            links.append(("Compare on GG.deals", gg))
        primary = deal or steam_page
        primary_label = "Open deal" if deal else "Open on Steam"
    if steam_page:
        links.append(("Open on Steam", steam_page))
    if base and appid:
        gdb = gamedb.safe_url(gamedb.detail_url(base, appid), base)
        if gdb:
            links.append(("Open in Game-db", gdb))
    return primary, primary_label, links


def steam_hero_url(appid) -> str | None:
    return (f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{int(appid)}"
            "/library_hero.jpg") if str(appid or '').isdigit() else None


class DealArt(QWidget):
    """A deal's Steam header art with its badges painted on: the discount
    top-left, FitGirl / owned top-right, a countdown bottom-left."""

    def __init__(self, w: int, h: int, radius: int):
        super().__init__()
        self.setFixedSize(w, h)
        self.radius = radius
        self._pm: QPixmap | None = None
        self._path = None
        self.top_left: list[tuple[str, str]] = []      # (text, role)
        self.top_right: list[tuple[str, str]] = []
        self.bottom_left: list[tuple[str, str]] = []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def badges(self) -> list[str]:
        return [t for t, _r in self.top_left + self.top_right + self.bottom_left]

    def set_url(self, url: str | None):
        url = gamedb.safe_url(url)
        if not url:
            return
        path = ART.cached(url)
        if path:
            self._got(path)
        else:
            ART.fetch(url, self._got)

    def _got(self, path):
        try:
            self._path = path
            self._decode()
        except RuntimeError:                     # the tile is gone
            pass

    def _decode(self):
        if not self._path:
            return
        w, h = self.width(), self.height()
        pm = ART.peek(self._path, w, h)
        if pm is not None:
            self._pm = pm
            self.update()
            return
        want = (self._path, w, h)
        ART.request(self._path, w, h, lambda pm, k=want: self._decoded(k, pm))

    def _decoded(self, key, pm):
        if key == (self._path, self.width(), self.height()) and pm is not None:
            self._pm = pm
            self.update()

    def resize_to(self, w: int, h: int):
        """New size; the old pixmap is drawn scaled until refresh_size() (the
        page calls it once a resize settles: decoding per pixel step flooded
        the decoder and evicted the library's art from the cache)."""
        if (w, h) != (self.width(), self.height()):
            self.setFixedSize(w, h)
            self.update()

    def refresh_size(self):
        pm = self._pm
        if pm is None or (pm.width(), pm.height()) != (self.width(), self.height()):
            self._decode()

    def _badge(self, pt, x, y, text, role, right=False, up=False):
        f = QFont(FONTS.ui, -1, QFont.Weight.DemiBold)
        f.setPixelSize(FS['xs'])
        pt.setFont(f)
        w = pt.fontMetrics().horizontalAdvance(text) + 14
        h = 20
        r = QRectF(x - w if right else x, y - h if up else y, w, h)
        if role == 'glass':
            bg = QColor(P['scrim']); bg.setAlphaF(0.78)
            fg = QColor(P['on_art'])
        else:
            bg, fg = QColor(P.get(role, P['accent'])), QColor(P['on_accent'])
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(bg)
        pt.drawRoundedRect(r, h / 2, h / 2)
        pt.setPen(fg)
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        return w

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()                      # rounded on top only
        clip.addRoundedRect(r.adjusted(0, 0, 0, self.radius), self.radius, self.radius)
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        if self._pm is not None:
            if self._pm.size() == self.size():
                pt.drawPixmap(0, 0, self._pm)
            else:                                  # mid-resize: scaled until re-decoded
                pt.drawPixmap(self.rect(), self._pm)
        pad = SP['2']
        x = pad
        for text, role in self.top_left:
            x += self._badge(pt, x, pad, text, role) + SP['1']
        x = self.width() - pad
        for text, role in self.top_right:
            x -= self._badge(pt, x, pad, text, role, right=True) + SP['1']
        x = pad
        for text, role in self.bottom_left:
            x += self._badge(pt, x, self.height() - pad, text, role, up=True) + SP['1']
        pt.end()


class DealCard(QFrame):
    """One Game-db game as a store tile: art (with the discount and FitGirl
    badges), title, price and the old price, the all-time-low verdict and a
    facts line. Focusable like a GameTile — Return/Space/click does the
    primary action, the Menu key or right-click opens the link menu. Only
    https links (or the configured local Game-db) are ever opened."""
    W = 280
    activated = pyqtSignal(str)        # url (https:// or steam://)
    repack_action = pyqtSignal(str, dict, dict)   # 'grab'|'page'|'reject', item, match

    @staticmethod
    def art_h(w: int) -> int:
        return int((w - 2) * 215 / 460)           # Steam's header.jpg

    def __init__(self, item: dict, kind: str, base: str | None, installed: set,
                 repack: dict | None = None, owned: bool = False, width: int = W):
        super().__init__()
        self.setObjectName("DealTile")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.item, self.kind, self.base = item, kind, base
        self.repack = repack if kind in ('deal', 'upcoming') else None
        self.W = width
        self.setFixedWidth(width)
        self.primary, self.primary_label, self.links = deal_links(item, kind, base, installed)
        price = item.get('price') or {}
        best = price.get('best') or {}
        self.name = item.get('title') or f"App {item.get('appid')}"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, SP['3'])
        lay.setSpacing(SP['1_5'])
        self.art = DealArt(width - 2, self.art_h(width), R['card'] - 1)
        if price.get('cutPct') and kind != 'tonight':
            self.art.top_left.append((f"-{price['cutPct']}%", 'accent'))
        if self.repack:
            exact = self.repack.get('confidence') == 'exact'
            self.art.top_right.append(("FitGirl" if exact else "FitGirl?",
                                       'success' if exact else 'warning'))
            hv = hypervisor.badge(self.repack.get('hv'))
            if hv:
                # Red only where it can't run at all: Linux, with no non-HV build.
                self.art.top_right.append(
                    (hv, 'danger' if has('proton') and self.repack['hv'] == 'hv' else 'warning'))
        if owned and kind != 'tonight':
            self.art.top_right.append(("Owned", 'glass'))
        if kind == 'upcoming':
            when = item.get('countdown') or gamedb.release_label(item)
            if when and when != item.get('releaseDateText'):
                self.art.bottom_left.append((when, 'glass'))
        self.art.set_url(item.get('headerImage'))
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)

        body = QVBoxLayout()
        body.setContentsMargins(SP['3'], SP['0_5'], SP['3'], 0)
        body.setSpacing(SP['1'])
        self.title = QLabel()
        self.title.setObjectName("NewsTitle")
        tf = QFont(FONTS.ui, -1, QFont.Weight.Medium)
        tf.setPixelSize(FS['sm'])
        self._fm = QFontMetrics(tf)
        self.title.setFixedHeight(self._fm.lineSpacing() * 2 + 2)
        self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._set_title()
        body.addWidget(self.title)

        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        if kind == 'tonight':
            act = label(self.primary_label, None)
            act.setStyleSheet(f"color: {P['accent']}; font-size: {FS['sm']}px; font-weight: 600;")
            row.addWidget(act)
        elif kind == 'upcoming':
            # The countdown is on the art; here, the date itself.
            when = label(item.get('releaseDateText') or gamedb.release_label(item) or "TBA",
                         None)
            when.setStyleSheet(f"color: {P['accent']}; font-size: {FS['base']}px;"
                               f" font-weight: 600;")
            row.addWidget(when)
        elif price:
            cur = label(price.get('currentStr') or '', None)
            cur.setStyleSheet(f"color: {P['text']}; font-size: {FS['base']}px;"
                              f" font-weight: 600;")
            row.addWidget(cur)
            was = deal_was(price)
            if was:
                old = label(f"<s>{html.escape(was)}</s>", "Muted")
                old.setTextFormat(Qt.TextFormat.RichText)
                row.addWidget(old, 0, Qt.AlignmentFlag.AlignBottom)
        else:
            row.addWidget(label("No price yet" if not item.get('comingSoon') else
                                gamedb.release_label(item), "Muted"))
        row.addStretch()
        self.grab = None
        if self.repack:
            self.grab = QPushButton("Grab")
            self.grab.setObjectName("Chip")
            self.grab.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # the card's menu has it too
            set_glyph_icon(self.grab, G['download'], 14, 'accent')
            tip = f"Download {self.repack.get('title')} with your torrent client"
            if self.repack.get('hv'):
                tip += "\n" + hypervisor.note(self.repack['hv'])
            self.grab.setToolTip(tip)
            self.grab.clicked.connect(self._grab)
            row.addWidget(self.grab)
        body.addLayout(row)

        verdict = QHBoxLayout()
        verdict.setSpacing(SP['1_5'])
        notes = []
        if kind == 'tonight':
            reasons = item.get('reasons') or []
            if reasons:
                why = label(" · ".join(reasons), "Muted", wrap=True)
                why.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                verdict.addWidget(why, 1)
        elif kind == 'upcoming':
            if item.get('owned'):
                verdict.addWidget(_pill("Owned", 'accent'))
            elif price:
                text, preorder = gamedb.price_line(item)
                if text:
                    notes.append(text)
                if preorder:
                    verdict.addWidget(_pill("Pre-order", 'warning'))
        elif price:
            if price.get('preorder'):
                verdict.addWidget(_pill("Pre-order", 'warning'))
            elif price.get('atHistoricalLow'):
                verdict.addWidget(_pill("All-time low", 'success'))
            elif price.get('onSale'):
                verdict.addWidget(_pill("Near all-time low", 'success'))
            if price.get('historicalLowStr') and not (price.get('atHistoricalLow') or
                                                      price.get('preorder')):
                notes.append(f"low {price['historicalLowStr']}")
            if best.get('priceStr') and best.get('price') is not None and \
                    price.get('current') and best['price'] < price['current'] - 0.005:
                notes.append(f"{best['priceStr']} at "
                             f"{best.get('shop') or ('a keyshop' if best.get('category') == 'keyshop' else 'a shop')}")
        if item.get('inBundle'):
            verdict.addWidget(_pill("In a bundle", 'warning'))
        if notes:
            n = ElideLabel(" · ".join(notes), "Muted")
            n.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            verdict.addWidget(n, 1)
        else:
            verdict.addStretch()
        body.addLayout(verdict)

        bits = deal_meta(item)
        if kind == 'library':
            c = item.get('completion') or {}
            if c.get('state') == 'in-progress':
                bits.append((f"{int(c.get('fraction', 0) * 100)}% through", None))
        self.meta = label(meta_html(bits), "Muted")
        self.meta.setTextFormat(Qt.TextFormat.RichText)
        self.meta.setVisible(bool(bits))
        body.addWidget(self.meta)
        self.dl = label("", None)
        self.dl.setStyleSheet(f"color: {P['accent']}; font-size: {FS['xs']}px; font-weight: 600;")
        self.dl.setVisible(False)
        body.addWidget(self.dl)
        lay.addLayout(body)
        lay.addStretch()
        for w in self.findChildren(QLabel):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        tip = self.primary or ''
        self.setToolTip(f"{self.name}\n{self.primary_label}: {tip}" if tip else self.name)

    def _set_title(self):
        self.title.setText(elide_lines(self.name, self._fm, self.W - 2 * SP['3'] - 8, 2))

    def set_width(self, w: int):
        if w == self.W:
            return
        self.W = w
        self.setFixedWidth(w)
        self.art.resize_to(w - 2, self.art_h(w))
        self._set_title()

    def _grab(self):
        self.repack_action.emit('grab', self.item, self.repack)

    def repack_items(self) -> list[tuple]:
        """The FitGirl entries for this card's menu: (text, glyph, fn)."""
        if not self.repack:
            return []
        emit = self.repack_action.emit
        return [("Download repack", G['download'], self._grab),
                ("Open the repack page", G['external'],
                 lambda: emit('page', self.item, self.repack)),
                ("Not this repack", G['close'],
                 lambda: emit('reject', self.item, self.repack))]

    def set_download(self, row: dict | None):
        """The repack's torrent, while one is going: "Downloading 42%"."""
        text = ''
        if row and not row.get('gone') and not row.get('installed'):
            if row.get('done'):
                text = "Downloaded — installing" if row.get('auto_install') else "Downloaded"
            else:
                text = (f"{'Paused' if row.get('paused') else 'Downloading'} "
                        f"{row.get('percent', 0):.0f}%")
        self.dl.setText(text)
        self.dl.setVisible(bool(text))
        if self.grab is not None:
            self.grab.setEnabled(not text)

    def activate(self):
        if self.primary:
            self.activated.emit(self.primary)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        # On release, inside: the primary action can launch a game through
        # Steam, so a press that turns into a drag (or slides off) does nothing.
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                ev.position().toPoint()):
            self.activate()
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        self._menu(ev.globalPos())

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate()
        elif ev.key() == Qt.Key.Key_Menu:
            self._menu(self.mapToGlobal(QPoint(self.width() // 3, self.height() // 2)))
        else:
            super().keyPressEvent(ev)

    def _menu(self, pos):
        web = next((u for _t, u in self.links if u.startswith(('https://', 'http://'))), None)
        sheet = getattr(self.window(), 'sheet', None)
        if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
            # A controller gets the sheet, never a desktop menu.
            items = self.repack_items() + [
                (text, G['external'], lambda u=url: self.activated.emit(u))
                for text, url in self.links]
            if web:
                items.append(("Copy link", G['copy'],
                              lambda: QApplication.clipboard().setText(web)))
            if items:
                sheet.choose(self.name, items)
            return
        m = QMenu(self)
        repack = self.repack_items()
        for text, _glyph, fn in repack:
            m.addAction(text, fn)
        if repack:
            m.addSeparator()
        for text, url in self.links:
            m.addAction(text, lambda u=url: self.activated.emit(u))
        if web:
            m.addSeparator()
            m.addAction("Copy link", lambda: QApplication.clipboard().setText(web))
        if not m.isEmpty():
            m.exec(pos)
        m.deleteLater()


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
        else:
            super().keyPressEvent(ev)


class DealsPage(QWidget):
    """Game-db in Nexus, as a storefront: the best deal in a spotlight,
    the wishlist's numbers, then a grid of store tiles — deals (sortable,
    searchable, FitGirl matches marked), upcoming releases by month, and
    tonight's backlog picks. Everything comes from one cached
    `nexus-cli --gamedb --json` call; nothing is fetched until the page
    (or the Library's background warm-up) asks."""
    toast = pyqtSignal(str, str)
    counts = pyqtSignal(int)
    open_settings = pyqtSignal()
    repack = pyqtSignal(str, dict, dict)      # a FitGirl action on a wishlist game
    MAX_CARDS = 200                           # building more stalls every keystroke
    SECTIONS = (('deals', "Deals"), ('upcoming', "Upcoming"), ('backlog', "Backlog"))
    DEAL_FILTERS = (('sale', "On sale"), ('atl', "All-time low"), ('fitgirl', "On FitGirl"),
                    ('all', "All wishlist"))
    SORTS = (('best', "Best deal"), ('discount', "Biggest discount"), ('price', "Lowest price"),
             ('reviews', "Best reviewed"), ('added', "Recently added"), ('name', "Name"))
    BUDGETS = (('lt1h', "Under an hour"), ('1to3', "1–3 hours"), ('3plus', "3+ hours"),
               ('long', "Long session"))
    SPOTLIGHT = 5                     # deals the spotlight steps through
    MIN_TILE, GAP = 250, SP['3']

    loaded = pyqtSignal()

    def __init__(self, prefs=None):
        super().__init__()
        self.prefs = prefs
        self.res: dict = {}
        self._downloads: dict = {}
        st = getattr(prefs, 'deals_state', None) or {}
        self._section = st.get('section') if st.get('section') in dict(self.SECTIONS) else 'deals'
        self._filter = st.get('filter') if st.get('filter') in dict(self.DEAL_FILTERS) else 'sale'
        self._sort = st.get('sort') if st.get('sort') in dict(self.SORTS) else 'best'
        self._budget = st.get('budget') if st.get('budget') in dict(self.BUDGETS) else '1to3'
        self._hide_owned = st.get('hide_owned') is True
        self._query = ''
        self._loaded = self._busy = False
        self._cards: list[DealCard] = []
        self._grids: list[QGridLayout] = []
        self._cols = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        outer.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        head.addWidget(label("Deals", "PageTitle"))
        head.addSpacing(SP['3'])
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your wishlist")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(320)
        self.search.textChanged.connect(self._searched)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._fill)
        self._art_settle = QTimer(self)
        self._art_settle.setSingleShot(True)
        self._art_settle.setInterval(150)
        self._art_settle.timeout.connect(self._settle_art)
        head.addWidget(self.search, 1)
        head.addStretch()
        self.status = label("", "Muted")
        head.addWidget(self.status)
        self.btn_refresh = icon_button(G['refresh'], "Refresh from Game-db (bypasses the "
                                                     "45-minute cache)", 32)
        self.btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        head.addWidget(self.btn_refresh)
        self.btn_site = QPushButton("Open Game-db")
        self.btn_site.clicked.connect(self._open_site)
        head.addWidget(self.btn_site)
        outer.addLayout(head)
        self.banner = label("", "Banner", wrap=True)
        self.banner.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        self.banner.setVisible(False)
        outer.addWidget(self.banner)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        body = QVBoxLayout(host)
        body.setContentsMargins(0, 0, SP['2'], SP['4'])
        body.setSpacing(SP['3'])
        self.spot = DealSpotlight()
        self.spot.activated.connect(self._open)
        self.spot.repack_action.connect(self.repack)
        body.addWidget(self.spot)
        self.spot.setVisible(False)
        self.stats_host = QWidget()
        self.stats = QHBoxLayout(self.stats_host)
        self.stats.setContentsMargins(0, 0, 0, 0)
        self.stats.setSpacing(SP['2'])
        body.addWidget(self.stats_host)
        self.stats_host.setVisible(False)
        self.bar_host = QWidget()
        bar = QHBoxLayout(self.bar_host)
        bar.setContentsMargins(0, SP['1'], 0, 0)
        bar.setSpacing(SP['2'])
        self.seg = SegmentedControl('underline', 34)
        for key, text in self.SECTIONS:
            self.seg.add(key, text)
        self.seg.set_current(self._section, animate=False)
        self.seg.changed.connect(lambda k: self._pick('_section', k))
        bar.addWidget(self.seg)
        bar.addStretch()
        self.owned_chip = QPushButton("Hide owned")
        self.owned_chip.setObjectName("Chip")
        self.owned_chip.setCheckable(True)
        self.owned_chip.setChecked(self._hide_owned)
        self.owned_chip.setToolTip("Hide wishlist games you already have: in your Steam "
                                   "library, installed, or installed from a repack")
        self.owned_chip.toggled.connect(self._owned_toggled)
        bar.addWidget(self.owned_chip)
        self.sort = QComboBox()
        for key, text in self.SORTS:
            self.sort.addItem(f"Sort: {text}", key)
        self.sort.setCurrentIndex(max(0, self.sort.findData(self._sort)))
        self.sort.activated.connect(lambda _i: self._pick('_sort', self.sort.currentData()))
        bar.addWidget(self.sort)
        body.addWidget(self.bar_host)
        self.sub_host = QWidget()
        self.sub = QHBoxLayout(self.sub_host)
        self.sub.setContentsMargins(0, 0, 0, 0)
        self.sub.setSpacing(SP['1_5'])
        body.addWidget(self.sub_host)
        self.summary = label("", "Muted", wrap=True)
        body.addWidget(self.summary)
        self.list = QVBoxLayout()
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(SP['3'])
        body.addLayout(self.list)
        body.addStretch()
        self.scroll.setWidget(host)
        # Columns follow the viewport, not the page: a page filled while hidden
        # gets its real viewport width only when first shown, with no resize of
        # its own.
        self.scroll.viewport().installEventFilter(self)
        outer.addWidget(self.scroll, 1)
        self._fill_tabs()

    # ---- data ----
    def refresh(self, force=False):
        if self._busy or (self._loaded and not force):
            return
        self._busy = True
        self.btn_refresh.setEnabled(False)
        if not self._loaded:
            self.status.setText("Loading…")

        def done(data, err):
            self._busy = False
            try:
                self.btn_refresh.setEnabled(True)
            except RuntimeError:
                return
            failed = {'kind': 'net', 'message': err}
            if data is not None:
                self.res = data
            elif self._data():                 # keep what's on screen, under the banner
                self.res = {**self.res, 'error': failed}
            else:
                self.res = {'configured': True, 'error': failed}
            self._loaded = data is not None or bool(self._data())
            self._render()
            self.loaded.emit()
        cli_json(['--gamedb', '--json'] + (['--refresh'] if force else []), done, 90000)

    @property
    def error(self) -> str:
        """Why there's nothing to show ('' while loading or when there is)."""
        if self._busy or self._data():
            return ''
        e = self.res.get('error')
        return (e.get('message') or e.get('kind') or 'error') if isinstance(e, dict) else ''

    def retry(self):
        self.res = {}
        self.refresh(force=True)

    def _data(self) -> dict:
        return self.res.get('summary') or {}

    def _base(self):
        return self.res.get('url')

    def _owned(self) -> set:
        return {int(a) for a in (self.res.get('owned_appids') or [])
                + (self.res.get('installed_appids') or []) if str(a).isdigit()}

    def _open_site(self):
        base = self._base() or gamedb.settings()['url']
        if not base:
            self.toast.emit("Add your Game-db address in Settings → Game-db first.", 'info')
            return
        self._open(base + ('/' if self.res.get('summary') else '/account'))

    def _open(self, url):
        if url.startswith('steam://'):
            CliQuery(['--open-url', url], lambda out, err: out is None and self.toast.emit(
                f"Couldn't open Steam: {err}", 'error'))
        elif gamedb.safe_url(url, self._base()):
            QDesktopServices.openUrl(QUrl(url))      # browser; nothing rendered here

    def _save_state(self):
        if self.prefs is None:
            return
        self.prefs.deals_state = {'section': self._section, 'filter': self._filter,
                                  'sort': self._sort, 'budget': self._budget,
                                  'hide_owned': self._hide_owned}
        self.prefs.save()

    # ---- rendering ----
    def _render(self):
        r = self.res
        err = r.get('error') or {}
        configured = r.get('configured', False)
        if not configured:
            self.status.setText("")
            self.banner.setVisible(False)
        elif err:
            msg = err.get('message', 'unknown error')
            ago = fmt_ago(r.get('fetched_at'))
            when = ((f" — showing data from {ago}" if ago else " — showing the last data")
                    if r.get('summary') else '')
            self.banner.setText(html.escape(msg) + when)
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)
        if r.get('summary'):
            d = self._data()
            self.status.setText(f"synced {fmt_ago(r.get('fetched_at'))} · region "
                                f"{d.get('region', '?')}")
            self.counts.emit(int((d.get('radar') or {}).get('onSaleCount') or 0))
        elif configured:
            self.status.setText("")
        self._fill_tabs()
        self._fill()

    def _chip(self, lay, text, checked, fn):
        c = QPushButton(text)
        c.setObjectName("Chip")
        c.setCheckable(True)
        c.setChecked(checked)
        c.clicked.connect(fn)
        lay.addWidget(c)
        return c

    def _filtered(self, key: str) -> list[dict]:
        """The wishlist under one deal filter (before search / hide owned)."""
        items = self._data().get('wishlist') or []
        if key == 'sale':
            return [g for g in items if (g.get('price') or {}).get('onSale')]
        if key == 'atl':
            return [g for g in items if (g.get('price') or {}).get('atHistoricalLow')]
        if key == 'fitgirl':
            return [g for g in items if str(g.get('appid')) in self.repacks()]
        return list(items)

    def _fill_tabs(self):
        d = self._data()
        has = bool(self.res.get('summary'))
        n = {'deals': (d.get('radar') or {}).get('onSaleCount'),
             'upcoming': len(gamedb.upcoming(d)) or None}
        for key, _text in self.SECTIONS:
            self.seg.set_badge(key, str(n[key]) if n.get(key) else '')
        self.seg.set_current(self._section, animate=False)
        clear_layout(self.sub)
        if self._section == 'deals':
            for key, text in self.DEAL_FILTERS:
                count = len(self._filtered(key)) if has else 0
                self._chip(self.sub, f"{text}  {count}" if count else text,
                           key == self._filter,
                           lambda _=False, k=key: self._pick('_filter', k))
        elif self._section == 'backlog':
            for key, text in self.BUDGETS:
                self._chip(self.sub, text, key == self._budget,
                           lambda _=False, k=key: self._pick('_budget', k))
        self.sub.addStretch()
        self.sub_host.setVisible(has and self._section in ('deals', 'backlog'))
        self.bar_host.setVisible(has)
        self.sort.setVisible(self._section == 'deals')
        self.owned_chip.setVisible(self._section in ('deals', 'upcoming'))
        self.search.setVisible(has)

    def _fill_stats(self):
        clear_layout(self.stats)
        d = self._data()
        tiles = []
        if self._section == 'deals':
            radar = d.get('radar') or {}
            cur = d.get('currency') or ''
            wl = d.get('wishlist') or []
            tiles = [(G['deals'], "on sale", str(radar.get('onSaleCount', 0)), 'accent'),
                     (G['trophy'], "at their all-time low",
                      str(len(self._filtered('atl'))), 'success'),
                     (G['star'], "you'd save on all of them",
                      f"{radar['totalSavings']:.2f} {cur}" if radar.get('totalSavings')
                      else None, 'warning'),
                     (G['download'], "on FitGirl", str(len(self.repacks())) if self.repacks()
                      else None, 'success'),
                     (G['library'], "games on your wishlist", str(len(wl)), 'subtext')]
        elif self._section == 'backlog':
            b = d.get('backlog') or {}
            tiles = [(G['trophy'], "beaten", b.get('beaten'), 'success'),
                     (G['play'], "in progress", b.get('inProgress'), 'accent'),
                     (G['clock'], "not started", b.get('notStarted'), 'warning'),
                     (G['timer'], "hours left", b.get('backlogHours'), 'subtext')]
        for glyph, cap, val, role in tiles:
            t = StatTile(glyph, cap, role)
            t.set(str(val) if val is not None else None)
            self.stats.addWidget(t, 1)
        self.stats_host.setVisible(bool(tiles))

    def section(self) -> str:
        return self._section

    def show_section(self, key: str):
        """Switch section without moving focus (for go('upcoming'))."""
        if key in dict(self.SECTIONS) and key != self._section:
            self._section = key
            self._fill_tabs()
            self._fill()

    def _pick(self, attr, key):
        if getattr(self, attr) == key and attr != '_section':
            return
        setattr(self, attr, key)
        self._save_state()
        self._fill_tabs()
        self._fill()
        if attr in ('_filter', '_budget'):
            for i in range(self.sub.count()):
                w = self.sub.itemAt(i).widget()
                if isinstance(w, QPushButton) and w.isChecked():
                    w.setFocus()

    def _owned_toggled(self, on):
        self._hide_owned = on
        self._save_state()
        self._fill()

    def _searched(self, text):
        self._query = text.strip().casefold()
        self._search_timer.start()

    def _empty_state(self):
        box = QFrame()
        box.setObjectName("Card")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(SP['5'], SP['5'], SP['5'], SP['5'])
        lay.setSpacing(SP['2'])
        t = label("Connect Game-db", None)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['sm']}px; font-weight: 500;")
        lay.addWidget(t)
        url = self._base() or gamedb.settings()['url']
        lay.addWidget(label(
            "See deals on your Steam wishlist, upcoming releases and what to play tonight. "
            + (f"Create a desktop app token on {url}/account, then paste it into "
               "Settings → Game-db." if url else
               "Add your Game-db server's address and a desktop app token in "
               "Settings → Game-db."), "Muted", wrap=True))
        row = QHBoxLayout()
        site = QPushButton("Create a token…")
        site.clicked.connect(lambda: self._open(url + '/account'))
        site.setVisible(bool(url))
        go = QPushButton("Open Settings")
        go.setObjectName("Primary")
        go.clicked.connect(self.open_settings.emit)
        row.addWidget(go)
        row.addWidget(site)
        row.addStretch()
        lay.addLayout(row)
        return box

    def _groups(self, d: dict) -> tuple[list[tuple[str, list]], str, str]:
        """([(heading or '', items)], tile kind, empty-state text) for the
        current section, filter, sort, search and owned setting."""
        owned = self._owned()
        q = self._query

        def keep(g):
            if q and q not in (g.get('title') or '').casefold():
                return False
            return not (self._hide_owned and (g.get('appid') in owned or g.get('owned')))
        if self._section == 'deals':
            items = [g for g in self._filtered(self._filter) if keep(g)]
            items.sort(key=deal_sort_key(self._sort))
            empty = ("Your wishlist is empty." if self._filter == 'all' else
                     "No wishlist game has a FitGirl repack yet." if self._filter == 'fitgirl'
                     else "Nothing on sale right now.")
            return [('', items)], 'deal', ("Nothing matches your search." if q else empty)
        if self._section == 'upcoming':
            groups: dict[str, list] = {}
            for g in gamedb.upcoming(d):
                if keep(g):
                    groups.setdefault(self._month(g), []).append(g)
            return (list(groups.items()), 'upcoming',
                    "Nothing matches your search." if q else
                    "No upcoming releases on your wishlist.")
        items = [g for g in (d.get('tonight') or {}).get(self._budget) or [] if keep(g)]
        return [('', items)], 'tonight', "No picks for this time budget."

    @staticmethod
    def _month(g: dict) -> str:
        """"October 2026" from the release date; "Later" without one."""
        try:
            when = datetime.fromisoformat(str(g.get('releaseDate') or '').replace('Z', '+00:00'))
        except ValueError:
            return "Later"
        now = datetime.now()
        if (when.year, when.month) == (now.year, now.month):
            return "This month"
        return when.strftime("%B %Y")

    def _pooled(self) -> dict:
        """Cards built for the current data, by _card_key, kept across fills
        along with their group grids (self._hosts, by heading): a search
        keystroke or filter change re-places them instead of rebuilding up to
        MAX_CARDS tiles. Cards stay in their grid's host, hidden when unused —
        reparenting a styled widget costs nearly as much as building it."""
        if getattr(self, '_pool_res', None) is not self.res:
            for card in getattr(self, '_pool', {}).values():
                card.deleteLater()
            for host, _grid in getattr(self, '_hosts', {}).values():
                host.deleteLater()
            self._pool, self._hosts, self._pool_res = {}, {}, self.res
        return self._pool

    def _host(self, heading) -> QGridLayout:
        if heading not in self._hosts:
            host = QWidget()
            grid = QGridLayout(host)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(self.GAP)
            grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self._hosts[heading] = (host, grid)
        host, grid = self._hosts[heading]
        self.list.addWidget(host)
        host.show()
        grid._cards = []
        return grid

    def _clear_list(self):
        """Empty self.list, keeping the pooled grid hosts (and their cards)."""
        keep = {id(host) for host, _g in self._hosts.values()}
        for host, grid in self._hosts.values():
            for card in getattr(grid, '_cards', []):
                grid.removeWidget(card)
                card.hide()
        while self.list.count():
            item = self.list.takeAt(0)
            w = item.widget()
            if w is not None and id(w) in keep:
                w.hide()
            elif w is not None:
                w.hide()
                w.deleteLater()
            elif item.layout() is not None:
                clear_layout(item.layout())

    @staticmethod
    def _card_key(g, kind, base, installed, repack, owned) -> tuple:
        # id(g) is stable while self.res (which holds g) is: the pool is
        # dropped whenever self.res is replaced.
        return (id(g), kind, base, g.get('appid') in installed, repr(repack), owned)

    def _fill(self):
        pool = self._pooled()
        self._clear_list()
        self._cards, self._grids = [], []
        self._cols = 0
        self.summary.setText("")
        r = self.res
        d = self._data()
        self._fill_stats() if d else self.stats_host.setVisible(False)
        self._fill_spotlight(d)
        if r and not r.get('configured'):
            self.list.addWidget(self._empty_state())
            return
        if not d:
            if r:
                self.list.addWidget(EmptyState(G['deals'], "Nothing to show yet"))
            return
        installed = set(r.get('installed_appids') or [])
        owned = self._owned()
        base = self._base()
        repacks = self.repacks()
        groups, kind, empty = self._groups(d)
        total = sum(len(items) for _h, items in groups)
        if self._section == 'deals':
            cur = d.get('currency') or ''
            radar = d.get('radar') or {}
            bits = [f"{total} game{'s' if total != 1 else ''}"]
            if self._filter == 'sale' and radar.get('totalNow'):
                bits.append(f"{radar['totalNow']:.2f} {cur} for all of them")
            self.summary.setText(" · ".join(bits))
        elif self._section == 'upcoming':
            self.summary.setText(f"{total} unreleased games, soonest first")
        else:
            self.summary.setText("Tonight's picks from your owned games — the same three "
                                 "all day, chosen by Game-db")
        shown = 0
        for heading, items in groups:
            if shown >= self.MAX_CARDS:
                break
            if heading:
                h = label(heading, "SectionTitle")
                h.setStyleSheet(f"color: {P['text']}; font-size: {FS['base']}px;"
                                f" font-weight: 600;")
                self.list.addWidget(h)
            grid = self._host(heading)
            for g in items[:self.MAX_CARDS - shown]:
                repack, own = repacks.get(str(g.get('appid'))), g.get('appid') in owned
                key = self._card_key(g, kind, base, installed, repack, own)
                card = pool.get(key)
                if card is None:
                    card = pool[key] = DealCard(g, kind, base, installed, repack, own,
                                                self._tile_w())
                    card.activated.connect(self._open)
                    card.repack_action.connect(self.repack)
                card.set_download(self._downloads.get(str(g.get('appid'))))
                self._cards.append(card)
                grid._cards.append(card)
            shown += len(grid._cards)
            self._grids.append(grid)
        self._place()
        if total > shown:
            self.list.addWidget(label(f"Showing the first {shown} of {total}. Search or "
                                      f"filter to find the rest.", "Muted", wrap=True))
        if not total:
            self.list.addWidget(EmptyState(G['deals'], empty))

    def _fill_spotlight(self, d: dict):
        on = (self._section == 'deals' and bool(d) and not self._query)
        top = []
        if on:
            sale = [g for g in d.get('wishlist') or [] if (g.get('price') or {}).get('onSale')]
            if self._hide_owned:
                owned = self._owned()
                sale = [g for g in sale if g.get('appid') not in owned]
            top = sorted(sale, key=deal_sort_key('best'))[:self.SPOTLIGHT]
        self.spot.setVisible(bool(top))
        if top:
            self.spot.set_items(top, self.repacks(), self._base())

    # Columns fill the row: as many MIN_TILE-wide tiles as fit, then
    # stretched to share the width evenly.
    def _avail(self) -> int:
        vp = self.scroll.viewport().width() if hasattr(self, 'scroll') else self.width()
        return max(self.MIN_TILE, vp - SP['2'])

    def _columns(self) -> int:
        return max(1, (self._avail() + self.GAP) // (self.MIN_TILE + self.GAP))

    def _tile_w(self) -> int:
        cols = self._columns()
        return (self._avail() - self.GAP * (cols - 1)) // cols

    def _place(self):
        cols, w = self._columns(), self._tile_w()
        if any(card.W != w for card in self._cards):
            for card in self._cards:
                card.set_width(w)
            self._art_settle.start()               # re-decode art once resizing stops
        if cols == self._cols:
            return
        self._cols = cols
        for grid in self._grids:
            for card in grid._cards:
                grid.removeWidget(card)
            for i, card in enumerate(grid._cards):
                grid.addWidget(card, i // cols, i % cols)
                card.show()                        # a pooled card was hidden

    def _settle_art(self):
        for card in self._cards:
            try:
                card.art.refresh_size()
            except RuntimeError:
                pass

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and getattr(self, '_cards', None):
            try:
                self._place()
            except RuntimeError:
                pass
        return super().eventFilter(obj, ev)

    def repacks(self) -> dict:
        """{appid (str): FitGirl match} for the wishlist."""
        return self.res.get('repacks') or {}

    def forget_repack(self, appid: str):
        """A match was wrong: drop it here too (the CLI remembers)."""
        if self.repacks().pop(str(appid), None) is not None:
            self._fill_tabs()
            self._fill()

    def set_downloads(self, rows: list[dict]):
        """The torrents Nexus grabbed for wishlist games, onto their cards."""
        by_appid = {str(r['appid']): r for r in rows if r.get('appid') is not None}
        if by_appid == self._downloads:
            return
        self._downloads = by_appid
        for card in getattr(self, '_cards', []):
            try:
                card.set_download(by_appid.get(str(card.item.get('appid'))))
            except RuntimeError:
                pass


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .actions import ActionSheet  # noqa: E402
from .art import ART  # noqa: E402
from .details import ElideLabel, StatTile  # noqa: E402
from .gameinfo import TIER_COLORS  # noqa: E402
from .input import input_mode  # noqa: E402
from .jobs import cli_json, CliQuery  # noqa: E402
from .newspage import elide_lines  # noqa: E402
from .widgets import (clear_layout, EmptyState, icon_button, label, SegmentedControl,
    set_glyph_icon)  # noqa: E402
