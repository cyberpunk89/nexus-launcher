"""First-run setup: a page (never a dialog — niri floats those) that asks only
what changes how Nexus behaves, and shows the facts each answer needs where
it is asked (w-104). Three steps and an optional fourth:

1. How you play — what Nexus found here (Steam, Heroic, a torrent client),
   "Play the games I have" or "Also install repacks" (picked for you from
   what was said before or a client that answers; nothing found picks
   nothing), and whether new games are added to Steam, the app menu, Heroic.
2. Where games go (repack mode only) — drives as cards with free space, the
   folder, where downloads come from, and only what blocks an install.
3. Make it yours — theme, size, the background agent (what it installs is
   said before the choice) and the install count ask (nothing picked).
+  Connect services (optional, w-105) — SteamGridDB, the Steam Web API and
   Game-db: why Nexus asks, how to get each key, the field. Empty writes
   nothing.

The facts come from one read-only `--setup-facts --json` (w-103) and the
library load; MainWindow hands them over (set_facts, set_found). Shown once
(library.json `setup_done`); Settings → System → "Run setup again" opens it
later. "Skip, use the defaults" writes nothing and ends setup.

The page is one centred column: the wordmark and a dot per step, the step's
title and lead, the step (scrolling when it doesn't fit), then the buttons.
Choices are cards (ChoiceCard, ToggleCard), styled from the theme's tokens."""

