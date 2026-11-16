"""Theatre's game page: one game full screen, from the couch.

The page is a stage (the art, the logo, one quiet line of facts, a big
Play, a round Favourite and Options) with the section tabs along its
bottom edge. D-pad Down (or LB / RB) slides those tabs up into a glass
drawer over the lower half: Overview, Friends (w-82), Media, News,
Achievements and Compatibility, each hidden while it has nothing to show. While the drawer
is open the game shrinks to a band at the top, with Play one Up away.

In the Ribbon layout (w-145) the page opens as a rail instead of the stage:
the logo and a strip of facts, Play, a glass card per section and a small
grid of actions, beside a pane that previews the focused section
(game_rail.py says what goes in them). A on a card opens that section in
the same drawer (B comes back to the card); the session band keeps the
stage."""

import html
import time
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QEvent, QRectF, Qt, QTimer, QVariantAnimation
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from nexus import gamedb
from nexus.features import has

from .common import alive
from .fmt import fmt_duration, fmt_hours, fmt_relative, fmt_size
from .overview import credits_text, overview_facts, overview_model, OverviewTiles, Tile, TierBadge
from .theme import G, LOOK, P, pill, R, SP


SECTIONS = (('overview', "Overview"), ('friends', "Friends"), ('media', "Media"),
            ('news', "News"), ('achievements', "Achievements"), ('compat', "Compatibility"))
FRIENDS_SHOWN = 8               # owners in the drawer's Friends, then "and N more"
PRIVATE_SHOWN = 6


def fact_text(g: dict, friends: bool = False) -> str:
    """The game's one quiet line, as rich text: time played, when, its
    ProtonDB tier and an update waiting. The stage, the drawer's band and
    Home's spotlight all show it instead of a row of pills. `friends` (the
    game page) adds "Ana and Ben playing" while friends are in it."""
    parts = []
    if friends:
        live = [r.get('name') or '' for r in FRIENDS.playing_in(_game_store_id(g))]
        if live:
            parts.append(f"<span style='color:{P['live_on_art']}'>"
                         f"{html.escape(names_text(live))} playing</span>")
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
    if friends and parts and 'playing</span>' in parts[0]:
        parts.append(parts.pop(0))               # last: the news of the moment
    return "&nbsp;&nbsp;·&nbsp;&nbsp;".join(parts)


def fact_tags(g: dict) -> tuple:
    """fact_text's facts as Ribbon tags ((text, role), ...): time played,
    when, its ProtonDB tier in the tier's colour, an update waiting."""
    tags = []
    hours = fmt_hours(played_minutes(g))
    last = fmt_relative(g.get('last_played'))
    if hours:
        tags.append((f"{hours} played", 'subtext'))
    if last:
        tags.append((f"Played {last}", 'subtext'))
    if not hours and not last:
        tags.append(("Not played yet", 'accent'))
    tier = g.get('protondb') if has('proton') else None
    if tier in TIER_COLORS:
        tags.append((f"ProtonDB {tier.capitalize()}", TIER_COLORS[tier]))
    if g.get('update_pending'):
        tags.append(("Update ready", 'warning'))
    return tuple(tags)


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
        self._add('friends', QWidget())              # filled by TheatreDetails._fill_friends
        QVBoxLayout(self.sections['friends']).setContentsMargins(0, 0, 0, 0)
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
        """Overview: About and the credits, Your progress, and under them
        the same stat tiles as the desktop page (w-135), in one row that
        scrolls sideways. The page itself scrolls up and down so a 720p
        screen clips nothing."""
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, SP['3'])           # room for a focused tile's ring
        v.setSpacing(SP['4'])
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(SP['8'] * 3)
        left = QVBoxLayout()
        left.setSpacing(SP['4'])
        self.about = label("", "TheatreAbout", wrap=True)
        self.credits = label("", "TheatreSub", wrap=True)
        for lab in (self.about, self.credits):
            left.addWidget(lab)
        left.addStretch()
        top.addLayout(left, 3)
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
        top.addLayout(right, 2)
        v.addLayout(top)
        self.tiles = OverviewTiles(theatre=True, scale=THEATRE_SCALE)
        self.tiles_scroll = QScrollArea()
        self.tiles_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tiles_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tiles_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tiles_scroll.setWidgetResizable(True)
        self.tiles_scroll.setWidget(self.tiles)          # added before hidden: no stray window
        v.addWidget(self.tiles_scroll)
        self.tiles_scroll.hide()
        v.addStretch()
        self.overview_scroll = QScrollArea()
        self.overview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.overview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.overview_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.overview_scroll.setWidgetResizable(True)
        self.overview_scroll.setWidget(body)
        self._add('overview', self.overview_scroll)

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


