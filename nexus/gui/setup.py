"""First-run setup: a page (never a dialog — niri floats those) that walks
a new user through what Nexus needs, using the same checks and commands as
the rest of the app. Six steps, each skippable, in this order:

1. Checks — Wine, umu, Proton, the install folder (the doctor's
   Requirements, with its fix buttons).
2. Your launchers — what was found: the Steam account, Heroic.
3. Folders — where games install, where repacks download to.
4. Proton — the build games play with; get GE-Proton if there's none.
5. Extras — a SteamGridDB key, a torrent client found on this machine.
6. Look & background — theme, size, the background agent, the tray.

Shown once (library.json `setup_done`), when Nexus has never run here;
Settings → Library → "Run setup again" opens it later. Windows has no Proton
step (`steps` is STEPS without it).

The page is one centred column: the wordmark and a dot per step, the step's
title and lead, a card with the step in it (scrolling when it doesn't fit),
then Back / Next. Choices are cards (ChoiceCard, ToggleCard), styled from
the theme's tokens like the rest of the app."""

from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from nexus import platform
from nexus.features import has

from .common import HOME, INSTALL_BASE_DEFAULT
from .theme import FONTS, FS, G, GRAD, P, SCALES, SP, THEMES

STEPS = (('checks', "Checks"), ('launchers', "Your launchers"), ('folders', "Folders"),
         ('proton', "Proton"), ('extras', "Extras"), ('look', "Look & background"))
STATUS_GLYPH = {'ok': ('check_circle', 'success'), 'warn': ('warn', 'warning'),
                'fail': ('fail', 'danger'), 'info': ('info', 'muted')}
COLUMN_W = 860                 # the page's column never gets wider than this


class StepTrack(QWidget):
    """The steps as numbered dots on a line: done ones ringed in the accent,
    the current one filled with the dusk gradient, the rest outlined.
    Painted from the palette at paint time, so it follows the theme."""

    D, GAP = 22, 14             # dot diameter; the line between two dots

    def __init__(self):
        super().__init__()
        self.names: list[str] = []
        self.current = 0
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_steps(self, names: list[str], current: int):
        self.names, self.current = names, current
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
            if i:                               # the line from the dot before
                pt.setPen(QPen(QColor(P['accent'] if i <= self.current else P['border']), 2))
                pt.drawLine(int(x - self.GAP + 3), int(y), int(x - 3), int(y))
            r = QRectF(x, 1, self.D, self.D)
            if i == self.current:
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
                pt.setPen(QPen(QColor(P['border']), 1.5))
                pt.setBrush(QColor(P['surface_2']))
                fg = P['muted']
            pt.drawEllipse(r.adjusted(0.75, 0.75, -0.75, -0.75))
            pt.setPen(QColor(fg))
            pt.drawText(r, Qt.AlignmentFlag.AlignCenter, str(i + 1))
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

    def __init__(self, spacing: int = SP['2']):
        super().__init__()
        self.lay = FlowLayout(self, 0, spacing)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self.activated.emit)
        self._data: list = []

    def add(self, card: ChoiceCard, data):
        self.group.addButton(card, len(self._data))
        self._data.append(data)
        self.lay.addWidget(card)
        if len(self._data) == 1:
            card.setChecked(True)

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
        text.addWidget(label(title, "ChoiceTitle", wrap=True))
        if isinstance(note, str):
            note = label(note, None, wrap=True)
        if note is not None:
            note.setObjectName("ChoiceNote")
            text.addWidget(note)
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