import os
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QRectF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (QButtonGroup, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLineEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from nexus import gamedb, platform, stats
from nexus.features import has

from .common import HOME, INSTALL_BASE_DEFAULT
from .theme import FONTS, FS, G, GRAD, P, SCALES, SETUP_THEMES, SP, THEMES

STEPS = (('play', "How you play"), ('where', "Where games go"), ('look', "Make it yours"),
         ('services', "Connect services"))
STATUS_GLYPH = {'ok': ('check_circle', 'success'), 'warn': ('warn', 'warning'),
                'fail': ('fail', 'danger'), 'info': ('info', 'muted')}
COLUMN_W = 860                 # the page's column never gets wider than this
CLIENTS = {'transmission': "Transmission", 'qbittorrent': "qBittorrent"}
STORES = (('legendary', "Epic"), ('gog', "GOG"), ('nile', "Amazon"))
NOT_FINISHED = "Some checks didn't finish"
# The agent card's title: with Downloads (repacks) or without (play only).
AGENT_TITLE = {True: "Keep checking downloads and sending alerts",
               False: "Keep sending your alerts"}


def facts_failed_text(err: str | None) -> str:
    """Ready to install when --setup-facts as a whole failed or timed out."""
    return f"Couldn't look at this computer ({err or 'no answer'}). Nexus uses the defaults."


# ---- pure helpers: facts in, what a step shows out ----
def _plural(n: int, one: str) -> str:
    return f"{n} {one}" + ("" if n == 1 else "s")


def _size(n: float) -> str:
    return f"{n / 1e12:.1f} TB" if n >= 1e12 else f"{n / 1e9:.0f} GB"


def client_name(client: str | None) -> str:
    return CLIENTS.get(client or '', (client or "a torrent client").capitalize())


def prepick(facts: dict | None, config: dict, counts: dict | None = None
            ) -> tuple[bool | None, str]:
    """How you play, picked for the user: (repacks?, the reason line). What
    [repacks] said before wins, then a torrent client that answers (or one in
    the config when the facts didn't come), then games found and no client.
    Nothing found: (None, '') and Next waits for a pick (user 2026-11-09)."""
    facts = facts or {}
    was = facts.get('repacks_was')
    if was is not None:
        return bool(was), ("Picked for you: repacks were on before." if was else
                           "Picked for you: repacks were off before.")
    tor = facts.get('torrent')
    if tor and tor.get('client') and not tor.get('error'):
        return True, f"Picked for you: {client_name(tor['client'])} is running here."
    if not facts and config.get('torrent_client'):
        return True, f"Picked for you: {client_name(config['torrent_client'])} is set up."
    counts = counts or {}
    if counts.get('steam') or counts.get('heroic'):
        return False, "Picked for you: games were found here and no torrent client."
    return None, ''


def found_counts(games: list[dict]) -> dict:
    """What the library load found, for How you play's tiles."""
    steam = sum(1 for g in games if g.get('source') == 'steam')
    heroic = [g for g in games if g.get('source') == 'heroic']
    runners = {g.get('runner') for g in heroic}
    return {'steam': steam, 'heroic': len(heroic),
            'heroic_stores': [name for r, name in STORES if r in runners]}


def found_tiles(counts: dict | None, facts: dict | None, heroic: bool | None = None
                ) -> list[tuple[str, str, str, str]]:
    """(glyph, gradient, name, line) per source found: Steam (with the drives
    its games are on), Heroic (Linux) and a torrent client that answered."""
    counts, facts = counts or {}, facts or {}
    heroic = has('heroic') if heroic is None else heroic
    out = []
    if counts.get('steam'):
        k = sum(1 for d in facts.get('drives') or [] if d.get('steam_games'))
        out.append(('steam', 'tide', "Steam", _plural(counts['steam'], "game")
                    + (f" · on {_plural(k, 'drive')}" if k else "")))
    if heroic and counts.get('heroic'):
        stores = counts.get('heroic_stores') or []
        out.append(('library', 'aurora', "Heroic", _plural(counts['heroic'], "game")
                    + (" · " + " and ".join(stores) if stores else "")))
    tor = facts.get('torrent')
    if tor and tor.get('client') and not tor.get('error'):
        out.append(('download', 'ember', client_name(tor['client']),
                    "running · answers on its web address"))
    return out


def drive_used(d: dict) -> float | None:
    if d.get('free') is None or not d.get('total'):
        return None
    return max(0.0, min(1.0, 1 - d['free'] / d['total']))


def drive_text(d: dict, windows: bool = False, system: str = 'C:') -> tuple[str, str, str]:
    """A drive card's (title, free space, note)."""
    path = d.get('path') or ''
    if windows:
        title = path + ("  System" if path.rstrip('\\/').upper() == system.upper() else "")
    else:
        title = path + (f"   {d['fs']}" if d.get('fs') else "")
    free = (f"{_size(d['free'])} free of {_size(d['total'])}"
            if d.get('free') is not None and d.get('total') else "Free space unknown")
    notes = (["Default folder"] if d.get('default') else []) + (
        [f"Steam library · {_plural(d['steam_games'], 'game')}"] if d.get('steam_games') else [])
    return title, free, " · ".join(notes)


def split_drives(drives: list[dict]) -> tuple[list[dict], list[str]]:
    """(cards: the default first, then by free space; paths that didn't
    answer). A default drive that didn't answer stays a card."""
    cards, stuck = [], []
    for d in drives or []:
        if str(d.get('id', '')).startswith('timeout:') and not d.get('default'):
            stuck.append(d['path'])
        else:
            cards.append(d)
    cards.sort(key=lambda d: (not d.get('default'), -(d.get('free') or 0)))
    return cards, stuck


NOT_CHECKED_WHY = ("Didn't answer within 3 s (a network drive?). Pick it with Change… if you "
                   "mean it.")


def not_checked_text(paths: list[str]) -> str:
    return f"Not checked: {', '.join(paths)}. {NOT_CHECKED_WHY}"


def need_rows(needs: list[dict], windows: bool) -> list[dict]:
    """Ready to install: what blocks an install, each with its fix; one ok
    row when nothing does; a check that didn't finish holds nothing up."""
    if not needs:
        return [{'status': 'ok', 'title': "Long paths and free space are fine" if windows
                 else "Wine and Proton are ready", 'detail': "Nothing to fix before games "
                 "install.", 'actions': []}]
    out = []
    for n in needs:
        if n.get('title') == NOT_FINISHED:
            out.append({'status': 'warn', 'title': NOT_FINISHED, 'actions': [],
                        'detail': "They took longer than 5 s. Nothing is held up: System › "
                                  "Health has the full list once you're in."})
        elif n.get('title') == 'Proton' and n.get('status') == 'fail':
            out.append({'status': 'fail', 'title': "No GE-Proton yet",
                        'detail': "Installers and games run with it. About 500 MB, checked "
                                  "before it's used. Runs in the background while you finish.",
                        'actions': n.get('actions') or []})
        elif n.get('title') == 'Long paths':
            out.append({'status': n.get('status', 'warn'), 'title': "Long folder names are off",
                        'detail': n.get('fix') or '', 'actions': n.get('actions') or []})
        else:
            detail = (n.get('detail') or '').strip()
            detail = detail[:1].upper() + detail[1:] if detail else ''
            fix = (n.get('fix') or '').strip()
            out.append({'status': n.get('status', 'warn'), 'title': n.get('title', ''),
                        'detail': ". ".join(p.rstrip('.') for p in (detail, fix) if p)
                                  + ("." if detail or fix else ""),
                        'actions': n.get('actions') or []})
    return out


def torrent_state(t: dict | None) -> tuple[str, str, str]:
    """('none' | 'error' | 'ok', the card's title, its note)."""
    if not t or not t.get('client'):
        return 'none', "No torrent client answered", ''
    name = client_name(t['client'])
    if t.get('error'):
        return 'error', f"{name} at {t.get('url') or 'its address'}", \
            f"Couldn't ask where it downloads ({t['error']})"
    if t.get('download_dir'):
        return 'ok', f"{name}, downloading to {t['download_dir']}", \
            "Nexus watches that folder and says when a download is ready to install."
    return 'ok', f"{name} at {t.get('url')}", \
        "Nexus watches your Downloads folder and says when a download is ready to install."


# ---- widgets ----
class StepTrack(QWidget):
    """The steps as numbered dots on a line: done ones ringed in the accent,
    the current one filled with the dusk gradient, the rest outlined. Steps
    from `optional_from` on are optional: a dashed ring with a "+", joined
    by a dotted line. Painted from the palette at paint time."""

    D, GAP = 22, 14             # dot diameter; the line between two dots

    def __init__(self):
        super().__init__()
        self.names: list[str] = []
        self.current = 0
        self.optional_from = 0
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_steps(self, names: list[str], current: int, optional_from: int | None = None):
        self.names, self.current = names, current
        self.optional_from = len(names) if optional_from is None else optional_from
        self.setFixedSize(self.sizeHint())
        self.setToolTip(" → ".join(names))
        self.update()

    def sizeHint(self) -> QSize:
        n = max(1, len(self.names))
        return QSize(n * self.D + (n - 1) * self.GAP + 2, self.D + 2)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(FONTS.ui, -1, QFont.Weight.DemiBold)
        f.setPixelSize(FS['xs_plus'])
        pt.setFont(f)
        y = 1 + self.D / 2
        for i in range(len(self.names)):
            x = 1 + i * (self.D + self.GAP)
            opt = i >= self.optional_from
            if i:                               # the line from the dot before
                pen = QPen(QColor(P['accent'] if i <= self.current else P['border']), 2.0)
                if opt:
                    pen.setStyle(Qt.PenStyle.DotLine)
                pt.setPen(pen)
                pt.drawLine(int(x - self.GAP + 3), int(y), int(x - 3), int(y))
            r = QRectF(x, 1.0, float(self.D), float(self.D))
            if i == self.current and not opt:
                g = QLinearGradient(r.topLeft(), r.bottomRight())
                a, b = (QColor(c) for c in GRAD['dusk'])
                g.setColorAt(0.0, a)
                g.setColorAt(1.0, b)
                pt.setPen(Qt.PenStyle.NoPen)
                pt.setBrush(g)
                fg = P['on_accent']
            elif i < self.current:
                tint = QColor(P['accent'])
                tint.setAlphaF(0.16)
                pt.setPen(QPen(QColor(P['accent']), 1.5))
                pt.setBrush(tint)
                fg = P['accent']
            else:
                pen = QPen(QColor(P['accent'] if i == self.current else P['border']), 1.5)
                if opt:
                    pen.setStyle(Qt.PenStyle.DashLine)
                pt.setPen(pen)
                pt.setBrush(Qt.BrushStyle.NoBrush if opt else QColor(P['surface_2']))
                fg = P['accent'] if i == self.current else P['muted']
            pt.drawEllipse(r.adjusted(0.75, 0.75, -0.75, -0.75))
            pt.setPen(QColor(fg))
            pt.drawText(r, Qt.AlignmentFlag.AlignCenter, "+" if opt else str(i + 1))
        pt.end()


class StepStack(QStackedWidget):
    """The steps, sized by the one shown (a QStackedWidget asks every page,
    so a short step would scroll as far as the tallest)."""

    def _cur(self):
        return self.currentWidget()

    def sizeHint(self) -> QSize:
        return self._cur().sizeHint() if self._cur() else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self._cur().minimumSizeHint() if self._cur() else super().minimumSizeHint()

    def hasHeightForWidth(self) -> bool:
        return bool(self._cur()) and self._cur().hasHeightForWidth()

    def heightForWidth(self, w: int) -> int:
        return self._cur().heightForWidth(w) if self._cur() else -1


class ChoiceCard(QPushButton):
    """One of a few choices, as a card: a checkable button holding a layout
    (a preview, a name, a note). Its size is its layout's; the frame and the
    picked state come from the stylesheet (#ChoiceCard)."""

    def __init__(self, name: str):
        super().__init__()
        self.setObjectName("ChoiceCard")
        self.setCheckable(True)
        self.setAccessibleName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(SP['2_5'], SP['2_5'], SP['2_5'], SP['2_5'])
        self.body.setSpacing(SP['1'])

    def add(self, w: QWidget):
        w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.body.addWidget(w)

    def sizeHint(self) -> QSize:
        return self.body.sizeHint().expandedTo(self.minimumSize())

    def minimumSizeHint(self) -> QSize:
        return self.body.minimumSize()


class CardChoice(QWidget):
    """One-of-N ChoiceCards in a row that wraps when it runs out of width.
    It answers like the combo box it replaced (findData, currentData,
    setCurrentIndex, activated), so the page's logic is the same."""
    activated = pyqtSignal(int)

    def __init__(self, spacing: int = SP['2'], pick_first: bool = True):
        super().__init__()
        self.lay = FlowLayout(self, 0, spacing)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self.activated.emit)
        self._data: list = []
        self.pick_first = pick_first          # False: nothing picked until the user picks

    def add(self, card: ChoiceCard, data):
        self.group.addButton(card, len(self._data))
        self._data.append(data)
        self.lay.addWidget(card)
        if len(self._data) == 1 and self.pick_first:
            card.setChecked(True)

    def clear(self):
        """Every card out (a step filled again from new facts)."""
        for b in self.group.buttons():
            self.group.removeButton(b)
        self._data = []
        clear_layout(self.lay)

    def clear_choice(self):
        """None picked (an exclusive group can't uncheck its last button
        while it's exclusive)."""
        self.group.setExclusive(False)
        for b in self.group.buttons():
            b.setChecked(False)
        self.group.setExclusive(True)

    def cards(self) -> list:
        return [self.group.button(i) for i in range(len(self._data))]

    def count(self) -> int:
        return len(self._data)

    def findData(self, data) -> int:
        return self._data.index(data) if data in self._data else -1

    def currentIndex(self) -> int:
        return self.group.checkedId()

    def currentData(self):
        i = self.group.checkedId()
        return self._data[i] if 0 <= i < len(self._data) else None

    def setCurrentIndex(self, i: int):
        b = self.group.button(i)
        if b is not None:
            b.setChecked(True)


