"""The Proton page: installed builds, who uses them, GE-Proton updates."""

import re

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QMenu, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .fmt import fmt_bytes
from .theme import FS, G, P, SP


class ProtonPage(QWidget):
    """Like ProtonPlus, inside the launcher: install/update GE-Proton (verified
    download into Steam's compatibilitytools.d), see which games use each
    build, move games between builds, and remove builds nothing needs."""
    request = pyqtSignal(list, str, bool, str)
    update_available = pyqtSignal(bool)
    toast = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.info: dict = {}
        self.releases: list[dict] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        host = QWidget()
        scroll.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        head.addWidget(label("Proton", "PageTitle"))
        head.addStretch()
        self.btn_check = QPushButton("Check for updates")
        self.btn_check.clicked.connect(lambda: self.refresh(force=True))
        head.addWidget(self.btn_check)
        lay.addLayout(head)

        # Update banner
        self.banner = QFrame()
        self.banner.setObjectName("Card")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(SP['4'], SP['3'], SP['3'], SP['3'])
        bl.addWidget(icon_label(G['download'], 18, P['accent']))
        self.banner_text = label("", None, wrap=True)
        self.banner_text.setStyleSheet(f"color: {P['text']};")
        bl.addWidget(self.banner_text, 1)
        self.btn_install_latest = QPushButton("Install")
        self.btn_install_latest.setObjectName("Primary")
        self.btn_install_latest.clicked.connect(lambda: self._install('latest'))
        bl.addWidget(self.btn_install_latest)
        self.banner.setVisible(False)
        lay.addWidget(self.banner)

        lay.addWidget(label("Installed", "ShelfTitle"))
        self.installed = QVBoxLayout()
        self.installed.setSpacing(SP['2'])
        lay.addLayout(self.installed)

        lay.addWidget(label("GE-Proton releases", "ShelfTitle"))
        self.rel_list = QVBoxLayout()
        self.rel_list.setSpacing(SP['1_5'])
        lay.addLayout(self.rel_list)
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("Console")
        self.notes.setReadOnly(True)
        self.notes.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.notes.setMaximumHeight(220)
        self.notes.setVisible(False)
        lay.addWidget(self.notes)
        lay.addStretch()

    def refresh(self, force=False):
        self.btn_check.setEnabled(False)
        args = ['--protons', '--json'] + (['--refresh'] if force else [])

        def done(data, err):
            try:
                self.btn_check.setEnabled(True)
            except RuntimeError:
                return
            if data is None:
                self.toast.emit(f"Couldn't read Proton builds: {err}", 'error')
                return
            self.info = data
            self._fill_installed()
            self._fill_banner()
        cli_json(args, done, 60000, ttl=0 if force else 120)

        def rel_done(data, err):
            try:
                self._fill_releases((data or {}).get('releases', []), err)
            except RuntimeError:
                pass
        cli_json(['--proton-releases', '--json'] + (['--refresh'] if force else []),
                 rel_done, 60000, ttl=0 if force else 600)

    def _fill_banner(self):
        ge = self.info.get('families', {}).get('ge', {})
        upd = bool(ge.get('update'))
        self.update_available.emit(upd)
        dismissed = ge.get('latest') in self.info.get('protongen_dismissed', [])
        self.banner.setVisible(upd)
        if upd:
            self.banner_text.setText(
                f"{ge['latest']} is available."
                + ("  (You dismissed it in protongen.)" if dismissed else "")
                + "  Installing adds it next to your current builds; nothing is replaced.")
        cy = self.info.get('families', {}).get('cachyos', {})
        self._cachy = cy

    def _fill_installed(self):
        clear_layout(self.installed)
        rows = self.info.get('installed', [])
        others = [r['name'] for r in rows]
        for r in rows:
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
            cl.setSpacing(SP['1'])
            top = QHBoxLayout()
            name = plain_label(r['name'], None)
            name.setStyleSheet(f"font-weight: 500; color: {P['text']};")
            top.addWidget(name)
            tag = {'user': "installed by you", 'system': "system package"}.get(r['source'], '')
            top.addWidget(label(tag, "Muted"))
            if r['name'] == self.info.get('default'):
                top.addWidget(_pill("default", 'accent'))
            top.addStretch()
            users = [u for u in r['used_by'] if u != 'Default for new games']
            if r['source'] == 'system' and r.get('family') == 'cachyos':
                cy = self.info.get('families', {}).get('cachyos', {})
                top.addWidget(label(f"pacman {cy.get('installed') or ''}", "Muted"))
                copy = QPushButton("Copy update command")
                copy.setToolTip("System package — update it with pacman")
                copy.clicked.connect(lambda: (QApplication.clipboard().setText(
                    "sudo pacman -Syu proton-cachyos"),
                    self.toast.emit("Copied: sudo pacman -Syu proton-cachyos", 'info')))
                top.addWidget(copy)
            if len(others) > 1 and users:
                mv = QPushButton("Move games to…")
                menu = QMenu(mv)
                for o in others:
                    if o != r['name']:
                        menu.addAction(o).triggered.connect(
                            lambda _=False, old=r['name'], new=o, n=len(users):
                            self._ask_migrate(old, new, n))
                mv.setMenu(menu)
                top.addWidget(mv)
            if r.get('removable'):
                rm = QPushButton("Remove")
                rm.setObjectName("Danger")
                rm.setToolTip("Moves it to the Trash" if not users else
                              "Still in use — move its games to another build first")
                rm.setEnabled(not users)
                rm.clicked.connect(lambda _=False, n=r['name']: self.request.emit(
                    ['--proton-remove', n], f"Removing {n}", False, 'task'))
                top.addWidget(rm)
            cl.addLayout(top)
            self._add_usage(cl, r['used_by'])
            self.installed.addWidget(card)
        for v in self.info.get('valve', []):
            card = QFrame()
            card.setObjectName("Card")
            cl = QHBoxLayout(card)
            cl.setContentsMargins(SP['4'], SP['2_5'], SP['4'], SP['2_5'])
            n = plain_label(v['name'])
            cl.addWidget(n)
            cl.addWidget(label("managed by Steam", "Muted"))
            cl.addStretch()
            self.installed.addWidget(card)

    def _ask_migrate(self, old, new, n):
        ask(self, f"Move {n} game{'s' if n != 1 else ''} to {new}?",
            f"Every game that uses {old} switches to {new}. You can move them back the "
            f"same way.", "Move games",
            lambda: self.request.emit(['--proton-migrate', old, new],
                                      f"Moving games from {old} to {new}", True, 'task'),
            danger=False)

    def _add_usage(self, lay, used_by):
        """"Default · used by 11 games (4 Nexus, 5 Steam, 2 Heroic)", with the
        names one click away instead of a three-line run-on paragraph."""
        default = 'Default for new games' in used_by
        games = [u for u in used_by if u != 'Default for new games']
        groups: dict[str, list[str]] = {}
        for u in games:
            src, _, name = u.partition(': ')
            groups.setdefault(src if name else '', []).append(name or u)
        bits = ["Default for new games"] if default else []
        if games:
            n = len(games)
            by_src = ", ".join(f"{len(v)} {k}" for k, v in groups.items() if k)
            bits.append(f"used by {n} game{'s' if n != 1 else ''}"
                        + (f" ({by_src})" if by_src else ""))
        text = " · ".join(bits) if bits else "Not used by any game"
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        summary = label(text[0].upper() + text[1:], "Muted", wrap=True)
        row.addWidget(summary, 1)
        if games:
            names = label("\n".join(f"{k}: {', '.join(v)}" if k else ', '.join(v)
                                    for k, v in groups.items()), "Muted", wrap=True)
            names.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['xs']}px;")
            names.setVisible(False)
            more = QPushButton("Show games")
            more.setObjectName("Ghost")
            more.clicked.connect(lambda _=False, w=names, b=more: (
                w.setVisible(not w.isVisible()),
                b.setText("Hide games" if w.isVisible() else "Show games")))
            row.addWidget(more, 0, Qt.AlignmentFlag.AlignTop)
            lay.addLayout(row)
            lay.addWidget(names)
        else:
            lay.addLayout(row)

    def _fill_releases(self, rels, err):
        clear_layout(self.rel_list)
        self.releases = rels
        if not rels:
            self.rel_list.addWidget(label(f"Couldn't load releases: {err}" if err else
                                          "No releases found.", "Muted", wrap=True))
            return
        for r in rels[:8]:
            row = QHBoxLayout()
            t = QPushButton(f"{r['tag']}   ·   {(r.get('published_at') or '')[:10]}   ·   "
                            f"{fmt_bytes(r.get('tar_size'))}")
            t.setObjectName("ToolRow")
            t.setStyleSheet("text-align: left;")
            t.setToolTip("Show release notes")
            t.clicked.connect(lambda _=False, r=r: self._show_notes(r))
            row.addWidget(t, 1)
            # Same-width trailing slot on every row so the rows line up.
            slot = QWidget()
            slot.setFixedWidth(84)
            sl = QHBoxLayout(slot)
            sl.setContentsMargins(0, 0, 0, 0)
            if r.get('installed'):
                sl.addWidget(_pill("Installed", 'success'), 0, Qt.AlignmentFlag.AlignCenter)
            else:
                b = QPushButton("Install")
                b.setEnabled(bool(r.get('tar_url') and r.get('sha_url')))
                b.clicked.connect(lambda _=False, tag=r['tag']: self._install(tag))
                sl.addWidget(b)
            row.addWidget(slot)
            self.rel_list.addLayout(row)

    def _show_notes(self, r):
        body = re.sub(r'<[^>]+>', '', r.get('body') or '').strip()
        self.notes.setPlainText(f"{r['name']}\n\n{body or 'No release notes.'}")
        self.notes.setVisible(True)

    def _install(self, tag):
        self.request.emit(['--proton-install', tag],
                          f"Installing {tag if tag != 'latest' else 'the latest GE-Proton'}",
                          False, 'install-proton')


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .deals import _pill  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .widgets import ask, clear_layout, icon_label, label, plain_label  # noqa: E402