class SetupPage(QWidget):
    request = pyqtSignal(list, str, bool, str)        # a fix button: (args, title, steam, kind)
    finished = pyqtSignal(dict, dict)                 # (config updates, appearance + options)
    preview = pyqtSignal(dict)                        # the look, applied as it's picked

    def __init__(self):
        super().__init__()
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
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        # The step scrolls inside its card: at 150 % on a small screen the
        # look step doesn't fit, and Back / Next must stay in view.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack = StepStack()
        scroll.setWidget(self.stack)
        cl.addWidget(scroll)
        outer.addWidget(card, 1)
        outer.addSpacing(SP['2'])
        nav = QHBoxLayout()
        nav.setSpacing(SP['2'])
        self.btn_skip_all = QPushButton("Skip setup")
        self.btn_skip_all.setObjectName("Ghost")
        self.btn_skip_all.clicked.connect(self._finish)
        nav.addWidget(self.btn_skip_all)
        nav.addStretch()
        self.btn_back = QPushButton("Back")
        self.btn_back.clicked.connect(lambda: self.show_step(self.step - 1))
        self.btn_next = QPushButton("Next")
        self.btn_next.setObjectName("Primary")
        self.btn_next.clicked.connect(self._next)
        for b in (self.btn_back, self.btn_next):
            b.setMinimumWidth(96)
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_next)
        outer.addLayout(nav)
        self.step = 0
        self.steps = [s for s in STEPS if s[0] != 'proton' or has('proton')]
        self.checks: list[dict] = []
        self.answers: dict = {}
        self._build()

    # ---- the steps ----
    def _page(self) -> QVBoxLayout:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['2'])
        self.stack.addWidget(w)
        return lay

    def _build(self):
        self.req_rows = self._page()                   # 1 checks
        self.req_rows.addWidget(label("Looking at this machine…", "Muted"))
        self.int_rows = self._page()                   # 2 launchers
        lay = self._page()                             # 3 folders
        lay.addWidget(label("Install games to", "StatLabel"))
        self.base = QLineEdit()
        self.base.setPlaceholderText(INSTALL_BASE_DEFAULT)
        lay.addLayout(self._browse_row(self.base))
        # Why the folder is a poor place for games (Windows: OneDrive, Program
        # Files, a network drive, a long path), as it's typed.
        self.base_warn = plain_label("", None, wrap=True)
        self.base_warn.setStyleSheet(f"color: {P['warning']};")
        self.base_warn.setVisible(False)
        lay.addWidget(self.base_warn)
        self.base.textChanged.connect(self._check_base)
        lay.addWidget(label("Each game gets its own folder" + (
            " (and, on btrfs, its own subvolume so updates can be undone)." if has('btrfs')
            else "."), "Muted", wrap=True))
        lay.addSpacing(SP['4'])
        lay.addWidget(label("Repacks download to", "StatLabel"))
        self.scan = QLineEdit()
        self.scan.setPlaceholderText(str(HOME / 'Downloads'))
        lay.addLayout(self._browse_row(self.scan))
        lay.addWidget(label("Nexus watches this folder and says when a download is ready to "
                            "install.", "Muted", wrap=True))
        lay.addStretch()
        lay = self._page()                             # 4 proton
        lay.addWidget(label("Play games with", "StatLabel"))
        self.proton = NoWheelCombo()
        self.proton.addItem("The newest installed", None)
        lay.addWidget(self.proton)
        self.proton_note = label("", "Muted", wrap=True)
        lay.addWidget(self.proton_note)
        self.get_ge = QPushButton("Install the latest GE-Proton")
        self.get_ge.clicked.connect(lambda: self.request.emit(
            ['--proton-install', 'latest'], "Installing GE-Proton", False, 'task'))
        lay.addWidget(self.get_ge, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch()
        lay = self._page()                             # 5 extras
        lay.addWidget(label("SteamGridDB API key (optional)", "StatLabel"))
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("from steamgriddb.com → Preferences → API")
        lay.addWidget(self.key)
        lay.addWidget(label("Covers, heroes and logos for every game. Without one, games get "
                            "a plain placeholder.", "Muted", wrap=True))
        lay.addSpacing(SP['4'])
        lay.addWidget(label("Torrent client", "StatLabel"))
        self.torrent_note = label("Looking for Transmission or qBittorrent…", None, wrap=True)
        self.use_torrent = Switch()
        lay.addWidget(ToggleCard(self.use_torrent, "Use it for repack downloads",
                                 self.torrent_note))
        self.use_torrent.setEnabled(False)
        self._torrent = None
        lay.addStretch()
        lay = self._page()                             # 6 look & background
        lay.addWidget(label("Theme", "StatLabel"))
        self.theme = CardChoice()
        for k, (name, t) in THEMES.items():
            c = ChoiceCard(name)
            sw = Swatch('theme', k, name, (84, 50))
            sw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            c.add(sw)
            c.add(label(name, "ChoiceTitle"))
            c.add(label("Light" if t.get('kind') == 'light' else "Dark", "ChoiceNote"))
            self.theme.add(c, k)
        self.theme.activated.connect(lambda _i: self.preview.emit(self._look()))
        lay.addWidget(self.theme)
        lay.addSpacing(SP['3'])
        lay.addWidget(label("Size", "StatLabel"))
        self.size = CardChoice()
        for v in SCALES:
            pct = f"{int(round(v * 100))}%"
            c = ChoiceCard(pct)
            c.setMinimumWidth(88)
            # A sample at the size it picks: the stylesheet's sizes follow the
            # size in use, so this one is set here (its colour still comes from
            # #ChoiceTitle, so a theme change reaches it).
            aa = label("Aa", "ChoiceTitle")
            aa.setStyleSheet(f"font-size: {round(FS['lg'] * v)}px; font-weight: 600;")
            aa.setFixedHeight(round(FS['lg'] * max(SCALES) * 1.4))
            aa.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
            c.add(aa)
            c.add(label(pct, "ChoiceTitle"))
            c.add(label("Default" if v == 1.0 else "Smaller" if v < 1.0 else "Larger",
                        "ChoiceNote"))
            self.size.add(c, v)
        self.size.setCurrentIndex(SCALES.index(1.0))
        self.size.activated.connect(lambda _i: self.preview.emit(self._look()))
        lay.addWidget(self.size)
        lay.addSpacing(SP['3'])
        lay.addWidget(label("With the window closed", "StatLabel"))
        self.agent = Switch()
        lay.addWidget(ToggleCard(self.agent, "Keep watching downloads and sending alerts",
                                 "While Nexus is closed" + (" (a systemd user service)."
                                                            if has('systemd_agent') else ".")))
        self.tray = Switch()
        lay.addWidget(ToggleCard(self.tray, "Close to the tray instead of quitting",
                                 "Closing the window leaves Nexus running in the tray."
                                 if tray_available() else "This desktop has no tray."))
        self.tray.setEnabled(tray_available())
        lay.addStretch()

    def _browse_row(self, edit):
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        row.addWidget(edit, 1)
        b = QPushButton("Browse…")
        set_glyph_icon(b, G['folder'], 14)
        b.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(
            self, "Choose a folder", edit.text() or edit.placeholderText())) and edit.setText(d))
        row.addWidget(b)
        return row

    def _check_base(self, _text=None):
        warns = platform.install_base_warnings(
            Path(self.base.text().strip() or INSTALL_BASE_DEFAULT))
        self.base_warn.setText(("Not a good place for games: " + "; ".join(warns) + ".")
                               if warns else "")
        self.base_warn.setVisible(bool(warns))

    # ---- data from the CLI ----
    def open(self, config: dict, appearance: dict):
        self.base.setText(config.get('install_base') or '')
        self._check_base()
        self.scan.setText(config.get('scan_dir') or '')
        self.key.setText(config.get('api_key') or '')
        i = self.theme.findData(appearance.get('theme'))
        self.theme.setCurrentIndex(max(0, i))
        i = self.size.findData(appearance.get('scale', 1.0))
        self.size.setCurrentIndex(max(0, i))
        self.show_step(0)
        cli_json(['--doctor', '--json'], self._got_checks, 120000)
        if has('proton'):
            cli_json(['--list-protons', '--json'], self._got_protons, 30000)
        cli_json(['--torrent-detect', '--json'], self._got_torrent, 15000)

    def _rows(self, lay, rows):
        """A check per row card: its status icon, title, detail and how to
        fix it, and its fix buttons on the right."""
        clear_layout(lay)
        for c in rows:
            glyph, role = STATUS_GLYPH.get(c['status'], STATUS_GLYPH['info'])
            card = QFrame()
            card.setObjectName("SetupRow")
            row = QHBoxLayout(card)
            row.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
            row.setSpacing(SP['3'])
            row.addWidget(icon_label(G[glyph], 18, P[role]), 0, Qt.AlignmentFlag.AlignTop)
            text = QVBoxLayout()
            text.setSpacing(SP['0_5'])
            text.addWidget(label(c['title'], "ChoiceTitle", wrap=True))
            if c.get('detail'):
                text.addWidget(plain_label(c['detail'], "ChoiceNote", wrap=True))
            if c.get('fix') and c['status'] in ('warn', 'fail'):
                text.addWidget(label(c['fix'], "ChoiceNote", wrap=True))
            row.addLayout(text, 1)
            for a in c.get('actions') or []:
                b = QPushButton(amp(a['label']))
                b.clicked.connect(lambda _=False, a=a, t=c['title']: self.request.emit(
                    a['args'], t, bool(a.get('steam')), 'task'))
                row.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(card)
        lay.addStretch()

    def _got_checks(self, data, err):
        try:
            checks = (data or {}).get('checks') or []
            self.checks = checks
            req = [c for c in checks if c['group'] == 'Requirements']
            self._rows(self.req_rows, req or [{'status': 'warn', 'title': "Couldn't check",
                                               'detail': err or "no answer"}])
            self._rows(self.int_rows, [c for c in checks if c['group'] == 'Integrations'
                                       and c['title'] in ('Steam account', 'Heroic',
                                                          'SteamGridDB artwork', 'Play Proton')])
            self._lead()
        except RuntimeError:
            pass

    def _got_protons(self, data, _err):
        try:
            names = [p['name'] for p in (data or {}).get('protons') or []]
            for n in names:
                if self.proton.findData(n) < 0:
                    self.proton.addItem(n, n)
            ge = any(n.lower().startswith('ge-proton') for n in names)
            self.get_ge.setVisible(not ge)
            self.proton_note.setText(
                "GE-Proton is installed — it runs FitGirl's installers best." if ge else
                "No GE-Proton yet: FitGirl's installers need it. Install the latest (about "
                "500 MB, checked before it's used).")
        except RuntimeError:
            pass

    def _got_torrent(self, data, _err):
        try:
            found = (data or {}).get('found')
            self._torrent = found
            if found:
                self.torrent_note.setText(f"Found {found['client'].capitalize()} at "
                                          f"{found['url']}.")
                self.use_torrent.setEnabled(True)
                self.use_torrent.setChecked(True)
            else:
                self.torrent_note.setText("None found with remote access on. You can set one "
                                          "up later in Settings → Torrent client.")
        except RuntimeError:
            pass

    # ---- moving between steps ----
    LEADS = {
        'checks': "First, what this machine has. Anything red needs fixing before games will "
                  "install; the buttons do it.",
        'launchers': "Nexus works alongside Steam and Heroic: it adds games to them and shows "
                     "their games too.",
        'launchers-win': "Nexus works alongside Steam: it adds games to it and shows its "
                         "games too.",
        'folders': "Where games go, and where to look for downloads.",
        'proton': "Proton runs Windows games. Each game can use another one later.",
        'extras': "Both optional — and both can be set later in Settings.",
        'look': "Last: how it looks, and whether it keeps working with the window closed.",
    }

    def _lead(self):
        key = self.steps[self.step][0]
        text = self.LEADS[key + '-win' if key == 'launchers' and not has('heroic') else key]
        if key == 'checks' and self.checks:
            bad = [c for c in self.checks if c['group'] == 'Requirements'
                   and c['status'] == 'fail']
            if bad:
                text = f"{len(bad)} thing(s) to fix first: " + ", ".join(c['title'] for c in bad)
        self.lead.setText(text)

    def show_step(self, i: int):
        last = len(self.steps) - 1
        self.step = max(0, min(last, i))
        self.stack.setCurrentIndex(STEPS.index(self.steps[self.step]))   # a page per STEPS
        self.stack.updateGeometry()
        self.step_note.setText(f"Step {self.step + 1} of {len(self.steps)} · "
                               f"{self.steps[self.step][1]}")
        self.track.set_steps([name for _k, name in self.steps], self.step)
        self.title.setText("Welcome to Nexus" if self.step == 0 else self.steps[self.step][1])
        self.btn_back.setVisible(self.step > 0)
        self.btn_next.setText("Done" if self.step == last else "Next")
        self._lead()

    def _next(self):
        if self.step == len(self.steps) - 1:
            self._finish()
        else:
            self.show_step(self.step + 1)

    def _look(self) -> dict:
        return {'theme': self.theme.currentData(), 'scale': self.size.currentData()}

    def answers_now(self) -> tuple[dict, dict]:
        """(config.toml updates, {'appearance', 'agent', 'tray'})."""
        u = {}
        if self.base.text().strip():
            u[('install', 'base')] = self.base.text().strip()
        if self.scan.text().strip():
            u[('install', 'scan_dir')] = self.scan.text().strip()
        if self.key.text().strip():
            u[('steamgriddb', 'api_key')] = self.key.text().strip()
        if self.proton.currentData() and has('proton'):
            u[('runner', 'proton')] = self.proton.currentData()
        if self._torrent and self.use_torrent.isChecked():
            u[('torrent', 'client')] = self._torrent['client']
            u[('torrent', 'url')] = self._torrent['url']
        return u, {'appearance': self._look(), 'agent': self.agent.isChecked(),
                   'tray': self.tray.isChecked() and self.tray.isEnabled()}

    def _finish(self):
        u, opts = self.answers_now()
        self.finished.emit(u, opts)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .jobs import cli_json  # noqa: E402
from .settings import Swatch, Switch  # noqa: E402
from .topbar import Wordmark  # noqa: E402
from .tray import tray_available  # noqa: E402
from .widgets import (amp, clear_layout, FlowLayout, icon_label, label, NoWheelCombo,  # noqa: E402
    plain_label, set_glyph_icon)
