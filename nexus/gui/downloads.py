"""The Downloads page: the torrent client from inside Nexus."""

import time
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QEvent, QPoint, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout,
    QLineEdit, QMenu, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from nexus import pipeline

from .fmt import fmt_bytes, fmt_eta
from .theme import FS, G, P, scaled, SP


def torrent_state(r: dict) -> str:
    """One line for a torrent: "42% · 8.1 MB/s · 12 min left"."""
    if r.get('gone'):
        return "No longer in the torrent client"
    if r.get('error'):
        return "Error: " + str(r['error'])
    state = r.get('state') or ('paused' if r.get('paused') else '')
    if r.get('installed'):
        # The game is in the library; this line is about its torrent (w-94).
        bits = ["Game installed", "torrent seeding" if state == 'seeding' else "torrent kept"]
        if r.get('ratio') is not None:
            bits.append(f"ratio {r['ratio']:.2f}")
        return " · ".join(bits)
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


def _home(path: str) -> str:
    home = str(Path.home())
    return '~' + path[len(home):] if path == home or path.startswith(home + '/') else path


def others_note(info: dict) -> str:
    """Why a download can look stuck here: another Transmission runs with its
    own torrents (torrent.other_transmissions), and Nexus follows only the
    one answering its RPC port."""
    others = info.get('others') or []
    if not others:
        return ''
    names = ", ".join(f"{o['name']} ({_home(o['config_dir'])})" for o in others)
    one = len(others) == 1
    return (f"Another Transmission is running: {names}. Nexus follows the one in "
            f"{_home(info.get('config_dir') or '')}, so {'its' if one else 'their'} torrents "
            f"and progress don't show here. Quit the one you don't use.")


FINISH_SOON = 15            # s: a download this close to done is polled every 2 s


def finishing(rows: list) -> bool:
    """Whether a download is about to finish (Transmission's ETA, seconds),
    so the poll speeds up and the Install page, Theatre and auto-install
    see the finish within 2 s instead of 10."""
    return any(not r.get('done') and not r.get('gone') and r.get('state') == 'downloading'
               and r.get('eta') is not None and r['eta'] <= FINISH_SOON for r in rows)


def grab_fit_line(page: dict) -> str:
    """The Grab sheet's space line (pipeline.grab_fit): "Needs about 13 GB
    on /home (12 GB free) · short by 1 GB", or each drive named when the
    download and the install go to different ones. "Short by" counts what
    the downloads in flight still need. '' when the free space is unknown."""
    fit = pipeline.grab_fit(page)
    if not fit['drives']:
        return ''
    parts = [f"{'about ' if d['role'] != 'download' and fit['install_known'] else ''}"
             f"{fmt_bytes(d['need'])} on {d['where']} ({fmt_bytes(d['free'])} free)"
             for d in fit['drives']]
    line = "Needs " + " and ".join(parts)
    if fit['short_by']:
        line += f" · short by {fmt_bytes(fit['short_by'])}"
    return line


READY_HOURS = pipeline.READY_HOURS   # a finished download or install stays a row
PRIMARY_LABELS = {'install': "Install", 'play': "Play", 'pause': "Pause", 'resume': "Resume",
                  'retry': "Retry", 'show': "Show game"}


