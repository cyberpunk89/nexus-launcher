"""The Downloads page: the torrent client from inside Nexus."""

from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QPoint, Qt
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMenu,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .fmt import fmt_bytes, fmt_eta
from .theme import FS, G, P, SP


def torrent_state(r: dict) -> str:
    """One line for a torrent: "42% · 8.1 MB/s · 12 min left"."""
    if r.get('gone'):
        return "No longer in the torrent client"
    if r.get('error'):
        return "Error: " + str(r['error'])
    if r.get('installed'):
        return "Installed"
    state = r.get('state') or ('paused' if r.get('paused') else '')
    if r.get('done'):
        bits = ["Seeding" if state == 'seeding' else "Downloaded"]
        if r.get('up'):
            bits.append(f"↑ {fmt_bytes(r['up'])}/s")
        if r.get('ratio') is not None:
            bits.append(f"ratio {r['ratio']:.2f}")
        return " · ".join(bits)
    if state == 'paused':
        return f"Paused at {r.get('percent', 0):.0f}%"
    if state in ('queued', 'checking'):
        return f"{state.capitalize()} · {r.get('percent', 0):.0f}%"
    bits = [f"{r.get('percent', 0):.0f}%"]
    if r.get('rate'):
        bits.append(f"↓ {fmt_bytes(r['rate'])}/s")
    if r.get('eta'):
        bits.append(fmt_eta(r['eta']))
    return " · ".join(bits)


class DownloadRow(QFrame):
    """One torrent: name, state, progress, a pause/resume button and ⋮."""
    action = pyqtSignal(str, dict)   # pause|resume|remove|delete|folder|install, row

    def __init__(self, r: dict):
        super().__init__()
        self.setObjectName("Card")
        self.r: dict = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['3'], SP['2_5'], SP['2'], SP['2_5'])
        lay.setSpacing(SP['3'])
        col = QVBoxLayout()
        col.setSpacing(SP['1'])
        top = QHBoxLayout()
        top.setSpacing(SP['1_5'])
        self.name = label("", None)
        self.name.setStyleSheet(f"color: {P['text']}; font-size: {FS['sm']}px; font-weight: 500;")
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.name, 1)
        self.tag = _pill("Nexus", 'accent')
        top.addWidget(self.tag)
        col.addLayout(top)
        self.state = label("", "Muted")
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.state)
        self.bar = QProgressBar()
        self.bar.setObjectName("CardProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setRange(0, 1000)
        col.addWidget(self.bar)
        lay.addLayout(col, 1)
        self.size = label("", "Muted")
        lay.addWidget(self.size)
        self.btn_toggle = icon_button(G['pause'], "Pause", 32)
        self.btn_toggle.clicked.connect(self._toggle)
        lay.addWidget(self.btn_toggle)
        self.btn_more = icon_button(G['more'], "More", 32)
        self.btn_more.clicked.connect(self._menu)
        lay.addWidget(self.btn_more)
        self.set(r)

    def set(self, r: dict):
        if r == self.r:
            return
        self.r = r
        self.name.setText(r.get('name') or r['hash'][:12])
        self.name.setToolTip(r.get('path') or r.get('name') or '')
        self.tag.setVisible(bool(r.get('tracked')))
        self.tag.setText("Nexus · installs when done" if r.get('auto_install')
                         and not r.get('installed') and not r.get('done') else "Nexus")
        self.state.setText(torrent_state(r))
        self.state.setStyleSheet(f"color: {P['danger']};" if r.get('error') else '')
        gone = bool(r.get('gone'))
        self.bar.setVisible(not gone and not r.get('done'))
        self.bar.setValue(int(10 * (r.get('percent') or 0)))
        self.size.setText(fmt_bytes(r['size']) if r.get('size') else '')
        paused = r.get('state') == 'paused' or (r.get('paused') and not r.get('state'))
        set_glyph_icon(self.btn_toggle, G['play'] if paused else G['pause'], 16)
        self.btn_toggle.setToolTip("Resume" if paused else "Pause")
        self.btn_toggle.setVisible(not gone)

    def _toggle(self):
        paused = self.r.get('state') == 'paused' or self.r.get('paused')
        self.action.emit('resume' if paused else 'pause', self.r)

    def items(self) -> list[tuple]:
        r, out = self.r, []
        path = r.get('path')
        if r.get('gone'):
            return [("Stop following it", G['close'], lambda: self.action.emit('forget', r))]
        if path and Path(path).exists():
            out.append(("Open the folder", G['folder'], lambda: self.action.emit('folder', r)))
            if r.get('done') and not r.get('installed') and Path(path).is_dir():
                out.append(("Install it now", G['download'],
                            lambda: self.action.emit('install', r)))
        out.append(("Remove from the client", G['close'], lambda: self.action.emit('remove', r)))
        out.append(("Remove and delete the files", G['trash'],
                    lambda: self.action.emit('delete', r)))
        return out

    def _menu(self):
        sheet = getattr(self.window(), 'sheet', None)
        if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
            sheet.choose(self.name.text(), self.items())
            return
        m = QMenu(self)
        for text, _glyph, fn in self.items():
            m.addAction(text, fn)
        m.exec(self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height())))
        m.deleteLater()