class RailCard(QPushButton):
    """A glass card on the game rail (w-145): glyph, title, and a count on
    the right; `small` for the action grid's cells."""

    def __init__(self, card, h: int, small: bool = False):
        super().__init__()
        self.card = card
        self.setObjectName("RailCard")
        self.setProperty('small', small)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(h)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'] if not small else SP['4'], 0, SP['5'], 0)
        lay.setSpacing(SP['4'] if not small else SP['3'])
        self.icon = QLabel()
        self.icon.setFixedWidth(30 if not small else 26)
        lay.addWidget(self.icon)
        self.title = plain_label("", "RailCardTitle")
        lay.addWidget(self.title, 1)
        self.count = plain_label("", "RailCardCount")
        lay.addWidget(self.count)
        self.small = small
        self.set_card(card)

    def set_card(self, card):
        self.card = card
        self.setAccessibleName(f"{card.title}, {card.count}" if card.count else card.title)
        self.title.setText(card.title)
        self.count.setText(card.count)
        self.count.setVisible(bool(card.count))
        role = 'danger' if card.danger else 'warning' if card.checked else 'on_art'
        self.icon.setPixmap(glyph_pixmap(G[card.glyph], P[role], 24 if not self.small else 20))
        if self.property('danger') != card.danger:
            self.setProperty('danger', card.danger)
            self.style().unpolish(self)
            self.style().polish(self)


class RailPreview(QFrame):
    """The rail's preview pane (w-145): what the focused section holds,
    without pressing A (game_rail.preview)."""

    def __init__(self):
        super().__init__()
        self.setObjectName("RailPreview")
        v = QVBoxLayout(self)
        v.setContentsMargins(SP['8'], SP['8'], SP['8'], SP['6'])
        v.setSpacing(SP['4'])
        self.kicker = label("", "RailLabel")
        self.big = plain_label("", "RailBig")
        self.big.setWordWrap(True)
        v.addWidget(self.kicker)
        v.addWidget(self.big)
        self.bar_row = QWidget()
        h = QHBoxLayout(self.bar_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP['4'])
        self.bar = _bar()
        self.bar.setFixedWidth(420)
        self.bar_words = plain_label("", "RailLine")
        h.addWidget(self.bar)
        h.addWidget(self.bar_words, 1)
        v.addWidget(self.bar_row)
        self.line = plain_label("", "RailLine")
        self.line.setWordWrap(True)
        v.addWidget(self.line)
        self.rule = QFrame()
        self.rule.setObjectName("RailRule")
        v.addWidget(self.rule)
        self.about_label = label("ABOUT", "RailLabel")
        self.about = plain_label("", "RailAbout")
        self.about.setWordWrap(True)
        v.addWidget(self.about_label)
        v.addWidget(self.about)
        v.addStretch()
        self.hint = plain_label("", "RailHint")
        v.addWidget(self.hint)
        self.text = None

    def set_text(self, pv):
        self.text = pv
        self.kicker.setText(pv.kicker)
        self.big.setText(pv.big)
        self.bar_row.setVisible(pv.progress is not None)
        if pv.progress is not None:
            _set_bar(self.bar, pv.progress[0])
            self.bar_words.setText(pv.progress[1])
        self.line.setText(pv.line)
        self.line.setVisible(bool(pv.line))
        for w in (self.rule, self.about_label, self.about):
            w.setVisible(bool(pv.about))             # no data: no About block
        self.about.setText(pv.about)
        self.hint.setText(pv.hint)
        self.hint.setVisible(bool(pv.hint))


