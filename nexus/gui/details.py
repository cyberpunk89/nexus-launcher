"""The game page (details panel), docked beside the grid."""

import html
import os
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QPoint, QPointF, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QLinearGradient, QPainter, QPixmap,
    QRadialGradient, QTextLayout)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from nexus import facts, gamedb
from nexus.features import has

from .common import alive, HOME, MAX_NOTE
from .fmt import fmt_ago, fmt_hours, fmt_relative, fmt_size
from .theme import FS, G, LOOK, P, R, SP


def _hero_art(game: dict) -> str | None:
    return art_source(game, HERO_KINDS)[0]


def art_tint(pm: QPixmap) -> QColor:
    """The art's average colour."""
    return pm.toImage().scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)


def wash_color(c: QColor) -> QColor:
    """An art colour made richer, and dark (on a light theme, light) enough
    to sit behind text. Worked out at paint time, so a theme change applies."""
    h, s, v, _a = c.getHsvF()
    v = max(v, 0.85) if LOOK['light'] else min(v, 0.55)
    return QColor.fromHsvF(max(h, 0.0), min(1.0, s * 1.2), v)


class TintedPage(QWidget):
    """The details page: a soft wash of the hero art's colour behind its top,
    fading into the panel's background. Follows Appearance: Background glow
    sets its strength, and "Tint the glow with game art" off turns it off."""
    REACH = 520                                # px the wash fades over

    def __init__(self):
        super().__init__()
        self._tint = None

    def set_tint(self, color):
        self._tint = color
        self.update()

    def paintEvent(self, ev):
        strength = 0.32 * LOOK['glow'] if LOOK['art_tint'] else 0.0
        if self._tint is None or strength <= 0:
            return
        pt = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.REACH)
        c0, c1 = wash_color(self._tint), wash_color(self._tint)
        c0.setAlphaF(min(0.6, strength))
        c1.setAlphaF(0.0)
        g.setColorAt(0.0, c0)
        g.setColorAt(1.0, c1)
        pt.fillRect(0, 0, self.width(), self.REACH, QBrush(g))
        pt.end()


class HeroBanner(QWidget):
    """Wide art strip across the top of the panel. The art fades out at the
    bottom and edges into whatever is behind it (the panel, tinted with the
    art's own colour by TintedPage), so it blends in instead of sitting in a
    card.

    Art and logo are decoded at about the size they're shown at, not full
    size: a 3840x1240 SteamGridDB hero cost ~30 ms on every selection (too
    big for Qt's pixmap cache, so even re-showing a game decoded it again).
    Big files decode on a worker; small ones (most) stay synchronous so the
    banner doesn't flash a placeholder; the last few stay decoded."""
    logo_changed = pyqtSignal(bool)            # a logo is (not) shown now
    tint_changed = pyqtSignal(object)          # the art's average QColor, or None

    DECODE_W = 1600                            # wider than any details panel
    SYNC_BYTES = 1_500_000                     # smaller decodes faster than a frame
    _RECENT: dict[tuple, QPixmap] = {}         # shared by every banner, oldest first
    RECENT_MAX = 8

    def __init__(self):
        super().__init__()
        self.setFixedHeight(200)
        self._pm = None
        self._logo = None
        self._name = ''
        self._waiting = None                   # the hero URL being downloaded for us
        self._keys = {'art': None, 'logo': None}   # (path, mtime, size) shown or on its way
        self._logo_pending = False
        # The art and logo scaled to the current size: smooth-scaling on
        # every repaint made moving through games stutter.
        self._frames: dict[str, tuple] = {}

    def has_logo(self) -> bool:
        """A logo is shown, or still decoding (so the name stays hidden)."""
        return self._logo is not None or self._logo_pending

    def _box(self, which: str) -> tuple[int, int]:
        dpr = self.devicePixelRatioF()
        w = max(self.DECODE_W, round(self.width() * dpr))
        if which == 'art':
            return w, round(self.height() * dpr)
        return round(w * 0.62), round(self.height() * 0.42 * dpr)

    def _want(self, which: str, path: str | None):
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        key = (path, st.st_mtime_ns, st.st_size) if st else None
        if key == self._keys[which]:
            return                              # already shown (or decoding)
        self._keys[which] = key
        if key is None:
            self._set(which, None)
            return
        w, h = self._box(which)
        cache = (which, key, w, h)
        if cache in self._RECENT:
            self._RECENT[cache] = self._RECENT.pop(cache)       # most recent last
            self._set(which, self._RECENT[cache])
        elif key[2] <= self.SYNC_BYTES:
            self._decoded(which, key, cache, fit_image(path, w, h, which == 'art'))
        else:
            self._set(which, None, pending=True)   # placeholder until it's decoded
            in_thread(lambda: fit_image(path, w, h, which == 'art'),
                      lambda img, _e: self._decoded(which, key, cache, img))

    def _decoded(self, which, key, cache, img):
        pm = QPixmap.fromImage(img) if img is not None else None
        if pm is not None:                      # kept even if we've moved on: going
            self._RECENT[cache] = pm            # back is then instant
            while len(self._RECENT) > self.RECENT_MAX:
                self._RECENT.pop(next(iter(self._RECENT)))
        try:
            if key != self._keys[which]:
                return                          # moved on to another game meanwhile
            self._set(which, pm)
        except RuntimeError:                    # the banner is gone
            pass

    def _set(self, which, pm, pending=False):
        if which == 'art':
            self._pm = pm
            self._frames.pop('faded', None)
            self.tint_changed.emit(art_tint(pm) if pm is not None else None)
        else:
            self._logo, self._logo_pending = pm, pending
            self.logo_changed.emit(self.has_logo())
        self._frames.pop(which, None)
        self.update()

    def set_game(self, game: dict):
        self._name = game.get('display_name', '')
        logo, _url = art_source(game, LOGO_KINDS)
        self._want('logo', logo)
        path = _hero_art(game)
        self._waiting = None
        if not path and game.get('hero_url'):
            self._waiting = game['hero_url']
            ART.fetch(self._waiting, lambda p, u=self._waiting: self._arrived(u, p))
        if not path and game.get('cover_url'):
            path = ART.cached(game['cover_url'])
        self._load(path)

    def _arrived(self, url, path):
        try:
            if url == self._waiting:            # not a game we've since moved away from
                self._waiting = None
                self._load(path)
        except RuntimeError:
            pass

    def _load(self, path):
        self._want('art', path)

    def _scaled(self, which: str, pm: QPixmap, w: int, h: int, mode) -> QPixmap:
        hit = self._frames.get(which)
        if hit is None or hit[0] != (w, h):
            hit = ((w, h), pm.scaled(w, h, mode, Qt.TransformationMode.SmoothTransformation))
            self._frames[which] = hit
        return hit[1]

    def _faded(self) -> QPixmap:
        """The art (or placeholder) at widget size with its bottom and sides
        faded to transparent; cached until the art or the size changes."""
        w, h = self.width(), self.height()
        hit = self._frames.get('faded')
        if hit is not None and hit[0] == (w, h, self._pm is None):
            return hit[1]
        dpr = self.devicePixelRatioF()
        out = QPixmap(round(w * dpr), round(h * dpr))
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.GlobalColor.transparent)
        pt = QPainter(out)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._pm is not None:
            s = self._scaled('art', self._pm, w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            # Hero art keeps the top (logos / faces); covers look best centred.
            pt.drawPixmap(0, -(s.height() - h) // 3, s)
        else:
            pt.drawPixmap(0, 0, placeholder_pixmap(self._name, w, h))
        pt.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        down = QLinearGradient(0, 0, 0, h)
        down.setColorAt(0.0, QColor(0, 0, 0, 255))
        down.setColorAt(0.45, QColor(0, 0, 0, 255))
        down.setColorAt(1.0, QColor(0, 0, 0, 0))
        pt.fillRect(0, 0, w, h, QBrush(down))
        across = QLinearGradient(0, 0, w, 0)
        across.setColorAt(0.0, QColor(0, 0, 0, 120))
        across.setColorAt(0.12, QColor(0, 0, 0, 255))
        across.setColorAt(0.88, QColor(0, 0, 0, 255))
        across.setColorAt(1.0, QColor(0, 0, 0, 120))
        pt.fillRect(0, 0, w, h, QBrush(across))
        pt.end()
        self._frames['faded'] = ((w, h, self._pm is None), out)
        return out

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        pt.drawPixmap(0, 0, self._faded())
        # A little shade under the close button and behind the logo.
        top = QLinearGradient(0, 0, 0, 56)
        top.setColorAt(0.0, QColor(0, 0, 0, 70))
        top.setColorAt(1.0, QColor(0, 0, 0, 0))
        pt.fillRect(QRectF(0, 0, r.width(), 56), QBrush(top))
        if self._logo is not None:
            lg = self._scaled('logo', self._logo, int(self.width() * 0.62),
                              int(self.height() * 0.42), Qt.AspectRatioMode.KeepAspectRatio)
            x, y = SP['4'], self.height() - lg.height() - SP['3']
            if LOOK['light']:                   # light logos on a light fade: shade them
                c = QPointF(x + lg.width() / 2, y + lg.height() / 2)
                shade = QRadialGradient(c, max(lg.width(), lg.height()) * 0.75)
                shade.setColorAt(0.0, QColor(0, 0, 0, 110))
                shade.setColorAt(1.0, QColor(0, 0, 0, 0))
                pt.fillRect(self.rect(), QBrush(shade))
            pt.drawPixmap(x, y, lg)
        pt.end()


class ElideLabel(QLabel):
    """A one-line label that ends in … instead of clipping."""

    def __init__(self, text='', name=None):
        super().__init__(text)
        if name:
            self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setFont(self.font())
        pt.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                             self.width())
        pt.drawText(self.rect(), int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), text)
        pt.end()


class ClampLabel(QLabel):
    """Wrapped plain text cut to `lines` lines, the last one ending in …"""

    def __init__(self, lines: int = 3, name=None):
        super().__init__()
        if name:
            self.setObjectName(name)
        self.full, self.lines = '', lines
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full(self, text: str):
        self.full = text or ''
        self._clamp()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._clamp()

    def _clamp(self):
        w = self.contentsRect().width()
        if w <= 10:                          # not laid out yet: the resize comes later
            self.setText(self.full)
            return
        lay = QTextLayout(self.full, self.font())
        lay.beginLayout()
        out = []
        while (ln := lay.createLine()).isValid():
            ln.setLineWidth(w)
            out.append(self.full[ln.textStart():ln.textStart() + ln.textLength()])
        lay.endLayout()
        if len(out) > self.lines:
            rest = ''.join(out[self.lines - 1:]).replace('\n', ' ')
            out = out[:self.lines - 1] + [self.fontMetrics().elidedText(
                rest, Qt.TextElideMode.ElideRight, w)]
        text = ''.join(out)
        if text != self.text():
            self.setText(text)
        self.setToolTip(self.full if len(text) < len(self.full) else '')


_FACTS: dict = {'mtime': None, 'games': {}}


def game_facts(slug: str | None) -> dict | None:
    """A library game's record in the facts index. The CLI writes the index
    (--game-facts, the agent); the window only reads it, again when the
    file changes."""
    if not slug:
        return None
    try:
        mtime = facts.index_path().stat().st_mtime
    except OSError:
        return None
    if mtime != _FACTS['mtime']:
        _FACTS.update(mtime=mtime, games=facts.read_index().get('games') or {})
    rec = _FACTS['games'].get(slug)
    return rec if isinstance(rec, dict) else None


STATUS_PILLS = {'early_access': ("Early access", 'accent'), 'upcoming': ("Unreleased", 'muted'),
                'delayed': ("Delayed", 'warning'), 'cancelled': ("Cancelled", 'danger')}


def status_pill(rec: dict | None) -> tuple[str, str] | None:
    """(text, role) of the release status chip, or None for a released game."""
    pill = STATUS_PILLS.get((rec or {}).get('status'))
    if pill and rec.get('status') == 'upcoming' and rec.get('releaseText'):
        return f"Unreleased · {rec['releaseText']}", pill[1]
    return pill