class DownloadsPage(QWidget):
    """Every torrent in your client, live: progress, speed, pause / resume /
    remove, and the repacks Nexus installs when they finish. The data comes
    from `nexus-cli --torrents --all --json`, polled by the main window."""
    toast = pyqtSignal(str, str)
    control = pyqtSignal(str, list, bool)     # pause|resume|remove, hashes, delete files
    add = pyqtSignal(str, bool)               # magnet/.torrent, install when done
    row_action = pyqtSignal(str, dict)        # folder|install|forget, row
    open_settings = pyqtSignal()
    use_client = pyqtSignal(dict)             # {'client', 'url'} found by --torrent-detect
    FILTERS = (('all', "All"), ('active', "Downloading"), ('done', "Finished"),
               ('nexus', "From Nexus"))

    def __init__(self):
        super().__init__()
        self.data: dict = {}
        self.rows: dict[str, DownloadRow] = {}
        self._filter = 'all'
        self._detected = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        outer.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.addWidget(label("Downloads", "PageTitle"))
        head.addStretch()
        self.status = label("", "Muted")
        head.addWidget(self.status)
        self.btn_pause_all = QPushButton("Pause all")
        self.btn_pause_all.clicked.connect(lambda: self._all('pause'))
        head.addWidget(self.btn_pause_all)
        self.btn_resume_all = QPushButton("Resume all")
        self.btn_resume_all.clicked.connect(lambda: self._all('resume'))
        head.addWidget(self.btn_resume_all)
        outer.addLayout(head)
        self.addbox = QWidget()
        arow = QHBoxLayout(self.addbox)
        arow.setContentsMargins(0, 0, 0, 0)
        arow.setSpacing(SP['1_5'])
        self.magnet = QLineEdit()
        self.magnet.setPlaceholderText("Paste a magnet link, or pick a .torrent file")
        self.magnet.returnPressed.connect(self._send)
        tfile = QPushButton("File…")
        tfile.clicked.connect(self._pick)
        tgo = QPushButton("Download")
        tgo.setObjectName("Primary")
        tgo.clicked.connect(self._send)
        self.auto_install = QCheckBox("Install when finished")
        self.auto_install.setChecked(True)
        arow.addWidget(self.magnet, 1)
        arow.addWidget(tfile)
        arow.addWidget(tgo)
        arow.addWidget(self.auto_install)
        outer.addWidget(self.addbox)
        self.chips_host = QWidget()
        self.chips = QHBoxLayout(self.chips_host)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(SP['1_5'])
        self._chip_btns = {}
        for key, text in self.FILTERS:
            c = QPushButton(text)
            c.setObjectName("Chip")
            c.setCheckable(True)
            c.setChecked(key == self._filter)
            c.clicked.connect(lambda _=False, k=key: self._pick_filter(k))
            self.chips.addWidget(c)
            self._chip_btns[key] = c
        self.chips.addStretch()
        outer.addWidget(self.chips_host)
        self.empty_host = QWidget()
        elay = QVBoxLayout(self.empty_host)
        elay.setContentsMargins(0, 0, 0, 0)
        self.empty = EmptyState(G['download'], "Loading…")
        self.empty.action.connect(self._empty_action)
        elay.addWidget(self.empty)
        self.empty2 = QPushButton("Open Settings")
        self.empty2.clicked.connect(self.open_settings.emit)
        elay.addWidget(self.empty2, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty2.setVisible(False)
        elay.addStretch()
        outer.addWidget(self.empty_host, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.list = QVBoxLayout(host)
        self.list.setContentsMargins(0, 0, 0, SP['4'])
        self.list.setSpacing(SP['2'])
        self.list.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)
        self.set_data({})

    # ---- input ----
    def _send(self):
        src = self.magnet.text().strip()
        if not src:
            return
        if not (src.lower().startswith('magnet:?') or src.lower().endswith('.torrent')):
            self.toast.emit("That isn't a magnet link or a .torrent file.", 'warn')
            return
        self.add.emit(src, self.auto_install.isChecked())
        self.magnet.clear()

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "Pick a .torrent file", str(Path.home()),
                                              "Torrent files (*.torrent)")
        if path:
            self.magnet.setText(path)
            self._send()

    def _all(self, action):
        hashes = [r['hash'] for r in self.data.get('torrents') or [] if not r.get('gone')
                  and (r.get('state') == 'paused') == (action == 'resume')]
        if hashes:
            self.control.emit(action, hashes, False)

    def _row_action(self, action, r):
        if action in ('pause', 'resume', 'remove'):
            self.control.emit(action, [r['hash']], False)
        elif action == 'delete':
            self.control.emit('remove', [r['hash']], True)
        else:
            self.row_action.emit(action, r)

    def _pick_filter(self, key):
        self._filter = key
        for k, c in self._chip_btns.items():
            c.setChecked(k == key)
        self._fill()

    def _empty_action(self):
        if self._detected:
            self.use_client.emit(self._detected)
        else:
            self.open_settings.emit()

    def set_detected(self, found: dict | None):
        """A client answering on its default port while none is set up."""
        self._detected = found
        self._render_state()

    # ---- data ----
    def set_data(self, data: dict | None, err: str | None = None):
        self.data = data if data is not None else {
            'error': {'kind': 'net', 'message': err or "no answer"}, 'configured': True,
            'torrents': []}
        self._render_state()
        self._fill()

    def configured(self) -> bool:
        return self.data.get('configured', True) is not False

    def _render_state(self):
        d, err = self.data, (self.data.get('error') or {})
        info = d.get('client') or {}
        rows = d.get('torrents') or []
        ok = bool(d) and not err
        self.addbox.setVisible(ok)
        self.chips_host.setVisible(ok and bool(rows))
        self.btn_pause_all.setVisible(ok and any(r.get('state') not in ('paused', None)
                                                 for r in rows if not r.get('gone')))
        self.btn_resume_all.setVisible(ok and any(r.get('state') == 'paused' for r in rows))
        if ok:
            self.status.setText(f"{info.get('client', '')} {info.get('version', '')}".strip()
                                + f" · ↓ {fmt_bytes(info.get('down') or 0)}/s"
                                + f" · ↑ {fmt_bytes(info.get('up') or 0)}/s")
        else:
            self.status.setText("")
        self.empty2.setVisible(False)
        if not d:
            self.empty.set("Loading…", "", G['download'])
            self.empty.button.setVisible(False)
        elif not self.configured():
            if self._detected:
                name = self._detected['client'].capitalize()
                self.empty.set(f"{name} is running", f"Nexus found {name}'s remote access at "
                               f"{self._detected['url']}. Use it to manage your downloads "
                               "here and to grab repacks from your wishlist.", G['download'])
                self.empty.button.setText(f"Use {name}")
                self.empty2.setVisible(True)
            else:
                self.empty.set("Connect your torrent client",
                               "Nexus talks to Transmission or qBittorrent on this machine. "
                               "Turn on its remote access, then pick it in Settings → "
                               "Torrent client.", G['download'])
                self.empty.button.setText("Open Settings")
            self.empty.button.setVisible(True)
        elif err:
            self.empty.set("Can't reach the torrent client",
                           f"{err.get('message', '')}\n\nIn Transmission: Edit → Preferences → "
                           "Remote → Allow remote access. In qBittorrent: Tools → Options → "
                           "Web UI.", G['download'], 'warning')
            self.empty.button.setText("Open Settings")
            self.empty.button.setVisible(True)
        else:
            self.empty.set("No torrents", "Paste a magnet link above, or grab a repack from "
                           "Deals → On FitGirl.", G['download'], 'muted')
            self.empty.button.setVisible(False)

    def _shown(self, r) -> bool:
        if r.get('installed') and r.get('gone'):
            return False
        f = self._filter
        if f == 'active':
            return not r.get('done') and not r.get('gone')
        if f == 'done':
            return bool(r.get('done'))
        if f == 'nexus':
            return bool(r.get('tracked'))
        return True

    def _fill(self):
        rows = [r for r in self.data.get('torrents') or [] if self._shown(r)]
        keep = {r['hash'] for r in rows}
        for h in [h for h in self.rows if h not in keep]:
            w = self.rows.pop(h)
            self.list.removeWidget(w)
            w.deleteLater()
        for i, r in enumerate(rows):
            w = self.rows.get(r['hash'])
            if w is None:
                w = DownloadRow(r)
                w.action.connect(self._row_action)
                self.rows[r['hash']] = w
                self.list.insertWidget(i, w)
            else:
                w.set(r)
                if self.list.indexOf(w) != i:
                    self.list.removeWidget(w)
                    self.list.insertWidget(i, w)
        has = bool(rows)
        self.scroll.setVisible(has)
        self.empty_host.setVisible(not has)
        if not has and self.data.get('torrents') and not self.data.get('error'):
            self.empty.set("Nothing here", "No torrent matches this filter.", G['download'])
            self.empty.button.setVisible(False)

    def active_count(self) -> int:
        return sum(1 for r in self.data.get('torrents') or []
                   if not r.get('done') and not r.get('gone') and r.get('state') != 'paused')


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .actions import ActionSheet  # noqa: E402
from .deals import _pill  # noqa: E402
from .input import input_mode  # noqa: E402
from .widgets import EmptyState, icon_button, label, set_glyph_icon  # noqa: E402