class SessionBand(QWidget):
    """After a game closes (w-60): what the session was, in place of the
    facts and the Play row. Session length, total play time and, when there
    are any, the achievements unlocked, with Play again and Back to Home."""
    CHIP_MIN_HEIGHT = 900            # chips need a taller page than 720p
    MAX_CHIPS = 3

    def __init__(self):
        super().__init__()
        self.setObjectName("SessionBand")
        self.items: list[dict] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SP['6'])
        self.stats = QHBoxLayout()
        self.stats.setSpacing(SP['8'] * 2)
        self._values: dict[str, QLabel] = {}
        for key, cap in (('session', "THIS SESSION"), ('total', "TOTAL PLAYED"),
                         ('ach', "ACHIEVEMENTS")):
            col = QVBoxLayout()
            col.setSpacing(SP['1'])
            val = label("", "SessionBig")
            self._values[key] = val
            col.addWidget(val)
            col.addWidget(label(cap, "SessionUnit"))
            holder = QWidget()
            holder.setLayout(col)
            self._values[key + '_box'] = holder
            self.stats.addWidget(holder)
        self.stats.addStretch()
        v.addLayout(self.stats)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(SP['4'])
        v.addLayout(self.chips)
        row = QHBoxLayout()
        row.setSpacing(SP['5'])
        self.again = QPushButton("Play again")
        self.again.setObjectName("PlayBig")
        set_glyph_icon(self.again, G['play'], int(96 * 0.36), 'on_accent', hover_role=None)
        self.again.setFixedSize(420, 96)
        self.home = QPushButton("Back to Home")
        self.home.setObjectName("Glass")
        set_glyph_icon(self.home, G['home'], 28, 'on_art')
        self.home.setFixedSize(300, 96)
        row.addWidget(self.again)
        row.addWidget(self.home)
        row.addStretch()
        v.addLayout(row)
        self.set_stats("", "", 0)

    def set_stats(self, session: str, total: str, n: int):
        self._values['session'].setText(session)
        self._values['total'].setText(total)
        self._values['ach'].setText(str(n))
        self._values['ach_box'].setVisible(n > 0)     # a count only when there is one

    def set_achievements(self, items: list[dict], tall: bool):
        self.items = items[:]
        self.set_stats(self._values['session'].text(), self._values['total'].text(),
                       len(items))
        clear_layout(self.chips)
        if tall:
            for a in items[:self.MAX_CHIPS]:
                self.chips.addWidget(self._chip(a))
        self.chips.addStretch()

    def _chip(self, a: dict) -> QFrame:
        c = QFrame()
        c.setObjectName("AchChip")
        h = QHBoxLayout(c)
        h.setContentsMargins(SP['4'], SP['3'], SP['5'], SP['3'])
        h.setSpacing(SP['3'])
        h.addWidget(icon_label(G['trophy'], 26, P['warning']))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(plain_label(a.get('title') or "Achievement", "AchTitle"))
        if a.get('desc'):
            col.addWidget(plain_label(a['desc'], "TheatreSub"))
        h.addLayout(col)
        return c