class GameContext(QWidget):
    """What the game is, under the quick buttons: credits, up to three genre
    tags (+ Co-op), a three-line description and the play length with your
    progress. Built only from facts that exist: each row hides on its own,
    and with nothing known the whole block hides, leaving no gap (w-40)."""
    filter_library = pyqtSignal(str)             # 'genre:<Nexus genre>' or 'smart:coop'
    open_about = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("GameContext")
        v = QVBoxLayout(self)
        v.setContentsMargins(SP['1'], 0, SP['1'], 0)
        v.setSpacing(SP['2'])
        self.credits = ElideLabel('', "ContextCredits")
        v.addWidget(self.credits)
        self.tags_host = QWidget()
        self.tags = FlowLayout(self.tags_host, margin=0, spacing=SP['1_5'])
        v.addWidget(self.tags_host)
        self.tag_btns: list[QPushButton] = []
        self._tag_keys: tuple = ()
        self.desc = ClampLabel(3, "ContextBlurb")
        v.addWidget(self.desc)
        self.more = QPushButton("More about the game")
        self.more.setObjectName("ContextMore")
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.clicked.connect(self.open_about)
        v.addWidget(self.more, 0, Qt.AlignmentFlag.AlignLeft)
        self.length = QWidget()
        ll = QVBoxLayout(self.length)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(SP['1'])
        lrow = QHBoxLayout()
        lrow.setSpacing(SP['2'])
        self.length_text = label("", "ContextLength", wrap=True)
        lrow.addWidget(self.length_text, 1)
        self.length_pct = label("", "Muted")
        lrow.addWidget(self.length_pct, 0, Qt.AlignmentFlag.AlignTop)
        ll.addLayout(lrow)
        self.bar = QProgressBar()
        self.bar.setObjectName("CardProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setRange(0, 1000)
        ll.addWidget(self.bar)
        v.addWidget(self.length)
        self.shows: set[str] = set()             # what About can leave out: 'facts', 'hltb'
        self.setVisible(False)

    @staticmethod
    def credits_text(rec: dict) -> tuple[str, str]:
        """("FromSoftware · Bandai Namco · 2022", full text for the tooltip);
        the publisher is dropped when it is the developer."""
        devs = [d for d in rec.get('developers') or [] if d]
        pubs = [p for p in rec.get('publishers') or [] if p]
        dev = devs[0] if devs else ''
        pub = next((p for p in pubs if p.casefold() not in {d.casefold() for d in devs}), '')
        year = str(rec['year']) if rec.get('year') else ''
        short = " · ".join(x for x in (dev, pub, year) if x)
        full = "\n".join(x for x in (
            ("Developer: " if len(devs) == 1 else "Developers: ") + ", ".join(devs) if devs else '',
            ("Publisher: " if len(pubs) == 1 else "Publishers: ") + ", ".join(pubs) if pubs else '',
            f"Released: {rec.get('releaseText') or rec.get('release')}"
            if rec.get('releaseText') or rec.get('release') else '') if x)
        return short, full

    @staticmethod
    def length_line(length: dict, minutes: int) -> tuple[str, str, float | None, str]:
        """(text, right-hand note, fraction of the main story or None, tooltip)."""
        main, done = length.get('main'), (minutes or 0) / 60
        text = f"Main story about {main:g} h"
        if length.get('complete'):
            text += f" · everything {length['complete']:g} h"
        if length.get('source') == 'igdb':
            n = length.get('count')
            tip = "From IGDB" + (f", {int(n)} submission{'s' if n != 1 else ''}" if n else '')
        else:
            tip = "From HowLongToBeat"
        if length.get('plus'):
            tip += f"\nMain story and extras about {length['plus']:g} h"
        if done <= 0:
            return text, '', None, tip
        frac = done / main
        note = "past the main story" if frac > 1 else f"{round(100 * frac)} %"
        return text, note, min(1.0, frac), tip

    def set_facts(self, rec: dict | None, minutes: int = 0) -> bool:
        """Show what `rec` (a facts index record) knows; True when anything shows."""
        rec = rec or {}
        self.shows = set()
        short, full = self.credits_text(rec)
        self.credits.setText(short)
        self.credits.setToolTip(full)
        self.credits.setVisible(bool(short))
        if short:
            self.shows.add('facts')
        tags = [(f"genre:{g}", g) for g in (rec.get('genres') or [])[:3]]
        if 'coop' in (rec.get('modes') or []):
            tags.append(('smart:coop', "Co-op"))
        keys = tuple(tags)
        if keys != self._tag_keys:               # rebuilt only when they change: keeps focus
            clear_layout(self.tags)
            self._tag_keys, self.tag_btns = keys, []
            for key, text in tags:
                b = QPushButton(amp(text))
                b.setObjectName("TagChip")
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip(f"Show {'co-op' if key == 'smart:coop' else text} games "
                             "in the Library")
                b.clicked.connect(lambda _=False, k=key: self.filter_library.emit(k))
                self.tags.addWidget(b)
                self.tag_btns.append(b)
        self.tags_host.setVisible(bool(tags))
        desc = (rec.get('description') or '').strip()
        self.desc.set_full(desc)
        self.desc.setVisible(bool(desc))
        self.more.setVisible(bool(desc))
        length = rec.get('length') or {}
        if length.get('main'):
            text, note, frac, tip = self.length_line(length, minutes)
            self.length_text.setText(text)
            self.length_pct.setText(note)
            self.length_pct.setVisible(bool(note))
            self.bar.setVisible(frac is not None)
            self.bar.setValue(int((frac or 0) * 1000))
            self.length.setToolTip(tip)
            self.length.setVisible(True)
            self.shows.add('hltb')
        else:
            self.length.setVisible(False)
        shown = bool(short or tags or desc or length.get('main'))
        self.setVisible(shown)
        return shown


class AchievementRow(QFrame):
    """One achievement: its icon (greyed while locked), title, description
    and when it was unlocked. A locked hidden one keeps its secret."""
    ICON = 36

    def __init__(self, a: dict, scale: float = 1.0):
        super().__init__()
        self.setObjectName("AchRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(int(SP['2_5'] * scale))
        self.size = int(self.ICON * scale)
        self.icon = QLabel()
        self.icon.setFixedSize(self.size, self.size)
        self.icon.setStyleSheet(f"background: {P['surface_2']}; border-radius: {R['sm']}px;")
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        secret = a.get('hidden') and not a.get('unlocked')
        title = plain_label("Hidden achievement" if secret else a.get('title', ''), None, wrap=True)
        title.setStyleSheet(f"color: {P['text'] if a.get('unlocked') else P['subtext']};"
                            f" font-weight: 500;")
        col.addWidget(title)
        desc = "Keep playing to find out." if secret else a.get('desc') or ''
        if desc:
            col.addWidget(label(desc, "Muted", wrap=True))
        lay.addLayout(col, 1)
        if a.get('time'):
            when = datetime.fromtimestamp(a['time'])
            stamp = label(fmt_relative(when.isoformat()) or '', "Muted")
            stamp.setToolTip(when.strftime('%Y-%m-%d %H:%M'))
            lay.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
        url = a.get('icon_url')
        if url and not secret:
            path = ART.cached(url)
            if path:
                self._show(path)
            else:
                ART.fetch(url, self._show)

    def _show(self, path):
        ART.request(path, self.size, self.size, self._set)

    def _set(self, pm):
        if pm is not None:
            self.icon.setPixmap(rounded_pixmap(pm, R['sm']))


class StatTile(QFrame):
    """One number about the game: icon, value, caption. Used in the details
    panel's stats grid."""

    def __init__(self, glyph: str, caption: str, role: str = 'accent'):
        super().__init__()
        self.setObjectName("StatTile")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['2_5'], SP['2'], SP['2_5'], SP['2'])
        lay.setSpacing(SP['2_5'])
        self.icon = icon_label(glyph, 18, P[role])
        self.icon.setFixedWidth(22)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.value = ElideLabel("—", "StatValue")
        col.addWidget(self.value)
        self.caption = ElideLabel(caption, "StatLabel")
        col.addWidget(self.caption)
        lay.addLayout(col, 1)

    def set(self, value: str | None, caption: str | None = None, color: str | None = None,
            tip: str = ''):
        self.value.setText(value or "—")
        self.value.setStyleSheet(f"color: {color};" if color else "")
        if caption is not None:
            self.caption.setText(caption)
        self.setToolTip(tip)


# The batch panel's buttons (several games selected): key, text, tooltip.
BATCH_ACTIONS = (
    ('apply', "Re-apply settings", "Rewrite launch.sh, Steam and Heroic from each game's settings"),
    ('proton', "Set Proton…", "Play them all with one Proton build"),
    ('collection', "Add to a collection…", "Put them all in one collection"),
    ('steam', "Add to Steam", "Add a Steam shortcut for each"),
    ('heroic', "Add to Heroic", "Add each to Heroic"),
    ('pg-default', "Use protongen defaults", "Apply protongen's default profile to each"),
    ('art', "Re-fetch artwork", "Download art again"),
    ('snapshot', "Snapshot now", "A btrfs snapshot of each (undo an update)"),
    ('move', "Move to another drive…", "Move them all to one folder"),
    ('uninstall', "Uninstall…", "Uninstall them all (saves are backed up first)"),
)
# Batch buttons that need a Linux-only feature (features.has).
BATCH_NEEDS = {'proton': 'proton', 'heroic': 'heroic', 'pg-default': 'protongen',
               'snapshot': 'btrfs'}

class Disclosure(QWidget):
    """A section that opens and closes under a one-line header, so the
    panel stays compact. Each section remembers whether it was open."""
    OPEN: dict[str, bool] = {}
    toggled = pyqtSignal(bool)

    def __init__(self, key: str, title: str, glyph: str, body: QWidget, summary: str = ''):
        super().__init__()
        self.key = key
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        self.head = ClickRow()
        self.head.setObjectName("DisclosureHead")
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(SP['3'], SP['2'], SP['2_5'], SP['2'])
        hl.setSpacing(SP['2_5'])
        hl.addWidget(icon_label(glyph, 16, P['accent']))
        t = plain_label(title, None)
        t.setStyleSheet(f"color: {P['text']}; font-weight: 500;")
        hl.addWidget(t)
        self.summary = label(summary, "Muted")
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hl.addWidget(self.summary, 1)
        self.chev = icon_label(G['chevron_down'], 16, P['muted'])
        hl.addWidget(self.chev)
        for w in self.head.findChildren(QLabel):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.head.clicked.connect(lambda: self.set_open(not self.is_open))
        lay.addWidget(self.head)
        self.body = body
        lay.addWidget(body)
        self.set_open(self.OPEN.get(key, False), remember=False)

    @property
    def is_open(self) -> bool:
        return not self.body.isHidden()

    def set_open(self, on: bool, remember: bool = True):
        self.body.setVisible(on)
        self.chev.setText(G['chevron_up'] if on else G['chevron_down'])
        if remember:
            Disclosure.OPEN[self.key] = on
        self.toggled.emit(on)

    def set_summary(self, text: str):
        self.summary.setText(text)


class StatChip(QFrame):
    def __init__(self, caption):
        super().__init__()
        self.setObjectName("StatChip")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['2_5'], SP['2'], SP['2_5'], SP['2'])
        lay.setSpacing(0)
        lay.addWidget(label(caption, "StatLabel"))
        self.value = label("—", "StatValue")
        lay.addWidget(self.value)

    def set(self, text):
        self.value.setText(text or "—")


def _group(title: str, hint: str = '') -> tuple[QWidget, QGridLayout]:
    """One titled block inside a section: a small heading, then a grid of
    label | control rows (column 0 the label, 1-3 the control)."""
    w = QWidget()
    outer = QVBoxLayout(w)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(SP['1_5'])
    head = QHBoxLayout()
    head.addWidget(label(title, "StatLabel"))
    if hint:
        h = label(hint, "Muted")
        h.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        h.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(h, 1)
    outer.addLayout(head)
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(SP['2'])
    grid.setVerticalSpacing(SP['1_5'])
    grid.setColumnStretch(1, 1)
    outer.addLayout(grid)
    return w, grid


def _grid_row(grid: QGridLayout, name: str, widget: QWidget):
    row = grid.rowCount()
    lab = label(name, "Muted")
    lab.setMinimumWidth(96)
    grid.addWidget(lab, row, 0)
    grid.addWidget(widget, row, 1, 1, 3)
    return lab