def theatre_rows(torrents: list, jobs: list, games: list, ready_at: dict, now: float,
                 mono: float) -> list[dict]:
    """What Theatre's Downloads tab lists, grouped Ready to play / In progress
    / Up next. Pure, so each source is tested with fake data. Rows:
    group, key, kind (torrent|job|game), title, state (a line), big (the one
    number), frac (0..1 or None), paused, primary (an action key or None),
    slug, ref (what the action needs: the torrent row, the job, the game).
    `torrents` are Nexus's own (tracked); `jobs` all of them (installs are
    looked at); `ready_at` is {slug: epoch} for installs that just finished;
    `mono` is time.monotonic() (jobs stamp their end with it)."""
    rows: list[dict] = []
    by_slug = {g.get('slug'): g for g in games}

    def row(group, key, kind, title, state, big, frac=None, paused=False, primary=None,
            slug=None, ref=None):
        rows.append({'group': group, 'key': key, 'kind': kind, 'title': title, 'state': state,
                     'big': big, 'frac': frac, 'paused': paused, 'primary': primary,
                     'slug': slug, 'ref': ref})

    for slug, t in ready_at.items():                   # installs that finished
        g = by_slug.get(slug)
        if g is None or now - t > READY_HOURS * 3600:
            continue
        played = g.get('last_played') or ''
        if played and played >= _iso(t):
            continue                                   # played since: done with
        row('ready', f'game:{slug}', 'game', g.get('display_name') or slug,
            "Installed · ready to play", "Ready", 1.0, primary='play', slug=slug, ref=g)
    installing = {j.slug for j in jobs if j.kind == 'install' and j.active}
    for r in torrents:
        if not r.get('tracked') or r.get('gone') or r.get('installed'):
            continue
        title = r.get('name') or r.get('title') or str(r.get('hash', ''))[:12]
        key = f"torrent:{r.get('hash')}"
        pct = float(r.get('percent') or 0)
        state = pipeline.download_state(r)
        if state == 'error':
            row('progress', key, 'torrent', title, torrent_state(r), "Error", pct / 100, ref=r)
        elif state == 'done':
            row('ready', key, 'torrent', title, torrent_state(r), "Downloaded", 1.0,
                primary='install' if r.get('path') else None, ref=r)
        elif state == 'paused':
            row('progress', key, 'torrent', title, torrent_state(r), f"{pct:.0f}%", pct / 100,
                paused=True, primary='resume', ref=r)
        elif state in ('queued', 'checking'):
            row('queue', key, 'torrent', title, torrent_state(r), f"{pct:.0f}%", pct / 100,
                primary='pause', ref=r)
        else:
            row('progress', key, 'torrent', title, torrent_state(r),
                fmt_eta(r['eta']) if r.get('eta') else f"{pct:.0f}%", pct / 100,
                primary='pause', ref=r)
    waiting = [j for j in jobs if j.kind == 'install' and j.state == 'queued']
    for j in jobs:
        if j.kind != 'install':
            continue
        key = f'job:{id(j)}'
        title = j.name or j.title
        if j.state in ('running', 'cancelling'):
            frac = (j.progress[0] / j.progress[1]) if j.progress and j.progress[1] else None
            row('progress', key, 'job', title, j.step or "Installing",
                f"{int(frac * 100)}%" if frac is not None else "Installing", frac,
                slug=j.slug, ref=j)
        elif j.state == 'queued':
            n = waiting.index(j) + 1
            row('queue', key, 'job', title, "Waiting for the current job",
                f"#{n} in line", slug=j.slug, ref=j)
        elif j.state == 'failed' and j.slug not in installing and j.ended \
                and mono - j.ended < READY_HOURS * 3600:
            row('progress', key, 'job', title, j.step, "Failed", primary='retry',
                slug=j.slug, ref=j)
    for g in games:                                     # Steam and Heroic downloads
        inst = g.get('installing')
        if not isinstance(inst, dict) or g.get('slug') in installing:
            continue
        done, total = int(inst.get('done') or 0), int(inst.get('total') or 0)
        frac = done / total if total else None
        row('progress', f"game:{g.get('slug')}", 'game', g.get('display_name') or g['slug'],
            "Downloading", f"{int(frac * 100)}%" if frac is not None else "Downloading",
            frac, primary='show', slug=g['slug'], ref=g)
    rows.sort(key=lambda r: ('ready', 'progress', 'queue').index(r['group']))
    return rows


