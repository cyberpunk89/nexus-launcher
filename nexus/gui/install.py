"""The Install page and the watcher that offers finished downloads.

Two tabs: "Install a repack" (repacks found in the download folders as
cards, a torrent box, a folder picker, then the safety look and options,
with a step strip saying where you are) and "Add a game" (an installer, a
game folder, or the .exe of a game another launcher installed). With
Settings > Repack features off only "Add a game" is there."""

import html
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QFileSystemWatcher, QObject, Qt, QTimer
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from nexus import paths as NX, platform, watch
from nexus.features import has

from .common import CLI, HOME, INSTALL_BASE_DEFAULT
from .fmt import fmt_bytes, fmt_size
from .theme import FS, G, P, SP

REPACK_STEPS = ("Pick a repack", "Safety check", "Options", "Install")


def wine_prefix_of(path: str) -> str | None:
    """The Wine prefix an .exe sits in (the folder above its drive_c), if any."""
    p = Path(path)
    return next((str(d.parent) for d in p.parents if d.name == 'drive_c'), None)


class StepStrip(QWidget):
    """Numbered steps in a row: done ones ticked, the current one in the
    accent, the rest muted. Plain labels, coloured from the palette."""

    def __init__(self, names):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['3'])
        self.labels = []
        for _n in names:
            lb = QLabel()
            lb.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(lb)
            self.labels.append(lb)
        lay.addStretch()
        self.names = list(names)
        self.current = -1
        self.set_current(0)

    def set_current(self, i: int):
        self.current = i
        for k, (lb, name) in enumerate(zip(self.labels, self.names)):
            mark = "✓" if k < i else str(k + 1)
            role = 'success' if k < i else 'accent' if k == i else 'muted'
            weight = 600 if k == i else 400
            lb.setText(f'<span style="color:{P[role]}; font-weight:{weight}">'
                       f'{mark}&nbsp;&nbsp;{html.escape(name)}</span>')


def import_name_guess(path: str) -> str:
    """What `--import` will probably call it (the CLI decides; this fills
    the placeholder): GOG's setup_<name>_<version>… tidied, or the folder."""
    import re
    p = Path(path)
    stem = p.stem if p.suffix.lower() in ('.exe', '.msi') else p.name
    m = re.match(r'^setup_(.+?)(?:_v?\d[\w.]*)?(?:_\(\d+bit\))?(?:_\(\d+\))*$', stem, re.I)
    stem = re.sub(r'[_.]+', ' ', m.group(1) if m else stem).strip()
    return stem[:1].upper() + stem[1:]


def protongen_has_default() -> bool:
    """Whether protongen has a default profile saved (`global_profile`)."""
    try:
        import tomllib
        with open(NX.PROTONGEN_CONFIG / 'state.toml', 'rb') as f:
            return isinstance(tomllib.load(f).get('global_profile'), dict)
    except (OSError, ValueError):
        return False