class TheatreDetails(QWidget):
    """A game, full screen: the stage, and the drawer below it."""
    play = pyqtSignal(dict)
    action = pyqtSignal(str, dict)
    back_asked = pyqtSignal()                    # the (mouse-only) ‹ in the corner
    trailer = pyqtSignal(dict)
    home_asked = pyqtSignal()                    # the session band's Back to Home
    group_asked = pyqtSignal(dict, str)          # the rail's Collections / Maintenance: the sheet, that group

    LOGO = (760, 280)
    BAND_LOGO = (420, 150)
    RAIL_LOGO = (360, 150)
    RAIL_W = 460                                 # the rail's column (Play, the cards, the grid)

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
        self._session: dict | None = None        # the band's text while it shows
        self._shown_at = 0.0
        self._rail_on = False                    # the Ribbon layout: the rail, not the stage (w-145)
        self._rail_from = None                   # the rail card that opened the drawer
        self._rail_cards: dict = {}              # key -> RailCard (kept: pad focus survives a refill)
        self._rail_grid: dict = {}
        self._preview_key = 'overview'
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
        self.actions = QWidget()                 # Play, Favourite, Options: hides as one
        row = QHBoxLayout(self.actions)
        row.setContentsMargins(0, 0, 0, 0)
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
        sv.addWidget(self.actions)
        self.session = SessionBand()
        self.session.again.clicked.connect(lambda: self._band_clicked(self._play))
        self.session.home.clicked.connect(lambda: self._band_clicked(self.home_asked.emit))
        sv.addWidget(self.session)
        self.session.hide()
        sv.addStretch(1)
        lay.addWidget(self.stage, 1)
        # The Ribbon layout's rail (w-145), in the stage's place.
        self.rail = self._build_rail(m)
        lay.addWidget(self.rail, 1)
        self.rail.hide()
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

    def _build_rail(self, m: int) -> QWidget:
        """The rail: the logo (or the title), the kicker and a strip of
        facts; under them Play, the section cards and the action grid on the
        left, the preview pane on the right."""
        w = QWidget()
        w.setObjectName("GameRail")
        v = QVBoxLayout(w)
        v.setContentsMargins(m, 0, m, SP['6'])
        v.setSpacing(0)
        v.addStretch(1)
        self.rail_logo = QLabel()
        self.rail_logo.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        v.addWidget(self.rail_logo)
        self.rail_title = label("", "HeroTitle", wrap=True)
        self.rail_title.setStyleSheet("font-size: 48px;")
        v.addWidget(self.rail_title)
        v.addSpacing(SP['5'])
        self.rail_kicker = label("", "RailKicker")
        self.rail_kicker.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.rail_kicker)
        v.addSpacing(SP['3'])
        self.rail_strip = TagStrip(THEATRE_SCALE)
        v.addWidget(self.rail_strip)
        v.addSpacing(SP['6'])
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SP['8'] + SP['3'])
        left = QVBoxLayout()
        left.setSpacing(SP['2'])
        self.rail_play = self._play_button(self.RAIL_W, 76)
        left.addWidget(self.rail_play)
        left.addSpacing(SP['1'])
        self.rail_cards = QVBoxLayout()
        self.rail_cards.setSpacing(SP['2'])
        left.addLayout(self.rail_cards)
        left.addSpacing(SP['3'])
        self.rail_grid_lay = QGridLayout()
        self.rail_grid_lay.setHorizontalSpacing(SP['2'])
        self.rail_grid_lay.setVerticalSpacing(SP['2'])
        left.addLayout(self.rail_grid_lay)
        left.addStretch()
        row.addLayout(left)
        self.preview = RailPreview()
        self.preview.setMaximumWidth(900)
        row.addWidget(self.preview, 1)
        row.addStretch(0)
        v.addLayout(row)
        return w

    def _rail_k(self) -> float:
        """The rail's sizes follow the page height: 1 at 1080 p, less on a
        720 p screen so every card fits."""
        return max(0.62, min(1.0, (self.height() or 1080) / 1080))

    def _rail_sizes(self):
        k = self._rail_k()
        self.rail_play.setFixedSize(self.RAIL_W, int(76 * k))
        for c in self._rail_cards.values():
            c.setFixedHeight(int(52 * k))
        for c in self._rail_grid.values():
            c.setFixedSize((self.RAIL_W - SP['2']) // 2, int(48 * k))

    # ---- the rail (the Ribbon layout, w-145) ----
    def set_rail(self, on: bool):
        """The Ribbon layout's page: the rail instead of the stage."""
        if on == self._rail_on:
            return
        self._rail_on = on
        self._rail_from = None
        self._present()
        if on:
            self._sync_rail()
        self.update()

    def rail_shown(self) -> bool:
        """The rail is on screen (the Ribbon layout, no drawer, no session band)."""
        return self._rail_on and self._session is None and not self.expanded()

    def _present(self):
        """The stage or the rail, as the layout and the session band say.
        Focus moves first, then the other one hides (w-60)."""
        if self.expanded():
            return
        rail = self._rail_on and self._session is None
        show, hide = (self.rail, self.stage) if rail else (self.stage, self.rail)
        show.show()
        f = QApplication.focusWidget()
        if f is not None and (hide.isAncestorOf(f) or (rail and self.drawer.isAncestorOf(f))):
            t = self.default_focus()
            if t is not None and t.isVisible():
                t.setFocus(Qt.FocusReason.OtherFocusReason)
        hide.hide()
        self.drawer.setVisible(not rail)        # the rail's cards are the sections

    def _game_info(self) -> 'GameInfo':
        g = self.game
        appid = _game_store_id(g) if g else None
        known = appid is not None or _store_key(g) is not None
        owners = FRIENDS.owners_of(g) if known and FRIENDS.usable() else []
        return GameInfo(overview_facts(g, self.store, self.tips), self.store, self.tips,
                        self.news, owners, self._achievements())

    def _sync_rail(self):
        """The rail follows what the page knows: cards and grid kept by key
        (a late answer never drops pad focus), the strip, the kicker, the
        preview of the focused section."""
        if not self._rail_on or not self.game:
            return
        g, info = self.game, self._game_info()
        cards, grid = rail(self.drawer.visible_keys(), info, game_actions(g, theatre=True), g)
        k = self._rail_k()
        self._refill(self.rail_cards, self._rail_cards, cards,
                     lambda c: RailCard(c, int(52 * k)), None)
        self._refill(self.rail_grid_lay, self._rail_grid, grid,
                     lambda c: RailCard(c, int(48 * k), small=True), 2)
        self._rail_sizes()
        kicker = live_kicker(g) if g.get('playing') or g.get('launching') else html.escape(
            game_kicker(g, info.rec, SOURCE_LABEL.get(g.get('source', 'nexus'), '')))
        self.rail_kicker.setText(kicker)
        self.rail_kicker.setVisible(bool(kicker))
        added = fmt_relative(g.get('added_at') or g.get('installed_at'))
        size = fmt_size(g.get('disk_bytes'))
        extra = tuple((t, 'subtext') for t in (size, added and f"Installed {added}") if t)
        tags = fact_tags(g)
        self.rail_strip.set_tags(tags[:2] + extra + tags[2:])
        if self._preview_key not in self._rail_cards:
            self._preview_key = next(iter(self._rail_cards), 'overview')
        self.preview.set_text(preview(self._preview_key, g, info))

    def _refill(self, lay, have: dict, cards: list, make, cols):
        """Cards into `lay` in order: the same key keeps its widget (updated
        in place), a gone one leaves (focus first goes to Play), a new one
        is made. `cols`: a grid of that many columns."""
        keys = [c.key for c in cards]
        f = QApplication.focusWidget()
        for key in [k for k in have if k not in keys]:
            w = have.pop(key)
            if f is w:
                focus_and_reveal(self.default_focus())
            lay.removeWidget(w)
            w.hide()
            w.deleteLater()
        for i, c in enumerate(cards):
            w = have.get(c.key)
            if w is None:
                w = make(c)
                w.clicked.connect(lambda _=False, b=w: self._rail_pressed(b))
                have[c.key] = w
            else:
                w.set_card(c)
                lay.removeWidget(w)
            if cols:
                lay.addWidget(w, i // cols, i % cols)
            else:
                lay.addWidget(w)
            w.show()                             # added first, then shown (w-54, w-57)
        if not cols and keys != list(have):
            ordered = {k: have[k] for k in keys}
            have.clear()
            have.update(ordered)

    def _rail_pressed(self, card: 'RailCard'):
        """A on a rail card: a section opens in the drawer (B comes back to
        the card); Favourite toggles; Collections and Maintenance open the
        options sheet on that group; Uninstall asks as the sheet does."""
        c = card.card
        if c.key in SECTION_CARDS:
            self.open_section(c.key, card)
        elif c.key in ('collections', 'maintenance'):
            self.group_asked.emit(self.game, "Collections" if c.key == 'collections'
                                  else "Maintenance")
        elif c.action:
            self.action.emit(c.action, self.game)

    def open_section(self, key: str, origin=None):
        """The rail's layer 3: today's drawer, open on `key`."""
        self._rail_from = origin
        self.drawer.show_section(key)
        self.expand()

    def _play_button(self, w, h) -> QPushButton:
        b = QPushButton("Play")
        b.setObjectName("PlayBig")
        set_glyph_icon(b, G['play'], int(h * 0.36), 'on_accent', hover_role=None)
        b.setFixedSize(w, h)
        b.clicked.connect(self._play)
        return b

    def _play(self):
        self.clear_session()
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
        self.facts.setText(fact_text(g, friends=True))
        self.band_facts.setText(fact_text(g, friends=True))
        self.set_play(g)
        fav = bool(g.get('favorite'))
        self.btn_fav.setText(G['star'] if fav else G['star_outline'])
        self.btn_fav.setToolTip("Favourite" if fav else "Add to favourites")
        r = pill(self.btn_fav.height())
        self.btn_fav.setStyleSheet(f"border-radius: {r}px; color: {P['warning']};" if fav
                                   else f"border-radius: {r}px;")
        if not same:
            self.clear_session()
            self.tips, self.launch, self.store, self.news = None, None, None, []
            self.collapse(instant=True, refocus=False)
            self._rail_from = None
            self._preview_key = 'overview'
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
        if self._session is not None and not live:
            self.kicker.setText(html.escape(f"SESSION ENDED · {self._session['ended']}"))
            if self.kicker.property('live'):
                self.kicker.setProperty('live', False)
                self.kicker.style().unpolish(self.kicker)
                self.kicker.style().polish(self.kicker)
            return
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
        for b in (self.btn_play, self.band_play, self.rail_play):
            b.setText("Resume" if g.get('playing') else play_label(g))
            b.setEnabled(bool(g.get('playing')) or play_enabled(g))
        self.session.again.setEnabled(bool(g.get('playing')) or play_enabled(g))

    def default_focus(self):
        if self.drawer.is_open():
            return self.drawer.tabs.item(self.drawer.tabs.current())
        if self._session is not None:
            return self.session.again if self.session.again.isEnabled() else self.session.home
        if self._rail_on:
            if self.rail_play.isEnabled():
                return self.rail_play
            return next(iter(self._rail_cards.values()), None) or self.rail_play
        return self.btn_play if self.btn_play.isEnabled() else self.btn_fav

    # ---- the session band (w-60) ----
    A_GUARD = 1.0            # seconds A is ignored: the press that ended the game

    def show_session(self, summary: dict):
        """Replace the facts and the Play row with the band. `summary` has
        'ended' (HH:MM), 'session' and 'total' (text)."""
        self._session = dict(summary)
        self._shown_at = time.monotonic()
        self.session.set_stats(summary['session'], summary['total'], 0)
        self.session.set_achievements([], False)
        self.session.show()
        self.set_kicker(self.game)
        # Focus first: hiding the focused Play would throw it into the drawer.
        f = QApplication.focusWidget()
        if f is not None and self.actions.isAncestorOf(f):
            self.default_focus().setFocus()
        self.facts.hide()
        self.actions.hide()
        self._present()                          # the band keeps the stage (w-145)
        QTimer.singleShot(0, self._focus_default)

    def set_session_achievements(self, items: list[dict]):
        if self._session is not None:
            self.session.set_achievements(items, self.height() >= SessionBand.CHIP_MIN_HEIGHT)

    def clear_session(self):
        if self._session is None:
            return
        f = QApplication.focusWidget()
        had_focus = f is not None and self.session.isAncestorOf(f)
        self._session = None
        self.facts.show()
        self.actions.show()
        if had_focus:                    # hiding the focused button would throw focus into the drawer
            self.default_focus().setFocus()
        self.session.hide()
        self._present()                  # the Ribbon layout: back to the rail
        self.set_kicker(self.game)

    def session_shown(self) -> bool:
        return self._session is not None

    def _band_clicked(self, act):
        """A band button, unless the press is the one that closed the game."""
        if time.monotonic() - self._shown_at >= self.A_GUARD:
            act()

    def hideEvent(self, ev):
        if not ev.spontaneous():
            self.clear_session()
        super().hideEvent(ev)

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
            self._fill_friends()
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
        self._sync_rail()

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
        rec = overview_facts(g, self.store, self.tips)       # the desktop's facts
        about = (rec.get('description') or st.get('blurb') or '').strip()
        d.about.setText(about)
        d.about.setVisible(bool(about))
        credits = credits_text(rec)[0]
        d.credits.setText(credits)
        d.credits.setVisible(bool(credits))
        # The stat tiles, from the model the desktop page uses. Unchanged
        # tiles keep their widgets, so a late answer does not drop pad focus.
        d.tiles_scroll.setVisible(d.tiles.set_sections(overview_model(
            rec, self.store, self.tips, g, linux=has('proton'))))
        d.tiles_scroll.setFixedHeight(d.tiles.row_height())
        # Your progress: Game-db's completion, How long to beat, the hours.
        gd = (self.tips or {}).get('gamedb') or {}
        hl = rec.get('length') or {}
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

    def _fill_friends(self):
        """Friends (w-82): who owns it (a 2-column grid, playing now
        first, at most 8 then "and N more") and, on the right, the friends
        whose game details are private. Nothing in it is a focus stop, as
        in Overview. Hidden without friends data, or for a game with neither
        a Steam app id nor (Game-db friends, w-84) a store id."""
        g = self.game
        appid = _game_store_id(g) if g else None
        known = appid is not None or _store_key(g) is not None
        owners = FRIENDS.owners_of(g) if known else []
        private = FRIENDS.private() if known else []
        live = {r['steamid']: r for r in FRIENDS.playing_in(appid)}
        owners.sort(key=lambda r: r['steamid'] not in live)      # stable: minutes after
        now = time.time()
        minutes = {sid: max(0, int((now - (_ts((r.get('playing') or {}).get('since'))
                                           or now)) // 60)) for sid, r in live.items()}
        played = {r['steamid']: FRIENDS.minutes(r, g) for r in owners}
        sig = (appid, _store_key(g), tuple((r['steamid'], r.get('name'), r.get('avatar'),
                                            played[r['steamid']], minutes.get(r['steamid']))
                                           for r in owners),
               tuple((r['steamid'], r.get('name'), r.get('avatar')) for r in private))
        show = FRIENDS.usable() and known and bool(owners or private)
        self.drawer.set_section_visible('friends', show)
        if sig == getattr(self, '_friends_sig', None):
            return
        self._friends_sig = sig
        lay = self._rows('friends')
        if not show:
            return
        body = QWidget()
        h = QHBoxLayout(body)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP['8'] * 2)
        if owners:
            left = QVBoxLayout()
            left.setSpacing(SP['3'])
            n = len(owners)
            left.addWidget(label(f"{n} FRIEND{'S' if n != 1 else ''} OWN{'' if n != 1 else 'S'} "
                                 "THIS", "TheatreLabel"))
            grid = QGridLayout()
            grid.setHorizontalSpacing(SP['5'])
            grid.setVerticalSpacing(SP['3'])
            for i, r in enumerate(owners[:FRIENDS_SHOWN]):
                grid.addWidget(self._friend_cell(r, played[r['steamid']],
                                                 minutes.get(r['steamid'])),
                               i // 2, i % 2)
            left.addLayout(grid)
            if n > FRIENDS_SHOWN:
                left.addWidget(label(f"and {n - FRIENDS_SHOWN} more", "TheatreSub"))
            left.addStretch()
            h.addLayout(left, 3)
        if private:
            right = QVBoxLayout()
            right.setSpacing(SP['3'])
            right.addWidget(label("PRIVATE ON STEAM", "TheatreLabel"))
            for r in private[:PRIVATE_SHOWN]:
                row = QHBoxLayout()
                row.setSpacing(SP['3'])
                row.addWidget(Avatar(r, 44, dim=True))
                name = plain_label(r.get('name') or r.get('steamid') or '?', "TheatreSub")
                name.setProperty('dim', True)
                row.addWidget(name, 1)
                right.addLayout(row)
            if len(private) > PRIVATE_SHOWN:
                right.addWidget(label(f"and {len(private) - PRIVATE_SHOWN} more",
                                      "TheatreSub"))
            note = label("Nexus can't see their games. Steam shows game details to apps "
                         "only when a friend sets them to Public.", "TheatreSub", wrap=True)
            note.setProperty('dim', True)
            right.addWidget(note)
            right.addStretch()
            h.addLayout(right, 2)
        if not owners:
            h.addStretch(3)
        lay.addWidget(body)
        lay.addStretch()

    @staticmethod
    def _friend_cell(r: dict, played, live_minutes) -> QFrame:
        """One owner: avatar, name, "Playing now · 1 h 20 min" or "41 h
        played". Display only."""
        cell = QFrame()
        cell.setObjectName("TheatreRow")
        cell.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cl = QHBoxLayout(cell)
        cl.setContentsMargins(SP['3'], SP['2'], SP['5'], SP['2'])
        cl.setSpacing(SP['3'])
        cl.addWidget(Avatar(r, 56, live=live_minutes is not None))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(plain_label(r.get('name') or r.get('steamid') or '?', "TheatreMeta"))
        if live_minutes is not None:
            sub = plain_label(f"Playing now · {fmt_duration(live_minutes)}", "TheatreSub")
            sub.setProperty('live', True)
        else:
            hours = fmt_hours(played)
            sub = plain_label(f"{hours} played" if hours else "Not played yet", "TheatreSub")
        col.addWidget(sub)
        cl.addLayout(col, 1)
        return cell

    def refresh_friends(self):
        """New friends data, or presence went stale: the Friends section
        and the fact line follow (nothing rebuilt when nothing changed)."""
        if not self.game:
            return
        self.facts.setText(fact_text(self.game, friends=True))
        self.band_facts.setText(fact_text(self.game, friends=True))
        self._fill_friends()
        d = self.drawer
        if not d.reader_open():
            d.show_section(d.tabs.current())
        self._sync_rail()

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
            if i == 0 and big and tier:                  # the tier, with its badge
                badge = TierBadge(self.drawer.sections['compat'], THEATRE_SCALE, 'on_art')
                badge.set_tier(tier, text)
                row = QHBoxLayout()
                row.setSpacing(SP['4'])
                row.addWidget(badge)
                row.addWidget(lab, 1)
                lay.addLayout(row)
                continue
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
        self.clear_session()
        self.band.show()
        self.gap.show()
        self.drawer.show()                       # the rail hides it while closed
        self.drawer.set_open(True)
        f = QApplication.focusWidget()
        if f is None or not (self.drawer.isAncestorOf(f) or f is self.band_play):
            focus_and_reveal(self.default_focus())   # first, then hide what had it (w-60)
        self.top.hide()
        self.stage.hide()
        self.rail.hide()
        self._slide_to(self._open_h())
        self.update()

    def collapse(self, instant: bool = False, refocus: bool = True):
        """Back to the stage (the reader closes with it)."""
        d = self.drawer
        if d.reader_open():
            d.show_section(d.tabs.current())
        if not self.expanded():
            return
        rail = self._rail_on and self._session is None
        self.top.show()
        (self.rail if rail else self.stage).show()
        if rail:                                 # back to the card that opened it, first (w-60)
            back = self._rail_from if alive(self._rail_from) and \
                self._rail_from.isVisible() else None
            self._rail_from = None
            f = QApplication.focusWidget()
            if refocus and not instant or (f is not None and self.isAncestorOf(f)):
                t = back or self.default_focus()
                if t is not None:
                    focus_and_reveal(t)
        d.set_open(False)
        self.band.hide()
        self.gap.hide()
        self._slide.stop()
        d.setMinimumHeight(0)
        d.setMaximumHeight(16777215)
        if rail:
            d.hide()
        self.update()
        if refocus and not instant and not rail:
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
        self._rail_sizes()

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
        elif isinstance(new, RailCard) and self.rail.isAncestorOf(new) \
                and new.card.key in SECTION_CARDS and new.card.key != self._preview_key:
            self._preview_key = new.card.key     # the pane previews the focused section
            self.preview.set_text(preview(new.card.key, self.game, self._game_info()))

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
        if self.expanded() and isinstance(f, Tile):      # a stat tile: nothing to press
            return [('↔', 'Tiles'), ('LB RB', 'Section'), ('b', 'Back to game')]
        if self.expanded():
            a = ("Watch trailer" if isinstance(f, TheatreShot) else
                 "Read" if isinstance(f, TheatreRow) else
                 self._play_word() if f is self.band_play else "Select")
            return [('a', a), ('LB RB', 'Section'),
                    ('↓', 'More') if f is self.band_play else ('↑', 'Play'),
                    ('b', 'Back to game')]
        if self._session is not None:
            return [('a', "Play again" if f is self.session.again else "Select"),
                    ('x', 'Options'), ('↓', 'More about this game'), ('b', 'Home')]
        if self._rail_on:
            return [('a', self._play_word() if f is self.rail_play else "Open"),
                    ('↕', 'Choose'), ('b', 'Back')]
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
            self.rail_logo.setPixmap(pm.scaled(*self.RAIL_LOGO, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
            self.band_logo.setPixmap(pm.scaled(*self.BAND_LOGO,
                                               Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        name = self.game.get('display_name', '')
        self.logo.setVisible(has_logo)
        self.band_logo.setVisible(has_logo)
        self.rail_logo.setVisible(has_logo)
        self.title.setText(name)
        self.band_title.setText(name)
        self.rail_title.setText(name)
        self.title.setVisible(not has_logo)
        self.band_title.setVisible(not has_logo)
        self.rail_title.setVisible(not has_logo)

    def _set_hero(self, path):
        self._pm = screen_pixmap(path, self)
        self._scaled = None
        self.update()

    # The rail's art (w-145): full bleed, faded to the page colour from the
    # left (stops: share of the width, alpha) and from the bottom; on a light
    # theme it shows at a third of its strength, under High contrast not at all.
    RAIL_FADE = ((0.0, 1.0), (0.22, 0.88), (0.48, 0.35), (0.85, 0.0))
    RAIL_BOTTOM = 0.30
    RAIL_ART = {'dark': 0.95, 'light': 0.35}

    def _paint_rail(self, pt, r: QRectF):
        if not LOOK['contrast'] and self._pm is not None and not self._pm.isNull():
            pt.setOpacity(self.RAIL_ART['light' if LOOK['light'] else 'dark'])
            self._draw_hero(pt)
            pt.setOpacity(1.0)
        base = QColor(P['bg'])
        g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
        for at, a in self.RAIL_FADE:
            c = QColor(base); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))
        g = QLinearGradient(0.0, r.bottom(), 0.0, r.top())
        for at, a in ((0.0, 1.0), (self.RAIL_BOTTOM, 0.0)):
            c = QColor(base); c.setAlphaF(a); g.setColorAt(at, c)
        pt.fillRect(r, QBrush(g))

    def _draw_hero(self, pt):
        key = (self.width(), self.height())
        if self._scaled is None or self._scaled[0] != key:
            self._scaled = (key, self._pm.scaled(
                self.width(), self.height(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation))
        s = self._scaled[1]
        pt.drawPixmap((self.width() - s.width()) // 2, -(s.height() - self.height()) // 3, s)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        pt.fillRect(r, QColor(P['bg']))
        if self.rail_shown():
            self._paint_rail(pt, r)
            pt.end()
            return
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
from .actions import game_actions  # noqa: E402
from .game_rail import GameInfo, preview, rail, SECTION_CARDS  # noqa: E402
from .ribbon import TagStrip  # noqa: E402
from .ribbon_model import game_kicker  # noqa: E402
from .gameinfo import (compat_facts, live_kicker, play_enabled, play_label,  # noqa: E402
    played_minutes, SOURCE_LABEL, TIER_COLORS)
from .friends import _game_store_id, _store_key, _ts, Avatar, FRIENDS, names_text  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .theatre import THEATRE_SCALE  # noqa: E402
from .widgets import (clear_layout, glyph_pixmap, icon_button, icon_label, label, plain_label,
    SegmentedControl, set_glyph_icon)  # noqa: E402
