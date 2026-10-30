"""Theatre's game page: one game full screen, from the couch.

The page is a stage (the art, the logo, one quiet line of facts, a big
Play, a round Favourite and Options) with the section tabs along its
bottom edge. D-pad Down (or LB / RB) slides those tabs up into a glass
drawer over the lower half: Overview, Media, News, Achievements and
Compatibility, each hidden while it has nothing to show. While the drawer
is open the game shrinks to a band at the top, with Play one Up away."""

import html
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QEvent, QRectF, Qt, QTimer, QVariantAnimation
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QScrollArea, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from nexus import gamedb
from nexus.features import has

from .common import alive
from .fmt import fmt_hours, fmt_relative, fmt_size
from .theme import G, P, R, SP


SECTIONS = (('overview', "Overview"), ('media', "Media"), ('news', "News"),
            ('achievements', "Achievements"), ('compat', "Compatibility"))


def fact_text(g: dict) -> str:
    """The game's one quiet line, as rich text: time played, when, its
    ProtonDB tier and an update waiting. The stage, the drawer's band and
    Home's spotlight all show it instead of a row of pills."""
    parts = []
    hours = fmt_hours(played_minutes(g))         # a running session counted in
    last = fmt_relative(g.get('last_played'))
    if hours:
        parts.append(html.escape(f"{hours} played"))
    if last:
        parts.append(html.escape(f"Last played {last}"))
    if not hours and not last:
        parts.append("Not played yet")
    tier = g.get('protondb') if has('proton') else None
    if tier in TIER_COLORS:
        parts.append(f"<span style='color:{TIER_COLORS[tier]}'>"
                     f"ProtonDB {html.escape(tier.capitalize())}</span>")
    if g.get('update_pending'):
        parts.append(f"<span style='color:{P['warning']}'>Update ready</span>")
    return "&nbsp;&nbsp;·&nbsp;&nbsp;".join(parts)


def fact_line(name: str = "TheatreMeta") -> QLabel:
    lab = label("", name)
    lab.setTextFormat(Qt.TextFormat.RichText)
    return lab


def _bar() -> QProgressBar:
    pb = QProgressBar()
    pb.setObjectName("CardProgress")
    pb.setTextVisible(False)
    pb.setFixedHeight(int(SP['2'] * 1.25))
    pb.setRange(0, 1000)
    return pb


def _set_bar(pb: QProgressBar, frac: float | None):
    pb.setVisible(frac is not None)
    if frac is not None:
        pb.setValue(int(max(0.0, min(1.0, frac)) * 1000))


def _news_html(items: list[dict]) -> str:
    out = []
    for i in items:
        when = fmt_relative(i.get('date')) or ''
        out.append(f"<h3>{html.escape(i.get('title') or '')}</h3>"
                   f"<p style='color:{P['subtext']}'>{html.escape(when)}</p>"
                   f"<p>{html.escape(i.get('summary') or '')}</p>")
    return "".join(out)


class TheatreRow(QFrame):
    """A glass row in the drawer (a news item, every achievement, the
    compatibility details): focusable, and A opens its longer version in
    the drawer's reader."""
    activated = pyqtSignal(object)

    def __init__(self, title: str, sub: str = '', more: str = '', heading: str = '',
                 wrap: bool = True):
        super().__init__()
        self.title, self.more = title, more
        self.heading = heading or title          # the reader's title
        self.setObjectName("TheatreNewsRow")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['3'], SP['5'], SP['3'])
        lay.setSpacing(SP['1'])
        lay.addWidget(plain_label(title, "TheatreMeta"))
        if sub:
            s = plain_label(sub, "TheatreSub")
            s.setWordWrap(wrap)
            lay.addWidget(s)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self)
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self)
        super().mouseReleaseEvent(ev)


