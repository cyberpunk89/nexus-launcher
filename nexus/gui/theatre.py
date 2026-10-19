"""Theatre mode: full screen, controller first, for the TV."""

import html
import random
import re
from datetime import datetime

from PyQt6.QtCore import (pyqtSignal, QEvent, QObject, QPoint, QRectF, Qt, QTimer, QUrl,
    QVariantAnimation)
from PyQt6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPixmap,
    QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from nexus import gamedb

from .common import alive, COLLECTION_NAMES, MAX_COLLECTION_NAME
from .fmt import fmt_hours, fmt_relative, fmt_size
from .theme import build_stylesheet, FONTS, FS, G, P, pill_qss, SP


THEATRE_SCALE = 1.4


class TheatreCard(QFrame):
    """One glass card on Theatre's game page (Stats, Compatibility, Game-db,
    News). Focusable with the D-pad; A opens its longer version (`more`) in
    the page's reader."""
    open = pyqtSignal(object)

    def __init__(self, key: str, title: str, glyph: str):
        super().__init__()
        self.key, self.title, self.more = key, title, ''
        self.setObjectName("TheatreCard")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(330)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['4'])
        lay.setSpacing(SP['1_5'])
        head = QHBoxLayout()
        head.setSpacing(SP['1_5'])
        head.addWidget(icon_label(glyph, 16, P['accent']))
        head.addWidget(label(title.upper(), "TheatreCardTitle"))
        head.addStretch()
        lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(SP['1'])
        lay.addLayout(self.body)
        lay.addStretch()

    def set_lines(self, lines: list, more: str = '', bar: float | None = None):
        """lines: (text, colour or None, big?) — the first is usually big."""
        clear_layout(self.body)
        for text, color, big in lines:
            lab = label(text, "TheatreCardBig" if big else "TheatreCardLine", wrap=True)
            if color:
                lab.setStyleSheet(f"color: {color};")
            self.body.addWidget(lab)
        if bar is not None:
            pb = QProgressBar()
            pb.setObjectName("CardProgress")
            pb.setTextVisible(False)
            pb.setFixedHeight(6)
            pb.setRange(0, 1000)
            pb.setValue(int(max(0.0, min(1.0, bar)) * 1000))
            self.body.addWidget(pb)
        self.more = more
        self.setVisible(bool(lines))

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.open.emit(self)
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.open.emit(self)
        super().mouseReleaseEvent(ev)


def _news_html(items: list[dict]) -> str:
    out = []
    for i in items:
        when = fmt_relative(i.get('date')) or ''
        out.append(f"<h3>{html.escape(i.get('title') or '')}</h3>"
                   f"<p style='color:{P['subtext']}'>{html.escape(when)}</p>"
                   f"<p>{html.escape(i.get('summary') or '')}</p>")
    return "".join(out)