class PerfPanel(QWidget):
    """Performance switches for one game (`nexus-cli --perf`), in three
    groups: Display (gamescope, upscaler), Frame rate (cap, frame
    generation, power mode) and Fixes and overlay (MangoHud, Wine Wayland,
    hidraw). `set_state` takes --show-launch's `perf`; `items()` is the CLI
    argument list for what's shown now; `changed` fires on a user edit."""
    FPS = (0, 30, 40, 45, 60, 72, 90, 120, 144, 165, 240)
    RES = ((1280, 720), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160))
    REFRESH = (60, 75, 90, 120, 144, 165, 240)
    TOOL_NAMES = {'gamescope': "gamescope", 'mangohud': "MangoHud",
                  'game-performance': "game-performance", 'gamemoderun': "GameMode"}
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['4'])
        self.missing = label("", "Muted", wrap=True)     # tools that aren't installed
        self.missing.setVisible(False)
        lay.addWidget(self.missing)

        # Display: gamescope (its settings only while it's on) and the upscaler.
        disp, dg = _group("Display")
        self.ck_gs = QCheckBox("Run inside gamescope")
        self.ck_gs.setToolTip("Valve's micro-compositor: a fixed size and refresh, FSR "
                              "upscaling, HDR. Helps games that pick the wrong mode.")
        self.ck_gs.toggled.connect(self._gs_toggled)
        dg.addWidget(self.ck_gs, 0, 0, 1, 4)
        self.cb_res = NoWheelCombo()
        for w_, h_ in self.RES:
            self.cb_res.addItem(f"{w_}×{h_}", (w_, h_))
        self.cb_hz = NoWheelCombo()
        for hz in self.REFRESH:
            self.cb_hz.addItem(f"{hz} Hz", hz)
        self.ck_fsr = QCheckBox("FSR from")
        self.cb_render = NoWheelCombo()
        for w_, h_ in self.RES:
            self.cb_render.addItem(f"{w_}×{h_}", (w_, h_))
        self.ck_full = QCheckBox("Fullscreen")
        self.ck_hdr = QCheckBox("HDR")
        self.gs_box = QWidget()                 # gamescope's own settings, under the switch
        gl = QGridLayout(self.gs_box)
        gl.setContentsMargins(SP['5'], 0, 0, 0)
        gl.setHorizontalSpacing(SP['2'])
        gl.setVerticalSpacing(SP['1_5'])
        gl.addWidget(self.cb_res, 0, 0, 1, 2)
        gl.addWidget(self.cb_hz, 0, 2, 1, 2)
        gl.addWidget(self.ck_fsr, 1, 0)
        gl.addWidget(self.cb_render, 1, 1, 1, 3)
        gl.addWidget(self.ck_full, 2, 0, 1, 2)
        gl.addWidget(self.ck_hdr, 2, 2, 1, 2)
        dg.addWidget(self.gs_box, 1, 0, 1, 4)
        self.cb_up = NoWheelCombo()
        for text, key in (("Off", 'off'), ("FSR 4 upgrade", 'fsr4'),
                          ("OptiScaler (DLSS/XeSS → FSR 4)", 'optiscaler')):
            self.cb_up.addItem(text, key)
        self.cb_up.setToolTip("FSR 4 needs an RDNA 4 GPU and a Proton that supports it "
                              "(GE-Proton 10+, proton-cachyos)")
        _grid_row(dg, "Upscaler", self.cb_up)
        lay.addWidget(disp)

        # Frame rate: a cap, frame generation, the power profile.
        rate, rg = _group("Frame rate")
        self.cb_fps = NoWheelCombo()
        for n in self.FPS:
            self.cb_fps.addItem("No cap" if not n else f"{n} fps", n)
        self.cb_fps.setToolTip("DXVK_FRAME_RATE / VKD3D_FRAME_RATE: a frame cap inside the "
                               "game's renderer (Direct3D 8–12)")
        _grid_row(rg, "Frame cap", self.cb_fps)
        self.cb_fg = NoWheelCombo()
        for text, key in (("Off", None), ("2× (lsfg-vk)", 2), ("3× (lsfg-vk)", 3),
                          ("4× (lsfg-vk)", 4)):
            self.cb_fg.addItem(text, key)
        self.cb_fg.setToolTip("Lossless Scaling frame generation through lsfg-vk: a profile "
                              "for this game's exe in ~/.config/lsfg-vk/conf.toml")
        _grid_row(rg, "Frame generation", self.cb_fg)
        self.lsfg = None                 # None: lsfg-vk isn't set up
        self.cb_mode = NoWheelCombo()
        for text, key in (("Off", 'off'), ("game-performance (power profile)", 'game-performance'),
                          ("gamemoderun (Feral GameMode)", 'gamemoderun')):
            self.cb_mode.addItem(text, key)
        _grid_row(rg, "Power mode", self.cb_mode)
        lay.addWidget(rate)

        # Fixes and overlay: switches you try when something looks or feels wrong.
        fixes, fg = _group("Fixes and overlay")
        self.ck_mango = QCheckBox("MangoHud overlay (fps, frame times)")
        self.ck_wayland = QCheckBox("Wine Wayland driver")
        self.ck_wayland.setToolTip("PROTON_ENABLE_WAYLAND=1: no XWayland in between. "
                                   "Newer Protons only; some games misbehave.")
        self.ck_hidraw = QCheckBox("Plain controller input (no hidraw)")
        self.ck_hidraw.setToolTip("PROTON_DISABLE_HIDRAW=1: the game sees an ordinary pad "
                                  "instead of the raw DualSense — try it when buttons or "
                                  "glyphs are wrong")
        for i, ck in enumerate((self.ck_mango, self.ck_wayland, self.ck_hidraw)):
            fg.addWidget(ck, i, 0, 1, 4)
        lay.addWidget(fixes)

        self._gs_widgets = (self.cb_res, self.cb_hz, self.ck_fsr, self.cb_render, self.ck_full,
                            self.ck_hdr)
        self.niri = False
        for cb in (self.cb_fps, self.cb_mode, self.cb_up, self.cb_fg, self.cb_res, self.cb_hz,
                   self.cb_render):
            cb.currentIndexChanged.connect(self._changed)
        for ck in (self.ck_gs, self.ck_fsr, self.ck_full, self.ck_hdr, self.ck_mango,
                   self.ck_wayland, self.ck_hidraw):
            ck.toggled.connect(self._changed)
        self._gs_toggled(False)

    def _changed(self, *_):
        if not self._loading:
            self.changed.emit()

    def _gs_toggled(self, on: bool):
        self.gs_box.setVisible(on)
        for w in self._gs_widgets:
            w.setEnabled(on)
        if self.niri:
            self.ck_hdr.setEnabled(False)

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        if i < 0 and value is not None:
            text = (f"{value[0]}×{value[1]}" if isinstance(value, tuple) else
                    f"{value} Hz" if combo.itemText(0).endswith('Hz') else f"{value} fps")
            combo.addItem(text, value)
            i = combo.count() - 1
        combo.setCurrentIndex(max(0, i))

    def set_state(self, perf: dict, tools: dict | None = None, niri: bool = False,
                  lsfg: dict | None = None):
        self._loading = True
        try:
            self._set_state(perf, tools, niri, lsfg)
        finally:
            self._loading = False

    def _set_state(self, perf, tools, niri, lsfg):
        perf, tools = perf or {}, tools or {}
        gone = [name for key, name in self.TOOL_NAMES.items() if tools.get(key) is False]
        self.missing.setText("Not installed here: " + ", ".join(gone)
                             + ". Their switches stay off until you install them."
                             if gone else "")
        self.missing.setVisible(bool(gone))
        self.niri = niri
        self.lsfg = lsfg
        self._select(self.cb_fg, (lsfg or {}).get('multiplier'))
        self.cb_fg.setEnabled(lsfg is not None)
        self.cb_fg.setItemText(0, "Off" if lsfg is not None else "Off (lsfg-vk isn't set up)")
        if lsfg is None:
            self.cb_fg.setToolTip("lsfg-vk isn't set up: install it and point its "
                                  "conf.toml at Lossless Scaling (owned on Steam)")
        self._select(self.cb_fps, perf.get('fps') or 0)
        self._select(self.cb_mode, perf.get('mode') or 'off')
        self._select(self.cb_up, perf.get('upscaler') or 'off')
        model = self.cb_mode.model()
        for i in range(self.cb_mode.count()):
            key = self.cb_mode.itemData(i)
            model.item(i).setEnabled(key == 'off' or tools.get(key, True))
        self.ck_mango.setChecked(bool(perf.get('mangohud')))
        self.ck_mango.setEnabled(tools.get('mangohud', True) or bool(perf.get('mangohud')))
        self.ck_wayland.setChecked(bool(perf.get('wayland')))
        self.ck_hidraw.setChecked(bool(perf.get('no_hidraw')))
        gs = perf.get('gamescope')
        self.ck_gs.setEnabled(tools.get('gamescope', True) or bool(gs))
        self.ck_gs.setChecked(bool(gs))
        gs = gs or {}
        if gs.get('w') and gs.get('h'):
            self._select(self.cb_res, (gs['w'], gs['h']))
        if gs.get('r'):
            self._select(self.cb_hz, gs['r'])
        self.ck_fsr.setChecked(bool(gs.get('fsr')))
        if gs.get('render_w') and gs.get('render_h'):
            self._select(self.cb_render, (gs['render_w'], gs['render_h']))
        self.ck_full.setChecked(bool(gs.get('fullscreen')))
        self.ck_hdr.setChecked(bool(gs.get('hdr')) and not niri)
        self.ck_hdr.setToolTip("niri can't show HDR yet, so gamescope can't pass it on"
                               if niri else "gamescope --hdr-enabled (needs an HDR desktop)")
        self._gs_toggled(self.ck_gs.isChecked())

    def items(self) -> list[str]:
        out = [f"fps={self.cb_fps.currentData() or 0}", f"mode={self.cb_mode.currentData()}",
               f"upscaler={self.cb_up.currentData()}",
               f"mangohud={'on' if self.ck_mango.isChecked() else 'off'}",
               f"wayland={'on' if self.ck_wayland.isChecked() else 'off'}",
               f"hidraw={'off' if self.ck_hidraw.isChecked() else 'on'}"]
        if self.lsfg is not None:
            out.append(f"lsfg={self.cb_fg.currentData() or 'off'}")
        if not self.ck_gs.isChecked():
            return out + ["gamescope=off"]
        w, h = self.cb_res.currentData()
        spec = [f"{w}x{h}@{self.cb_hz.currentData()}"]
        if self.ck_fsr.isChecked():
            rw, rh = self.cb_render.currentData()
            spec += ['fsr', f"render={rw}x{rh}"]
        if self.ck_full.isChecked():
            spec.append('fullscreen')
        if self.ck_hdr.isChecked() and not self.niri:
            spec.append('hdr')
        return out + ["gamescope=" + ','.join(spec)]