def _iso(epoch: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(epoch).isoformat(timespec='seconds')


def torrent_items(r: dict, emit) -> list[tuple]:
    """A torrent's ⋮ choices, (text, glyph, fn): `emit(action, r)` with
    folder|install|remove|delete|forget. A download row's and a pipeline
    card's (w-94)."""
    out = []
    path = r.get('path')
    if r.get('gone'):
        return [("Stop following it", G['close'], lambda: emit('forget', r))]
    if path and Path(path).exists():
        out.append(("Open the folder", G['folder'], lambda: emit('folder', r)))
        if r.get('done') and not r.get('installed') and Path(path).is_dir():
            out.append(("Install it now", G['download'], lambda: emit('install', r)))
    out.append(("Remove from the client", G['close'], lambda: emit('remove', r)))
    out.append(("Remove and delete the files", G['trash'], lambda: emit('delete', r)))
    return out


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
        name = r.get('name') or r['hash'][:12]
        if r.get('tracked') and r.get('installed') and not r.get('gone'):
            # Also in Done recently as a game: here it reads as its torrent.
            name = f"{r.get('title') or name} · seeding" if r.get('state') == 'seeding' \
                else f"{r.get('title') or name} · torrent"
        self.name.setText(name)
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
        return torrent_items(self.r, self.action.emit)

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
    """Every torrent in your client, live. Nexus's own are pipeline cards
    from the torrent to Play (w-94): a disk forecast, In progress, Ready to
    play, then Done recently and Other torrents (the client's other rows),
    both collapsed. The data comes from `nexus-cli --torrents --all
    --json`, polled by the main window. `game_of(card)` finds a card's game
    in the library, `job_of(card)` the install job that failed (Show log)."""
    toast = pyqtSignal(str, str)
    control = pyqtSignal(str, list, bool)     # pause|resume|remove, hashes, delete files
    add = pyqtSignal(str, bool)               # magnet/.torrent, install when done
    row_action = pyqtSignal(str, dict)        # folder|install|forget, row
    card_action = pyqtSignal(str, dict)       # retry|verify|install|play|show|log|clear, card
    open_settings = pyqtSignal()
    use_client = pyqtSignal(dict)             # {'client', 'url'} found by --torrent-detect
    SHIMMER_MS = 40
    FILTER_FROM = 8           # Other torrents gets a filter above this many rows

    def __init__(self, game_of=None, job_of=None):
        super().__init__()
        self.data: dict = {}
        self.rows: dict[str, DownloadRow] = {}          # Other torrents
        self.cards: dict[str, PipelineCard] = {}
        self.done_rows: dict[str, DoneRow] = {}
        self.game_of = game_of or (lambda c: None)
        self.job_of = job_of or (lambda c: None)
        self._seen: dict = {}                           # pipeline.stalled's memory
        self._cleared: set[str] = set()                 # Clear pressed, poll not back yet
        self._detected = None
        self._shimmer = QTimer(self)
        self._shimmer.setInterval(self.SHIMMER_MS)
        self._shimmer.timeout.connect(self._shimmer_tick)
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
        self.others = label("", None, wrap=True)        # a second Transmission is running
        self.others.setStyleSheet(f"color: {P['warning']};")
        self.others.setVisible(False)
        outer.addWidget(self.others)
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
        self.list.setSpacing(SP['3'])
        self.forecast = ForecastStrip(host)
        self.kick_progress = kicker("In progress")
        self.kick_progress.setParent(host)
        self.kick_ready = kicker("Ready to play")
        self.kick_ready.setParent(host)
        done_body = QWidget(host)
        dv = QVBoxLayout(done_body)
        dv.setContentsMargins(0, 0, 0, 0)
        dv.setSpacing(SP['2'])
        ctop = QHBoxLayout()
        ctop.addStretch()
        self.btn_clear = QPushButton("Clear", done_body)
        self.btn_clear.setToolTip("Hide these here. The games and files stay.")
        self.btn_clear.clicked.connect(self._clear)
        ctop.addWidget(self.btn_clear)
        dv.addLayout(ctop)
        self.done_list = QVBoxLayout()
        self.done_list.setSpacing(SP['2'])
        dv.addLayout(self.done_list)
        self.done_box = Disclosure('downloads_done', "Done recently", G['check_circle'],
                                   done_body)
        self.done_box.setParent(host)
        other_body = QWidget(host)
        ov = QVBoxLayout(other_body)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(SP['2'])
        self.other_filter = QLineEdit(other_body)            # only for a long list
        self.other_filter.setPlaceholderText("Filter these torrents")
        self.other_filter.setClearButtonEnabled(True)
        self.other_filter.textChanged.connect(self._filter_other)
        ov.addWidget(self.other_filter)
        self.other_filter.setVisible(False)
        self.other_none = label("No torrent matches", "Muted")
        self.other_none.setParent(other_body)
        ov.addWidget(self.other_none)
        self.other_none.setVisible(False)
        self.other_list = QVBoxLayout()
        self.other_list.setSpacing(SP['2'])
        ov.addLayout(self.other_list)
        self.other_box = Disclosure('downloads_other', "Other torrents", G['download'],
                                    other_body)
        self.other_box.setParent(host)
        for w in (self.forecast, self.kick_progress, self.kick_ready, self.done_box,
                  self.other_box):
            self.list.addWidget(w)
        self.list.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)
        self.set_data({})
        self.scroll.viewport().installEventFilter(self)     # last (w-98)

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

    def _card_act(self, action, c):
        if action in ('pause', 'resume'):
            self.control.emit(action, [c['hash']], False)
        else:
            self.card_action.emit(action, c)

    def _clear(self):
        hashes = [h for h in self.done_rows]
        if not hashes:
            return
        fw = QApplication.focusWidget()
        inside = fw is not None and self.done_box.isAncestorOf(fw)
        self._cleared.update(hashes)
        self._fill()
        if inside:
            # Done recently is gone now (w-60): somewhere that stays.
            self._visible_focus().setFocus(Qt.FocusReason.OtherFocusReason)
        self.card_action.emit('clear', {'hashes': hashes})

    def clear_failed(self, hashes: list):
        """The CLI couldn't clear them: show them again."""
        self._cleared.difference_update(hashes)
        self._fill()

    def _visible_focus(self) -> QWidget:
        for w in (*(self.cards[k] for k in self.cards), self.other_box):
            if not w.isHidden():
                return w.default_focus() if isinstance(w, PipelineCard) else w.head
        return self.magnet

    def _filter_other(self):
        text = self.other_filter.text().strip().casefold()
        shown = 0
        for w in self.rows.values():
            hit = not text or text in w.name.text().casefold()
            w.setVisible(hit)
            shown += hit
        self.other_none.setVisible(bool(self.rows) and not shown)

    def restyle(self):
        """A theme or Size change (MainWindow.set_appearance): restyle in
        place, so focus stays."""
        self.forecast.restyle()
        for w in self.cards.values():
            w.restyle()

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
        self.btn_pause_all.setVisible(ok and any(r.get('state') not in ('paused', None)
                                                 for r in rows if not r.get('gone')))
        self.btn_resume_all.setVisible(ok and any(r.get('state') == 'paused' for r in rows))
        if ok:
            self.status.setText(f"{info.get('client', '')} {info.get('version', '')}".strip()
                                + f" · ↓ {fmt_bytes(info.get('down') or 0)}/s"
                                + f" · ↑ {fmt_bytes(info.get('up') or 0)}/s")
        else:
            self.status.setText("")
        text = others_note(info) if ok else ''
        self.others.setText(text)
        self.others.setVisible(bool(text))
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

    def _place(self, lay, widgets: list):
        """Put `widgets` first in `lay`, in this order, without reparenting."""
        for i, w in enumerate(widgets):
            if lay.indexOf(w) != i:
                lay.removeWidget(w)
                lay.insertWidget(i, w)

    def _drop(self, store: dict, keep: set, lay):
        for k in [k for k in store if k not in keep]:
            w = store.pop(k)
            fw = QApplication.focusWidget()
            if fw is not None and (fw is w or w.isAncestorOf(fw)):
                self.scroll.setFocus(Qt.FocusReason.OtherFocusReason)    # before it goes
            lay.removeWidget(w)
            w.hide()
            w.deleteLater()

    def _fill(self):
        d = self.data
        rows = d.get('torrents') or []
        cards = d.get('pipeline') or []
        now = time.time()
        by_hash = {r.get('hash'): r for r in rows}
        games = {c['key']: self.game_of(c) for c in cards}
        played = {c.get('slug'): (games[c['key']] or {}).get('last_played') for c in cards}
        secs = pipeline.sections(cards, rows, played)
        # Clear pressed: hidden until the poll shows the mark (or the row left).
        self._cleared = {h for h in self._cleared
                         if h in by_hash and not by_hash[h].get('history_cleared')}
        stuck = pipeline.stalled(self._seen, cards, now)
        client = (d.get('client') or {}).get('client') or ''
        shown = secs['progress'] + secs['ready']
        self._drop(self.cards, {c['key'] for c in shown}, self.list)
        narrow = self._narrow()
        for c in shown:
            w = self.cards.get(c['key'])
            if w is None:
                w = PipelineCard(c, self.scroll.widget(), row_sink=self._row_action)
                w.action.connect(self._card_act)
                self.cards[c['key']] = w
                self.list.insertWidget(0, w)
                w.set_narrow(narrow)
            w.set(c, by_hash.get(c['hash']), stalled=c['hash'] in stuck,
                  has_log=c['state'] == 'failed' and self.job_of(c) is not None,
                  game=games[c['key']], client=client)
        n_prog, n_ready = len(secs['progress']), len(secs['ready'])
        self.kick_progress.setText(f"IN PROGRESS · {n_prog}")
        self.kick_ready.setText(f"READY TO PLAY · {n_ready}")
        self._place(self.list, [self.forecast, self.kick_progress,
                                *(self.cards[c['key']] for c in secs['progress']),
                                self.kick_ready,
                                *(self.cards[c['key']] for c in secs['ready']),
                                self.done_box, self.other_box])
        for w in self.cards.values():
            w.show()
        self.forecast.set_drives(d.get('drives') or [] if secs['progress'] else [])
        self.kick_progress.setVisible(bool(n_prog))
        self.kick_ready.setVisible(bool(n_ready))
        # Done recently
        done = [r for r in secs['done'] if r.get('hash') not in self._cleared]
        self._drop(self.done_rows, {r['hash'] for r in done}, self.done_list)
        for r in done:
            w = self.done_rows.get(r['hash'])
            if w is None:
                w = self.done_rows[r['hash']] = DoneRow(self.done_box.body)
                w.action.connect(self.card_action)
                self.done_list.addWidget(w)
                w.show()
            w.set(r, now, self.game_of(r) is not None)
        self._place(self.done_list, [self.done_rows[r['hash']] for r in done])
        self.done_box.set_summary(str(len(done)))
        self.done_box.setVisible(bool(done))
        # Other torrents: the client's rows that aren't cards
        other = secs['other']
        self._drop(self.rows, {r['hash'] for r in other}, self.other_list)
        for r in other:
            w = self.rows.get(r['hash'])
            if w is None:
                w = self.rows[r['hash']] = DownloadRow(r)
                w.action.connect(self._row_action)
                self.other_list.addWidget(w)
                w.show()
            else:
                w.set(r)
        self._place(self.other_list, [self.rows[r['hash']] for r in other])
        self.other_box.set_summary(str(len(other)))
        self.other_box.setVisible(bool(other))
        long = len(other) > self.FILTER_FROM
        if not long and self.other_filter.text():
            self.other_filter.clear()                   # (refilters)
        self.other_filter.setVisible(long)
        self._filter_other()
        has = bool(shown or done or other)
        self.scroll.setVisible(has)
        self.empty_host.setVisible(not has)
        self._sync_shimmer()

    # ---- motion ----
    def _moving(self) -> list:
        return [w for w in self.cards.values() if w.track.shimmer]

    def _sync_shimmer(self):
        """One page timer repaints the moving tracks: only with motion on,
        the page on screen and something moving."""
        on = art.MOTION and self._on_screen() and bool(self._moving())
        if on and not self._shimmer.isActive():
            self._shimmer.start()
        elif not on and self._shimmer.isActive():
            self._shimmer.stop()

    def _shimmer_tick(self):
        moving = self._moving()
        for w in moving:
            w.track.tick()
        if not moving or not self._on_screen():
            self._shimmer.stop()            # the next poll starts it again

    def _on_screen(self) -> bool:
        """Shown, and its window neither minimised nor covered (isVisible
        stays True for a minimised window)."""
        if not self.isVisible():
            return False
        win = self.window()
        if win.windowState() & Qt.WindowState.WindowMinimized:
            return False
        handle = win.windowHandle()
        return handle is None or handle.isExposed()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._sync_shimmer()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._shimmer.stop()

    # ---- width ----
    def _narrow(self) -> bool:
        # The scroll's viewport, never the cards' own width (w-101).
        return self.scroll.viewport().width() < scaled(NARROW)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.scroll.viewport():
            narrow = self._narrow()
            for w in self.cards.values():
                w.set_narrow(narrow)
        return False

    def active_count(self) -> int:
        return sum(1 for r in self.data.get('torrents') or []
                   if not r.get('done') and not r.get('gone') and r.get('state') != 'paused')


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from . import art  # noqa: E402
from .actions import ActionSheet  # noqa: E402
from .deals import _pill  # noqa: E402
from .details import Disclosure  # noqa: E402
from .pipeline_view import DoneRow, kicker, NARROW, PipelineCard, ForecastStrip  # noqa: E402
from .input import input_mode  # noqa: E402
from .widgets import EmptyState, icon_button, label, set_glyph_icon  # noqa: E402
