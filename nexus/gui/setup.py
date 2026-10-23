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
Settings → Library → "Run setup again" opens it later."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLineEdit, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget)

from .common import HOME, INSTALL_BASE_DEFAULT
from .theme import G, P, SCALES, SP, THEMES

STEPS = (('checks', "Checks"), ('launchers', "Your launchers"), ('folders', "Folders"),
         ('proton', "Proton"), ('extras', "Extras"), ('look', "Look & background"))
STATUS_GLYPH = {'ok': ('check_circle', 'success'), 'warn': ('warn', 'warning'),
                'fail': ('fail', 'danger'), 'info': ('info', 'muted')}


class SetupPage(QWidget):
    request = pyqtSignal(list, str, bool, str)        # a fix button: (args, title, steam, kind)
    finished = pyqtSignal(dict, dict)                 # (config updates, appearance + options)
    preview = pyqtSignal(dict)                        # the look, applied as it's picked

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['8'], SP['6'], SP['8'], SP['6'])
        outer.setSpacing(SP['4'])
        head = QHBoxLayout()
        self.title = label("Welcome to Nexus", "PageTitle")
        head.addWidget(self.title, 1)
        self.step_note = label("", "Muted")
        head.addWidget(self.step_note)
        outer.addLayout(head)
        self.lead = label("", "Muted", wrap=True)
        outer.addWidget(self.lead)
        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(820)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        self.stack = QStackedWidget()
        cl.addWidget(self.stack)
        outer.addWidget(card, 1)
        nav = QHBoxLayout()
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
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_next)
        w = QWidget()
        w.setMaximumWidth(820)
        w.setLayout(nav)
        outer.addWidget(w)
        self.step = 0
        self.checks: list[dict] = []
        self.answers: dict = {}
        self._build()

    # ---- the steps ----
    def _page(self) -> QVBoxLayout:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
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
        lay.addWidget(label("Each game gets its own folder (and, on btrfs, its own subvolume "
                            "so updates can be undone).", "Muted", wrap=True))
        lay.addSpacing(SP['2'])
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
        lay.addSpacing(SP['2'])
        lay.addWidget(label("Torrent client", "StatLabel"))
        self.torrent_note = label("Looking for Transmission or qBittorrent…", "Muted", wrap=True)
        lay.addWidget(self.torrent_note)
        self.use_torrent = Switch()
        lay.addLayout(self._switch_row(self.use_torrent, "Use it for repack downloads"))
        self.use_torrent.setEnabled(False)
        self._torrent = None
        lay.addStretch()
        lay = self._page()                             # 6 look & background
        lay.addWidget(label("Theme", "StatLabel"))
        self.theme = NoWheelCombo()
        for k, (name, t) in THEMES.items():
            self.theme.addItem(name + ("  (light)" if t.get('kind') == 'light' else ""), k)
        self.theme.activated.connect(lambda _i: self.preview.emit(self._look()))
        lay.addWidget(self.theme)
        lay.addWidget(label("Size", "StatLabel"))
        self.size = NoWheelCombo()
        for v in SCALES:
            self.size.addItem(f"{int(round(v * 100))}%", v)
        self.size.setCurrentIndex(SCALES.index(1.0))
        self.size.activated.connect(lambda _i: self.preview.emit(self._look()))
        lay.addWidget(self.size)
        lay.addSpacing(SP['2'])
        self.agent = Switch()
        lay.addLayout(self._switch_row(self.agent, "Keep watching downloads and sending alerts "
                                       "while Nexus is closed (a systemd user service)"))
        self.tray = Switch()
        lay.addLayout(self._switch_row(self.tray, "Close to the tray instead of quitting"))
        self.tray.setEnabled(tray_available())
        lay.addStretch()

    def _browse_row(self, edit):
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(
            self, "Choose a folder", edit.text() or edit.placeholderText())) and edit.setText(d))
        row.addWidget(b)
        return row

    def _switch_row(self, sw, text):
        row = QHBoxLayout()
        row.addWidget(label(text, None, wrap=True), 1)
        row.addWidget(sw)
        return row

    # ---- data from the CLI ----
    def open(self, config: dict, appearance: dict):
        self.base.setText(config.get('install_base') or '')
        self.scan.setText(config.get('scan_dir') or '')
        self.key.setText(config.get('api_key') or '')
        i = self.theme.findData(appearance.get('theme'))
        self.theme.setCurrentIndex(max(0, i))
        i = self.size.findData(appearance.get('scale', 1.0))
        self.size.setCurrentIndex(max(0, i))
        self.show_step(0)
        cli_json(['--doctor', '--json'], self._got_checks, 120000)
        cli_json(['--list-protons', '--json'], self._got_protons, 30000)
        cli_json(['--torrent-detect', '--json'], self._got_torrent, 15000)

    def _rows(self, lay, rows):
        clear_layout(lay)
        for c in rows:
            glyph, role = STATUS_GLYPH.get(c['status'], STATUS_GLYPH['info'])
            row = QHBoxLayout()
            row.setSpacing(SP['2'])
            row.addWidget(icon_label(G[glyph], 16, P[role]))
            text = label(f"{c['title']} — {c['detail']}" if c.get('detail') else c['title'],
                         None, wrap=True)
            row.addWidget(text, 1)
            for a in c.get('actions') or []:
                b = QPushButton(a['label'])
                b.clicked.connect(lambda _=False, a=a, t=c['title']: self.request.emit(
                    a['args'], t, bool(a.get('steam')), 'task'))
                row.addWidget(b)
            lay.addLayout(row)
            if c.get('fix') and c['status'] in ('warn', 'fail'):
                lay.addWidget(label(c['fix'], "Muted", wrap=True))
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
        'folders': "Where games go, and where to look for downloads.",
        'proton': "Proton runs Windows games. Each game can use another one later.",
        'extras': "Both optional — and both can be set later in Settings.",
        'look': "Last: how it looks, and whether it keeps working with the window closed.",
    }

    def _lead(self):
        key = STEPS[self.step][0]
        text = self.LEADS[key]
        if key == 'checks' and self.checks:
            bad = [c for c in self.checks if c['group'] == 'Requirements'
                   and c['status'] == 'fail']
            if bad:
                text = f"{len(bad)} thing(s) to fix first: " + ", ".join(c['title'] for c in bad)
        self.lead.setText(text)

    def show_step(self, i: int):
        self.step = max(0, min(len(STEPS) - 1, i))
        self.stack.setCurrentIndex(self.step)
        self.step_note.setText(f"Step {self.step + 1} of {len(STEPS)} · {STEPS[self.step][1]}")
        self.title.setText("Welcome to Nexus" if self.step == 0 else STEPS[self.step][1])
        self.btn_back.setVisible(self.step > 0)
        self.btn_next.setText("Done" if self.step == len(STEPS) - 1 else "Next")
        self._lead()

    def _next(self):
        if self.step == len(STEPS) - 1:
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
        if self.proton.currentData():
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
from .settings import Switch  # noqa: E402
from .tray import tray_available  # noqa: E402
from .widgets import clear_layout, icon_label, label, NoWheelCombo  # noqa: E402