class DetailsPanel(QFrame):
    """Everything about one game: Play, stats, integrations, Proton and launch
    options, protongen, maintenance tools and logs. With several tiles selected
    it becomes a batch-actions panel instead.

    Changes are CLI jobs (emitted via `request`) so they land in launch.sh,
    Steam and Heroic exactly as the command line does it."""
    request = pyqtSignal(list, str, bool, str)   # (args, title, needs_steam, kind)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    closed = pyqtSignal()
    toast = pyqtSignal(str, str)
    trailer = pyqtSignal(dict)                   # play a store trailer
    reveal = pyqtSignal(str)                     # show another library game
    notes_changed = pyqtSignal(str, str)         # (slug, text)
    filter_library = pyqtSignal(str)             # a context tag: 'genre:RPG', 'smart:coop'

    def __init__(self):
        super().__init__()
        self.setObjectName("Details")
        self.setMinimumWidth(340)
        self.setMaximumWidth(440)
        self.game: dict = {}
        self.games: list[dict] = []
        self.info: dict = {}
        self._info_query = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        # ---- single game ----
        page = TintedPage()
        self.page = page
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack.addWidget(scroll)
        # The hero runs edge to edge; everything under it keeps the margins.
        bleed = QVBoxLayout(page)
        bleed.setContentsMargins(0, 0, 0, SP['4'])
        bleed.setSpacing(0)
        lay = QVBoxLayout()
        lay.setContentsMargins(SP['4'], 0, SP['4'], 0)
        lay.setSpacing(SP['3'])

        hero_wrap = QWidget()
        hw = QGridLayout(hero_wrap)
        hw.setContentsMargins(0, 0, 0, 0)
        self.hero = HeroBanner()
        self.hero.logo_changed.connect(lambda shown: self.name.setVisible(not shown))
        hw.addWidget(self.hero, 0, 0)
        self.hero.tint_changed.connect(page.set_tint)
        close = icon_button(G['close'], "Close (Esc)", 28, "TileMore")
        close.clicked.connect(self.closed)
        corner = QWidget()                      # the art is edge to edge; the button isn't
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, SP['2'], SP['2'], 0)
        cl.addWidget(close)
        hw.addWidget(corner, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        bleed.addWidget(hero_wrap)
        bleed.addLayout(lay)

        self.name = label("", "PageTitle", wrap=True)
        lay.addWidget(self.name)
        # Status that matters right now: source, playing / starting, update.
        self.chips_host = QWidget()
        self.chips = FlowLayout(self.chips_host, margin=0, spacing=SP['1_5'])
        lay.addWidget(self.chips_host)

        prow = QHBoxLayout()
        self.btn_play = QPushButton("Play")
        self.btn_play.setObjectName("PlayBig")
        set_glyph_icon(self.btn_play, G['play'], 18, 'on_accent', hover_role=None)
        self.btn_play.setMinimumHeight(42)
        self.btn_play.clicked.connect(lambda: self.play.emit(self.game))
        prow.addWidget(self.btn_play, 1)
        more = icon_button(G['more'], "Every action for this game", 42, "QuickAction",
                           round_=True)
        more.clicked.connect(self._emit_menu)
        prow.addWidget(more)
        self.btn_more = more
        lay.addLayout(prow)

        # The few things you reach for; everything else is in ⋮ or a section.
        self.quick = QHBoxLayout()
        self.quick.setSpacing(SP['1_5'])
        lay.addLayout(self.quick)
        # What the game is, from the facts index (below the buttons, so Play
        # stays at the same height for every game).
        self.context = GameContext()
        self.context.filter_library.connect(self.filter_library)
        self.context.open_about.connect(self._open_about)
        lay.addWidget(self.context)

        # The stats grid.
        grid = QGridLayout()
        grid.setHorizontalSpacing(SP['2'])
        grid.setVerticalSpacing(SP['2'])
        self.st_played = StatTile(G['timer'], "Play time")
        self.st_last = StatTile(G['history'], "Last played")
        self.st_size = StatTile(G['storage'], "On disk", 'info')
        self.st_added = StatTile(G['calendar'], "Installed", 'info')
        self.st_proton = StatTile(G['proton'], "Proton", 'warning')
        self.st_rating = StatTile(G['medal'], "ProtonDB", 'warning')
        for i, t in enumerate((self.st_played, self.st_last, self.st_size, self.st_added,
                               self.st_proton, self.st_rating)):
            grid.addWidget(t, i // 2, i % 2)
        if not has('proton'):           # hide only: a parentless show() is a window
            self.st_proton.hide()
            self.st_rating.hide()
        self.st_ach = StatTile(G['trophy'], "Achievements", 'success')   # Steam games
        self.st_ach.setVisible(False)
        grid.addWidget(self.st_ach, 3, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        # Inline uninstall confirmation (replaces the old modal message box).
        self.confirm = QFrame()
        self.confirm.setObjectName("DangerBar")
        cl = QVBoxLayout(self.confirm)
        cl.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        self.confirm_text = label("", wrap=True)
        cl.addWidget(self.confirm_text)
        self.confirm_backup = QCheckBox("Back up save-game files first, if any are found")
        self.confirm_backup.setChecked(True)
        if not has('save_backup'):
            self.confirm_backup.hide()
        cl.addWidget(self.confirm_backup)
        # Snapshots hold copies of the saves: kept unless ticked; shown only
        # when the game has some (ask_uninstall asks the CLI).
        self.confirm_snaps = QCheckBox("")
        self.confirm_snaps.hide()
        cl.addWidget(self.confirm_snaps)
        crow = QHBoxLayout()
        crow.addStretch()
        keep = QPushButton("Keep it")
        keep.clicked.connect(lambda: self.confirm.setVisible(False))
        gone = QPushButton("Uninstall")
        gone.setObjectName("DangerSolid")
        gone.clicked.connect(self._do_uninstall)
        crow.addWidget(keep)
        crow.addWidget(gone)
        cl.addLayout(crow)
        self.confirm.setVisible(False)
        lay.addWidget(self.confirm)

        # Five sections, closed until you need them: Launch (how a repack
        # starts, on Linux; advice for Steam/Heroic games), Game and files,
        # Troubleshoot, About, Notes.
        self.pg_notice = self._pg_notice()
        lay.addWidget(self.pg_notice)        # protongen drift: always in view
        self.ac_notice = label("", None, wrap=True)
        self.ac_notice.setObjectName("DangerBar")
        self.ac_notice.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        self.ac_notice.setVisible(False)
        lay.addWidget(self.ac_notice)        # online play broken: always in view
        self._dirty: set[str] = set()
        self._launch_full = False            # a Linux repack: the whole Launch section
        self._about_summary = ''
        self._sent = False                   # applied; waiting for the jobs to finish
        self._was_busy = False
        self._base = {'proton': '', 'opts': '', 'via': 'nexus'}
        self._perf_base: list[str] = []
        self.sec_launch = Disclosure('launch', "Launch", G['tune'], self._launch_tab())
        self.sec_files = Disclosure('files', "Game and files", G['folder'], self._files_tab())
        self.sec_files.toggled.connect(
            lambda on: on and not self.snap_box.isHidden() and self._load_snapshots(force=True))
        self.sec_trouble = Disclosure('troubleshoot', "Troubleshoot", G['wrench'],
                                      self._logs_tab())
        self.sec_trouble.toggled.connect(lambda on: on and self._load_log())
        self.sec_about = Disclosure('about', "About", G['info'], self._about_tab())
        self.sec_about.toggled.connect(lambda on: on and self._load_achievements())
        self.sec_about.setVisible(False)
        self._store: dict = {}
        self._tips_hltb = None
        self._quick_btns: dict = {}
        self._about_parts = {'store': False, 'compat': False, 'deal': False, 'ach': False}
        self._snap_at = (None, 0.0)
        self._ach_for = None
        # Notes: yours, saved as you type (a second after you stop).
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Where you left off, settings that worked, "
                                           "a code to remember…")
        self.notes_edit.setFixedHeight(120)
        self._notes_timer = QTimer(self)
        self._notes_timer.setSingleShot(True)
        self._notes_timer.setInterval(1000)
        self._notes_timer.timeout.connect(self._save_notes)
        self.notes_edit.textChanged.connect(self._notes_timer.start)
        self._notes_for = None
        self.sec_notes = Disclosure('notes', "Notes", G['pencil'], self.notes_edit)
        self.sections = QVBoxLayout()
        self.sections.setSpacing(SP['2'])
        for sec in (self.sec_launch, self.sec_files, self.sec_trouble, self.sec_about,
                    self.sec_notes):
            self.sections.addWidget(sec)
        lay.addLayout(self.sections)
        lay.addStretch()
        self._tips_query = None

        # ---- batch (several selected) ----
        batch = QWidget()
        bl = QVBoxLayout(batch)
        bl.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        bl.setSpacing(SP['3'])
        bhead = QHBoxLayout()
        self.batch_title = label("", "PageTitle")
        bhead.addWidget(self.batch_title, 1)
        bclose = icon_button(G['close'], "Clear selection (Esc)", 28, "Ghost")
        bclose.clicked.connect(self.closed)
        bhead.addWidget(bclose)
        bl.addLayout(bhead)
        self.batch_names = label("", "Muted", wrap=True)
        bl.addWidget(self.batch_names)
        # What can be done to all of them; the window gates each game (busy,
        # running) and offers only what applies to the whole selection.
        self.batch_btns: dict[str, QPushButton] = {}
        for key, text, tip in BATCH_ACTIONS:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=key: self.request.emit(
                ['__batch__', k], '', False, 'action'))
            if key == 'uninstall':
                b.setObjectName("Danger")
            bl.addWidget(b)
            self.batch_btns[key] = b
        bl.addStretch()
        self.stack.addWidget(batch)

    # ---- tabs ----
    def _row(self, lay, title):
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        name = label(title)
        name.setMinimumWidth(78)
        name.setStyleSheet(f"color: {P['muted']};")
        row.addWidget(name, 0, Qt.AlignmentFlag.AlignTop)
        val = label("", wrap=True)
        # Never let a value widen the panel; long ones wrap via soft_wrap().
        val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        val.setMinimumWidth(0)
        row.addWidget(val, 1)
        lay.addLayout(row)
        return row, val

    @staticmethod
    def _body(margin_top: int = SP['3']) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(SP['1'], margin_top, SP['1'], SP['1'])
        lay.setSpacing(SP['3'])
        return w, lay

    def _pg_notice(self) -> QFrame:
        """protongen drift notice with the fix right there."""
        box = QFrame()
        box.setObjectName("Card")
        pl = QVBoxLayout(box)
        pl.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        self.pg_notice_text = label("", wrap=True)
        pl.addWidget(self.pg_notice_text)
        prow = QHBoxLayout()
        prow.addStretch()
        pull = QPushButton("Pull from protongen")
        pull.clicked.connect(self._pull_protongen)
        prow.addWidget(pull)
        pl.addLayout(prow)
        box.setVisible(False)
        return box

    def _files_tab(self):
        """Game and files: what the game is called and runs, where it lives,
        which launchers know it, and its snapshots."""
        w, lay = self._body()
        lay.setSpacing(SP['2'])
        lay.addWidget(label("Name", "StatLabel"))
        nrow = QHBoxLayout()
        self.ed_name = QLineEdit()
        self.ed_name.textEdited.connect(self._name_edited)
        self.ed_name.returnPressed.connect(self._rename)
        rename = QPushButton("Rename")
        rename.clicked.connect(self._rename)
        nrow.addWidget(self.ed_name, 1)
        nrow.addWidget(rename)
        lay.addLayout(nrow)

        lay.addWidget(label("Executable", "StatLabel"))
        erow = QHBoxLayout()
        self.ed_exe = QLineEdit()
        self.ed_exe.setReadOnly(True)
        change = QPushButton("Change…")
        change.clicked.connect(self._pick_exe)
        erow.addWidget(self.ed_exe, 1)
        erow.addWidget(change)
        lay.addLayout(erow)

        rows = QVBoxLayout()
        rows.setContentsMargins(0, SP['2'], 0, 0)
        rows.setSpacing(SP['2_5'])
        r, self.ov_folder = self._row(rows, "Folder")
        self.btn_copy_folder = icon_button(G['copy'], "Copy folder path", 28, "Ghost")
        self.btn_copy_folder.clicked.connect(lambda: self._copy(self._folder))
        r.addWidget(self.btn_copy_folder, 0, Qt.AlignmentFlag.AlignTop)
        self._folder = ''
        r, self.ov_steam = self._row(rows, "Steam")
        self.btn_steam = QPushButton()
        self.btn_steam.clicked.connect(self._toggle_steam)
        r.addWidget(self.btn_steam)
        r, self.ov_heroic = self._row(rows, "Heroic")
        self.btn_heroic = QPushButton()
        self.btn_heroic.clicked.connect(self._toggle_heroic)
        r.addWidget(self.btn_heroic)
        if not has('heroic'):
            _hide_row(r)
        lay.addLayout(rows)

        # Snapshots (btrfs): undo an update, a Proton change, winetricks.
        self.snap_box = QWidget()
        snl = QVBoxLayout(self.snap_box)
        snl.setContentsMargins(0, SP['2'], 0, 0)
        snl.setSpacing(SP['1_5'])
        self.snap_title = label("Snapshots", "StatLabel")
        snl.addWidget(self.snap_title)
        self.snap_note = label("", "Muted", wrap=True)
        snl.addWidget(self.snap_note)
        self.snap_rows = QVBoxLayout()
        self.snap_rows.setSpacing(SP['1'])
        snl.addLayout(self.snap_rows)
        sbr = QHBoxLayout()
        self.snap_take = QPushButton("Snapshot now")
        self.snap_take.clicked.connect(lambda: self._act('snapshot-now'))
        self.snap_enable = QPushButton("Enable snapshots")
        self.snap_enable.setToolTip("Moves the game into its own btrfs subvolume — instant, "
                                    "and it takes no extra space")
        self.snap_enable.clicked.connect(lambda: self._act('snapshots-enable'))
        sbr.addWidget(self.snap_take)
        sbr.addWidget(self.snap_enable)
        sbr.addStretch()
        snl.addLayout(sbr)
        self.snap_box.setVisible(False)
        lay.addWidget(self.snap_box)
        return w

    def _about_tab(self):
        """About: the store's blurb, reviews, time to beat, trailers, similar
        games; then compatibility (ProtonDB, Deck, anti-cheat, PCGamingWiki)
        and achievements (loaded when the section opens)."""
        w, lay = self._body()
        self.tune_deal = label("", None, wrap=True)          # Game-db: price, progress
        self.tune_deal.setOpenExternalLinks(True)
        lay.addWidget(self.tune_deal)
        self.store_box = QWidget()
        abl = QVBoxLayout(self.store_box)
        abl.setContentsMargins(0, 0, 0, 0)
        abl.setSpacing(SP['1_5'])
        self.about_text = label("", None, wrap=True)
        self.about_text.setOpenExternalLinks(True)
        abl.addWidget(self.about_text)
        self.trailer_rows = QVBoxLayout()
        self.trailer_rows.setSpacing(SP['1'])
        abl.addLayout(self.trailer_rows)
        self.similar_title = label("More like this", "StatLabel")
        abl.addWidget(self.similar_title)
        self.similar_rows = QVBoxLayout()
        self.similar_rows.setSpacing(SP['0_5'])
        abl.addLayout(self.similar_rows)
        lay.addWidget(self.store_box)
        self.compat_box, cg = _group("Compatibility")
        self.tune_pdb = label("", None, wrap=True)
        self.tune_pdb.setOpenExternalLinks(True)
        cg.addWidget(self.tune_pdb, 0, 0, 1, 4)
        self.compat_text = label("", None, wrap=True)
        self.compat_text.setOpenExternalLinks(True)
        cg.addWidget(self.compat_text, 1, 0, 1, 4)
        lay.addWidget(self.compat_box)
        self.ach_box, ag = _group("Achievements")
        self.ach_body = QWidget()
        self.ach_rows = QVBoxLayout(self.ach_body)
        self.ach_rows.setContentsMargins(0, 0, 0, 0)
        self.ach_rows.setSpacing(SP['1_5'])
        ag.addWidget(self.ach_body, 0, 0, 1, 4)
        lay.addWidget(self.ach_box)
        for box in (self.store_box, self.compat_box, self.ach_box, self.tune_deal,
                    self.tune_pdb):
            box.setVisible(False)
        return w

    def _launch_tab(self):
        """Launch: Runtime, the performance groups, suggested fixes and the
        Advanced line, with one Apply bar for everything that waits. Steam
        and Heroic games (whose launchers own their options) get only the
        advice: suggestions and protongen's tuning to copy."""
        w, lay = self._body()
        lay.setSpacing(SP['4'])

        self.runtime_box, rg = _group("Runtime")
        self.cb_proton = NoWheelCombo()
        self.cb_proton.activated.connect(self._proton_picked)
        self.cb_proton.setToolTip("The Proton launch.sh, the Steam shortcut and Heroic all use")
        _grid_row(rg, "Proton", self.cb_proton)
        self.seg_via = SegmentedControl('pill', 30)
        self.seg_via.add('nexus', "Nexus", tip="Play starts the game from Nexus: Discord "
                         "status, stay-awake and session tuning run")
        self.seg_via.add('steam', "Steam", tip="Play starts the game's Steam shortcut: Steam "
                         "Input, the overlay, recording and your Steam status")
        self.seg_via.set_current('nexus', animate=False)
        self.seg_via.changed.connect(self._via_picked)
        via = QWidget()
        vl = QHBoxLayout(via)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.addWidget(self.seg_via)
        vl.addStretch()
        _grid_row(rg, "Start from", via)
        lay.addWidget(self.runtime_box)

        self.perf = PerfPanel()
        self.perf.changed.connect(self._perf_edited)
        lay.addWidget(self.perf)

        # Suggested fixes: recipes for this GPU and game (Add applies one now;
        # for Steam/Heroic games, Copy for their own launch options).
        self.sugg_box = QWidget()
        sl = QVBoxLayout(self.sugg_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(SP['1_5'])
        self.tune_sugg_title = label("", "StatLabel")
        sl.addWidget(self.tune_sugg_title)
        self.tune_rows = QVBoxLayout()
        self.tune_rows.setSpacing(SP['1'])
        sl.addLayout(self.tune_rows)
        self.sugg_box.setVisible(False)
        lay.addWidget(self.sugg_box)

        # protongen's saved options for a Steam/Heroic game, to paste there.
        self.tune_pg_row = QWidget()
        tpl = QVBoxLayout(self.tune_pg_row)
        tpl.setContentsMargins(0, 0, 0, 0)
        tpl.setSpacing(SP['1_5'])
        self.tune_pg_title = label("protongen tuning", "StatLabel")
        tpl.addWidget(self.tune_pg_title)
        pgrow = QHBoxLayout()
        self.tune_pg = QLineEdit()
        self.tune_pg.setReadOnly(True)
        pgrow.addWidget(self.tune_pg, 1)
        self.tune_pg_copy = QPushButton("Copy")
        self.tune_pg_copy.setToolTip("Copy, then paste into Steam → Properties → Launch options")
        self.tune_pg_copy.clicked.connect(lambda: self._copy(self.tune_pg.text()))
        pgrow.addWidget(self.tune_pg_copy)
        tpl.addLayout(pgrow)
        self.tune_pg_row.setVisible(False)
        lay.addWidget(self.tune_pg_row)

        # Advanced: the launch options line itself, and protongen.
        self.adv_box, ag = _group("Advanced")
        al = QVBoxLayout()
        al.setSpacing(SP['1_5'])
        orow = QHBoxLayout()
        self.ed_opts = QLineEdit()
        self.ed_opts.setPlaceholderText("PROTON_ENABLE_WAYLAND=1 mangohud %command%")
        self.ed_opts.setToolTip("The whole launch line, Steam style, or a protongen:v1:… "
                                "preset code. Empty it and apply to clear the options.")
        self.ed_opts.textEdited.connect(self._opts_edited)
        self.ed_opts.returnPressed.connect(self._apply_launch)
        orow.addWidget(self.ed_opts, 1)
        self.btn_copy_opts = icon_button(G['copy'], "Copy launch options", 30, "Ghost")
        self.btn_copy_opts.clicked.connect(lambda: self._copy(self.ed_opts.text()))
        orow.addWidget(self.btn_copy_opts)
        al.addLayout(orow)
        self.opts_state = label("", "Muted", wrap=True)
        al.addWidget(self.opts_state)
        al.addWidget(label("protongen", "StatLabel"))
        self.pg_state = label("", "Muted", wrap=True)
        al.addWidget(self.pg_state)
        self.pg_opts = QLineEdit()
        self.pg_opts.setReadOnly(True)
        al.addWidget(self.pg_opts)
        grow = QHBoxLayout()
        grow.addStretch()
        self.btn_pg_open = QPushButton("Open protongen")
        self.btn_pg_open.setToolTip("Copies this game's name, so you can find it there")
        self.btn_pg_open.clicked.connect(self._open_protongen)
        self.btn_pg_pull = QPushButton("Pull tuning")
        self.btn_pg_pull.clicked.connect(self._pull_protongen)
        self.btn_pg_default = QPushButton("Use defaults")
        self.btn_pg_default.clicked.connect(self._protongen_default)
        grow.addWidget(self.btn_pg_open)
        grow.addWidget(self.btn_pg_default)
        grow.addWidget(self.btn_pg_pull)
        al.addLayout(grow)
        ag.addLayout(al, 0, 0, 1, 4)
        lay.addWidget(self.adv_box)

        # One Apply for Proton, Start from, the switches and the line: shown
        # only while something differs from what the game uses now.
        self.apply_bar = QFrame()
        self.apply_bar.setObjectName("Card")
        bl = QHBoxLayout(self.apply_bar)
        bl.setContentsMargins(SP['3'], SP['2'], SP['2'], SP['2'])
        self.pending_text = label("", None)
        bl.addWidget(self.pending_text, 1)
        discard = QPushButton("Discard")
        discard.clicked.connect(self._discard_launch)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setObjectName("Primary")
        self.btn_apply.setToolTip("Writes the changes into launch.sh, the Steam shortcut and "
                                  "Heroic")
        self.btn_apply.clicked.connect(self._apply_launch)
        bl.addWidget(discard)
        bl.addWidget(self.btn_apply)
        self.apply_bar.setVisible(False)
        lay.addWidget(self.apply_bar)
        return w

    def _logs_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, SP['3'], 0, 0)
        row = QHBoxLayout()
        self.cb_log = NoWheelCombo()
        self.cb_log.addItem("Last launch", 'launch_log')
        self.cb_log.addItem("Install log", 'install_log')
        self.cb_log.currentIndexChanged.connect(self._load_log)
        row.addWidget(self.cb_log, 1)
        reload_b = icon_button(G['refresh'], "Reload", 30)
        reload_b.clicked.connect(self._load_log)
        folder = icon_button(G['folder'], "Open logs folder", 30)
        folder.clicked.connect(lambda: self.info.get('install_log') and
                               QDesktopServices.openUrl(QUrl.fromLocalFile(
                                   str(Path(self.info.get('install_log')).parent))))
        row.addWidget(reload_b)
        row.addWidget(folder)
        self.btn_explain = QPushButton("Explain with local AI")
        self.btn_explain.setToolTip("Uses the local model configured in protongen; the log "
                                    "never leaves this machine")
        self.btn_explain.setVisible(False)
        self.btn_explain.clicked.connect(lambda: self._emit(
            ['--explain-log', self.slug], "Explaining the log", False, 'task'))
        row.addWidget(self.btn_explain)
        lay.addLayout(row)
        self.bench_text = label("", "Muted", wrap=True)      # benchmark runs, when any
        self.bench_text.setVisible(False)
        lay.addWidget(self.bench_text)
        self.logview = QPlainTextEdit()
        self.logview.setObjectName("Console")
        self.logview.setReadOnly(True)
        self.logview.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.logview.setMinimumHeight(220)
        lay.addWidget(self.logview, 1)
        return w

    # ---- data ----
    @property
    def slug(self):
        return self.game.get('slug')

    def show_games(self, games: list[dict]):
        self.games = games
        if len(games) > 1:
            self.batch_title.setText(f"{len(games)} games selected")
            self.batch_names.setText(", ".join(g.get('display_name', g['slug'])
                                               for g in games))
            win = self.window()
            for key, b in self.batch_btns.items():
                b.setVisible((key not in BATCH_NEEDS or has(BATCH_NEEDS[key]))
                             and (not hasattr(win, 'batch_applies')
                                  or win.batch_applies(key, games)))
            self.stack.setCurrentIndex(1)
            return
        self.stack.setCurrentIndex(0)
        same = games and games[0].get('slug') == self.slug
        self.game = games[0] if games else {}
        if not same:
            self.confirm.setVisible(False)
            self.info = {}
            self._store, self._tips_hltb = {}, None
            self._dirty = set()                 # edits belong to the game they were made on
            self._sent = self._was_busy = False
        # Applied changes wait for their jobs: once the game stops being busy,
        # ask again what it uses now, and let go of the edits.
        busy = bool(self.game.get('busy'))
        done_job = same and self._was_busy and not busy
        self._was_busy = busy
        if done_job and self._sent:
            self._sent = False
            self._dirty -= self.LAUNCH_KEYS
        if not same:
            if self._notes_timer.isActive():        # keep what was typed for the last game
                self._notes_timer.stop()
                self._save_notes()
            self.notes_edit.blockSignals(True)
            self.notes_edit.setPlainText(self.game.get('notes') or '')
            self.notes_edit.blockSignals(False)
            self._notes_for = self.slug
            self.sec_notes.set_summary((self.game.get('notes') or '').strip().split('\n')[0][:40])
        self._fill()
        self._refresh_info(force=done_job)
        if not same:
            self._load_tips()
        self.snap_box.setVisible(has('btrfs') and self.game.get('source', 'nexus') == 'nexus'
                                 and bool(self.game.get('owned_dir')))
        if self.sec_files.is_open and not self.snap_box.isHidden():
            self._load_snapshots(force=not same)

    def _load_snapshots(self, force: bool = False):
        """The game's snapshots, asked from the CLI (when the section is open;
        again after a job, at most every few seconds)."""
        slug = self.slug
        if not slug:
            return
        if not force and self._snap_at[0] == slug and time.monotonic() - self._snap_at[1] < 3:
            return
        self._snap_at = (slug, time.monotonic())

        def done(data, err, slug=slug):
            if slug != self.slug:
                return
            try:
                self._fill_snapshots(data or {'available': False, 'why': err or "no answer",
                                              'snapshots': []})
            except RuntimeError:
                pass
        cli_json(['--snapshots', slug, '--json'], done, 30000)

    def _fill_snapshots(self, s: dict):
        clear_layout(self.snap_rows)
        snaps = s.get('snapshots') or []
        self.snap_take.setVisible(bool(s.get('available')))
        self.snap_enable.setVisible(bool(s.get('convertible')))
        if not s.get('available'):
            self.snap_note.setText(f"Not available: {s.get('why')}." if s.get('why') else "")
        elif not snaps:
            self.snap_note.setText("None yet. One is taken before every update, Proton change "
                                   "and winetricks run.")
        else:
            self.snap_note.setText("Roll back puts the game and its prefix (settings, saves) "
                                   "back the way they were. What's there now is kept as a "
                                   "snapshot too.")
        self.snap_title.setText(f"Snapshots · {len(snaps)}" if snaps else "Snapshots")
        for x in snaps:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            when = x['created'].replace('T', ' ')[:16]
            text = label(f"{x['label']}\n{when}", None, wrap=True)
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(text, 1)
            back = QPushButton("Roll back")
            back.clicked.connect(lambda _=False, n=x['name']: self._act(f"rollback:{n}"))
            row.addWidget(back)
            rm = icon_button(G['trash'], "Delete this snapshot", 28, "Ghost")
            rm.clicked.connect(lambda _=False, n=x['name']: self._act(f"snapshot-delete:{n}"))
            row.addWidget(rm)
            w = QWidget()
            w.setLayout(row)
            self.snap_rows.addWidget(w)

    def _load_tips(self):
        self.sugg_box.setVisible(False)
        self.tune_pg_row.setVisible(False)
        self.sec_launch.setVisible(self._launch_full)
        for key in ('store', 'compat', 'deal'):
            self._about_parts[key] = False
        self._sync_about()
        self.ac_notice.setVisible(False)
        self._store, self._tips_hltb = {}, None
        if self.slug:
            slug = self.slug

            def got(info, _err, slug=slug):
                if slug == self.slug:
                    try:
                        self._fill_about(info or {})
                    except RuntimeError:
                        pass
            cli_json(['--store-info', slug, '--json'], got, 60000, ttl=600)
        slug = self.slug
        if not slug:
            return

        def done(info, _err, slug=slug):
            if slug != self.slug:
                return
            try:
                self._fill_tips(info or {})
            except RuntimeError:
                pass
        self._tips_query = cli_json(['--game-tips', slug, '--json'], done, 60000, ttl=300)

    def _fill_tips(self, info):
        g = self.game
        external = g.get('source', 'nexus') != 'nexus'
        pdb = info.get('protondb')
        if pdb and pdb.get('tier'):
            color = TIER_COLORS.get(pdb['tier'], P['muted'])
            self.tune_pdb.setText(
                f"ProtonDB: <b style='color:{color}'>{html.escape(str(pdb['tier']).capitalize())}</b>"
                + (f" · trending {html.escape(str(pdb['trending']))}"
                   if pdb.get('trending') not in (None, pdb['tier']) else "")
                + f" · {int(pdb.get('total') or 0)} reports · "
                  f"<a href='{html.escape(pdb['url'])}' style='color:{P['accent']}'>open</a>")
        elif pdb:
            self.tune_pdb.setText("ProtonDB: no reports yet")
        self.tune_pdb.setVisible(bool(pdb) and has('proton'))
        deal = self._gamedb_line(info.get('gamedb'))
        self.tune_deal.setText(deal)
        self.tune_deal.setVisible(bool(deal))
        self._about_parts['deal'] = bool(deal)
        pg = info.get('protongen')
        show_pg = bool(pg) and external and has('proton')
        self.tune_pg_row.setVisible(show_pg)
        if show_pg:
            self.tune_pg.setText(pg['launch_options'])
            self.tune_pg.setCursorPosition(0)
        clear_layout(self.tune_rows)
        recipes = info.get('recipes', [])
        self.tune_sugg_title.setText(f"Suggestions for {info.get('gpu') or 'your GPU'}"
                                     if recipes else "")
        for i, r in enumerate(recipes):
            row = QHBoxLayout()
            name = plain_label(r['name'])
            name.setToolTip((r.get('symptom') and f"Fixes: {r['symptom']}\n" or '')
                            + r.get('description', '') + "\n\n" + r['launch_options'])
            row.addWidget(name, 1)
            if r.get('kind') == 'fix':
                row.addWidget(label("fix", "Muted"))
            if external:
                b = QPushButton("Copy")
                b.clicked.connect(lambda _=False, t=r['launch_options']: self._copy(t))
            else:
                b = QPushButton("Add")
                b.setToolTip("Adds this to the game's launch options now (Steam, Heroic and "
                             "launch.sh)")
                b.clicked.connect(lambda _=False, n=r['name']: self._emit(
                    ['--apply-recipe', self.slug, n], f"Applying '{n}'",
                    bool(self.game.get('steam'))))
            row.addWidget(b)
            row.setContentsMargins(0, 0, 0, 0)
            w = QWidget()
            w.setLayout(row)
            self.tune_rows.addWidget(w)
            w.setVisible(i < 5)
        if len(recipes) > 5:
            more = QPushButton(f"Show all {len(recipes)} suggestions")
            more.setObjectName("Ghost")

            def expand(_=False, m=more):
                for j in range(self.tune_rows.count()):
                    wid = self.tune_rows.itemAt(j).widget()
                    if wid is not None:
                        wid.setVisible(True)
                m.setVisible(False)
            more.clicked.connect(expand)
            self.tune_rows.addWidget(more)
        self.btn_explain.setVisible(bool(info.get('llm')) and not external)
        lines, more = compat_facts(info)
        self.compat_text.setText(more)
        self.compat_text.setVisible(bool(more))
        self._about_parts['compat'] = bool(more) or not self.tune_pdb.isHidden()
        warn = anticheat_warning(info)
        self.ac_notice.setText(warn or '')
        self.ac_notice.setVisible(bool(warn))
        sugg = has('proton') and bool(recipes)
        self.sugg_box.setVisible(sugg)
        if not self._launch_full:            # Steam/Heroic: advice only, when there is some
            self.sec_launch.setVisible(sugg or show_pg)
            self.sec_launch.set_summary(
                f"{len(recipes)} suggestion{'s' if len(recipes) != 1 else ''}" if recipes else '')
        self._sync_about()
        if pdb and pdb.get('tier'):
            t = pdb['tier']
            self.st_rating.set(t.capitalize(), f"ProtonDB · {pdb.get('total') or 0} reports",
                               TIER_COLORS.get(t))
        hl = ((info.get('gamedb') or {}).get('hltb') or {}) or (info.get('hltb') or {})
        self._tips_hltb = info.get('hltb') or (hl if hl.get('main') else None)
        self._fill_context()
        if self._store or self._tips_hltb:
            self._fill_about()

    def _save_notes(self):
        if self._notes_for:
            text = self.notes_edit.toPlainText()[:MAX_NOTE]
            if self.game.get('slug') == self._notes_for:
                self.game['notes'] = text
            self.notes_changed.emit(self._notes_for, text)

    def _fill_about(self, info: dict | None = None):
        if info is not None:
            self._store = info
        info = self._store or {}
        st = info.get('store') or {}
        self._fill_context()
        self.about_text.setText(about_html(st, info.get('reviews'), self._tips_hltb,
                                           skip=tuple(self.context.shows)))
        clear_layout(self.trailer_rows)
        for t in (st.get('trailers') or [])[:3]:
            b = QPushButton(t.get('name') or "Trailer")
            b.setObjectName("Ghost")
            set_glyph_icon(b, G['play'], 14, 'accent')
            b.setToolTip("Play the trailer here")
            b.setStyleSheet("text-align: left;")
            b.clicked.connect(lambda _=False, t=t: self.trailer.emit(
                {**t, 'game': self.game.get('display_name', '')}))
            self.trailer_rows.addWidget(b)
        clear_layout(self.similar_rows)
        similar = (info.get('similar') or [])[:6]
        self.similar_title.setVisible(bool(similar))
        for s_ in similar:
            if s_.get('owned_slug'):
                b = QPushButton(f"{s_['name']}  ·  in your library")
                b.setObjectName("Ghost")
                b.setStyleSheet("text-align: left;")
                b.clicked.connect(lambda _=False, sl=s_['owned_slug']: self.reveal.emit(sl))
                self.similar_rows.addWidget(b)
            elif str(s_.get('url', '')).startswith('https://store.steampowered.com/'):
                lab = label(f"<a href='{html.escape(s_['url'])}' style='color:{P['text']}'>"
                            f"{html.escape(s_['name'])}</a>", None)
                lab.setOpenExternalLinks(True)
                self.similar_rows.addWidget(lab)
        self._about_parts['store'] = bool(st or info.get('reviews') or similar or self._tips_hltb)
        rv = info.get('reviews') or {}
        self._about_summary = rv.get('desc') or ', '.join((st.get('genres') or [])[:2])
        self._sync_about()

    def _sync_about(self):
        """About shows when any of its parts has something: the store blurb,
        Game-db's line, compatibility facts, achievements."""
        parts = self._about_parts
        self.store_box.setVisible(parts['store'])
        self.compat_box.setVisible(parts['compat'])
        self.ach_box.setVisible(parts['ach'])
        self.sec_about.setVisible(any(parts.values()))
        ach = self.game.get('achievements') if parts['ach'] else None
        self.sec_about.set_summary(" · ".join(x for x in (
            getattr(self, '_about_summary', '') if parts['store'] else '',
            f"{ach[0]} / {ach[1]}" if ach else '') if x))

    @staticmethod
    def _gamedb_line(gd) -> str:
        """Rich-text Game-db line: wishlist price, or HLTB/progress for owned
        games, or the release countdown. Every value is escaped; only https
        links are made clickable."""
        if not gd:
            return ''
        esc = html.escape
        bits = []
        price = gd.get('price') or {}
        if gd.get('list') == 'wishlist':
            if price:
                p = f"<b>{esc(price.get('currentStr') or '')}</b>"
                if price.get('cutPct'):
                    p += f" −{int(price['cutPct'])}%"
                bits.append(f"On your wishlist: {p}")
                if price.get('preorder'):
                    bits.append("pre-order")
                elif price.get('atHistoricalLow'):
                    bits.append(f"<span style='color:{P['success']}'>all-time low</span>")
                elif price.get('historicalLowStr'):
                    bits.append(f"low {esc(price['historicalLowStr'])}")
                url = gamedb.safe_url((price.get('best') or {}).get('url'))
                if url:
                    bits.append(f"<a href='{esc(url)}' style='color:{P['accent']}'>open deal</a>")
            else:
                bits.append("On your wishlist")
            rel = gd.get('release')
            if rel:
                bits.append(esc(gamedb.release_label(rel)))
        else:
            hl = gd.get('hltb') or {}
            if hl.get('main'):
                bits.append(f"HLTB {hl['main']:g} h main")
            c = gd.get('completion') or {}
            if c.get('state') == 'in-progress':
                bits.append(f"{int(c.get('fraction', 0) * 100)}% through")
            elif c.get('label'):
                bits.append(esc(c['label']))
        if gd.get('status_label') or gd.get('status'):        # IGDB: Early Access, Beta…
            bits.append(esc(gd.get('status_label') or str(gd['status']).replace('_', ' ')))
        if not bits:
            return ''
        page = gamedb.safe_url(gd.get('url'), gd.get('url', '').rsplit('/game/', 1)[0])
        head = (f"<a href='{esc(page)}' style='color:{P['accent']}'>Game-db</a>"
                if page else "Game-db")
        ago = fmt_ago(gd.get('fetched_at'))
        return f"{head}: " + " · ".join(bits) + (
            f" <span style='color:{P['muted']}'>({ago})</span>" if ago else '')

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.toast.emit("Copied — paste it into Steam → Properties → Launch options."
                        if self.game.get('source') == 'steam' else "Copied.", 'success')

    def _emit_menu(self):
        if self.game:
            self.menu.emit(self.game, self.btn_more.mapToGlobal(
                QPoint(0, self.btn_more.height())))

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu:                 # X, from anywhere in the panel
            self._emit_menu()
        else:
            super().keyPressEvent(ev)

    def _fill(self):
        g = self.game
        if not g:
            return
        self.hero.set_game(g)
        self.name.setText(g.get('display_name', g['slug']))
        self.name.setVisible(not self.hero.has_logo())     # the logo already names it
        src = g.get('source', 'nexus')
        while self.chips.count():
            it = self.chips.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for text, role, on in (
                (SOURCE_LABEL.get(src, ''), None, True),
                ("Not installed", 'muted', not is_installed(g)),
                ("Playing now", 'success', g.get('playing')),
                ("Starting…", 'accent', g.get('launching') and not g.get('playing')),
                ("Update ready", 'warning', g.get('update_pending')
                 and not (g.get('repack_online') and not g.get('repack_update'))),
                ("Update posted", 'warning', g.get('repack_online') and not g.get('repack_update')),
                ("Also in " + " & ".join(SOURCE_LABEL.get(x, x) for x in g.get('dup_also') or []),
                 None, g.get('dup_also')),
                ("Custom launch options", 'accent', g.get('launch_options'))):
            if on and text:
                self.chips.addWidget(_pill(text, role or 'muted'))
        rel = status_pill(self._context_facts())
        if rel:
            self.chips.addWidget(_pill(*rel))
        self.chips_host.setVisible(self.chips.count() > 0)
        external = src != 'nexus'
        self._launch_full = not external and has('launch_profiles')
        for w in (self.runtime_box, self.perf, self.adv_box):
            w.setVisible(self._launch_full)
        for sec in (self.sec_files, self.sec_trouble):
            sec.setVisible(not external)
        if self._launch_full:
            self.sec_launch.setVisible(True)
        elif not self.slug or self.sugg_box.isHidden() and self.tune_pg_row.isHidden():
            self.sec_launch.setVisible(False)
        if external:
            self.confirm.setVisible(False)
        self._fill_quick(g)
        self._fill_context()
        self.btn_play.setText(play_label(g))
        self.btn_play.setEnabled(play_enabled(g))
        self._fill_stats(g)
        if self._launch_full:
            self.sec_launch.set_summary(g.get('proton') or '')
        self.sec_files.set_summary(" · ".join(x for x in (
            "Steam" if g.get('steam') else '', "Heroic" if g.get('heroic') else '') if x))
        self.ov_steam.setText("In your Steam library" if g.get('steam') else "Not added")
        self.btn_steam.setText("Remove" if g.get('steam') else "Add")
        self.ov_heroic.setText("In your Heroic library" if g.get('heroic') else "Not added")
        self.btn_heroic.setText("Remove" if g.get('heroic') else "Add")
        exe = g.get('exe')
        self._folder = str(Path(exe).parent) if exe else (g.get('wine_prefix') or '')
        self.ov_folder.setText(soft_wrap(self._folder) or '—')
        self.btn_copy_folder.setVisible(bool(self._folder))
        # A refresh (library reload, a game starting…) must not throw away
        # what's being typed: edited fields keep their text until applied.
        if 'name' not in self._dirty and not self.ed_name.hasFocus():
            self.ed_name.setText(g.get('display_name', ''))
        self.ed_exe.setText(exe or '')
        if 'opts' not in self._dirty and not self.ed_opts.hasFocus():
            self.ed_opts.setText(g.get('launch_options') or '')
        self.btn_copy_opts.setVisible(bool(self.ed_opts.text()))
        self._update_pending()

    def _context_facts(self) -> dict | None:
        """The game's facts: the index first; else what --store-info brought
        (a game the index has not reached yet); the play length from
        --game-tips (HowLongToBeat or Game-db) when the index has none."""
        g = self.game
        rec = game_facts(g.get('slug'))
        st = (self._store or {}).get('store')
        if rec is None and st:
            rec = facts.merge({'appid': g.get('appid'), 'name': g.get('display_name'),
                               'steam': facts.from_steam(st), 'hltb': None})
        live = self._tips_hltb or {}
        if live.get('main') and not (rec or {}).get('length'):
            rec = {**(rec or {}), 'length': {**live, 'source': live.get('source') or 'hltb'}}
        return rec

    def _fill_context(self):
        self.context.set_facts(self._context_facts(), self.game.get('playtime_minutes') or 0)
        # Tab order: Play, ⋮, the quick buttons, the tags, More, then the rest.
        prev = self.btn_more
        for w in [*self._quick_btns.values(), *self.context.tag_btns, self.context.more]:
            QWidget.setTabOrder(prev, w)
            prev = w

    def _open_about(self):
        self.open_section('about')

    def _fill_quick(self, g):
        """Round buttons for the handful of things you'd actually do here.
        Rebuilt only when the set of actions changes: a refresh (or pressing
        the star with a pad) keeps the buttons — and the focus on them."""
        src = g.get('source', 'nexus')
        fav = bool(g.get('favorite'))
        acts = [(G['star'] if fav else G['star_outline'],
                 "Remove from favourites" if fav else "Add to favourites", 'favorite')]
        if not is_installed(g):
            if src == 'steam':
                acts.append((G['steam'], "Open in Steam", 'steam-page'))
        elif src == 'steam':
            acts += [(G['steam'], "Open in Steam", 'steam-page'),
                     (G['settings'], "Steam properties", 'steam-props'),
                     (G['wrench'], "Tune in protongen", 'protongen')]
        elif src == 'nexus':
            acts += [(G['tune'], "Launch", '__launch'),
                     (G['log'], "Log", '__log')]
        if is_installed(g):
            acts.append((G['folder'], "Open game folder", 'open-folder'))
        acts.append((G['image'], "Change artwork", 'artwork'))
        keys = tuple(a[2] for a in acts)
        if keys == getattr(self, '_quick_keys', None):
            star = self._quick_btns.get('favorite')
            if star is not None and bool(star.property('on')) != fav:
                star.setText(acts[0][0])
                star.setToolTip(acts[0][1])
                star.setProperty('on', fav)
                star.style().unpolish(star)
                star.style().polish(star)
            return
        clear_layout(self.quick)
        self._quick_keys, self._quick_btns = keys, {}
        for glyph, tip, act in acts:
            b = icon_button(glyph, tip, 38, "QuickAction", round_=True)
            self._quick_btns[act] = b
            if act == 'favorite' and fav:
                b.setProperty('on', True)
            if act == '__launch':
                b.clicked.connect(lambda: self.open_section('launch'))
            elif act == '__log':
                b.clicked.connect(lambda: self.open_section('log'))
            else:
                b.clicked.connect(lambda _=False, a=act: self._act(a))
            self.quick.addWidget(b)
        self.quick.addStretch()

    def _fill_stats(self, g):
        mins = g.get('playtime_minutes') or 0
        self.st_played.set(fmt_hours(mins) or "0 h", "Play time")
        last = g.get('last_played')
        self.st_last.set(fmt_relative(last) or "Never", "Last played",
                         tip=last.replace('T', ' ') if last else '')
        if is_installed(g):
            self.st_size.set(fmt_size(g.get('disk_bytes')) or "—", "On disk")
            added = g.get('added_at')
            self.st_added.set(fmt_relative(added) or "—", "Installed",
                              tip=added.replace('T', ' ') if added else '')
        else:
            self.st_size.set(fmt_size(g.get('download_bytes')) or "—", "Download")
            self.st_added.set(play_label(g) if g.get('installing') else "No", "Installed")
        proton = (self.info.get('proton') if self.info else None) or g.get('proton')
        src = g.get('source', 'nexus')
        self.st_proton.set(proton or ("Steam's choice" if src == 'steam' else "Default"),
                           "Proton")
        ach = g.get('achievements')
        self.st_ach.setVisible(bool(ach))
        self._about_parts['ach'] = bool(ach)
        self._sync_about()
        if ach:
            done, total = ach
            self.st_ach.set(f"{done} of {total}", f"Achievements · {round(100 * done / total)}%",
                            P['success'] if done == total else None)
            if self.sec_about.is_open and self._ach_for != (g['slug'], done):
                self._load_achievements()
        tier = g.get('protondb')
        if tier in TIER_COLORS:
            self.st_rating.set(tier.capitalize(), "ProtonDB", TIER_COLORS[tier])
        else:
            self.st_rating.set(SOURCE_LABEL.get(src, ''), "Library" if src != 'steam'
                               else "ProtonDB")

    def _load_achievements(self):
        """The list, from Steam's stats cache via the CLI — only once the
        section is opened (and again when the count changes)."""
        g = self.game
        if not g.get('achievements') or g.get('source', 'nexus') not in ('steam', 'nexus'):
            return
        if self._ach_for == (g['slug'], g['achievements'][0]):
            return                                  # already listed
        self._ach_for = (g['slug'], g['achievements'][0])
        clear_layout(self.ach_rows)
        self.ach_rows.addWidget(label("Loading…", "Muted"))
        slug = g['slug']

        def done(data, err):
            if self.game.get('slug') != slug:
                return
            self._fill_achievements(data or {}, err)
        ident = str(g['appid']) if g.get('source') == 'steam' else g['slug']
        cli_json(['--achievements', ident, '--json'], done, 30000, ttl=120)

    ACH_SHOWN = 60

    def _fill_achievements(self, data, err):
        clear_layout(self.ach_rows)
        items = data.get('items') or []
        if not items:
            self.ach_rows.addWidget(label(err or "Steam hasn't recorded any yet.", "Muted"))
            return
        for a in items[:self.ACH_SHOWN]:
            self.ach_rows.addWidget(AchievementRow(a))
        if len(items) > self.ACH_SHOWN:
            self.ach_rows.addWidget(label(f"…and {len(items) - self.ACH_SHOWN} more", "Muted"))

    def open_section(self, key):
        # Older keys still work: they name what moved into the five sections.
        sec = {'launch': self.sec_launch, 'tune': self.sec_launch,
               'files': self.sec_files, 'snapshots': self.sec_files,
               'troubleshoot': self.sec_trouble, 'log': self.sec_trouble,
               'about': self.sec_about, 'compat': self.sec_about,
               'achievements': self.sec_about, 'notes': self.sec_notes}.get(key)
        if sec is None or sec.isHidden():
            return
        sec.set_open(True)
        QTimer.singleShot(0, lambda: alive(sec.head) and focus_and_reveal(sec.head))

    def _refresh_info(self, force: bool = False):
        slug = self.slug
        if not slug or self.game.get('source', 'nexus') != 'nexus':
            return
        # Selection syncs often (every library refresh); one query per game
        # every few seconds is plenty, and again as soon as a job finishes.
        last = getattr(self, '_info_at', (None, 0.0))
        if not force and last[0] == slug and time.monotonic() - last[1] < 5 and self.info:
            return
        self._info_at = (slug, time.monotonic())

        def done(info, err, slug=slug):
            if slug != self.slug:
                return
            try:
                self._apply_info(info or {}, err)
            except RuntimeError:
                pass
        self._info_query = cli_json(['--show-launch', slug, '--json'], done)

    def _apply_info(self, info, err):
        self.info = info
        pinned = info.get('proton_pinned')
        items = [(f"Default ({info.get('proton') if not pinned else 'global'})", '')] \
            + [(n, n) for n in info.get('protons', [])]
        choice = self.cb_proton.currentData() if 'proton' in self._dirty else None
        if items != [(self.cb_proton.itemText(i), self.cb_proton.itemData(i))
                     for i in range(self.cb_proton.count())]:
            # Rebuilt only when the list changed: a refresh doesn't close an
            # open popup or drop a pick that hasn't been applied yet.
            self.cb_proton.blockSignals(True)
            self.cb_proton.clear()
            for text, data in items:
                self.cb_proton.addItem(text, data)
            self.cb_proton.blockSignals(False)
        want = choice if choice is not None else (pinned or '')
        self.cb_proton.setCurrentIndex(max(0, self.cb_proton.findData(want)))
        if info.get('exe'):
            self.ed_exe.setText(info['exe'])
        prof = info.get('profile') or {}
        via = info.get('launch_via') or 'nexus'
        self._base = {'proton': pinned or '', 'opts': info.get('launch_options') or '',
                      'via': via}
        if 'perf' not in self._dirty:
            self.perf.set_state(info.get('perf') or {}, info.get('perf_tools'), info.get('niri'),
                                info.get('lsfg'))
            self._perf_base = self.perf.items()
        if 'via' not in self._dirty:
            self.seg_via.set_current(via, animate=False)
        steam_item = self.seg_via.item('steam')
        steam_item.setEnabled(bool(info.get('in_steam')) or via == 'steam')
        if not info.get('in_steam'):
            steam_item.setToolTip("Add the game to Steam first (Game and files → Steam)")
        if 'opts' not in self._dirty and not self.ed_opts.hasFocus():
            self.ed_opts.setText(self._base['opts'])
            self.ed_opts.setCursorPosition(0)
        self.btn_copy_opts.setVisible(bool(self.ed_opts.text()))
        self.opts_state.setText(
            f"Managed here · from {prof.get('source', '?')}" if info.get('owns_options') else
            "Not managed yet: Steam and Heroic keep their own options until you apply some here.")
        pg = info.get('protongen')
        pd = info.get('protongen_default')
        self.btn_pg_default.setEnabled(bool(pd) and not pd.get('applied'))
        self.btn_pg_default.setToolTip(
            "No default profile in protongen yet (Settings → Save as default profile)"
            if not pd else "This game already uses your protongen defaults" if pd.get('applied')
            else f"Replace this game's options with protongen's default profile:\n"
                 f"{pd.get('launch_options') or '(no options)'}"
                 + (f"\nProton: {pd['proton']}" if pd.get('proton') else ''))
        self.btn_pg_open.setEnabled(bool(info.get('protongen_installed')))
        if not info.get('protongen_installed'):
            self.pg_state.setText("protongen isn't installed.")
            self.pg_opts.setVisible(False)
            self.btn_pg_pull.setEnabled(False)
            self.pg_notice.setVisible(False)
        elif pg:
            state = "differs from the options here" if pg.get('differs') else "in sync"
            self.pg_state.setText(f"Has tuning for this game (its {pg['source']} entry) — {state}.")
            self.pg_opts.setText(pg.get('launch_options') or '(no options)')
            self.pg_opts.setCursorPosition(0)
            self.pg_opts.setVisible(True)
            self.btn_pg_pull.setEnabled(bool(pg.get('differs')))
            self.pg_notice.setVisible(bool(pg.get('differs')) and has('protongen'))
            self.pg_notice_text.setText("protongen has different tuning for this game. Pull it "
                                        "in to use it in Steam, Heroic and launch.sh.")
        else:
            self.pg_state.setText(
                "No tuning saved for this game in protongen. Use your protongen defaults, "
                "or tune it there (via its Steam or Heroic entry) and pull it in." if pd else
                "No tuning saved for this game yet — tune it in protongen "
                "(via its Steam or Heroic entry), then pull it in.")
            self.pg_opts.setVisible(False)
            self.btn_pg_pull.setEnabled(False)
            self.pg_notice.setVisible(False)
        if self.sec_trouble.is_open:
            self._load_log()
        if self.game:
            self._fill_stats(self.game)
        self._update_pending()

    def _load_bench(self):
        slug = self.slug
        if not slug or self.game.get('source', 'nexus') != 'nexus':
            self.bench_text.setVisible(False)
            return

        def done(data, _err, slug=slug):
            if slug != self.slug:
                return
            runs = (data or {}).get('runs') or []
            self.bench_text.setText("<b>Benchmarks</b><br>" + "<br>".join(
                f"{html.escape(fmt_relative(r['at']) or r['at'])}: avg {r['avg_fps']:g} fps · "
                f"1% low {r['low_1']:g} · 0.1% low {r['low_01']:g} · {r['seconds']:g} s"
                for r in runs[:3]))
            self.bench_text.setVisible(bool(runs))
        cli_json(['--bench-results', slug, '--json'], done, 30000, ttl=30)

    def _load_log(self):
        self._load_bench()
        path = self.info.get(self.cb_log.currentData())
        if not path:
            self.logview.setPlainText("Nothing logged yet." if self.info else "Loading…")
            return
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            self.logview.setPlainText(f"Couldn't read {path}: {e}")
            return
        if len(data) > 2_000_000:
            data = b"[... earlier output truncated ...]\n" + data[-2_000_000:]
        self.logview.setPlainText(data.decode('utf-8', 'replace'))
        self.logview.moveCursor(self.logview.textCursor().MoveOperation.End)

    # ---- actions ----
    def _emit(self, args, title, needs_steam, kind='task'):
        name = self.game.get('display_name', self.slug)
        self.request.emit(args, f"{title} — {name}", needs_steam, kind)

    def _act(self, name):
        self.request.emit(['__action__', name], '', False, 'action')

    def _toggle_steam(self):
        on = self.game.get('steam')
        self._emit(['--remove-steam' if on else '--steam', self.slug],
                   "Removing from Steam" if on else "Adding to Steam", True)

    def _toggle_heroic(self):
        on = self.game.get('heroic')
        self._emit(['--remove-heroic' if on else '--heroic', self.slug],
                   "Removing from Heroic" if on else "Adding to Heroic", False)

    # The launch settings that wait for Apply (the rest act at once).
    LAUNCH_KEYS = frozenset({'proton', 'opts', 'perf', 'via'})

    def _name_edited(self, _text):
        self._dirty.add('name')

    def _opts_edited(self, _text):
        self._dirty.add('opts')
        self._update_pending()

    def _proton_picked(self, _i):
        self._dirty.add('proton')
        self._update_pending()

    def _perf_edited(self):
        self._dirty.add('perf')
        self._update_pending()

    def _via_picked(self, _key):
        self._dirty.add('via')
        self._update_pending()

    def _pending_ops(self) -> list[tuple[list, str, bool]]:
        """The CLI jobs Apply would run, in order: Proton, the typed line,
        the switches that changed, where Play starts from."""
        if not self.slug or not self.info or not self._launch_full:
            return []
        steam = bool(self.game.get('steam'))
        ops = []
        proton = self.cb_proton.currentData()
        if 'proton' in self._dirty and proton is not None and proton != self._base['proton']:
            ops.append((['--proton', self.slug, proton or 'default'], "Switching Proton", steam))
        text = self.ed_opts.text().strip()
        if 'opts' in self._dirty and text != self._base['opts']:
            ops.append((['--set-launch', self.slug, text],
                        "Applying launch options" if text else "Clearing launch options", steam))
        if 'perf' in self._dirty:
            changed = [i for i in self.perf.items() if i not in self._perf_base]
            if changed:
                ops.append((['--perf', self.slug, *changed], "Applying performance settings",
                            steam))
        via = self.seg_via.current() or 'nexus'
        if 'via' in self._dirty and via != self._base['via']:
            ops.append((['--launch-via', self.slug, via],
                        "Launching through Steam" if via == 'steam' else "Launching from Nexus",
                        False))
        return ops

    def _update_pending(self):
        ops = [] if self._sent else self._pending_ops()
        n = sum(len(args) - 2 if args[0] == '--perf' else 1 for args, _t, _s in ops)
        self.pending_text.setText(f"{n} change{'s' if n != 1 else ''} to apply")
        self.apply_bar.setVisible(bool(ops))

    def _apply_launch(self):
        ops = self._pending_ops()
        if not ops or self._sent:
            return
        line = next((args[2] for args, _t, _s in ops if args[0] == '--set-launch'), '')
        preview = protongen_code_preview(line) if line else None
        if preview is not None:          # someone else's preset: show what it runs first
            ask(self, "Apply this preset?", preview, "Apply",
                lambda: self._send_ops(ops), danger=False)
            return
        self._send_ops(ops)

    def _send_ops(self, ops):
        # The jobs run one after another, in this order. The edits stay on
        # screen until the game's jobs are done and its settings are read again.
        self._sent = True
        self._update_pending()
        for args, title, steam in ops:
            self._emit(args, title, steam)

    def _discard_launch(self):
        self._dirty -= self.LAUNCH_KEYS
        self._sent = False
        self.ed_opts.setText(self._base['opts'])
        if self.info:
            self._apply_info(self.info, None)
        else:
            self._update_pending()

    def _rename(self):
        name = self.ed_name.text().strip()
        self._dirty.discard('name')
        if name and name != self.game.get('display_name'):
            self._emit(['--rename', self.slug, name], "Renaming", bool(self.game.get('steam')))

    def _pick_exe(self):
        start = self.ed_exe.text() or self.game.get('wine_prefix') or str(HOME)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the game executable",
            str(Path(start).parent if start.endswith('.exe') else start),
            "Windows executables (*.exe)")
        if path:
            self._emit(['--set-exe', self.slug, path], "Changing executable",
                       bool(self.game.get('steam')))

    def _pull_protongen(self):
        self._emit(['--from-protongen', self.slug], "Pulling protongen tuning",
                   bool(self.game.get('steam')))

    def _protongen_default(self):
        self._emit(['--protongen-default', self.slug], "Applying protongen defaults",
                   bool(self.game.get('steam')))

    def _open_protongen(self):
        self._act('protongen')

    def ask_uninstall(self):
        name = self.game.get('display_name', self.slug)
        self.confirm_text.setText(
            f"Uninstall {name}? This deletes its install folder and removes it from Steam, "
            f"Heroic and the app menu. Logs are kept.")
        self.confirm_snaps.setChecked(False)
        self.confirm_snaps.hide()
        if not self.snap_box.isHidden():
            def done(data, _err, slug=self.slug):
                n = len((data or {}).get('snapshots') or [])
                if slug == self.slug and n and alive(self.confirm_snaps):
                    self.confirm_snaps.setText(
                        f"Also delete its {n} snapshot{'s' if n > 1 else ''} "
                        f"(they hold copies of its saves)")
                    self.confirm_snaps.show()
            cli_json(['--snapshots', self.slug, '--json'], done, 30000)
        self.confirm.setVisible(True)
        page = self.stack.currentWidget()
        if isinstance(page, QScrollArea):
            QTimer.singleShot(0, lambda: alive(page, self.confirm)
                              and page.ensureWidgetVisible(self.confirm))

    def _do_uninstall(self):
        self.confirm.setVisible(False)
        args = ['--uninstall', self.slug, '--yes']
        if has('save_backup') and self.confirm_backup.isChecked():
            args.append('--save-backup')
        if not self.confirm_snaps.isHidden() and self.confirm_snaps.isChecked():
            args.append('--remove-snapshots')
        self._emit(args, "Uninstalling", bool(self.game.get('steam')))



def _hide_row(row):
    """Hide every widget in one of DetailsPanel._row's rows."""
    for i in range(row.count()):
        if (w := row.itemAt(i).widget()) is not None:
            w.hide()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .actions import about_html  # noqa: E402
from .app import main  # noqa: E402
from .art import (ART, art_source, fit_image, HERO_KINDS, LOGO_KINDS, placeholder_pixmap,
    rounded_pixmap)  # noqa: E402
from .dashboard import ClickRow  # noqa: E402
from .deals import _pill  # noqa: E402
from .gameinfo import (anticheat_warning, compat_facts, is_installed, play_enabled,
    play_label, SOURCE_LABEL, TIER_COLORS)  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json, in_thread  # noqa: E402
from .widgets import (amp, ask, clear_layout, FlowLayout, icon_button, icon_label, label,
    NoWheelCombo, plain_label, protongen_code_preview, SegmentedControl, set_glyph_icon,
    soft_wrap)  # noqa: E402