class RepackWatcher(QObject):
    """Notices repack downloads in the scan folders. A QFileSystemWatcher
    catches new folders; while one is still downloading a slow poll follows
    it. A folder is announced (`ready`) once it's complete and its size held
    still between two scans, and only once (nexus/watch.py, shared with
    the background agent).
    Every scan's candidates also go out as `scanned` (update detection)."""
    ready = pyqtSignal(dict)
    scanned = pyqtSignal(list)

    POLL_MS = 15000

    def __init__(self, prefs, parent=None):
        super().__init__(parent)
        self.prefs = prefs
        self.fs = QFileSystemWatcher(self)
        self.fs.directoryChanged.connect(self._soon)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(3000)
        self.debounce.timeout.connect(self.scan)
        self.poll = QTimer(self)
        self.poll.setInterval(self.POLL_MS)
        self.poll.timeout.connect(self.scan)
        self._skip: set[str] = set()                  # folders Nexus installs itself
        self._sizes: dict[str, int] = {}
        self._busy = False
        self.enabled = True                           # Settings > Repack features
        watch.migrate(prefs.seen_repacks)             # once: it used to live in library.json

    def set_enabled(self, on: bool):
        """Off: no scans and no folders watched; on again: a scan soon."""
        if on == self.enabled:
            return
        self.enabled = on
        if on:
            self.start(3000)
            return
        self.debounce.stop()
        self.poll.stop()
        if self.fs.directories():
            self.fs.removePaths(self.fs.directories())

    def start(self, delay_ms: int = 8000):
        """First scan a little after startup (a child timer: it dies with us)."""
        self.debounce.start(delay_ms)

    def _soon(self, _path=None):
        self.debounce.start(3000)

    def scan(self):
        if self._busy or not self.enabled:
            return
        self._busy = True
        cli_json(['--scan', '--json'], self._done, 120000)

    def _done(self, data, _err):
        self._busy = False
        if data is None:
            return
        try:
            self.fs.directories()
        except RuntimeError:                   # the window closed while we scanned
            return
        dirs = [d for d in (data.get('scan_dirs') or [data.get('scan_dir')])
                if d and Path(d).is_dir()]
        old = set(self.fs.directories())
        if old - set(dirs):
            self.fs.removePaths(list(old - set(dirs)))
        if set(dirs) - old:
            self.fs.addPaths(sorted(set(dirs) - old))
        cands = data.get('candidates') or []
        self.scanned.emit(cands)
        self.handle(cands)

    def mark_seen(self, path: str):
        """A folder Nexus is already installing (a finished torrent): never
        offer it as a new download too."""
        self._skip.add(str(Path(path)))

    def handle(self, cands: list[dict]):
        # Under watch.py's lock: the background agent scans the same folders,
        # and whichever of us sees a finished download first announces it.
        with watch.edit_seen() as box:
            ready, box['seen'], waiting = watch.triage(cands, box['seen'], self._sizes,
                                                       self._skip)
        for c in ready:
            self.ready.emit(c)
        if waiting and not self.poll.isActive():
            self.poll.start()
        elif not waiting:
            self.poll.stop()


