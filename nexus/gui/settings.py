"""The Settings page."""

import os
import re

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QKeySequence, QLinearGradient,
    QPainter, QPen, QShortcut)
from PyQt6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from nexus import gamedb

from .common import ATTRACT_MINUTES, HOME, INSTALL_BASE_DEFAULT
from .theme import (_hex_ok, _mix, accent_hex, ACCENTS, APPEARANCE_DEFAULT, G,
    normalize_appearance, P, pill, R, R_BASE, SP, THEMES)


class Swatch(QAbstractButton):
    """A colour (or theme preview, or gradient) to pick, painted; checkable
    and focusable so the controller can choose too."""

    def __init__(self, kind: str, value, tip: str, size=(30, 30)):
        super().__init__()
        self.kind, self.value = kind, value
        self.setCheckable(True)
        self.setToolTip(tip)
        self.setFixedSize(*size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(f"border-radius: {pill(size[1]) if kind != 'theme' else R['control']}px;")

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        pt.setPen(Qt.PenStyle.NoPen)
        if self.kind == 'theme':
            t = THEMES[self.value][1]
            rad = float(R_BASE['control'])
            pt.setBrush(QColor(t['base']))
            pt.drawRoundedRect(r, rad, rad)
            card = QRectF(r.left() + 8, r.top() + 8, r.width() * 0.55, r.height() - 16)
            pt.setBrush(QColor(_mix(t['base'], t['overlay'], 0.6)))
            pt.drawRoundedRect(card, 4, 4)
            pt.setBrush(QColor(t['text']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.top() + 7, card.width() - 18, 4), 2, 2)
            pt.setBrush(QColor(t['muted']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.top() + 15, card.width() - 28, 3), 1.5, 1.5)
            pt.setBrush(QColor(P['accent']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.bottom() - 11, 26, 6), 3, 3)
            pt.setBrush(QColor(t['overlay_hi']))
            pt.drawEllipse(QPointF(r.right() - 16, r.center().y()), 7, 7)
            ring = r
        else:
            a, b = self.value if isinstance(self.value, tuple) else (self.value, self.value)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, QColor(a))
            g.setColorAt(1.0, QColor(b))
            pt.setBrush(QBrush(g))
            pt.drawEllipse(r.adjusted(3, 3, -3, -3))
            ring = r
        if self.isChecked():
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.setPen(QPen(QColor(P['text']), 2.0))
            if self.kind == 'theme':
                pt.drawRoundedRect(ring, float(R_BASE['control']), float(R_BASE['control']))
            else:
                pt.drawEllipse(ring)
        pt.end()