class ToggleCard(QFrame):
    """An on/off choice as a card: a title, a line about it, and its switch.
    A click anywhere on the card flips the switch (when it's enabled); the
    card is marked while it's on (#ToggleCard[on="true"])."""

    def __init__(self, sw, title: str, note=None):
        super().__init__()
        self.sw = sw
        self.setObjectName("ToggleCard")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        sw.setAccessibleName(title)
        row = QHBoxLayout(self)
        row.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        row.setSpacing(SP['4'])
        text = QVBoxLayout()
        text.setSpacing(SP['0_5'])
        self.title = label(title, "ChoiceTitle", wrap=True)
        text.addWidget(self.title)
        if isinstance(note, str):
            note = label(note, None, wrap=True)
        if note is not None:
            note.setObjectName("ChoiceNote")
            text.addWidget(note)
        self.note = note
        row.addLayout(text, 1)
        row.addWidget(sw, 0, Qt.AlignmentFlag.AlignVCenter)
        sw.toggled.connect(self._mark)
        self._mark(sw.isChecked())

    def _mark(self, on: bool):
        self.setProperty('on', 'true' if on else 'false')
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, ev):
        if (ev.button() == Qt.MouseButton.LeftButton and self.sw.isEnabled()
                and self.rect().contains(ev.position().toPoint())):
            self.sw.click()
        super().mouseReleaseEvent(ev)


class SourceTile(QFrame):
    """How you play's "Found here": a source on a gradient badge, its count."""

    def __init__(self, glyph: str, grad: str, name: str, line: str, parent=None):
        super().__init__(parent)
        self.setObjectName("SetupRow")
        self.setMinimumWidth(250)
        row = QHBoxLayout(self)
        row.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        row.setSpacing(SP['3'])
        row.addWidget(GlyphBadge(G[glyph], grad, 30))
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(label(name, "ChoiceTitle"))
        self.line = label(line, "ChoiceNote")
        text.addWidget(self.line)
        row.addLayout(text, 1)


class FreeBar(QWidget):
    """How full a drive is, as a thin bar (painted: floats only)."""

    def __init__(self, used: float):
        super().__init__()
        self.used = float(used)
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['surface_2']))
        pt.drawRoundedRect(r, rad, rad)
        pt.setBrush(QColor(P['accent']))
        pt.drawRoundedRect(QRectF(0.0, 0.0, r.width() * self.used, r.height()), rad, rad)
        pt.end()


class DriveCard(ChoiceCard):
    """Where games go: a drive with its free space, what's on it, and why it
    is a poor place for games (warning colour)."""

    def __init__(self, drive: dict, windows: bool = False, system: str = 'C:'):
        title, free, note = drive_text(drive, windows, system)
        super().__init__(title)
        self.drive = drive
        self.setMinimumWidth(380)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['1'])
        top = QHBoxLayout()
        top.setSpacing(SP['2'])
        top.addWidget(icon_label(G['storage'], 18))
        top.addWidget(label(title, "ChoiceTitle"), 1)
        lay.addLayout(top)
        used = drive_used(drive)
        if used is not None:
            lay.addWidget(FreeBar(used))
        lay.addWidget(label(free, "ChoiceNote"))
        if note:
            lay.addWidget(label(note, "ChoiceNote"))
        warns = [x for x in drive.get('warnings') or []
                 if not str(drive.get('id', '')).startswith('timeout:')]
        if warns:
            lay.addWidget(label("Not a good place for games: " + "; ".join(warns) + ".",
                                "ChoiceWarn", wrap=True))
        self.add(w)