class TheatreShot(QFrame):
    """A trailer in the drawer's Media section: its thumbnail, rounded, with
    a play mark. A plays it over Theatre."""
    activated = pyqtSignal(dict)
    focused = pyqtSignal(dict)

    def __init__(self, trailer: dict, w: int, h: int):
        super().__init__()
        self.trailer = trailer
        self.setObjectName("TheatreShot")
        self.setFixedSize(w, h)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pm: QPixmap | None = None
        url = str(trailer.get('thumb') or '')
        if url.startswith('https://'):
            path = ART.cached(url)
            if path:
                self._set_art(path)
            else:
                ART.fetch(url, lambda p, s=self: alive(s) and p and s._set_art(p))

    def _set_art(self, path):
        pm = QPixmap(path)
        if not pm.isNull():
            self._pm = pm.scaled(self.width(), self.height(),
                                 Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                 Qt.TransformationMode.SmoothTransformation)
            self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), R['tile'], R['tile'])
        pt.setClipPath(path)
        pt.fillRect(self.rect(), QColor(P['scrim']))
        if self._pm is not None:
            pt.drawPixmap((self.width() - self._pm.width()) // 2,
                          (self.height() - self._pm.height()) // 2, self._pm)
        c = QColor(P['scrim'])
        c.setAlphaF(0.35)
        pt.fillRect(self.rect(), c)
        d = min(self.width(), self.height()) // 4               # the play mark
        disc = QRectF((self.width() - d) / 2, (self.height() - d) / 2, d, d)
        c.setAlphaF(0.6)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(c)
        pt.drawEllipse(disc)
        mark = glyph_pixmap(G['play'], P['on_art'], d // 2)
        pt.drawPixmap(int(disc.center().x() - d / 4 + d * 0.04), int(disc.center().y() - d / 4),
                      mark)
        pt.end()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.focused.emit(self.trailer)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.trailer)
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.trailer)
        super().mouseReleaseEvent(ev)


class TheatreDrawer(QFrame):
    """The game page's lower half: the section tabs, and once open the
    section under them (or the reader with a row's longer version)."""
    SHOT_W, SHOT_H = 520, 292

    def __init__(self, margin: int):
        super().__init__()
        self.setObjectName("TheatreDrawer")
        self.setProperty('open', False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(margin, SP['6'], margin, SP['6'])
        lay.setSpacing(SP['8'])
        self.tabs = SegmentedControl('underline', 64)
        for key, text in SECTIONS:
            self.tabs.add(key, text)
        lay.addWidget(self.tabs)
        self.pages = QStackedWidget()
        self.sections: dict[str, QWidget] = {}
        self._overview()
        self._media()
        for key in ('news', 'achievements', 'compat'):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(SP['3'])
            self._add(key, w)
        self.reader = QTextBrowser()
        self.reader.setObjectName("TheatreReader")
        self.reader.setOpenLinks(False)          # no browser hand-off on a TV
        self.reader.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.pages.addWidget(self.reader)
        lay.addWidget(self.pages, 1)
        self.tabs.changed.connect(self.show_section)
        self.pages.hide()                        # closed: the tabs only

    def _add(self, key, w):
        self.sections[key] = w
        self.pages.addWidget(w)

    def _overview(self):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP['8'] * 3)
        left = QVBoxLayout()
        left.setSpacing(SP['4'])
        self.about = label("", "TheatreAbout", wrap=True)
        self.credits = label("", "TheatreSub", wrap=True)
        self.more_facts = fact_line("TheatreSub")       # compat facts without Proton
        self.more_facts.setWordWrap(True)
        for lab in (self.about, self.credits, self.more_facts):
            left.addWidget(lab)
        left.addStretch()
        h.addLayout(left, 3)
        right = QVBoxLayout()
        right.setSpacing(SP['2'])
        right.addWidget(label("YOUR PROGRESS", "TheatreLabel"))
        right.addSpacing(SP['2'])
        self.progress = label("", "TheatreBig")
        self.progress_bar = _bar()
        self.progress_sub = fact_line("TheatreSub")
        self.progress_sub.setWordWrap(True)
        self.ach_line = label("", "TheatreMeta")
        self.ach_bar = _bar()
        for wdg in (self.progress, self.progress_bar, self.progress_sub):
            right.addWidget(wdg)
        right.addSpacing(SP['6'])
        right.addWidget(self.ach_line)
        right.addWidget(self.ach_bar)
        right.addStretch()
        h.addLayout(right, 2)
        self._add('overview', w)

    def _media(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SP['4'])
        self.shots = QScrollArea()
        self.shots.setFrameShape(QFrame.Shape.NoFrame)
        self.shots.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shots.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shots.setWidgetResizable(True)
        self.shots.setFixedHeight(self.SHOT_H + SP['2'] * 2)
        host = QWidget()
        self.shot_row = QHBoxLayout(host)
        self.shot_row.setContentsMargins(SP['2'], SP['2'], SP['2'], SP['2'])
        self.shot_row.setSpacing(SP['5'])
        self.shots.setWidget(host)
        v.addWidget(self.shots)
        self.media_title = plain_label("", "TheatreMeta")
        self.media_sub = label("", "TheatreSub")
        v.addWidget(self.media_title)
        v.addWidget(self.media_sub)
        v.addStretch()
        self._add('media', w)

    # ---- state ----
    def is_open(self) -> bool:
        return bool(self.property('open'))

    def set_open(self, on: bool):
        self.setProperty('open', on)
        self.style().unpolish(self)
        self.style().polish(self)
        self.pages.setVisible(on)
        self.update()

    def visible_keys(self) -> list[str]:
        return [k for k, _t in SECTIONS if not self.tabs.item(k).isHidden()]

    def show_section(self, key: str):
        self.tabs.set_current(key)
        self.pages.setCurrentWidget(self.sections[key])

    def reader_open(self) -> bool:
        return self.pages.currentWidget() is self.reader

    def set_section_visible(self, key: str, on: bool):
        self.tabs.set_item_visible(key, on)
        if not on and self.tabs.current() == key:
            self.show_section('overview')