class AppearanceCard(QFrame):
    """Theme, accent, gradient, glow, corners — applied as you click, saved
    straight away (no Save needed)."""
    changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.setMaximumWidth(720)
        self.ap = dict(APPEARANCE_DEFAULT)
        lay = QFormLayout(self)
        lay.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        lay.setHorizontalSpacing(SP['3'])
        lay.setVerticalSpacing(SP['3'])

        def lab(t):
            w = label(t)
            w.setFixedWidth(SettingsPage.LABEL_W)
            return w

        self.groups: dict[str, list[Swatch]] = {}
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        for key, (name, _t) in THEMES.items():
            sw = Swatch('theme', key, name, (96, 56))
            sw.clicked.connect(lambda _=False, k=key: self._set('theme', k))
            row.addWidget(sw)
            self.groups.setdefault('theme', []).append(sw)
        row.addStretch()
        lay.addRow(lab("Theme"), row)
        self.theme_name = label("", "Muted")
        lay.addRow(lab(""), self.theme_name)

        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        for key, hexv in ACCENTS.items():
            sw = Swatch('color', hexv, key.capitalize())
            sw.clicked.connect(lambda _=False, k=key: self._set('accent', k))
            row.addWidget(sw)
            self.groups.setdefault('accent', []).append(sw)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("#rrggbb")
        self.custom.setMaxLength(7)
        self.custom.setFixedWidth(96)
        self.custom.setToolTip("Any colour, as a hex code")
        self.custom.editingFinished.connect(self._custom)
        row.addSpacing(SP['2'])
        row.addWidget(self.custom)
        row.addStretch()
        lay.addRow(lab("Accent colour"), row)

        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        self.partner_row = row
        lay.addRow(lab("Gradient"), row)

        self.glow = SegmentedControl('pill', 32)
        for k, t in (('off', "Off"), ('subtle', "Subtle"), ('vivid', "Vivid")):
            self.glow.add(k, t)
        self.glow.changed.connect(lambda k: self._set('glow', k))
        g = QHBoxLayout()
        g.addWidget(self.glow)
        self.art_tint = QCheckBox("Tint it with the featured game's art")
        self.art_tint.toggled.connect(lambda on: self._set('art_tint', on))
        g.addSpacing(SP['3'])
        g.addWidget(self.art_tint)
        g.addStretch()
        lay.addRow(lab("Background glow"), g)

        self.corners = SegmentedControl('pill', 32)
        for k, t in (('sharp', "Sharp"), ('default', "Soft"), ('round', "Round")):
            self.corners.add(k, t)
        self.corners.changed.connect(lambda k: self._set('corners', k))
        c = QHBoxLayout()
        c.addWidget(self.corners)
        c.addStretch()
        reset = QPushButton("Reset appearance")
        reset.setObjectName("Ghost")
        set_glyph_icon(reset, G['reset'], 15)
        reset.clicked.connect(lambda: self.load(dict(APPEARANCE_DEFAULT), emit=True))
        c.addWidget(reset)
        lay.addRow(lab("Corners"), c)

    def _partners(self):
        clear_layout(self.partner_row)
        self.groups['partner'] = []
        accent = accent_hex(self.ap['accent'])
        sw = Swatch('color', (accent, accent), "None — solid accent")
        sw.clicked.connect(lambda _=False: self._set('partner', 'none'))
        self.partner_row.addWidget(sw)
        self.groups['partner'].append(sw)
        sw.key = 'none'
        for key, hexv in ACCENTS.items():
            if hexv == accent:
                continue
            sw = Swatch('color', (accent, hexv), f"{accent} → {key.capitalize()}")
            sw.key = key
            sw.clicked.connect(lambda _=False, k=key: self._set('partner', k))
            self.partner_row.addWidget(sw)
            self.groups['partner'].append(sw)
        self.partner_row.addStretch()

    def load(self, ap: dict, emit=False):
        self.ap = normalize_appearance(ap)
        self._sync()
        if emit:
            self.changed.emit(dict(self.ap))

    def _sync(self):
        self._partners()
        for sw in self.groups['theme']:
            sw.setChecked(sw.value == self.ap['theme'])
        for sw, key in zip(self.groups['accent'], ACCENTS):
            sw.setChecked(key == self.ap['accent'])
        for sw in self.groups['partner']:
            sw.setChecked(sw.key == self.ap['partner'])
        self.custom.setText(self.ap['accent'] if _hex_ok(self.ap['accent']) else "")
        self.theme_name.setText(THEMES[self.ap['theme']][0])
        self.glow.set_current(self.ap['glow'], animate=False)
        self.corners.set_current(self.ap['corners'], animate=False)
        self.art_tint.blockSignals(True)
        self.art_tint.setChecked(self.ap['art_tint'])
        self.art_tint.blockSignals(False)
        self.art_tint.setEnabled(self.ap['glow'] != 'off')
        for grp in self.groups.values():
            for sw in grp:
                sw.update()

    def _set(self, key, value):
        if self.ap.get(key) == value:
            self._sync()
            return
        self.ap[key] = value
        if key == 'accent' and self.ap['partner'] not in ('none',) and \
                accent_hex(self.ap['partner']) == accent_hex(value):
            self.ap['partner'] = 'none'
        self._sync()
        self.changed.emit(dict(self.ap))

    def _custom(self):
        v = self.custom.text().strip()
        if not v.startswith('#'):
            v = '#' + v
        if _hex_ok(v):
            self._set('accent', v.lower())