class NeedRow(QFrame):
    """A Ready to install row: its status icon, title, what to do, and its
    fix buttons, which start a normal job and then read "Started"."""
    fix = pyqtSignal(list, str, bool)              # (args, title, steam)

    def __init__(self, need: dict, parent=None):
        super().__init__(parent)
        self.need = need
        self.setObjectName("SetupRow")
        glyph, role = STATUS_GLYPH.get(need.get('status'), STATUS_GLYPH['info'])
        row = QHBoxLayout(self)
        row.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        row.setSpacing(SP['3'])
        row.addWidget(icon_label(G[glyph], 18, P[role]), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(SP['0_5'])
        text.addWidget(label(need.get('title', ''), "ChoiceTitle", wrap=True))
        if need.get('detail'):
            text.addWidget(plain_label(need['detail'], "ChoiceNote", wrap=True))
        row.addLayout(text, 1)
        self.buttons: list[tuple[QPushButton, dict]] = []
        for a in need.get('actions') or []:
            b = QPushButton(amp(a['label']))
            b.clicked.connect(self._clicked)
            row.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
            self.buttons.append((b, a))

    def _clicked(self):
        for b, a in self.buttons:
            if b is self.sender():
                b.setEnabled(False)
                b.setText("Started")
                self.fix.emit(list(a['args']), self.need.get('title', ''), bool(a.get('steam')))


class ServiceCard(QFrame):
    """Connect services: one optional service (w-105). Why Nexus asks, how
    to get the key in short steps, the field(s) with Open the page, and what
    it costs. value(config) -> {(section, key): value} of what changed;
    error() -> why Open my library must wait, or None."""
    changed = pyqtSignal()

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        g = QGridLayout(self)
        g.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        g.setHorizontalSpacing(SP['5'])
        g.setVerticalSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['3'])
        head.addWidget(GlyphBadge(G[spec['glyph']], spec['grad'], 30), 0,
                       Qt.AlignmentFlag.AlignTop)
        hv = QVBoxLayout()
        hv.setSpacing(SP['0_5'])
        hv.addWidget(label(spec['name'], "ChoiceTitle"))
        hv.addWidget(label(spec['why'], "ChoiceNote", wrap=True))
        head.addLayout(hv, 1)
        g.addLayout(head, 0, 0, 1, 2)
        left = QVBoxLayout()
        left.setSpacing(SP['0_5'])
        for i, s in enumerate(spec['steps'], 1):
            left.addWidget(label(f"{i}.  {s}", "ChoiceNote", wrap=True))
        left.addStretch(1)
        g.addLayout(left, 1, 0)
        right = QVBoxLayout()
        right.setSpacing(SP['1_5'])
        self.fields: list[QLineEdit] = []
        self.btn_open = QPushButton("Open the page")
        set_glyph_icon(self.btn_open, G['external'], 13)
        self.btn_open.clicked.connect(self._open)
        for j, (_sec, _key, _cfg, placeholder, secret) in enumerate(spec['fields']):
            row = QHBoxLayout()
            row.setSpacing(SP['2'])
            e = QLineEdit()
            e.setPlaceholderText(placeholder)
            e.setAccessibleName(f"{spec['name']} {placeholder}")
            if secret:
                secret_field(e)
            e.textChanged.connect(self._edited)
            row.addWidget(e, 1)
            if j == len(spec['fields']) - 1:
                row.addWidget(self.btn_open)
            right.addLayout(row)
            self.fields.append(e)
        self.hint = label(spec['hint'], "Hint", wrap=True)
        right.addWidget(self.hint)
        self.err = label("", "RowError", wrap=True)
        right.addWidget(self.err)
        self.err.setVisible(False)
        right.addStretch(1)
        g.addLayout(right, 1, 1)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        self._open_note = ''
        self._env_token = False

    def load(self, config: dict):
        """Set up again: the fields show what the config holds. A Game-db
        token from NEXUS_GAMEDB_TOKEN counts as given."""
        for e, f in zip(self.fields, self.spec['fields']):
            e.blockSignals(True)
            e.setText(str(config.get(f[2]) or ''))
            e.blockSignals(False)
        self._env_token = self.spec['key'] == 'gamedb' \
            and gamedb.settings().get('token_source') == 'env'
        self._open_note = ''
        self._show()

    def _texts(self) -> list[str]:
        return [e.text().strip() for e in self.fields]

    def error(self) -> str | None:
        texts = self._texts()
        key = self.spec['key']
        if key == 'steam':
            if texts[0] and not STEAM_KEY_RE.match(texts[0]):
                return "That isn't a Steam key: they have 32 letters and digits."
        elif key == 'gamedb':
            url, tok = texts
            if bool(url) != (bool(tok) or self._env_token):
                return "Add the token too, or clear the address."
            if url:
                try:
                    gamedb.check_url(url)
                except gamedb.GameDbError as e:
                    return str(e)
            if tok and not gamedb.TOKEN_RE.match(tok):
                    return "A Game-db token is gdb_ followed by 43 letters, digits, - or _."
        return None

    def value(self, config: dict) -> dict:
        """What this card writes: each filled field whose value changed, and
        the service's switch on (Friends, Game-db) when its key is new.
        Empty fields write nothing; nothing at all while error() says no."""
        if self.error():
            return {}
        out = {}
        for text, (sec, key, cfg, _ph, _s) in zip(self._texts(), self.spec['fields']):
            was = config.get(cfg)
            if self.spec['key'] == 'gamedb' and key == 'url' and text:
                text = gamedb.check_url(text)
                try:                                    # "…/" in the config is the same URL
                    was = gamedb.check_url(was) if was else was
                except gamedb.GameDbError:
                    pass
            if text and text != was:
                out[(sec, key)] = text
        on = self.spec.get('enable')
        if out and on and config.get(on[2]) is not True:
            out[(on[0], on[1])] = True
        return out

    def error_text(self) -> str:
        return '' if self.err.isHidden() else self.err.text()

    def _edited(self, _text=''):
        self._open_note = ''
        self._show()
        self.changed.emit()

    def _show(self):
        msg = self.error() or self._open_note
        self.err.setText(msg)
        self.err.setVisible(bool(msg))
        self.hint.setVisible(not msg)

    def _open(self):
        url = self.spec.get('url')
        if self.spec['key'] == 'gamedb':
            addr = self.fields[0].text().strip()
            try:
                if not addr:
                    raise gamedb.GameDbError('config', "Type your Game-db address first.")
                url = gamedb.account_url(addr)
            except gamedb.GameDbError as e:
                self._open_note = str(e)
                self._show()
                return
        QDesktopServices.openUrl(QUrl(url))