class TheatreDetails(QWidget):
    """A game, full screen: the stage, and the drawer below it."""
    play = pyqtSignal(dict)
    action = pyqtSignal(str, dict)
    back_asked = pyqtSignal()                    # the (mouse-only) ‹ in the corner
    trailer = pyqtSignal(dict)

    LOGO = (760, 280)
    BAND_LOGO = (420, 150)

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("TheatreDetails")
        self.game: dict = {}
        self._pm = None
        self._scaled = None
        self._logo_pm = QPixmap()
        self.tips: dict | None = None            # --game-tips for this game
        self.launch: dict | None = None          # --show-launch (repacks)
        self.store: dict | None = None           # --store-info
        self.news: list[dict] = []
        self._ach_html: dict[tuple, str] = {}    # (slug, unlocked) -> the list, as HTML
        self._ach_asked: set[tuple] = set()
        self._filling = False
        self._reader_from = None
        m = SP['8'] * 3
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, SP['6'], 0, 0)
        lay.setSpacing(0)
        # The corner: ‹ for the mouse (B is back on a pad), and the clock.
        self.top = QWidget()
        top = QHBoxLayout(self.top)
        top.setContentsMargins(m, 0, m, 0)
        self.btn_back = icon_button(G['chevron_left'], "Back", 56, "GlassIcon")
        self.btn_back.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_back.clicked.connect(self.back_asked)
        top.addWidget(self.btn_back)
        top.addStretch()
        self.clock = label("", "TheatreClock")
        top.addWidget(self.clock)
        lay.addWidget(self.top)
        # The stage: the game, its line of facts and what you'd press.
        self.stage = QWidget()
        sv = QVBoxLayout(self.stage)
        sv.setContentsMargins(m, 0, m, 0)
        sv.setSpacing(0)
        sv.addStretch(3)
        self.kicker = label("", "HeroKicker")
        self.kicker.setTextFormat(Qt.TextFormat.RichText)
        sv.addWidget(self.kicker)
        sv.addSpacing(SP['4'])
        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        sv.addWidget(self.logo)
        self.title = label("", "HeroTitle", wrap=True)
        self.title.setStyleSheet("font-size: 56px;")
        sv.addWidget(self.title)
        sv.addSpacing(SP['6'])
        self.facts = fact_line()
        sv.addWidget(self.facts)
        sv.addSpacing(SP['8'])
        row = QHBoxLayout()
        row.setSpacing(SP['5'])
        self.btn_play = self._play_button(420, 96)
        row.addWidget(self.btn_play)
        row.addSpacing(SP['2'])
        self.btn_fav = icon_button(G['star_outline'], "Add to favourites", 80, "GlassIcon")
        self.btn_fav.clicked.connect(lambda: self.action.emit('favorite', self.game))
        row.addWidget(self.btn_fav)
        self.btn_opts = icon_button(G['more'], "Options (X)", 80, "GlassIcon")
        self.btn_opts.clicked.connect(lambda: self.action.emit('options', self.game))
        row.addWidget(self.btn_opts)
        row.addStretch()
        sv.addLayout(row)
        sv.addStretch(1)
        lay.addWidget(self.stage, 1)
        # Drawer open: the game as one band at the top, Play one Up away.
        self.band = QWidget()
        bh = QHBoxLayout(self.band)
        bh.setContentsMargins(m, SP['2'], m, 0)
        bh.setSpacing(SP['5'])
        self.band_logo = QLabel()
        bh.addWidget(self.band_logo, 0, Qt.AlignmentFlag.AlignVCenter)
        self.band_title = label("", "HeroTitle")
        self.band_title.setStyleSheet("font-size: 40px;")
        bh.addWidget(self.band_title, 0, Qt.AlignmentFlag.AlignVCenter)
        bh.addSpacing(SP['6'])
        self.band_facts = fact_line()
        bh.addWidget(self.band_facts, 0, Qt.AlignmentFlag.AlignVCenter)
        bh.addStretch()
        self.band_play = self._play_button(320, 84)
        bh.addWidget(self.band_play, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.band)
        self.band.hide()
        self.gap = QWidget()                     # what the drawer gives back as it opens
        lay.addWidget(self.gap, 1)
        self.gap.hide()
        self.drawer = TheatreDrawer(m)
        self.drawer.reader.installEventFilter(self)
        lay.addWidget(self.drawer)
        self._slide = QVariantAnimation(self)
        self._slide.valueChanged.connect(self._slid)
        self._slide.finished.connect(self._slide_done)
        QApplication.instance().focusChanged.connect(self._focus_changed)
        self.setVisible(False)

    def _play_button(self, w, h) -> QPushButton:
        b = QPushButton("Play")
        b.setObjectName("PlayBig")
        set_glyph_icon(b, G['play'], int(h * 0.36), 'on_accent', hover_role=None)
        b.setFixedSize(w, h)
        b.clicked.connect(self._play)
        return b

    def _play(self):
        if self.game.get('playing'):             # Resume: back to the running game
            self.action.emit('resume', self.game)
        else:
            self.play.emit(self.game)

    # ---- the game ----
    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu:                 # X: the options sheet
            self.action.emit('options', self.game)
        else:
            super().keyPressEvent(ev)

    def open(self, g: dict, refocus: bool = True):
        same = g.get('slug') == self.game.get('slug') and self.isVisible()
        self.game = g
        self.set_kicker(g)
        logo, url = art_source(g, LOGO_KINDS)
        self._set_logo(logo)
        if not logo and url:
            slug = g.get('slug')
            ART.fetch(url, lambda p, slug=slug: self.game.get('slug') == slug
                      and self._set_logo(p))
        self.facts.setText(fact_text(g))
        self.band_facts.setText(fact_text(g))
        self.set_play(g)
        fav = bool(g.get('favorite'))
        self.btn_fav.setText(G['star'] if fav else G['star_outline'])
        self.btn_fav.setToolTip("Favourite" if fav else "Add to favourites")
        self.btn_fav.setStyleSheet(f"border-radius: 40px; color: {P['warning']};" if fav
                                   else "border-radius: 40px;")
        if not same:
            self.tips, self.launch, self.store, self.news = None, None, None, []
            self.collapse(instant=True, refocus=False)
            self.drawer.show_section('overview')
        self.fill()
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
        if refocus or not same:
            QTimer.singleShot(0, self._focus_default)

    def _focus_default(self):
        t = self.default_focus()
        if self.isVisible() and t is not None and t.isVisible():
            t.setFocus()

    def set_kicker(self, g: dict):
        """Above the logo: where the game comes from, or NOW PLAYING · 42 MIN
        while it runs (STARTING… while it starts)."""
        live = bool(g.get('playing'))
        self.kicker.setText(live_kicker(g) if live or g.get('launching') else
                            html.escape(SOURCE_LABEL.get(g.get('source', 'nexus'), '').upper()))
        if self.kicker.property('live') != live:
            self.kicker.setProperty('live', live)
            self.kicker.style().unpolish(self.kicker)
            self.kicker.style().polish(self.kicker)

    def set_play(self, g: dict):
        """Play's text and state, on the stage and in the band. A running
        game gets Resume (Quit game is under Options)."""
        self.set_kicker(g)
        for b in (self.btn_play, self.band_play):
            b.setText("Resume" if g.get('playing') else play_label(g))
            b.setEnabled(bool(g.get('playing')) or play_enabled(g))

    def default_focus(self):
        if self.drawer.is_open():
            return self.drawer.tabs.item(self.drawer.tabs.current())
        return self.btn_play if self.btn_play.isEnabled() else self.btn_fav

    def set_info(self, tips=None, launch=None, news=None, store=None):
        """Data for the drawer as it arrives (each part is optional)."""
        if tips is not None:
            self.tips = tips
        if launch is not None:
            self.launch = launch
        if news is not None:
            self.news = news
        if store is not None:
            self.store = store
        self.fill()

    # ---- the drawer's sections ----
    def fill(self):
        d = self.drawer
        reading = d.reader_open()
        self._filling = True
        try:
            self._fill_overview()
            self._fill_media()
            self._fill_news()
            self._fill_achievements()
            self._fill_compat()
        finally:
            self._filling = False
        if reading:
            d.pages.setCurrentWidget(d.reader)
        else:
            d.show_section(d.tabs.current())

    def _rows(self, key) -> QVBoxLayout:
        lay = self.drawer.sections[key].layout()
        clear_layout(lay)
        return lay

    def _row(self, title, sub='', more='', heading='', wrap=True) -> 'TheatreRow':
        r = TheatreRow(title, sub, more, heading, wrap)
        r.activated.connect(self._open_row)
        return r

    def _fill_overview(self):
        d, g = self.drawer, self.game
        st = (self.store or {}).get('store') or {}
        d.about.setText(st.get('blurb') or '')
        d.about.setVisible(bool(st.get('blurb')))
        year = next((w for w in str(st.get('release') or '').replace(',', ' ').split()
                     if len(w) == 4 and w.isdigit()), '')
        credits = [x for x in (', '.join(st.get('developers') or []),
                               ', '.join((st.get('genres') or [])[:2]), year) if x]
        d.credits.setText("  ·  ".join(credits))
        d.credits.setVisible(bool(credits))
        # Without Proton there's no Compatibility section: its other facts
        # (Steam Deck, anti-cheat, PCGamingWiki) go here.
        facts = compat_facts(self.tips or {})[0] if not has('proton') else []
        d.more_facts.setText("<br>".join(
            (f"<span style='color:{c}'>{html.escape(t)}</span>" if c else html.escape(t))
            for t, c, _b in facts))
        d.more_facts.setVisible(bool(facts))
        # Your progress: Game-db's completion, How long to beat, the hours.
        gd = (self.tips or {}).get('gamedb') or {}
        hl = gd.get('hltb') or {}
        minutes = g.get('playtime_minutes') or 0
        hours = fmt_hours(minutes)
        big, frac, sub = hours and f"{hours} played" or "Not played yet", None, []
        if gd.get('list') == 'wishlist':
            price = gd.get('price') or {}
            big = "On your wishlist"
            if price.get('currentStr'):
                cut = f" (−{int(price['cutPct'])}%)" if price.get('cutPct') else ""
                sub.append(html.escape(price['currentStr'] + cut))
            if gd.get('release'):
                sub.append(html.escape(gamedb.release_label(gd['release'])))
        else:
            c = gd.get('completion') or {}
            if c.get('state') == 'in-progress':
                frac = float(c.get('fraction') or 0)
                big = f"{int(frac * 100)}% through"
            elif c.get('label'):
                big = c['label']
            if hl.get('main'):
                if frac is None and minutes:
                    frac = minutes / 60 / hl['main']
                sub.append(html.escape(f"{minutes / 60:.0f} h of about {hl['main']:g} h for the "
                                       "main story" if minutes >= 60 else
                                       f"About {hl['main']:g} h for the main story"))
        last = fmt_relative(g.get('last_played'))
        added = fmt_relative(g.get('added_at') or g.get('installed_at'))
        size = fmt_size(g.get('disk_bytes'))
        more = [x for x in (last and f"Last played {last}", added and f"Installed {added}",
                            size and f"{size} on disk") if x]
        if more:
            sub.append(html.escape("  ·  ".join(more)))
        d.progress.setText(big)
        _set_bar(d.progress_bar, frac)
        d.progress_sub.setText("<br>".join(sub))
        d.progress_sub.setVisible(bool(sub))
        ach = self._achievements()
        d.ach_line.setVisible(ach is not None)
        if ach:
            d.ach_line.setText(f"{ach[0]} of {ach[1]} achievements")
        _set_bar(d.ach_bar, ach[0] / ach[1] if ach else None)

    def _fill_media(self):
        """Trailers from the Steam store (screenshots wait for Game-db)."""
        d = self.drawer
        trailers = [t for t in ((self.store or {}).get('store') or {}).get('trailers') or []
                    if str(t.get('url') or '').startswith('https://')]
        have = [s.trailer for s in d.shots.widget().findChildren(TheatreShot)]
        if have != trailers:
            clear_layout(d.shot_row)
            for t in trailers:
                s = TheatreShot(t, d.SHOT_W, d.SHOT_H)
                s.activated.connect(self.trailer)
                s.focused.connect(self._shot_focused)
                d.shot_row.addWidget(s)
            d.shot_row.addStretch()
            if trailers:
                self._shot_focused(trailers[0])
        n = len(trailers)
        d.media_sub.setText(f"{n} trailer{'s' if n != 1 else ''}")
        d.set_section_visible('media', bool(trailers))

    def _shot_focused(self, t: dict):
        self.drawer.media_title.setText(t.get('name') or "Trailer")

    def _fill_news(self):
        g = self.game
        mine = [i for i in self.news if g.get('appid') and i.get('appid') == g.get('appid')][:3]
        lay = self._rows('news')
        for i in mine:
            when = fmt_relative(i.get('date')) or ''
            summary = i.get('summary') or ''
            lay.addWidget(self._row(i.get('title') or '', " · ".join(
                x for x in (when, summary[:160] + ("…" if len(summary) > 160 else '')) if x),
                _news_html([i]), "News"))
        lay.addStretch()
        self.drawer.set_section_visible('news', bool(mine))

    def _achievements(self) -> tuple[int, int] | None:
        g = self.game
        ach = g.get('achievements') if g.get('source', 'nexus') in ('steam', 'nexus') else None
        return tuple(ach) if ach and ach[1] else None

    def _fill_achievements(self):
        """Steam games, and repacks' emulator files: the count now, the
        list when it arrives."""
        g, ach = self.game, self._achievements()
        key = (g.get('slug'), ach[0]) if ach else None
        if ach and key not in self._ach_html and key not in self._ach_asked:
            # Asked before drawing: a fresh memo answers right away, and the
            # answer only stores the list while this fill runs (the Qt pitfall).
            self._ach_asked.add(key)
            cli_json(['--achievements', str(g['appid']) if g.get('source') == 'steam'
                      else g['slug'], '--json'],
                     lambda data, _e, key=key: self._ach_arrived(key, data or {}),
                     30000, ttl=120)
        lay = self._rows('achievements')
        self.drawer.set_section_visible('achievements', ach is not None)
        if not ach:
            return
        done, total = ach
        big = label(f"{done} of {total} unlocked", "TheatreBig")
        if done == total:
            big.setStyleSheet(f"color: {P['success']};")
        lay.addWidget(big)
        bar = _bar()
        _set_bar(bar, done / total)
        lay.addWidget(bar)
        lay.addWidget(label(f"{round(100 * done / total)}% of this game's achievements",
                            "TheatreSub"))
        if key in self._ach_html:
            lay.addSpacing(SP['4'])
            lay.addWidget(self._row("Every achievement", "The full list, with dates",
                                    self._ach_html[key], "Achievements", wrap=False),
                          0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch()

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
        if key in self._ach_html or not rows:
            return
        self._ach_html[key] = "".join(rows)
        try:
            if not self._filling and not self.drawer.reader_open() and \
                    (self.game.get('slug'), (self.game.get('achievements') or [None])[0]) == key:
                self._fill_achievements()
        except RuntimeError:
            pass

    def _fill_compat(self):
        """ProtonDB, Steam Deck, anti-cheat, the Proton it plays with: a
        Proton fact, so there's no such section without Proton."""
        lay = self._rows('compat')
        if not has('proton'):
            self.drawer.set_section_visible('compat', False)
            return
        g, tips, launch = self.game, self.tips or {}, self.launch or {}
        lines, more = [], []
        pdb = tips.get('protondb') or {}
        tier = pdb.get('tier') or g.get('protondb')
        if tier:
            lines.append((f"ProtonDB {tier.capitalize()}", TIER_COLORS.get(tier), True))
            if pdb.get('trending') and pdb['trending'] != tier:
                lines.append((f"Trending {pdb['trending']}", None, False))
            if pdb.get('total'):
                lines.append((f"{pdb['total']} reports", None, False))
        facts, facts_more = compat_facts(tips)
        lines.extend(facts)
        if facts_more:
            more.append(facts_more)
        proton = launch.get('proton') or g.get('proton')
        if proton:
            lines.append((f"Plays with {proton}"
                          + (" (pinned)" if launch.get('proton_pinned') else ""), None, False))
        opts = launch.get('launch_options') or g.get('launch_options')
        if opts:
            lines.append(("Custom launch options", None, False))
            more.append(f"<p><b>Launch options</b><br>{html.escape(opts)}</p>")
        recipes = tips.get('recipes') or []
        if recipes:
            lines.append((f"{len(recipes)} tuning suggestion{'s' if len(recipes) != 1 else ''}"
                          f" for {tips.get('gpu') or 'your GPU'}", None, False))
            more.append("<p><b>Suggestions</b></p>" + "".join(
                f"<p>{html.escape(r['name'])}<br><span style='color:{P['subtext']}'>"
                f"{html.escape(r.get('description') or r.get('launch_options') or '')}</span></p>"
                for r in recipes))
        for i, (text, color, big) in enumerate(lines):
            lab = plain_label(text, "TheatreBig" if i == 0 and big else "TheatreMeta")
            if color:
                lab.setStyleSheet(f"color: {color};")
            lay.addWidget(lab)
        if more:
            lay.addSpacing(SP['4'])
            lay.addWidget(self._row("Details", "Everything above, and the settings behind it",
                                    "".join([f"<p>{html.escape(t)}</p>" for t, _c, _b in lines]
                                            + more), "Compatibility", wrap=False),
                          0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch()
        self.drawer.set_section_visible('compat', bool(lines))

    # ---- open / close the drawer ----
    def expanded(self) -> bool:
        return self.drawer.is_open()

    def _open_h(self) -> int:
        return int(self.height() * 0.54)

    def _closed_h(self) -> int:
        return self.drawer.tabs.height() + SP['6'] * 2

    def expand(self):
        """Slide the drawer up over the lower half; the game becomes a band."""
        if self.expanded():
            return
        self.top.hide()
        self.stage.hide()
        self.band.show()
        self.gap.show()
        self.drawer.set_open(True)
        self._slide_to(self._open_h())
        self.update()
        f = QApplication.focusWidget()
        if f is None or not (self.drawer.isAncestorOf(f) or f is self.band_play):
            focus_and_reveal(self.default_focus())

    def collapse(self, instant: bool = False, refocus: bool = True):
        """Back to the stage (the reader closes with it)."""
        d = self.drawer
        if d.reader_open():
            d.show_section(d.tabs.current())
        if not self.expanded():
            return
        d.set_open(False)
        self.band.hide()
        self.gap.hide()
        self.top.show()
        self.stage.show()
        self._slide.stop()
        d.setMinimumHeight(0)
        d.setMaximumHeight(16777215)
        self.update()
        if refocus and not instant:
            focus_and_reveal(self.default_focus())

    def _slide_to(self, h: int):
        self._slide.stop()
        self._slide.setDuration(anim_ms(260))
        if self._slide.duration() == 0:
            self._slid(h)
            self._slide_done()
            return
        self._slide.setStartValue(self._closed_h())
        self._slide.setEndValue(h)
        self._slide.start()

    def _slid(self, h):
        self.drawer.setFixedHeight(int(h))

    def _slide_done(self):
        if self.expanded():
            self.drawer.setFixedHeight(self._open_h())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.expanded() and self._slide.state() != QVariantAnimation.State.Running:
            self.drawer.setFixedHeight(self._open_h())

    def cycle_section(self, d: int):
        """LB / RB on the game page: open the drawer, then switch sections."""
        dr = self.drawer
        if not self.expanded():
            self.expand()
        else:
            keys = dr.visible_keys()
            cur = dr.tabs.current()
            i = keys.index(cur) if cur in keys else 0
            dr.show_section(keys[(i + d) % len(keys)])
        focus_and_reveal(dr.tabs.item(dr.tabs.current()))

    def _focus_changed(self, _old, new):
        """Focus into the drawer (D-pad Down to the tabs, a click on one)
        opens it."""
        if new is not None and self.isVisible() and not self.expanded() \
                and self.drawer.isAncestorOf(new):
            self.expand()

    def mousePressEvent(self, ev):
        # A click on the art above the open drawer: back to the stage.
        if self.expanded() and ev.position().y() < self.drawer.y():
            self.collapse()
        super().mousePressEvent(ev)

    # ---- the reader ----
    def _open_row(self, row: 'TheatreRow'):
        if row.more:
            self.open_reader(row.heading, row.more, row)

    def open_reader(self, title: str, body: str, origin: QWidget | None = None):
        d = self.drawer
        d.reader.setHtml(f"<h2>{html.escape(title)}</h2>{body}")
        d.pages.setCurrentWidget(d.reader)
        self._reader_from = origin
        d.reader.setFocus(Qt.FocusReason.TabFocusReason)

    def back(self) -> bool:
        """B on the page: the reader first, then the drawer. True if handled."""
        d = self.drawer
        if self.expanded() and d.reader_open():
            d.show_section(d.tabs.current())
            origin = self._reader_from
            focus_and_reveal(origin if alive(origin) and origin.isVisible()
                             else d.tabs.item(d.tabs.current()))
            return True
        if self.expanded():
            self.collapse()
            return True
        return False

    def hints(self, f) -> list[tuple[str, str]]:
        """The button hints for where focus is on the page."""
        if self.expanded() and self.drawer.reader_open():
            return [('↕', 'Scroll'), ('LB RB', 'Section'), ('b', 'Close')]
        if self.expanded():
            a = ("Watch trailer" if isinstance(f, TheatreShot) else
                 "Read" if isinstance(f, TheatreRow) else
                 self._play_word() if f is self.band_play else "Select")
            return [('a', a), ('LB RB', 'Section'),
                    ('↓', 'More') if f is self.band_play else ('↑', 'Play'),
                    ('b', 'Back to game')]
        return [('a', self._play_word() if f is self.btn_play else "Select"), ('x', 'Options'),
                ('↓', 'More about this game'), ('b', 'Back')]

    def _play_word(self) -> str:
        return "Resume" if self.game.get('playing') else "Play"

    def eventFilter(self, obj, ev):
        # Up from the top of the reader goes back to the tabs (the reader
        # itself scrolls with the D-pad).
        if obj is self.drawer.reader and ev.type() == QEvent.Type.KeyPress \
                and ev.key() == Qt.Key.Key_Up and obj.verticalScrollBar().value() == 0:
            focus_and_reveal(self.drawer.tabs.item(self.drawer.tabs.current()))
            return True
        return False

    # ---- art ----
    def _set_logo(self, path):
        img = fit_image(path, *self.LOGO, cover=False) if path else None    # at shown size
        pm = QPixmap.fromImage(img) if img is not None else QPixmap()
        has_logo = not pm.isNull()
        if has_logo:
            self.logo.setPixmap(pm.scaled(*self.LOGO, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
            self.band_logo.setPixmap(pm.scaled(*self.BAND_LOGO,
                                               Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        name = self.game.get('display_name', '')
        self.logo.setVisible(has_logo)
        self.band_logo.setVisible(has_logo)
        self.title.setText(name)
        self.band_title.setText(name)
        self.title.setVisible(not has_logo)
        self.band_title.setVisible(not has_logo)

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
        for at, a in ((0.0, 0.90), (0.45, 0.45), (0.75, 0.05), (1.0, 0.0)):
            c = QColor(base); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))
        g = QLinearGradient(0.0, r.bottom(), 0.0, r.top())
        for at, a in ((0.0, 1.0), (0.35, 0.55), (0.6, 0.0)):
            c = QColor(base); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))
        if self.expanded():                      # the drawer's up: the art steps back
            c = QColor(base); c.setAlphaF(0.25)
            pt.fillRect(r, c)
        pt.end()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import (anim_ms, ART, art_source, fit_image, HERO_KINDS, LOGO_KINDS,
    screen_pixmap)  # noqa: E402
from .gameinfo import (compat_facts, live_kicker, play_enabled, play_label,  # noqa: E402
    played_minutes, SOURCE_LABEL, TIER_COLORS)
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .widgets import (clear_layout, glyph_pixmap, icon_button, label, plain_label,
    SegmentedControl, set_glyph_icon)  # noqa: E402