class TheatreDetails(QWidget):
    """A game, full screen: its art behind, logo, a few facts, a big Play
    button and the handful of things you'd do from the couch."""
    play = pyqtSignal(dict)
    action = pyqtSignal(str, dict)
    closed = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.game: dict = {}
        self._pm = None
        lay = QVBoxLayout(self)
        m = int(SP['8'] * 2.2)
        lay.setContentsMargins(m, m, m, m)
        lay.setSpacing(SP['3'])
        lay.addStretch()
        self.kicker = label("", "HeroKicker")
        lay.addWidget(self.kicker)
        self.logo = QLabel()
        lay.addWidget(self.logo)
        self.title = label("", "HeroTitle", wrap=True)
        self.title.setStyleSheet("font-size: 56px;")
        lay.addWidget(self.title)
        self.facts = QHBoxLayout()
        self.facts.setSpacing(SP['2'])
        lay.addLayout(self.facts)
        lay.addSpacing(SP['4'])
        row = QHBoxLayout()
        row.setSpacing(SP['3'])
        self.btn_play = QPushButton("Play")
        self.btn_play.setObjectName("PlayBig")
        set_glyph_icon(self.btn_play, G['play'], 28, 'on_accent', hover_role=None)
        self.btn_play.setMinimumSize(260, 64)
        self.btn_play.clicked.connect(lambda: self.play.emit(self.game))
        row.addWidget(self.btn_play)
        self.btn_fav = QPushButton()
        self.btn_fav.setObjectName("Glass")
        self.btn_fav.setMinimumHeight(64)
        self.btn_fav.clicked.connect(lambda: self.action.emit('favorite', self.game))
        row.addWidget(self.btn_fav)
        self.btn_opts = QPushButton("Options")
        self.btn_opts.setObjectName("Glass")
        set_glyph_icon(self.btn_opts, G['more'], 24, 'on_art', hover_role=None)
        self.btn_opts.setMinimumHeight(64)
        self.btn_opts.clicked.connect(lambda: self.action.emit('options', self.game))
        row.addWidget(self.btn_opts)
        back = QPushButton("Back")
        back.setObjectName("Glass")
        back.setMinimumHeight(64)
        back.clicked.connect(self.closed)
        row.addWidget(back)
        row.addStretch()
        lay.addLayout(row)
        lay.addSpacing(SP['6'])
        # Below the actions: glanceable cards, or one of them opened up.
        self.lower = QStackedWidget()
        self.lower.setFixedHeight(260)
        cards_host = QWidget()
        col = QVBoxLayout(cards_host)               # cards share the tallest one's height
        col.setContentsMargins(0, 0, 0, 0)
        crow = QHBoxLayout()
        crow.setSpacing(SP['4'])
        col.addLayout(crow)
        col.addStretch()
        self.cards = {}
        for key, title, glyph in (('stats', "Stats", G['clock']),
                                  ('compat', "Compatibility", G['proton']),
                                  ('achievements', "Achievements", G['medal']),
                                  ('gamedb', "Game-db", G['trophy']),
                                  ('news', "News", G['news'])):
            c = TheatreCard(key, title, glyph)
            c.open.connect(self._open_card)
            c.setVisible(False)
            crow.addWidget(c)
            self.cards[key] = c
        crow.addStretch()
        self.lower.addWidget(cards_host)
        self.reader = QTextBrowser()
        self.reader.setObjectName("TheatreReader")
        self.reader.setOpenLinks(False)          # no browser hand-off on a TV
        self.reader.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lower.addWidget(self.reader)
        lay.addWidget(self.lower)
        self.tips: dict | None = None            # --game-tips for this game
        self._ach_html: dict[tuple, str] = {}    # (slug, unlocked) -> the list, as HTML
        self.launch: dict | None = None          # --show-launch (repacks)
        self.news: list[dict] = []
        self.setVisible(False)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu:                 # X: the options sheet
            self.action.emit('options', self.game)
        else:
            super().keyPressEvent(ev)

    def open(self, g: dict, refocus: bool = True):
        same = g.get('slug') == self.game.get('slug') and self.isVisible()
        self.game = g
        playing = bool(g.get('playing'))
        self.kicker.setText(("Now playing" if playing else SOURCE_LABEL.get(
            g.get('source', 'nexus'), '')).upper())
        logo, url = art_source(g, LOGO_KINDS)
        self._set_logo(logo)
        if not logo and url:
            slug = g.get('slug')
            ART.fetch(url, lambda p, slug=slug: self.game.get('slug') == slug
                      and self._set_logo(p))
        clear_layout(self.facts)
        tier = g.get('protondb')
        for text, color in ((fmt_relative(g.get('last_played')) and
                             f"Played {fmt_relative(g.get('last_played'))}", None),
                            (fmt_hours(g.get('playtime_minutes')), None),
                            (tier.capitalize() if tier in TIER_COLORS else None,
                             TIER_COLORS.get(tier)),
                            (fmt_size(g.get('disk_bytes')), None),
                            ("Update ready" if g.get('update_pending') else None, P['warning'])):
            if text:
                lab = label(text, None)
                lab.setStyleSheet(pill_qss(P['glass'], color or P['on_art'], 34, 14)
                                  + " font-size: 17px;")
                lab.setFixedHeight(34)
                self.facts.addWidget(lab)
        self.facts.addStretch()
        self.btn_play.setText(play_label(g))
        self.btn_play.setEnabled(play_enabled(g))
        self.btn_fav.setText("★  Favourite" if g.get('favorite') else "☆  Add to favourites")
        if not same:
            self.tips, self.launch, self.news = None, None, []
            self.lower.setCurrentIndex(0)
        self.fill_cards()
        path, url = art_source(g, HERO_KINDS)
        if not same or path:
            self._set_hero(path)
        if not path and url:
            slug = g.get('slug')
            ART.fetch(url, lambda p, slug=slug: self.game.get('slug') == slug
                      and self._set_hero(p))
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.setVisible(True)
        if (refocus or not same) and (t := self.default_focus()) is not None:
            QTimer.singleShot(0, t.setFocus)

    def default_focus(self):
        return self.btn_play if self.btn_play.isEnabled() else self.btn_fav

    def set_info(self, tips=None, launch=None, news=None):
        """Data for the cards as it arrives (each part is optional)."""
        if tips is not None:
            self.tips = tips
        if launch is not None:
            self.launch = launch
        if news is not None:
            self.news = news
        self.fill_cards()

    def fill_cards(self):
        g, tips, launch = self.game, self.tips or {}, self.launch or {}
        # Stats
        hours = fmt_hours(g.get('playtime_minutes'))
        stats = [(hours or "Not played yet", None, True)]
        if fmt_relative(g.get('last_played')):
            stats.append((f"Last played {fmt_relative(g.get('last_played'))}", None, False))
        if fmt_size(g.get('disk_bytes')):
            stats.append((f"{fmt_size(g.get('disk_bytes'))} on disk", None, False))
        added = g.get('added_at') or g.get('installed_at')
        if fmt_relative(added):
            stats.append((f"Installed {fmt_relative(added)}", None, False))
        gd = tips.get('gamedb') or {}
        hl = gd.get('hltb') or {}
        bar = None
        if hl.get('main') and gd.get('list') != 'wishlist':
            played = (g.get('playtime_minutes') or 0) / 60
            bar = played / hl['main']
            stats.append((f"About {hl['main']:g} h to beat (HowLongToBeat)", P['subtext'], False))
        self.cards['stats'].set_lines(stats, bar=bar, more="".join(
            f"<p>{html.escape(t)}</p>" for t, _c, _b in stats))
        # Compatibility
        compat, more = [], []
        pdb = tips.get('protondb') or {}
        tier = pdb.get('tier') or g.get('protondb')
        if tier:
            compat.append((f"ProtonDB {tier.capitalize()}", TIER_COLORS.get(tier), True))
            if pdb.get('trending') and pdb['trending'] != tier:
                compat.append((f"Trending {pdb['trending']}", None, False))
            if pdb.get('total'):
                compat.append((f"{pdb['total']} reports", P['subtext'], False))
        facts, facts_more = compat_facts(tips)
        compat.extend(facts)
        if facts_more:
            more.append(facts_more)
        proton = launch.get('proton') or g.get('proton')
        if proton:
            compat.append((f"Plays with {proton}"
                           + (" (pinned)" if launch.get('proton_pinned') else ""), None, False))
        if launch.get('launch_options') or g.get('launch_options'):
            opts = launch.get('launch_options') or g.get('launch_options')
            compat.append(("Custom launch options", None, False))
            more.append(f"<p><b>Launch options</b><br>{html.escape(opts)}</p>")
        recipes = tips.get('recipes') or []
        if recipes:
            compat.append((f"{len(recipes)} tuning suggestion{'s' if len(recipes) != 1 else ''}"
                           f" for {tips.get('gpu') or 'your GPU'}", P['subtext'], False))
            more.append("<p><b>Suggestions</b></p>" + "".join(
                f"<p>{html.escape(r['name'])}<br><span style='color:{P['subtext']}'>"
                f"{html.escape(r.get('description') or r.get('launch_options') or '')}</span></p>"
                for r in recipes))
        self.cards['compat'].set_lines(compat, more="".join(
            [f"<p>{html.escape(t)}</p>" for t, _c, _b in compat] + more))
        # Achievements (Steam games, and repacks' emulator files): the count
        # now, the list when it arrives.
        ach = g.get('achievements') if g.get('source', 'nexus') in ('steam', 'nexus') else None
        if ach:
            done, total = ach
            self.cards['achievements'].set_lines(
                [(f"{done} of {total}", P['success'] if done == total else None, True),
                 (f"{round(100 * done / total)}% unlocked", P['subtext'], False)],
                bar=done / total, more=self._ach_html.get((g['slug'], done), ''))
            if (g['slug'], done) not in self._ach_html:
                key = (g['slug'], done)
                cli_json(['--achievements', str(g['appid']) if g.get('source') == 'steam'
                          else g['slug'], '--json'],
                         lambda data, _e, key=key: self._ach_arrived(key, data or {}),
                         30000, ttl=120)
        else:
            self.cards['achievements'].set_lines([])
        # Game-db
        gl = []
        if gd.get('list') == 'wishlist':
            price = gd.get('price') or {}
            gl.append(("On your wishlist", None, True))
            if price.get('currentStr'):
                gl.append((price['currentStr'] + (f" (−{int(price['cutPct'])}%)"
                                                  if price.get('cutPct') else ""),
                           P['success'] if price.get('atHistoricalLow') else None, False))
            if gd.get('release'):
                gl.append((gamedb.release_label(gd['release']), P['subtext'], False))
        elif gd:
            c = gd.get('completion') or {}
            if c.get('state') == 'in-progress':
                gl.append((f"{int(c.get('fraction', 0) * 100)}% through", None, True))
            elif c.get('label'):
                gl.append((c['label'], None, True))
            if hl.get('main'):
                gl.append((f"Main story ~{hl['main']:g} h", None, False))
            if hl.get('extra'):
                gl.append((f"With extras ~{hl['extra']:g} h", P['subtext'], False))
        self.cards['gamedb'].set_lines(gl, more="".join(
            f"<p>{html.escape(t)}</p>" for t, _c, _b in gl))
        # News for this game
        mine = [i for i in self.news if g.get('appid') and i.get('appid') == g.get('appid')][:3]
        self.cards['news'].set_lines(
            [(i.get('title') or '', None, n == 0) for n, i in enumerate(mine[:2])]
            + ([(fmt_relative(mine[0].get('date')) or '', P['subtext'], False)] if mine else []),
            more=_news_html(mine))

    def _ach_arrived(self, key, data):
        rows = []
        for a in data.get('items') or []:
            secret = a.get('hidden') and not a.get('unlocked')
            title = "Hidden achievement" if secret else a.get('title', '')
            when = (datetime.fromtimestamp(a['time']).strftime('%d %b %Y')
                    if a.get('time') else '')
            color = P['text'] if a.get('unlocked') else P['muted']
            rows.append(f"<p><span style='color:{color}'>{'✓ ' if a.get('unlocked') else ''}"
                        f"{html.escape(title)}</span>"
                        + (f" <span style='color:{P['subtext']}'>· {when}</span>" if when else "")
                        + (f"<br><span style='color:{P['subtext']}'>"
                           f"{html.escape(a.get('desc') or '')}</span>" if not secret else "")
                        + "</p>")
        self._ach_html[key] = "".join(rows)
        try:
            if (self.game.get('slug'), (self.game.get('achievements') or [None])[0]) == key:
                self.cards['achievements'].more = self._ach_html[key]
        except RuntimeError:
            pass

    def _open_card(self, card: TheatreCard):
        if not card.more:
            return
        self.reader.setHtml(f"<h2>{html.escape(card.title)}</h2>{card.more}")
        self.lower.setCurrentIndex(1)
        self._reader_from = card
        self.reader.setFocus(Qt.FocusReason.TabFocusReason)

    def back(self) -> bool:
        """B on the page: close an opened card first. True if handled."""
        if self.lower.currentIndex() == 1:
            self.lower.setCurrentIndex(0)
            card = getattr(self, '_reader_from', None)
            if card is not None and card.isVisible():
                focus_and_reveal(card)
            return True
        return False

    def _set_logo(self, path):
        pm = QPixmap(path) if path else QPixmap()
        if not pm.isNull():
            self.logo.setPixmap(pm.scaled(640, 220, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        self.logo.setVisible(not pm.isNull())
        self.title.setText(self.game.get('display_name', ''))
        self.title.setVisible(pm.isNull())

    def _set_hero(self, path):
        self._pm = QPixmap(path) if path else None
        self._scaled = None
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        pt.fillRect(r, QColor(P['bg']))
        if self._pm is not None and not self._pm.isNull():
            key = (self.width(), self.height())
            if self._scaled is None or self._scaled[0] != key:
                self._scaled = (key, self._pm.scaled(
                    self.width(), self.height(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation))
            s = self._scaled[1]
            pt.drawPixmap((self.width() - s.width()) // 2, -(s.height() - self.height()) // 3, s)
        base = QColor(P['bg'])
        g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
        c = QColor(base); c.setAlphaF(0.92); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.30); g.setColorAt(0.6, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(1.0, c)
        pt.fillRect(r, QBrush(g))
        g = QLinearGradient(0.0, r.bottom(), 0.0, r.top())
        c = QColor(base); c.setAlphaF(0.95); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(0.55, c)
        pt.fillRect(r, QBrush(g))
        pt.end()


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
            pm = QPixmap(path)
            if not pm.isNull():
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


class TheatreView(QWidget):
    """Theatre mode: one big hero, rows of big tiles, a clock, and nothing
    that needs a mouse. LB/RB switch Home and Library, A opens a game, B goes
    back, Start leaves. Styled with the same tokens at THEATRE_SCALE."""
    play = pyqtSignal(dict)
    action = pyqtSignal(str, dict)
    menu = pyqtSignal(dict, QPoint)
    exit = pyqtSignal()
    surprise = pyqtSignal()

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
        self.hero = HeroCarousel(scale=THEATRE_SCALE)
        self.hero.play.connect(self.play)
        self.hero.open.connect(self.open_game)
        self.hero.menu.connect(self.menu)
        # No ⋮ here: the game page's Options (or X) opens the controller sheet.
        self.hero.btn_more.hide()
        self.hero.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.home_lay.addWidget(self.hero)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['6'])
        self.home_lay.addLayout(self.rows)
        self.home_lay.addStretch()
        self.home.setWidget(host)
        self.pages.addWidget(self.home)
        # Library: every game, big tiles
        self.lib = QScrollArea()
        self.lib.setWidgetResizable(True)
        self.lib.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lhost = QWidget()
        self.lib_flow = FlowLayout(lhost, margin=SP['2'], spacing=SP['5'], justify=True)
        self.lib.setWidget(lhost)
        self.pages.addWidget(self.lib)
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
        self.details.closed.connect(self.close_game)
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
        clear_layout(self.rows)
        played = sorted((g for g in games if g.get('last_played')),
                        key=lambda g: g['last_played'], reverse=True)
        added = sorted((g for g in games if g.get('added_at')),
                       key=lambda g: g['added_at'], reverse=True)
        cols = [(n, [g for g in games if n in (g.get('collections') or ())], 'cover')
                for n in COLLECTION_NAMES]
        for title, items, kind in (("Continue playing", played[:12], 'wide'),
                                   ("Favourites", [g for g in games if g.get('favorite')],
                                    'cover'),
                                   *cols,
                                   ("Recently installed", added[:14], 'cover'),
                                   ("Not played yet",
                                    [g for g in games if not g.get('last_played')], 'cover'),
                                   ("Ready to install", owned[:20], 'cover')):
            if not items:
                continue
            w = self.WIDE if kind == 'wide' else self.TILE
            row = ShelfRow(title, w, kind)
            for g in items:
                row.add(self._tile(g, w, kind))
            row.finish()
            self.rows.addWidget(row)
        spare: dict[str, GameTile] = {}
        while self.lib_flow.count():
            w = self.lib_flow.takeAt(0).widget()
            if isinstance(w, GameTile):
                spare[w.game.get('slug')] = w
        for g in sorted(games, key=lambda g: (g.get('display_name') or '').lower()):
            t = spare.pop(g['slug'], None)
            if t is not None:
                t.set_game(g)
            else:
                t = self._tile(g, self.TILE, 'cover')
            self.lib_flow.addWidget(t)
        for w in spare.values():
            w.hide()
            w.deleteLater()
        if self.details.isVisible():
            cur = next((g for g in everything
                        if g['slug'] == self.details.game.get('slug')), None)
            if cur:
                self.details.open(cur, refocus=False)

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

    # ---- navigation ----
    def _tab(self, key):
        self.pages.setCurrentWidget(self.home if key == 'home' else self.lib)
        QTimer.singleShot(0, lambda: alive(self) and focus_and_reveal(self.default_focus()))

    def cycle_tab(self, d):
        keys = self.tabs.keys()
        i = (keys.index(self.tabs.current()) + d) % len(keys)
        self.tabs.set_current(keys[i], emit=True)

    def open_game(self, g):
        # What's behind the game page is out of reach (D-pad and Tab alike).
        self.pages.setEnabled(False)
        self.tabs.setEnabled(False)
        self.details.open(g)
        self._load_info(g)

    def _load_info(self, g):
        """The cards' data: cached per game for the session (the CLI caches
        ProtonDB / Game-db itself), fetched in the background."""
        slug = g['slug']
        tips = self._tips.get(slug)
        launch = self._launch.get(slug)
        self.details.set_info(tips, launch, self.news_items())
        if tips is None and slug not in self._asked:
            self._asked.add(slug)
            cli_json(['--game-tips', slug, '--json'],
                     lambda info, _e, slug=slug: self._got(self._tips, slug, info or {}),
                     ttl=300)
            if g.get('source', 'nexus') == 'nexus':
                cli_json(['--show-launch', slug, '--json'],
                         lambda info, _e, slug=slug: self._got(self._launch, slug, info or {}))

    def _got(self, store, slug, info):
        store[slug] = info
        try:
            if self.details.isVisible() and self.details.game.get('slug') == slug:
                self.details.set_info(self._tips.get(slug), self._launch.get(slug))
        except RuntimeError:
            pass

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
        """B: close the search or the game page; otherwise go to Home. True
        if handled."""
        if self.search.isVisible():
            self.search.close_search()
            return True
        if self.details.isVisible() and getattr(self, '_from_search', False) \
                and self.details.lower.currentIndex() == 0:
            # Back from a game found by search: to the search, as it was.
            self._from_search = False
            self.details.setVisible(False)
            self.search.open(self.everything or self.games)
            return True
        if self.details.isVisible():
            if not self.details.back():
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
        if self.search.isVisible():
            return self.search.default_focus()
        if self.details.isVisible():
            return self.details.default_focus()
        if self.pages.currentWidget() is self.home:
            if not self.hero.isHidden() and self.hero.btn.isEnabled():
                return self.hero.btn
        page = self.pages.currentWidget()
        return next((t for t in page.findChildren(GameTile) if not t.isHidden()), None)

    def _tick(self):
        self.clock.setText(datetime.now().strftime('%H:%M'))

    def showEvent(self, ev):
        super().showEvent(ev)
        self._tick()
        self._clock.start()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._clock.stop()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.hero.setFixedHeight(int(max(420, min(720, self.height() * 0.55))))
        if self.details.isVisible():
            self.details.setGeometry(self.rect())
        if self.search.isVisible():
            self.search.setGeometry(self.rect())


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
from .art import ART, art_source, HERO_KINDS, LOGO_KINDS  # noqa: E402
from .gameinfo import (can_quick_play, compat_facts, is_installed, play_enabled, play_label,
    SOURCE_LABEL, TIER_COLORS)  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .library import featured_games, HeroCarousel, ShelfRow, surprise_pick  # noqa: E402
from .tiles import GameTile  # noqa: E402
from .widgets import (clear_layout, FlowLayout, icon_button, icon_label, label,
    SegmentedControl, set_glyph_icon)  # noqa: E402
from .topbar import Wordmark  # noqa: E402