class SetupPage(QWidget):
    request = pyqtSignal(list, str, bool, str)        # a fix button: (args, title, steam, kind)
    finished = pyqtSignal(dict, dict)                 # (config updates, appearance + options)
    preview = pyqtSignal(dict)                        # the look, applied as it's picked

    def __init__(self):
        super().__init__()
        self.win = not has('proton')
        page = QHBoxLayout(self)
        page.setContentsMargins(SP['8'], SP['6'], SP['8'], SP['6'])
        col = QWidget()
        col.setMaximumWidth(COLUMN_W)
        page.addStretch(1)                              # centred once it's at COLUMN_W
        page.addWidget(col, 40)
        page.addStretch(1)
        outer = QVBoxLayout(col)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['3'])
        head.addWidget(Wordmark("Nexus", 34), 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.step_note = label("", "Muted")
        head.addWidget(self.step_note, 0, Qt.AlignmentFlag.AlignVCenter)
        self.track = StepTrack()
        head.addWidget(self.track, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(head)
        outer.addSpacing(SP['4'])
        self.title = label("Welcome to Nexus", "SetupTitle")
        outer.addWidget(self.title)
        self.lead = label("", "Muted", wrap=True)
        outer.addWidget(self.lead)
        outer.addSpacing(SP['2'])
        # No card around the step: its sections size to their content. It
        # scrolls when it doesn't fit (150 % on a small screen), so the
        # buttons stay in view.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack = StepStack()
        self.scroll.setWidget(self.stack)
        outer.addWidget(self.scroll, 1)
        outer.addSpacing(SP['2'])
        nav = QHBoxLayout()
        nav.setSpacing(SP['2'])
        self.btn_skip_all = QPushButton("Skip, use the defaults")
        self.btn_skip_all.setObjectName("Ghost")
        self.btn_skip_all.clicked.connect(self._skip)
        nav.addWidget(self.btn_skip_all)
        nav.addStretch()
        self.next_hint = label("Pick one to go on", "Hint")
        nav.addWidget(self.next_hint)
        nav.addSpacing(SP['2'])
        self.btn_back = QPushButton("Back")
        self.btn_back.clicked.connect(self.back)
        self.btn_next = QPushButton("Next")
        self.btn_next.setObjectName("Primary")
        self.btn_next.clicked.connect(self._next)
        for b in (self.btn_back, self.btn_next):
            b.setMinimumWidth(96)
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_next)
        outer.addLayout(nav)
        self.step = 0
        self.steps = [s for s in STEPS if s[0] != 'where']
        self._config: dict = {}
        self._facts: dict | None = None
        self._facts_in = False
        self._counts: dict | None = None
        self._user_picked = False
        self._folder_touched = self._tor_touched = False       # step 2 edits, kept
        self._folder = ''
        self._torrent = None
        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.setInterval(0)
        self._focus_timer.timeout.connect(self._focus_default)
        self._build()

    # ---- the steps ----
    def _page(self) -> QVBoxLayout:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, SP['2'], 0)
        lay.setSpacing(SP['2'])
        self.stack.addWidget(w)
        return lay

    @staticmethod
    def _section(lay, text: str):
        lay.addSpacing(SP['1'])
        lay.addWidget(label(text, "StatLabel"))

    def _build(self):
        self._build_play(self._page())
        self._build_where(self._page())
        self._build_look(self._page())
        self._build_services(self._page())

    def _build_play(self, lay):
        lay.addWidget(label("Found here", "StatLabel"))
        self.looking = label("Looking at this computer…", "Muted")
        lay.addWidget(self.looking)
        self.found = QWidget()
        self.found_lay = FlowLayout(self.found, 0, SP['2'])
        lay.addWidget(self.found)
        self.nothing = NeedRow({'status': 'info', 'title': "Nothing to bring in yet",
                                'detail': "No Steam" + ("" if self.win else " or Heroic")
                                + " games and no torrent client here. Games you add yourself "
                                  "show up in the Library."})
        lay.addWidget(self.nothing)
        self.nothing.setVisible(False)
        self._section(lay, "How do you use it?")
        self.fork = CardChoice(SP['3'], pick_first=False)
        self.fork_why: dict = {}
        for data, glyph, grad, title, note in (
                (False, 'play', 'dusk', "Play the games I have",
                 "Steam" + ("" if self.win else ", Heroic") + " and the games you add "
                 "yourself, in one library. No Downloads page."),
                (True, 'magnet', 'ember', "Also install repacks",
                 "Adds Downloads: it installs from a download folder and talks to your "
                 "torrent client.")):
            self.fork.add(self._big_choice(glyph, grad, title, note, data), data)
        self.fork.activated.connect(self._forked)
        lay.addWidget(self.fork)
        self._section(lay, "When Nexus installs or adds a game, also add it to")
        box = QWidget()
        flow = FlowLayout(box, 0, SP['2'])
        self.integrations: dict = {}
        for key, text in (('steam', "Steam (as a non-Steam game, with art)"),
                          ('desktop', "The Start Menu" if self.win else "The app menu"),
                          ('heroic', "Heroic")):
            if key == 'heroic' and not has('heroic'):
                continue
            sw = Switch()
            sw.setChecked(True)
            card = ToggleCard(sw, text)
            card.setMinimumWidth(250)
            flow.addWidget(card)
            self.integrations[key] = sw
        lay.addWidget(box)
        lay.addStretch(1)

    def _big_choice(self, glyph, grad, title, note, data) -> ChoiceCard:
        c = ChoiceCard(title)
        c.setMinimumWidth(380)
        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(SP['3'])
        tl.addWidget(GlyphBadge(G[glyph], grad, 36), 0, Qt.AlignmentFlag.AlignTop)
        tx = QVBoxLayout()
        tx.setSpacing(SP['0_5'])
        tx.addWidget(label(title, "ChoiceTitle"))
        tx.addWidget(label(note, "ChoiceNote", wrap=True))
        why = label("", "ChoiceWhy", wrap=True)
        tx.addWidget(why)
        tl.addLayout(tx, 1)
        c.add(top)
        why.setVisible(False)
        self.fork_why[data] = why
        return c

    def _build_where(self, lay):
        self.where_looking = label("Looking at this computer…", "Muted")
        lay.addWidget(self.where_looking)
        self.drives = CardChoice(SP['3'], pick_first=False)
        self.drives.activated.connect(self._drive_picked)
        lay.addWidget(self.drives)
        self.drive_rows = QVBoxLayout()
        self.drive_rows.setSpacing(SP['2'])
        lay.addLayout(self.drive_rows)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        cl.setSpacing(SP['2'])
        r = QHBoxLayout()
        r.setSpacing(SP['2'])
        r.addWidget(icon_label(G['folder'], 16))
        self.folder_label = plain_label("", "ChoiceTitle")
        r.addWidget(self.folder_label, 1)
        self.btn_change = QPushButton("Change…")
        self.btn_change.clicked.connect(self._change_folder)
        r.addWidget(self.btn_change)
        cl.addLayout(r)
        self.folder_warn = label("", "ChoiceWarn", wrap=True)
        cl.addWidget(self.folder_warn)
        self.folder_warn.setVisible(False)
        cl.addWidget(label("A big game needs about twice its size free while it installs.",
                           "ChoiceNote", wrap=True))
        lay.addWidget(card)
        self._section(lay, "Downloads come from")
        self.tor_rows = QVBoxLayout()
        lay.addLayout(self.tor_rows)
        self.use_torrent = Switch()
        self.use_torrent.setChecked(True)
        self.use_torrent.clicked.connect(self._tor_clicked)
        self.tor_note = label("", None, wrap=True)
        self.tor_card = ToggleCard(self.use_torrent, "", self.tor_note)
        lay.addWidget(self.tor_card)
        self.tor_card.setVisible(False)
        self._section(lay, "Ready to install")
        self.need_rows = QVBoxLayout()
        self.need_rows.setSpacing(SP['2'])
        lay.addLayout(self.need_rows)
        lay.addStretch(1)

    def _build_look(self, lay):
        lay.addWidget(label("Theme", "StatLabel"))
        self.theme = CardChoice()
        for k in SETUP_THEMES:                 # a few to start; Settings has them all
            self._theme_card(k)
        self.theme.activated.connect(self._preview)
        lay.addWidget(self.theme)
        row = QHBoxLayout()
        row.setSpacing(SP['3'])
        row.addWidget(label("Size", "StatLabel"), 0, Qt.AlignmentFlag.AlignVCenter)
        self.size = CardChoice(SP['1'])
        for v in SCALES:
            pct = f"{int(round(v * 100))}%"
            c = ChoiceCard(pct)
            c.setMinimumWidth(64)
            c.add(label(pct, "ChoiceTitle"))
            self.size.add(c, v)
        self.size.setCurrentIndex(SCALES.index(1.0))
        self.size.activated.connect(self._preview)
        row.addWidget(self.size, 1)
        lay.addLayout(row)
        self._section(lay, "While Nexus is closed")
        self.agent = Switch()
        self.agent_card = ToggleCard(
            self.agent, AGENT_TITLE[True],
            "Adds a small user service (nexus-agent, run by systemd) that starts with your "
            "session. Turning it off removes it." if has('systemd_agent') else
            "Starts with Windows and sits in the tray.")
        lay.addWidget(self.agent_card)
        self._section(lay, "Count this install anonymously?")
        self._stats_block(lay)
        lay.addStretch(1)

    def _stats_block(self, lay):
        """The install count ask (w-102): two equal cards and neither
        picked, so going past it (or Skip) answers nothing."""
        lay.addWidget(label("At most once a day: the Nexus version, the system and the "
                            "language. No id, no games, no paths. Nothing is sent until you "
                            "pick; Settings › System › Privacy changes it later.", "Muted",
                            wrap=True))
        self.stats = CardChoice(SP['3'], pick_first=False)
        for data, title, note in ((True, "Count me", "Helps test the systems people use."),
                                  (False, "Don't", "Nothing is sent.")):
            c = ChoiceCard(title)
            c.setMinimumWidth(380)               # equal cards: neither looks like the default
            c.add(label(title, "ChoiceTitle"))
            c.add(label(note, "ChoiceNote"))
            self.stats.add(c, data)
        lay.addWidget(self.stats)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(SP['3'], 0, SP['3'], SP['2'])
        self.stats_text = plain_label("", "ReportLine", wrap=True)
        self.stats_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.stats_text)
        self.stats_what = Disclosure('setup-stats-what', "What is sent", G['info'], body)
        self.stats_what.toggled.connect(self._stats_disclosed)
        lay.addWidget(self.stats_what)

    def _build_services(self, lay):
        self.services = []
        for spec in SERVICES:
            card = ServiceCard(spec)
            card.changed.connect(self._sync_next)
            lay.addWidget(card)
            self.services.append(card)
        lay.addStretch(1)

    def _stats_disclosed(self, on: bool):
        if on:
            self.stats_text.setText("Asking…")
            cli_json(['--stats-preview', '--json'], self._stats_previewed, 15000)

    def _stats_previewed(self, data, err):
        try:
            self.stats_text.setText(
                stats.pretty(data['payload']) if data and data.get('payload')
                else f"Couldn't ask: {err or 'no answer'}")
        except RuntimeError:
            pass

    def _theme_card(self, k: str):
        name, _t = THEMES[k]
        c = ChoiceCard(name)
        sw = Swatch('theme', k, name, (84, 50))
        sw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        c.add(sw)
        c.add(label(name, "ChoiceTitle"))
        self.theme.add(c, k)

    # ---- data in ----
    def open(self, config: dict, appearance: dict):
        self._config = dict(config)
        self._appearance_was = dict(appearance)
        self._facts, self._facts_in, self._counts = None, False, None
        self._user_picked = False
        self._folder_touched = self._tor_touched = False       # step 2 edits, kept
        self._torrent = None
        self.fork.clear_choice()
        for why in self.fork_why.values():
            why.setVisible(False)
        for key, sw in self.integrations.items():
            sw.setChecked(config.get(key, True) is not False)
        self.looking.setVisible(True)
        self.where_looking.setVisible(True)
        self.nothing.setVisible(False)
        clear_layout(self.found_lay)
        self.drives.clear()
        clear_layout(self.drive_rows)
        clear_layout(self.tor_rows)
        clear_layout(self.need_rows)
        self.tor_card.setVisible(False)
        self._set_folder(config.get('install_base') or INSTALL_BASE_DEFAULT)
        for card in self.services:
            card.load(config)
        theme = appearance.get('theme')
        if theme in THEMES and self.theme.findData(theme) < 0:
            self._theme_card(theme)            # set up again with one only Settings lists
        i = self.theme.findData(theme)
        self.theme.setCurrentIndex(max(0, i))
        i = self.size.findData(appearance.get('scale', 1.0))
        self.size.setCurrentIndex(max(0, i))
        self.agent.setChecked(False)
        self._stats_was = config.get('stats_count')
        if self._stats_was is None:
            self.stats.clear_choice()
        else:
            self.stats.setCurrentIndex(self.stats.findData(self._stats_was))
        self._apply_prepick()
        self.show_step(0)

    def set_found(self, counts: dict):
        """The library load's counts (found_counts), for the Found here tiles."""
        try:
            self._counts = dict(counts or {})
            self._fill_found()
            self._apply_prepick()
        except RuntimeError:
            pass

    def set_facts(self, facts, err=''):
        """`--setup-facts --json`'s answer (None and the reason on a failure)."""
        try:
            self._facts = facts if isinstance(facts, dict) else None
            self._facts_in = True
            self._facts_err = err
            self._fill_found()
            self._fill_where()
            self._apply_prepick()
        except RuntimeError:
            pass

    def _fill_found(self):
        if not self._facts_in:
            return
        self.looking.setVisible(False)
        clear_layout(self.found_lay)
        tiles = found_tiles(self._counts, self._facts)
        for glyph, grad, name, line in tiles:
            t = SourceTile(glyph, grad, name, line, self.found)
            self.found_lay.addWidget(t)
            t.show()
        self.nothing.setVisible(not tiles)

    def _fill_where(self):
        self.where_looking.setVisible(False)
        facts = self._facts or {}
        default = facts.get('default_base') or INSTALL_BASE_DEFAULT
        drives = facts.get('drives') or [{'id': 'default', 'path': default, 'fs': None,
                                          'free': None, 'total': None, 'folder': default,
                                          'default': True, 'steam_games': 0, 'warnings': []}]
        cards, stuck = split_drives(drives)
        self.drives.clear()
        clear_layout(self.drive_rows)
        system = os.environ.get('SystemDrive', 'C:')
        for d in cards:
            card = DriveCard(d, self.win, system)
            self.drives.add(card, d['id'])
            card.show()
        if stuck:
            row = NeedRow({'status': 'info', 'title': "Not checked: " + ", ".join(stuck),
                           'detail': NOT_CHECKED_WHY}, self)
            self.drive_rows.addWidget(row)
            row.show()
        if self._folder_touched:
            # Change… before the facts came: keep that folder, and mark the
            # drive it is on (none: it shows as the folder alone).
            pick = self._drive_for(self._folder)
            if pick is None:
                self.drives.clear_choice()
            else:
                self.drives.setCurrentIndex(self.drives.findData(pick['id']))
        else:
            base = self._config.get('install_base')
            pick = self._drive_for(base) if base else None
            if pick is None:
                pick = next((d for d in cards if d.get('default')), cards[0] if cards else None)
            if pick is not None:
                self.drives.setCurrentIndex(self.drives.findData(pick['id']))
                self._set_folder(base or pick['folder'])
        # Downloads come from
        clear_layout(self.tor_rows)
        tor = facts.get('torrent')
        self._torrent = tor
        state, title, note = torrent_state(tor)
        if state == 'none':
            where = "~/Downloads" if not self.win else str(HOME / 'Downloads')
            row = NeedRow({'status': 'info', 'title': title,
                           'detail': f"Nexus watches {where} for now. The Downloads page sets "
                                     "up a client when you want one."}, self)
            self.tor_rows.addWidget(row)
            row.show()
            self.tor_card.setVisible(False)
        else:
            self.tor_card.title.setText(title)
            self.use_torrent.setAccessibleName(title)
            self.tor_note.setText(note)
            if not self._tor_touched:               # the user's flip stands
                self.use_torrent.setChecked(True)
            self.tor_card.setVisible(True)
        # Ready to install
        clear_layout(self.need_rows)
        rows = need_rows(facts.get('needs') or [], self.win) if self._facts is not None else [
            {'status': 'warn', 'title': facts_failed_text(self._facts_err), 'actions': []}]
        for need in rows:
            row = NeedRow(need, self)
            row.fix.connect(self._fix)
            self.need_rows.addWidget(row)
            row.show()

    def _fix(self, args: list, title: str, steam: bool):
        self.request.emit(args, title, steam, 'task')

    def _drive_for(self, path: str | None) -> dict | None:
        """The drive card a folder is on (the longest mount that holds it)."""
        if not path:
            return None
        best = None
        for d in (self._facts or {}).get('drives') or []:
            if self.drives.findData(d['id']) < 0:
                continue
            try:
                inside = Path(path).is_relative_to(d['path'])
            except (TypeError, ValueError):
                inside = False
            if inside and (best is None or len(d['path']) > len(best['path'])):
                best = d
        return best

    def _set_folder(self, folder: str):
        self._folder = folder
        self.folder_label.setText(folder)
        warns = platform.install_base_warnings(Path(folder)) if folder else []
        self.folder_warn.setText("Not a good place for games: " + "; ".join(warns) + "."
                                 if warns else "")
        self.folder_warn.setVisible(bool(warns))

    def _drive_picked(self, i: int):
        self._folder_touched = True
        d = next((x for x in (self._facts or {}).get('drives') or []
                  if x['id'] == self.drives.currentData()), None)
        self._set_folder(d['folder'] if d else (self._facts or {}).get('default_base')
                         or INSTALL_BASE_DEFAULT)

    def _tor_clicked(self, _on: bool):
        self._tor_touched = True

    def _change_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder", self._folder)
        if d:
            self._folder_touched = True
            self._set_folder(d)
            drive = self._drive_for(d)
            if drive is not None:
                self.drives.setCurrentIndex(self.drives.findData(drive['id']))

    def _apply_prepick(self):
        """Late facts never undo what the user picked."""
        if self._user_picked:
            return
        # Until the facts are in (or failed), nothing is picked: counts alone
        # would pick "Play the games I have" before a running client is seen.
        pick, why = prepick(self._facts, self._config, self._counts) if self._facts_in \
            else (None, '')
        if pick is None:
            self.fork.clear_choice()
        else:
            self.fork.setCurrentIndex(self.fork.findData(pick))
        for data, lab in self.fork_why.items():
            lab.setText(why if data == pick else "")
            lab.setVisible(data == pick and bool(why))
        self._refresh()

    def _forked(self, _i: int):
        self._user_picked = True
        for lab in self.fork_why.values():
            lab.setVisible(False)
        self._refresh()

    def _preview(self, _i: int = 0):
        self.preview.emit(self._look())

    # ---- moving between steps ----
    LEADS = {
        'play': "Nexus puts the games you already have in one library. Here is what it found "
                "on this computer.",
        'play-win': "Nexus puts the games you already have in one library. Here is what it "
                    "found on this PC.",
        'look': "Three last things. All of them are in Settings later.",
        'services': "All optional: leave any of them empty. Settings › Accounts has them later.",
    }

    def _lead(self, key: str) -> str:
        if key == 'where':
            return "Pick a drive. Each game gets its own folder there" + (
                " (and, on btrfs, its own subvolume)." if has('btrfs') else ".")
        return self.LEADS[key + '-win' if key == 'play' and self.win else key]

    def _steps(self) -> list:
        repacks = self.fork.currentData() is True
        return [s for s in STEPS if s[0] != 'where' or repacks]

    def key(self) -> str:
        return self.steps[self.step][0]

    def show_step(self, i: int, focus: bool = True):
        self.steps = self._steps()
        repacks = self.fork.currentData() is not False      # play only: no downloads to watch
        self.agent_card.title.setText(AGENT_TITLE[repacks])
        self.agent.setAccessibleName(AGENT_TITLE[repacks])
        last = len(self.steps) - 1
        self.step = max(0, min(last, i))
        key, name = self.steps[self.step]
        self.stack.setCurrentIndex([k for k, _n in STEPS].index(key))   # a page per STEPS
        self.stack.updateGeometry()
        self.step_note.setText(f"Optional · {name}" if key == 'services' else
                               f"{self.step + 1} of {last} · {name}")
        self.track.set_steps([n for _k, n in self.steps], self.step, last)
        self.title.setText("Welcome to Nexus" if self.step == 0 else name)
        self.lead.setText(self._lead(key))
        self.btn_back.setVisible(self.step > 0)
        self.btn_skip_all.setVisible(key != 'services')
        self.btn_next.setText("Open my library" if key == 'services' else "Next")
        self._sync_next()
        self._tab_order()
        if focus:
            self.scroll.verticalScrollBar().setValue(0)
            self._focus_timer.start()

    def _refresh(self):
        """The step list follows the fork; the step shown stays the same."""
        key = self.key() if self.steps else 'play'
        steps = self._steps()
        keys = [k for k, _n in steps]
        self.show_step(keys.index(key) if key in keys else 0, focus=False)

    def _sync_next(self):
        key = self.key()
        if key == 'play':
            ok = self.fork.currentData() is not None
        elif key == 'services':
            ok = all(c.error() is None for c in self.services)
        else:
            ok = True
        self.btn_next.setEnabled(ok)
        self.next_hint.setText("Pick one to go on" if self._facts_in
                               else "Looking at this computer…")
        self.next_hint.setVisible(key == 'play' and not ok)

    def focus_chain(self) -> list:
        """The step's controls in pad order (Skip, Back, Next last)."""
        key = self.key()
        if key == 'play':
            chain = self.fork.cards() + list(self.integrations.values())
        elif key == 'where':
            chain = self.drives.cards() + [self.btn_change] + (
                [self.use_torrent] if not self.tor_card.isHidden() else []) + [
                b for row in self._rows(self.need_rows) for b, _a in row.buttons]
        elif key == 'look':
            chain = (self.theme.cards() + self.size.cards()
                     + [self.agent] + self.stats.cards() + [self.stats_what.head])
        else:
            chain = []
            for c in self.services:
                chain += c.fields + [c.btn_open]
        nav = [b for b in (self.btn_skip_all, self.btn_back, self.btn_next)
               if not b.isHidden()]
        return chain + nav

    @staticmethod
    def _rows(lay) -> list:
        return [lay.itemAt(i).widget() for i in range(lay.count())
                if isinstance(lay.itemAt(i).widget(), NeedRow)]

    def _tab_order(self):
        chain = self.focus_chain()
        for a, b in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, b)

    def default_focus(self) -> QWidget:
        key = self.key()
        if key == 'play':
            cards = self.fork.cards()
            picked = self.fork.group.checkedButton()
            return picked or (cards[0] if cards else self.btn_next)
        if key == 'where':
            return self.drives.group.checkedButton() or self.btn_change
        if key == 'look':
            return self.theme.group.checkedButton() or self.btn_next
        return self.btn_next

    def _focus_default(self):
        if self.isVisible():
            self.default_focus().setFocus(Qt.FocusReason.OtherFocusReason)

    def back(self) -> bool:
        """Back one step (B on a pad, Escape); False on the first."""
        if self.step == 0:
            return False
        self.show_step(self.step - 1)
        return True

    def _next(self):
        if not self.btn_next.isEnabled():
            return
        if self.key() == 'services':
            self._finish()
            return
        if self.key() == 'play':
            self._user_picked = True               # the pick is the user's from here on
        self.show_step(self.step + 1)

    def _look(self) -> dict:
        return {'theme': self.theme.currentData(), 'scale': self.size.currentData()}

    def answers_now(self) -> tuple[dict, dict]:
        """(config.toml updates, {'appearance', 'agent'}). Only what changed
        from the config setup opened with."""
        c = self._config
        u = {}
        repacks = self.fork.currentData()
        if repacks is not None and repacks != c.get('repacks', True):
            u[('repacks', 'enabled')] = repacks
        for key, sw in self.integrations.items():
            if sw.isChecked() != (c.get(key, True) is not False):
                u[('integrations', key)] = sw.isChecked()
        if repacks:
            default = (self._facts or {}).get('default_base') or INSTALL_BASE_DEFAULT
            if self._folder and self._folder != (c.get('install_base') or default):
                u[('install', 'base')] = self._folder
            state, _t, _n = torrent_state(self._torrent)
            if state != 'none' and self.use_torrent.isChecked():
                t = self._torrent
                if t['client'] != c.get('torrent_client'):
                    u[('torrent', 'client')] = t['client']
                if t.get('url') and t['url'] != c.get('torrent_url'):
                    u[('torrent', 'url')] = t['url']
                if state == 'ok' and t.get('download_dir') \
                        and t['download_dir'] != c.get('scan_dir'):
                    u[('install', 'scan_dir')] = t['download_dir']
        count = self.stats.currentData()             # None: neither card picked
        if count is not None and count != getattr(self, '_stats_was', None):
            u[('stats', 'count')] = count
        for card in self.services:
            u.update(card.value(c))
        return u, {'appearance': self._look(), 'agent': self.agent.isChecked()}

    def _finish(self):
        u, opts = self.answers_now()
        self.finished.emit(u, opts)

    def _skip(self):
        """Skip, use the defaults: nothing that was typed or picked is kept,
        Connect services included, and the look goes back to how it was
        (w-15)."""
        self.finished.emit({}, {'appearance': dict(getattr(self, '_appearance_was', {})),
                                'agent': False})


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .dashboard import GlyphBadge  # noqa: E402
from .details import Disclosure  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .settings import (FRIENDS_DESC, SGDB_KEY_URL, STEAM_KEY_RE, STEAM_KEY_URL,  # noqa: E402
    Swatch, Switch)