class InstallPage(QWidget):
    submit = pyqtSignal(list, str, str)     # (argv, game name, slug or '')
    back = pyqtSignal()
    reindex = pyqtSignal()
    torrent = pyqtSignal(str, bool)         # (magnet or .torrent path, install when done)

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        outer.setSpacing(SP['3'])
        head = QHBoxLayout()
        b = QPushButton("← Back")
        b.setObjectName("Ghost")
        b.clicked.connect(self.back)
        head.addWidget(b)
        self.title = label("Install a repack", "PageTitle")
        head.addWidget(self.title)
        head.addStretch()
        found = QPushButton("Already installed? Add existing games")
        found.setToolTip("Adds games that are in the install folder but missing from the "
                         "library (e.g. after moving machines)")
        found.clicked.connect(self.reindex)
        self.found_btn = found
        head.addWidget(found)
        outer.addLayout(head)
        self.tabs = SegmentedControl('pill', 36)
        self.tabs.add('repack', "Install a repack", G['download'])
        self.tabs.add('game', "Add a game", G['plus'])
        self.tabs.changed.connect(self.set_mode)
        outer.addWidget(self.tabs)
        self.steps = StepStrip(REPACK_STEPS)
        outer.addWidget(self.steps)
        self._mode = 'repack'
        self._repacks = True
        self._kind = None                  # 'exe' | 'import' for "Add a game", else None

        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(720)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        lay.setSpacing(SP['2'])
        # Everything repack-only, hidden with Settings > Repack features off.
        self.repack_box = QWidget()
        outer_lay, lay = lay, QVBoxLayout(self.repack_box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        srow = QHBoxLayout()
        self.scan_label = label("Found in your repack folders", "StatLabel")
        srow.addWidget(self.scan_label, 1)
        self.btn_all = QPushButton("Install all")
        self.btn_all.setToolTip("Queue every finished download that isn't installed yet, "
                                "with the options below; they install one after another")
        self.btn_all.clicked.connect(self._install_all)
        self.btn_all.setVisible(False)
        srow.addWidget(self.btn_all)
        lay.addLayout(srow)
        self.scan_list = QVBoxLayout()
        self.scan_list.setSpacing(SP['1'])
        lay.addLayout(self.scan_list)
        self.scan_cards = QWidget()            # what was found, as game cards
        self.scan_flow = FlowLayout(self.scan_cards, 0, SP['2'])
        lay.addWidget(self.scan_cards)
        # Downloads handed to the torrent client from here.
        lay.addWidget(label("Download with your torrent client", "StatLabel"))
        trow = QHBoxLayout()
        self.magnet = QLineEdit()
        self.magnet.setPlaceholderText("Paste a magnet link, or pick a .torrent file")
        self.magnet.returnPressed.connect(self._send_torrent)
        tfile = QPushButton("File…")
        tfile.clicked.connect(self._pick_torrent)
        tgo = QPushButton("Download")
        tgo.clicked.connect(self._send_torrent)
        trow.addWidget(self.magnet, 1)
        trow.addWidget(tfile)
        trow.addWidget(tgo)
        lay.addLayout(trow)
        self.auto_install = QCheckBox("Install it when the download finishes")
        self.auto_install.setChecked(True)
        lay.addWidget(self.auto_install)
        self.dl_list = QVBoxLayout()
        self.dl_list.setSpacing(SP['1'])
        lay.addLayout(self.dl_list)
        lay.addWidget(label("Repack folder", "StatLabel"))
        frow = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Pick the folder that contains setup.exe")
        self.folder.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        frow.addWidget(self.folder, 1)
        frow.addWidget(browse)
        lay.addLayout(frow)
        outer_lay.addWidget(self.repack_box)
        lay = outer_lay
        # "Add a game": anything that isn't a repack.
        self.add_box = QWidget()
        al = QVBoxLayout(self.add_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(SP['2'])
        cards = QWidget()
        cf = FlowLayout(cards, 0, SP['2'])
        self.add_cards = {}
        for key, title, note, fn in (
                ('exe', "Already installed", "A game from a launcher Nexus doesn't support: "
                 "pick its .exe. It stays where it is.", self._browse_exe),
                ('installer', "From an installer", "An offline installer (.exe or .msi), e.g. "
                 "from GOG. Inno Setup ones run without their window.", self._browse_installer),
                ('folder', "From a game folder", "A folder that already holds the game. "
                 + ("It's copied into the game's own prefix." if has('proton') else
                    "It's added where it is; nothing is copied."), self._browse_game_folder)):
            c = ChoiceCard(title)
            c.setCheckable(False)
            c.setFixedWidth(212)
            c.add(label(title, "ChoiceTitle"))
            lb = label(note, "ChoiceNote", wrap=True)
            lb.setFixedWidth(212 - 2 * SP['2_5'])   # a wrapped label needs its width to size
            lb.setFixedHeight(lb.heightForWidth(lb.width()))
            c.add(lb)
            c.clicked.connect(fn)
            cf.addWidget(c)
            self.add_cards[key] = c
        al.addWidget(cards)
        self.picked = label("", "Muted", wrap=True)
        self.picked.setVisible(False)
        al.addWidget(self.picked)
        self.in_prefix = QCheckBox("Run it in the Wine prefix it's in")
        self.in_prefix.setToolTip("Use the other launcher's prefix (needed when the game "
                                  "wants that launcher running). Proton may update the prefix.")
        self.in_prefix.setVisible(False)
        al.addWidget(self.in_prefix)
        lay.addWidget(self.add_box)
        lay.addWidget(label("Name", "StatLabel"))
        self.name = QLineEdit()
        self.name.textChanged.connect(lambda _: self._check())
        lay.addWidget(self.name)
        self.warning = label("", None, wrap=True)
        self.warning.setStyleSheet(f"color: {P['warning']};")
        self.warning.setVisible(False)
        lay.addWidget(self.warning)
        # The safety look at the folder (`--check-repack`).
        self.safety = label("", None, wrap=True)
        self.safety.setVisible(False)
        lay.addWidget(self.safety)
        srow = QHBoxLayout()
        self.safety_ok = QCheckBox("I've looked at it — install anyway")
        self.safety_ok.setVisible(False)
        self.safety_ok.toggled.connect(lambda on: self.btn_go.setEnabled(on))
        srow.addWidget(self.safety_ok)
        srow.addStretch()
        self.btn_clam = QPushButton("Scan with ClamAV")
        self.btn_clam.setVisible(False)
        self.btn_clam.clicked.connect(lambda: self._safety_check(clamav=True))
        srow.addWidget(self.btn_clam)
        lay.addLayout(srow)

        opts = QHBoxLayout()
        col1, col2 = QVBoxLayout(), QVBoxLayout()
        self.steam = QCheckBox("Add to Steam")
        self.heroic = QCheckBox("Add to Heroic")
        self.desktop = QCheckBox("App-menu entry")
        self.artwork = QCheckBox("Fetch artwork")
        self.artwork.setChecked(True)
        self.verify = QCheckBox("Verify checksums first (slow)")
        self.import_save = QCheckBox("Import a previous save backup")
        self.pg_default = QCheckBox("Use my protongen defaults")
        self.skipmem = QCheckBox("Skip memory check")
        self.skipdisk = QCheckBox("Skip disk-space check")
        self.silent = QCheckBox("Install without the setup window")
        self.silent.setToolTip("FitGirl's installer runs silently with its default choices "
                               "(languages, optional parts) into the game's own folder. "
                               "Untick to click through the setup window yourself.")
        col1.addWidget(self.silent)
        for c in (self.steam, self.heroic, self.desktop, self.artwork, self.pg_default):
            col1.addWidget(c)
        for c in (self.verify, self.import_save, self.skipmem, self.skipdisk):
            col2.addWidget(c)
        opts.addLayout(col1)
        opts.addLayout(col2)
        lay.addLayout(opts)
        self.deps = QLineEdit()
        self.deps.setPlaceholderText("Extra winetricks verbs, usually none (e.g. vcrun2019)")
        lay.addWidget(self.deps)
        # Why the install folder is a poor place for games (Windows: OneDrive,
        # Program Files, a network drive, a long path); hidden when it's fine.
        self.base_note = plain_label("", None, wrap=True)
        self.base_note.setStyleSheet(f"color: {P['warning']};")
        self.base_note.setVisible(False)
        lay.addWidget(self.base_note)
        outer.addWidget(card)
        row = QHBoxLayout()
        row.addStretch()
        self.btn_go = QPushButton("Install")
        self.btn_go.setObjectName("Primary")
        self.btn_go.clicked.connect(self._go)
        row.addWidget(self.btn_go)
        w = QWidget()
        w.setMaximumWidth(720)
        w.setLayout(row)
        outer.addWidget(w)
        outer.addStretch()
        self._detected = {}
        self._repack_only = (self.silent, self.verify, self.import_save, self.skipmem,
                             self.skipdisk)
        # Windows: setup.exe runs as itself, no prefix, no Proton, no Heroic
        # (hide only: these were never shown, and a parentless show() is a window).
        if not has('protongen'):
            self.pg_default.setVisible(False)
        if not has('proton'):
            self.deps.setVisible(False)
        if not has('heroic'):
            self.heroic.setVisible(False)
        if not has('desktop_entry'):
            self.desktop.setText("Start Menu shortcut")
        self.set_mode('repack')

    def set_repacks(self, on: bool):
        """Settings > Repack features: off leaves only "Add a game"."""
        self._repacks = on
        self.tabs.setVisible(on)
        if not on:
            self.set_mode('game')
        elif self._mode != 'repack' and self._kind is None:
            self.set_mode('repack')

    def set_mode(self, mode: str):
        """Switch tabs: what's shown, the title and the button."""
        if mode == 'repack' and not self._repacks:
            mode = 'game'
        self._mode = mode
        if self.tabs.current() != mode:
            self.tabs.set_current(mode)
        repack = mode == 'repack'
        self.title.setText("Install a repack" if repack else "Add a game")
        self.repack_box.setVisible(repack)
        self.steps.setVisible(repack)
        self.found_btn.setVisible(repack)
        self.add_box.setVisible(not repack)
        for c in self._repack_only:
            c.setVisible(repack)
        self._clear_pick()

    def _clear_pick(self):
        self._import, self._kind = None, None
        self.folder.clear()
        self.name.clear()
        self.name.setPlaceholderText("")
        for w in (self.warning, self.safety, self.safety_ok, self.btn_clam, self.picked,
                  self.in_prefix):
            w.setVisible(False)
        self.in_prefix.setChecked(False)
        self.btn_go.setEnabled(True)
        self.btn_go.setText("Install" if self._mode == 'repack' else "Add game")
        self._sync_steps()

    def _sync_steps(self):
        """Where the repack install stands: picking, being checked, options."""
        if not self.folder.text().strip():
            i = 0
        elif self.safety.isVisible() and self.safety.text().startswith(("Looking", "Scanning")):
            i = 1
        else:
            i = 2
        self.steps.set_current(i)

    def open(self, config, mode: str | None = None):
        self.set_repacks(config.get('repacks', True))
        self.set_mode(mode or ('repack' if self._repacks else 'game'))
        self.steam.setChecked(config.get('steam', True))
        self.heroic.setChecked(config.get('heroic', True) and has('heroic'))
        self.desktop.setChecked(config.get('desktop', True))
        self.silent.setChecked(config.get('unattended', True))
        has_pg = has('protongen') and protongen_has_default()
        self.pg_default.setEnabled(has_pg)
        self.pg_default.setChecked(has_pg)
        self.pg_default.setToolTip(
            "Launch options and Proton from protongen's default profile, so the game is "
            "tuned from its first launch" if has_pg else
            "No default profile in protongen yet (Settings → Save as default profile)")
        self.show_base_warnings(config.get('install_base') or INSTALL_BASE_DEFAULT)
        if not self._repacks:
            return
        clear_layout(self.scan_list)
        clear_layout(self.scan_flow)
        self.scan_list.addWidget(label("Scanning…", "Muted"))
        self.scan_label.setVisible(True)
        cli_json(['--scan', '--json'], self._scanned, 120000)

    def show_base_warnings(self, base: str):
        warns = platform.install_base_warnings(Path(base))
        self.base_note.setText(f"Games install to {base}: " + "; ".join(warns) + ". Pick a "
                               "plain folder on a local drive (e.g. D:\\Games) in Settings → "
                               "Installing games." if warns else "")
        self.base_note.setVisible(bool(warns))

    def _scanned(self, data, err):
        try:
            clear_layout(self.scan_list)
        except RuntimeError:
            return
        if data is None:                       # say why the list is empty
            self.scan_label.setVisible(False)
            self.scan_list.addWidget(label(f"Couldn't look for repacks — {err or 'unknown error'}. "
                                           "Pick a folder with Browse instead.", "Muted",
                                           wrap=True))
            return
        cands = (data or {}).get('candidates', [])
        dirs = (data or {}).get('scan_dirs') or []
        self.scan_label.setVisible(bool(cands))
        self.scan_label.setToolTip("\n".join(dirs))
        self._fresh = [c for c in cands if c.get('complete', True)
                       and (not c.get('installed') or c.get('update_for'))]
        self.btn_all.setText(f"Install all {len(self._fresh)}")
        self.btn_all.setVisible(len(self._fresh) > 1)
        clear_layout(self.scan_flow)
        for c in cands:
            self.scan_flow.addWidget(self._repack_card(c))
        if not cands:
            self.scan_list.addWidget(label("No repacks in your download folders yet. "
                                           "Pick one with Browse, or download one below.",
                                           "Muted", wrap=True))

    def _repack_card(self, c: dict) -> QPushButton:
        """A found repack as a small game card: stand-in art, name, size and
        what it is (new, an update, installed, still downloading)."""
        state = ("Update for an installed game" if c.get('update_for') else
                 "Already installed" if c.get('installed') else
                 "Still downloading" if not c.get('complete', True) else "Ready to install")
        card = ChoiceCard(c['name'])
        card.setCheckable(False)
        card.setFixedWidth(200)
        art = QLabel()
        art.setPixmap(placeholder_pixmap(c['name'], 176, 82))
        card.add(art)
        card.add(ElideLabel(c['name'], "ChoiceTitle"))
        card.add(label(f"{fmt_size(c.get('size_bytes')) or '?'}  ·  {state}", "ChoiceNote"))
        card.setToolTip(c['path'])
        card.clicked.connect(lambda _=False, c=c: self._pick(c['path'], c['name']))
        return card

    def _pick_torrent(self):
        f, _ = QFileDialog.getOpenFileName(self, "Pick a .torrent file", str(HOME / 'Downloads'),
                                           "Torrent files (*.torrent)")
        if f:
            self.magnet.setText(f)
            self._send_torrent()

    def _send_torrent(self):
        src = self.magnet.text().strip()
        if not src:
            return
        if not (src.lower().startswith('magnet:?') or src.lower().endswith('.torrent')):
            self.warning.setText("That isn't a magnet link or a .torrent file.")
            self.warning.setVisible(True)
            return
        self.torrent.emit(src, self.auto_install.isChecked())
        self.magnet.clear()

    def set_downloads(self, rows: list[dict]):
        """The torrents added from Nexus, with their progress."""
        try:
            clear_layout(self.dl_list)
        except RuntimeError:
            return
        for r in rows:
            if r.get('installed') or r.get('gone'):
                continue
            row = QHBoxLayout()
            name = plain_label(r.get('name') or r['hash'][:12], None)
            name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(name, 1)
            state = ("Error: " + r['error'] if r.get('error') else
                     "Downloaded" + (" — installing" if r.get('auto_install') else "")
                     if r.get('done') else "Paused" if r.get('paused') else
                     f"{r.get('percent', 0):.0f}%" + (f" · {fmt_bytes(r['rate'])}/s"
                                                       if r.get('rate') else ''))
            row.addWidget(label(state, "Muted"))
            self.dl_list.addLayout(row)
            bar = QProgressBar()
            bar.setObjectName("CardProgress")
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setRange(0, 1000)
            bar.setValue(int(10 * (r.get('percent') or 0)))
            self.dl_list.addWidget(bar)

    def _pick(self, path, name=None):
        if not self._repacks:                  # a dropped folder: just a game folder then
            return self.pick_import(path)
        if self._mode != 'repack':
            self.set_mode('repack')
        self._import, self._kind = None, None
        self.btn_go.setText("Install")
        self.folder.setText(path)
        self._detected = {}
        self.name.clear()
        self.name.setPlaceholderText(name or '')
        self._check_now()
        self._safety_check()

    def _safety_check(self, clamav: bool = False):
        folder = self.folder.text().strip()
        self.safety_ok.setVisible(False)
        self.btn_go.setEnabled(True)
        if not folder:
            self.safety.setVisible(False)
            return
        self.safety.setText("Scanning with ClamAV…" if clamav else "Looking the repack over…")
        self.safety.setStyleSheet(f"color: {P['muted']};")
        self.safety.setVisible(True)
        self._sync_steps()
        cli_json(['--check-repack', folder, '--json'] + (['--clamav'] if clamav else []),
                 lambda r, _e, f=folder: self._safety_done(f, r or {}), 900000)

    def _safety_done(self, folder, r):
        try:
            if folder != self.folder.text().strip():
                return
        except RuntimeError:
            return
        v = r.get('verdict')
        if not v:
            self.safety.setVisible(False)
            self._sync_steps()
            return
        head = {'ok': "Looks like a normal FitGirl repack.",
                'warn': "Some things look unusual — check them before installing:",
                'danger': "This repack looks tampered with. Don't install it unless you're "
                          "sure:"}[v]
        items = "".join(f"<br>• <b>{html.escape(f['file'])}</b>: {html.escape(f['why'])}"
                        for f in r.get('findings') or []
                        if f['level'] != 'info' or v == 'ok' or f.get('hv'))
        clam = r.get('clamav') or {}
        if clam.get('available') and not clam.get('infected') and not clam.get('error'):
            items += "<br>ClamAV found nothing."
        self.safety.setText(head + items)
        self.safety.setVisible(True)
        self.safety.setStyleSheet(f"color: {P[{'ok': 'success', 'warn': 'warning', 'danger': 'danger'}[v]]};")
        self.btn_clam.setVisible(bool(r.get('clamav_available')) and not clam.get('available'))
        # A hypervisor-bypass repack can't run under Proton: installing it
        # takes the same tick as a tampered one (both pass --ignore-safety).
        hv_here = bool(r.get('hypervisor')) and has('proton')
        danger = v == 'danger' or hv_here
        self.safety_ok.setText("I know it won't run under Proton — install anyway"
                               if hv_here and v != 'danger' else
                               "I've looked at it — install anyway")
        self.safety_ok.setChecked(False)
        self.safety_ok.setVisible(danger)
        self.btn_go.setEnabled(not danger)
        self._sync_steps()

    def _browse_installer(self):
        f, _ = QFileDialog.getOpenFileName(self, "Select a game installer", str(HOME / 'Downloads'),
                                           "Windows installers (*.exe *.msi *.EXE *.MSI)")
        if f:
            self.pick_import(f)

    def _browse_game_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select the game's folder",
                                             str(HOME / 'Downloads'))
        if d:
            self.pick_import(d)

    def _browse_exe(self):
        f, _ = QFileDialog.getOpenFileName(self, "Pick the game's .exe", str(HOME),
                                           "Windows programs (*.exe *.EXE)")
        if f:
            self.pick_exe(f)

    def pick_exe(self, path: str):
        """A game another launcher installed: added where it is (--add-exe)."""
        self.pick_import(path)
        self._kind = 'exe'
        self.name.setPlaceholderText(import_name_guess(str(Path(path).parent)))
        pfx = wine_prefix_of(path) if has('proton') else None
        self.in_prefix.setText(f"Run it in the Wine prefix it's in ({pfx})" if pfx else "")
        self.in_prefix.setVisible(bool(pfx))
        self.picked.setText(f"Adds {Path(path).name} where it is: nothing is copied, and "
                            "removing it from Nexus later leaves the game alone.")
        self.picked.setVisible(True)

    def pick_import(self, path: str):
        """Any other game: no repack checks; the CLI names it (editable)."""
        if self._mode != 'game':
            self.set_mode('game')
        self.folder.setText(path)
        self._import, self._kind = path, 'import'
        self.picked.setText(f"Adds {Path(path).name}.")
        self.picked.setVisible(True)
        self.in_prefix.setVisible(False)
        self._detected = {}
        self.name.clear()
        self.name.setPlaceholderText(import_name_guess(path))
        self.warning.setVisible(False)
        self.safety.setVisible(False)
        self.safety_ok.setVisible(False)
        self.btn_clam.setVisible(False)
        self.btn_go.setEnabled(True)
        self.btn_go.setText("Add game")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select repack folder", str(HOME / 'Downloads'))
        if d:
            self._pick(d)

    def _check(self):
        """Typing a name: ask the CLI (in the background) once typing pauses."""
        if not hasattr(self, '_check_timer'):
            self._check_timer = QTimer(self)
            self._check_timer.setSingleShot(True)
            self._check_timer.setInterval(250)
            self._check_timer.timeout.connect(self._check_now)
        self._check_timer.start()

    def _check_now(self):
        """What the folder would install as (name, slug, already installed,
        a new release), from `--detect` — the CLI's own slug logic."""
        folder = self.folder.text().strip()
        if not folder:
            self.warning.setVisible(False)
            return
        name = self.name.text().strip()
        cli_json(['--detect', folder, *(['--name', name] if name else [])],
                 lambda info, _err, want=(folder, name): self._detected_now(want, info or {}),
                 15000)

    def _detected_now(self, want, info):
        try:
            if want != (self.folder.text().strip(), self.name.text().strip()):
                return                             # an answer for an older folder/name
        except RuntimeError:
            return
        self._detected = info
        if not want[1] and not self.name.placeholderText():
            self.name.setPlaceholderText(info.get('name') or '')
        if info.get('update'):
            self.warning.setText(f"This is a new release of {info.get('name')}. Installing it "
                                 f"updates the game in place; saves and settings stay.")
            self.btn_go.setText("Install update")
        elif info.get('installed'):
            self.warning.setText(f"{info.get('name')} is already installed. Installing again "
                                 f"reuses its prefix — useful for an update or DLC repack.")
            self.btn_go.setText("Install on top")
        else:
            self.btn_go.setText("Install")
        self.warning.setVisible(bool(info.get('installed')))

    def _go(self):
        folder = self.folder.text().strip()
        if not folder:
            self.warning.setText("Pick a repack folder first." if self._mode == 'repack' else
                                 "Pick what to add first.")
            self.warning.setVisible(True)
            return
        name = self.name.text().strip()
        if getattr(self, '_import', None) == folder and self._kind == 'exe':
            cmd = self._command(folder, name, imported=True, exe=True)
            self.submit.emit(cmd, name or self.name.placeholderText() or Path(folder).stem, '')
            return
        if getattr(self, '_import', None) == folder:
            cmd = self._command(folder, name, imported=True)
            self.submit.emit(cmd, name or self.name.placeholderText() or Path(folder).name, '')
            return
        cmd = self._command(folder, name)
        if self.safety_ok.isVisible() and self.safety_ok.isChecked():
            cmd.append('--ignore-safety')
        shown = name or self.name.placeholderText() or Path(folder).name
        self.submit.emit(cmd, shown, '' if name else self._detected.get('slug') or '')

    def _install_all(self):
        for c in list(getattr(self, '_fresh', [])):
            self.submit.emit(self._command(c['path'], ''), c['name'], c.get('slug') or '')

    def _command(self, folder: str, name: str, imported: bool = False,
                 exe: bool = False) -> list[str]:
        """The install command line for the options on this page. Steam never
        holds it up: the shortcut is added afterwards, once Steam is closed.
        `imported`: any other game (--import), which has no repack checks;
        with `exe`, one that's installed already (--add-exe)."""
        cmd = ([*CLI, "--add-exe", folder] if exe else
               [*CLI, "--import", folder] if imported else [*CLI, folder]) + \
            ["--yes", "--log", "--defer-steam"]
        if name:
            cmd += ["--name", name]
        if exe and self.in_prefix.isVisible() and self.in_prefix.isChecked():
            cmd.append("--in-prefix")
        if not exe and not self.silent.isChecked():
            cmd.append("--interactive")
        deps = self.deps.text().strip()
        if deps:
            cmd += ["--deps", deps]
        for box, flag in ((self.verify, "--verify"), (self.skipmem, "--skip-mem-check"),
                          (self.skipdisk, "--skip-disk-check"),
                          (self.import_save, "--import-save"),
                          (self.pg_default, "--use-protongen-default")):
            if imported and flag != "--use-protongen-default":
                continue
            if box.isChecked():
                cmd.append(flag)
        for box, flag in ((self.steam, "--no-steam"), (self.heroic, "--no-heroic"),
                          (self.desktop, "--no-desktop"), (self.artwork, "--no-artwork")):
            if not box.isChecked():
                cmd.append(flag)
        return cmd


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .jobs import cli_json  # noqa: E402
from .art import placeholder_pixmap  # noqa: E402
from .details import ElideLabel  # noqa: E402
from .setup import ChoiceCard  # noqa: E402
from .widgets import (clear_layout, FlowLayout, label, plain_label,
    SegmentedControl)  # noqa: E402