class SettingsPage(QWidget):
    """Edits the config.toml keys the GUI owns; everything else in the file
    (comments, unknown keys) is preserved by save_config()."""
    request = pyqtSignal(list, str, bool, str)
    saved = pyqtSignal(bool)            # proton_changed
    controls_changed = pyqtSignal(dict)  # a library preference, applied at once

    def __init__(self):
        super().__init__()
        # Scrolls: three sections don't fit a half-height tiled window.
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        scroll.setWidget(host)
        page.addWidget(scroll)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        outer.setSpacing(SP['3'])
        outer.addWidget(label("Settings", "PageTitle"))
        self.error = label("", "Muted", wrap=True)
        outer.addWidget(self.error)
        head = label("Appearance", "ShelfTitle")
        head.setMaximumWidth(720)
        outer.addWidget(head)
        self.appearance = AppearanceCard()
        outer.addWidget(self.appearance)
        form = self._section(outer, "General")
        key_row = QHBoxLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self.key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row.addWidget(self.key)
        key_row.addWidget(show)
        fill = QPushButton("Fetch extra artwork")
        fill.setToolTip("Download missing hero, logo and banner images from SteamGridDB for "
                        "every game (Steam and Heroic too). Your own picks are kept.")
        fill.clicked.connect(lambda: self.request.emit(
            ['--fill-art'], "Fetching extra artwork from SteamGridDB", False, 'task'))
        key_row.addWidget(fill)
        self._add(form, "SteamGridDB API key", key_row)
        self.base = QLineEdit()
        self.base.setPlaceholderText(INSTALL_BASE_DEFAULT)
        self._add(form, "Install games to", self._with_browse(self.base))
        self.scan_dir = QLineEdit()
        self.scan_dir.setPlaceholderText(str(HOME / 'Downloads'))
        self._add(form, "Look for repacks in", self._with_browse(self.scan_dir))
        self.scan_more = QLineEdit()
        self.scan_more.setPlaceholderText("more folders, separated by ;  (e.g. ~/Games/Install)")
        self.scan_more.setToolTip("Nexus watches these too and tells you when a download there "
                                  "is ready to install")
        self._add(form, "Also look in", self.scan_more)
        self.runner = NoWheelCombo()
        for text, val in (("Automatic", 'auto'), ("umu-run + Proton", 'umu'),
                          ("Proton (direct)", 'proton'), ("System Wine", 'wine')):
            self.runner.addItem(text, val)
        self._add(form, "Runner", self.runner)
        self.proton = NoWheelCombo()
        self.proton.setToolTip("The Proton games are played with — in launch.sh, Steam and "
                               "Heroic. A game can override it on its Launch tab.")
        self._add(form, "Play with Proton", self.proton)
        self.account = QLineEdit()
        self.account.setPlaceholderText("auto-detect (last signed in)")
        self._add(form, "Steam account", self.account)
        self.steam = QCheckBox("Add new games to Steam")
        self.desktop = QCheckBox("Create app-menu entries for new games")
        self.heroic = QCheckBox("Add new games to Heroic")
        self.news = QCheckBox("Show the News page (experimental)")
        self.unattended = QCheckBox("Install repacks without the setup window")
        self.trash_repack = QCheckBox("Move a repack to the Trash once it's installed")
        iform = self._section(outer, "New games and pages")
        # A library preference (library.json), applied at once — not config.toml.
        self.show_owned = QCheckBox("Show games you own but haven't installed "
                                    "(Steam through Game-db, Epic and GOG through Heroic)")
        for c in (self.unattended, self.trash_repack, self.steam, self.desktop, self.heroic,
                  self.news, self.show_owned):
            self._add(iform, "", c)
        # Controls: what a double click and A / Enter do on a game. Library
        # preferences (library.json), applied at once like "show games you own".
        cform = self._section(outer, "Controls")
        self.ctl_double = NoWheelCombo()
        self.ctl_double.addItem("Play the game", 'play')
        self.ctl_double.addItem("Open its page", 'details')
        self._add(cform, "Double-click a game", self.ctl_double)
        self.ctl_a = NoWheelCombo()
        self.ctl_a.addItem("Open its page (press again to play)", 'details')
        self.ctl_a.addItem("Play the game", 'play')
        self._add(cform, "Enter / controller A", self.ctl_a)
        self.ctl_dedupe = NoWheelCombo()
        self.ctl_dedupe.addItem("Show it once (repack, then Steam, then Heroic)", True)
        self.ctl_dedupe.addItem("Show every copy", False)
        self._add(cform, "A game in several stores", self.ctl_dedupe)
        self.ctl_dedupe.activated.connect(lambda _i: self.controls_changed.emit(
            {'dedupe': self.ctl_dedupe.currentData()}))
        self.ctl_attract = NoWheelCombo()
        for m in ATTRACT_MINUTES:
            self.ctl_attract.addItem("Never" if not m else f"After {m} minutes idle", m)
        self._add(cform, "Theatre screensaver", self.ctl_attract)
        self.ctl_attract.activated.connect(lambda _i: self.controls_changed.emit(
            {'attract': self.ctl_attract.currentData()}))
        self._add(cform, "", label("Applied right away, in Theatre too. A game that's running, "
                                   "busy or not installed always opens its page.", "Muted",
                                   wrap=True))
        self.ctl_double.activated.connect(lambda _i: self.controls_changed.emit(
            {'double_click': self.ctl_double.currentData()}))
        self.ctl_a.activated.connect(lambda _i: self.controls_changed.emit(
            {'pad_a': self.ctl_a.currentData()}))
        # Game-db: wishlist deals, upcoming releases, backlog picks.
        gform = self._section(outer, "Game-db")
        self.gdb_enabled = QCheckBox("Show the Deals page (wishlist deals, releases, backlog)")
        self._add(gform, "", self.gdb_enabled)
        self.gdb_url = QLineEdit()
        self.gdb_url.setPlaceholderText(gamedb.DEFAULT_URL)
        self._add(gform, "Game-db address", self.gdb_url)
        tok_row = QHBoxLayout()
        self.gdb_token = QLineEdit()
        self.gdb_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.gdb_token.setPlaceholderText("gdb_…")
        tshow = QCheckBox("Show")
        tshow.toggled.connect(lambda on: self.gdb_token.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        get = QPushButton("Create a token…")
        get.setToolTip("Opens your Game-db account page in the browser")
        get.clicked.connect(self._open_gamedb_account)
        tok_row.addWidget(self.gdb_token, 1)
        tok_row.addWidget(tshow)
        tok_row.addWidget(get)
        self._add(gform, "Desktop app token", tok_row)
        self.gdb_note = label("", "Muted", wrap=True)
        self._add(gform, "", self.gdb_note)
        # Torrent client: the Downloads page and "Grab" on Deals.
        tform = self._section(outer, "Torrent client")
        self.tor_client = QComboBox()
        for text, key in (("Off", None), ("Transmission", 'transmission'),
                          ("qBittorrent", 'qbittorrent')):
            self.tor_client.addItem(text, key)
        self._add(tform, "Client", self.tor_client)
        self.tor_url = QLineEdit()
        self.tor_url.setPlaceholderText("http://127.0.0.1:9091")
        self._add(tform, "Remote address", self.tor_url)
        self.tor_user = QLineEdit()
        self.tor_user.setPlaceholderText("only if the client asks for one")
        self._add(tform, "Username", self.tor_user)
        prow = QHBoxLayout()
        self.tor_pass = QLineEdit()
        self.tor_pass.setEchoMode(QLineEdit.EchoMode.Password)
        pshow = QCheckBox("Show")
        pshow.toggled.connect(lambda on: self.tor_pass.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        prow.addWidget(self.tor_pass, 1)
        prow.addWidget(pshow)
        self._add(tform, "Password", prow)
        self.tor_dir = QLineEdit()
        self.tor_dir.setPlaceholderText("the client's own download folder")
        self._add(tform, "Download to", self._with_browse(self.tor_dir))
        self.tor_ratio = QLineEdit()
        self.tor_ratio.setPlaceholderText("keep seeding (the client's setting)")
        self._add(tform, "Stop seeding at ratio", self.tor_ratio)
        trow = QHBoxLayout()
        self.tor_test = QPushButton("Test connection")
        self.tor_test.setToolTip("Asks the client for its version (save first)")
        self.tor_test.clicked.connect(self._test_torrent)
        self.tor_result = label("", "Muted", wrap=True)
        trow.addWidget(self.tor_test)
        trow.addWidget(self.tor_result, 1)
        self._add(tform, "", trow)
        self._add(tform, "", label(
            "On this machine only. In Transmission turn on Edit → Preferences → Remote → "
            "Allow remote access; in qBittorrent, Tools → Options → Web UI.", "Muted",
            wrap=True))
        row = QHBoxLayout()
        self.dirty_note = label("Unsaved changes", "Muted")
        self.dirty_note.setVisible(False)
        row.addWidget(self.dirty_note)
        row.addStretch()
        revert = QPushButton("Revert")
        revert.clicked.connect(self.load)
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("Primary")
        self.btn_save.setToolTip("Save (Ctrl+S)")
        self.btn_save.clicked.connect(self._save)
        save_sc = QShortcut(QKeySequence("Ctrl+S"), self)
        save_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save_sc.activated.connect(self._save)
        row.addWidget(revert)
        row.addWidget(self.btn_save)
        w = QWidget()
        w.setMaximumWidth(720)
        w.setLayout(row)
        outer.addWidget(w)
        outer.addStretch()
        self.cfg = {}
        self._proton_current = None
        self.dirty = self._loading = False
        # Anything edited marks the page dirty: leaving keeps the edits (and
        # says so) instead of quietly reloading over them.
        for e in (self.key, self.base, self.scan_dir, self.scan_more, self.account,
                  self.gdb_url, self.gdb_token, self.tor_url, self.tor_user, self.tor_pass,
                  self.tor_dir, self.tor_ratio):
            e.textChanged.connect(self._mark_dirty)
        for c in (self.steam, self.desktop, self.heroic, self.news, self.unattended,
                  self.trash_repack, self.gdb_enabled):
            c.toggled.connect(self._mark_dirty)
        for cb in (self.runner, self.proton, self.tor_client):
            cb.activated.connect(self._mark_dirty)

    def set_controls(self, controls: dict):
        for box, key in ((self.ctl_double, 'double_click'), (self.ctl_a, 'pad_a'),
                         (self.ctl_dedupe, 'dedupe'), (self.ctl_attract, 'attract')):
            box.setCurrentIndex(max(0, box.findData(controls.get(key))))

    def _mark_dirty(self, *_):
        if not self._loading and not self.dirty:
            self.dirty = True
            self.dirty_note.setVisible(True)

    LABEL_W = 160

    def _section(self, outer, title):
        """Heading + card, with every form sharing one label column width."""
        head = label(title, "ShelfTitle")
        head.setMaximumWidth(720)
        outer.addWidget(head)
        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(720)
        form = QFormLayout(card)
        form.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        form.setHorizontalSpacing(SP['3'])
        form.setVerticalSpacing(SP['2_5'])
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(card)
        return form

    def _add(self, form, text, field):
        lab = label(text)
        lab.setFixedWidth(self.LABEL_W)
        form.addRow(lab, field)

    def _open_gamedb_account(self):
        try:
            base = gamedb.check_url(self.gdb_url.text() or gamedb.DEFAULT_URL)
        except gamedb.GameDbError as e:
            self.error.setText(str(e))
            self.error.setVisible(True)
            return
        QDesktopServices.openUrl(QUrl(base + '/account'))

    def _test_torrent(self):
        """Ask the saved client for its version (unsaved edits aren't used)."""
        self.tor_test.setEnabled(False)
        self.tor_result.setText("Asking…")

        def done(data, err):
            try:
                self.tor_test.setEnabled(True)
            except RuntimeError:
                return
            if data and data.get('ok'):
                where = f" — downloads to {data['download_dir']}" if data.get('download_dir') else ''
                self.tor_result.setText(f"✓ {data.get('client')} {data.get('version')}{where}")
            else:
                msg = ((data or {}).get('error') or {}).get('message') or err or "no answer"
                self.tor_result.setText(f"✗ {msg}")
        cli_json(['--torrent-test', '--json'], done, 15000)

    def _with_browse(self, edit):
        row = QHBoxLayout()
        row.addWidget(edit)
        b = QPushButton("…")
        b.setFixedWidth(36)
        b.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(
            self, "Choose folder", edit.text() or edit.placeholderText())) and edit.setText(d))
        row.addWidget(b)
        return row

    def load(self):
        self._loading = True
        try:
            self._load()
        finally:
            self._loading = False
        self.dirty = False
        self.dirty_note.setVisible(False)

    def _load(self):
        c = self.cfg = load_config()
        self.error.setText(f"config.toml can't be read, so it can't be saved from here until "
                           f"it's fixed: {c['error']}" if c.get('error') else "")
        self.error.setVisible(bool(c.get('error')))
        self.btn_save.setEnabled(not c.get('error'))
        self.key.setText(c.get('api_key') or "")
        self.base.setText(c.get('install_base') or "")
        self.scan_dir.setText(c.get('scan_dir') or "")
        self.scan_more.setText("; ".join(c.get('scan_dirs') or []))
        self.unattended.setChecked(c.get('unattended', True))
        self.trash_repack.setChecked(c.get('trash_repack', False))
        self.runner.setCurrentIndex(max(0, self.runner.findData(c.get('runner') or 'auto')))
        self.account.setText(c.get('steam_account') or "")
        self.steam.setChecked(c.get('steam', True))
        self.desktop.setChecked(c.get('desktop', True))
        self.heroic.setChecked(c.get('heroic', True))
        self.news.setChecked(c.get('news', True))
        self.gdb_enabled.setChecked(c.get('gamedb_cfg_enabled', False))
        self.gdb_url.setText(c.get('gamedb_url') or "")
        env_token = bool(os.environ.get('NEXUS_GAMEDB_TOKEN'))
        self.gdb_token.setText("" if env_token else (c.get('gamedb_token') or ""))
        self.gdb_token.setEnabled(not env_token)
        self.gdb_token.setPlaceholderText("set by NEXUS_GAMEDB_TOKEN" if env_token else "gdb_…")
        self.gdb_note.setText(
            "Read-only. Make a token on your Game-db account page (Desktop app tokens). "
            "It's kept in config.toml (readable only by you); data is cached 45 minutes.")
        self.tor_client.setCurrentIndex(max(0, self.tor_client.findData(c.get('torrent_client'))))
        self.tor_url.setText(c.get('torrent_url') or "")
        self.tor_user.setText(c.get('torrent_username') or "")
        self.tor_pass.setText(c.get('torrent_password') or "")
        self.tor_dir.setText(c.get('torrent_download_dir') or "")
        r = c.get('torrent_seed_ratio')
        self.tor_ratio.setText("" if r is None else f"{r:g}")
        self.tor_result.setText("")
        self._proton_current = c.get('proton') or c.get('heroic_proton')
        self.proton.clear()
        self.proton.addItem("Newest installed", None)
        if self._proton_current:
            self.proton.addItem(self._proton_current, self._proton_current)
            self.proton.setCurrentIndex(1)

        def done(data, err):
            try:
                if data is None:
                    # Say so, rather than offering only "Newest installed".
                    self.proton.addItem(f"Couldn't list installed Protons ({err or 'error'})")
                    self.proton.model().item(self.proton.count() - 1).setEnabled(False)
                    return
                for p in data.get('protons', []):
                    if self.proton.findData(p['name']) < 0:
                        self.proton.addItem(p['name'], p['name'])
            except RuntimeError:
                pass
        cli_json(['--list-protons', '--json'], done)

    def _save(self):
        c = self.cfg
        u = {}

        def put(section, key, new, old):
            if new != old:
                u[(section, key)] = new
        put('steamgriddb', 'api_key', self.key.text().strip() or None, c.get('api_key'))
        put('integrations', 'steam', self.steam.isChecked(), c.get('steam', True))
        put('integrations', 'desktop', self.desktop.isChecked(), c.get('desktop', True))
        put('integrations', 'heroic', self.heroic.isChecked(), c.get('heroic', True))
        put('news', 'enabled', self.news.isChecked(), c.get('news', True))
        gurl = self.gdb_url.text().strip() or None
        if gurl:
            try:
                gurl = gamedb.check_url(gurl)
            except gamedb.GameDbError as e:
                self.error.setText(f"Settings not saved: {e}")
                self.error.setVisible(True)
                return
        gtok = self.gdb_token.text().strip() or None
        if gtok and not gamedb.TOKEN_RE.match(gtok):
            self.error.setText("Settings not saved: a Game-db token looks like gdb_ followed "
                               "by 43 letters, digits, - or _")
            self.error.setVisible(True)
            return
        gamedb_changed = (gurl != c.get('gamedb_url') or
                          (self.gdb_token.isEnabled() and gtok != c.get('gamedb_token')))
        put('gamedb', 'enabled', self.gdb_enabled.isChecked(), c.get('gamedb_cfg_enabled'))
        put('gamedb', 'url', gurl, c.get('gamedb_url'))
        if self.gdb_token.isEnabled():
            put('gamedb', 'token', gtok, c.get('gamedb_token'))
        turl = self.tor_url.text().strip().rstrip('/') or None
        if turl and not re.match(r'^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$', turl):
            self.error.setText("Settings not saved: the torrent client must be on this machine "
                               "(http://127.0.0.1:<port>)")
            self.error.setVisible(True)
            return
        ratio = self.tor_ratio.text().strip().replace(',', '.') or None
        if ratio is not None:
            try:
                if float(ratio) < 0:
                    raise ValueError
            except ValueError:
                self.error.setText("Settings not saved: the seeding ratio is a number, like 1.5")
                self.error.setVisible(True)
                return
        put('torrent', 'client', self.tor_client.currentData(), c.get('torrent_client'))
        put('torrent', 'url', turl, c.get('torrent_url'))
        put('torrent', 'username', self.tor_user.text().strip() or None,
            c.get('torrent_username'))
        put('torrent', 'password', self.tor_pass.text() or None, c.get('torrent_password'))
        put('torrent', 'download_dir', self.tor_dir.text().strip() or None,
            c.get('torrent_download_dir'))
        old_ratio = c.get('torrent_seed_ratio')
        if (float(ratio) if ratio is not None else None) != old_ratio:
            u[('torrent', 'seed_ratio')] = ratio
        put('integrations', 'steam_account', self.account.text().strip() or None,
            c.get('steam_account'))
        put('install', 'base', self.base.text().strip() or None, c.get('install_base'))
        put('install', 'scan_dir', self.scan_dir.text().strip() or None, c.get('scan_dir'))
        more = [d.strip() for d in self.scan_more.text().split(';') if d.strip()]
        put('install', 'scan_dirs', more or None, c.get('scan_dirs') or None)
        put('install', 'unattended', self.unattended.isChecked(), c.get('unattended', True))
        put('install', 'trash_repack', self.trash_repack.isChecked(),
            c.get('trash_repack', False))
        put('runner', 'backend', self.runner.currentData(), c.get('runner') or 'auto')
        proton = self.proton.currentData()
        changed = proton != self._proton_current
        if changed:
            u[('runner', 'proton')] = proton
            u[('heroic', 'proton')] = None     # legacy key; [runner] proton replaces it
        if u:
            try:
                save_config(u)
            except (ConfigError, OSError) as e:
                self.error.setText(f"Settings not saved: {e}")
                self.error.setVisible(True)
                return
        if gamedb_changed:
            gamedb.clear_cache()               # old account's data off the disk too
        self.gamedb_changed = gamedb_changed
        self.load()
        self.saved.emit(changed)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .config import ConfigError, load_config, save_config  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .widgets import (clear_layout, label, NoWheelCombo, SegmentedControl, set_glyph_icon)  # noqa: E402
