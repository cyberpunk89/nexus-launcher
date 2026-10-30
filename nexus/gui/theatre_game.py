"""Theatre's game page: one game full screen, from the couch."""

import html
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QRectF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from nexus import gamedb
from nexus.features import has

from .fmt import fmt_hours, fmt_relative, fmt_size
from .theme import G, P, pill_qss, SP


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
        tier = (pdb.get('tier') or g.get('protondb')) if has('proton') else None
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
        img = fit_image(path, 640, 220, cover=False) if path else None    # at shown size
        pm = QPixmap.fromImage(img) if img is not None else QPixmap()
        if not pm.isNull():
            self.logo.setPixmap(pm.scaled(640, 220, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        self.logo.setVisible(not pm.isNull())
        self.title.setText(self.game.get('display_name', ''))
        self.title.setVisible(pm.isNull())

    def _set_hero(self, path):
        self._pm = screen_pixmap(path, self)
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
        base = QColor(P['scrim'])
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


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import ART, art_source, fit_image, HERO_KINDS, LOGO_KINDS, screen_pixmap  # noqa: E402
from .gameinfo import (compat_facts, play_enabled, play_label, SOURCE_LABEL,
    TIER_COLORS)  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .widgets import clear_layout, icon_label, label, set_glyph_icon  # noqa: E402