from .topbar import Wordmark  # noqa: E402
from .widgets import (amp, clear_layout, FlowLayout, icon_label, label,  # noqa: E402
    plain_label, secret_field, set_glyph_icon)

# Connect services (w-105): why Nexus asks, how to get each key, the field(s)
# as (section, key, load_config key, placeholder, secret), and the switch a
# new key turns on (user 2026-11-11). Settings' rows use the same URLs.
SERVICES = (
    {'key': 'sgdb', 'name': "SteamGridDB", 'glyph': 'image', 'grad': 'dusk',
     'why': "Covers, heroes and logos for every game. Without a key, games Nexus can't find "
            "art for get a plain placeholder.",
     'steps': ("Open the page and sign in.", "Make an API key there.",
               "Copy it into the field."),
     'fields': (('steamgriddb', 'api_key', 'api_key', "API key", True),),
     'url': SGDB_KEY_URL, 'hint': "Free · needs a SteamGridDB account", 'enable': None},
    {'key': 'steam', 'name': "Steam Web API", 'glyph': 'friends', 'grad': 'tide',
     'why': FRIENDS_DESC,
     'steps': ("Open the page and sign in to Steam.",
               "For the domain, type localhost, agree, register.",
               "Copy the key into the field."),
     'fields': (('steam', 'web_api_key', 'steam_web_api_key', "Web API key", True),),
     'url': STEAM_KEY_URL, 'hint': "Free · needs a Steam account",
     'enable': ('friends', 'enabled', 'friends_enabled')},
    {'key': 'gamedb', 'name': "Game-db", 'glyph': 'deals', 'grad': 'ember',
     'why': "Wishlist deals, upcoming releases and your backlog from your Game-db account, "
            "on a Deals page.",
     'steps': ("Type your Game-db address.",
               "Open its Account page, create a desktop app token.",
               "Copy the token into the field."),
     'fields': (('gamedb', 'url', 'gamedb_url', gamedb.EXAMPLE_URL, False),
                ('gamedb', 'token', 'gamedb_token', "Token (gdb_…)", True)),
     'url': None,
     'hint': "Only if you run Game-db: it is self-hosted, so there is no shared server",
     'enable': ('gamedb', 'enabled', 'gamedb_cfg_enabled')},
)
