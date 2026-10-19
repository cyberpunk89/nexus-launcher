"""The game menu: every per-game action, the action sheet, trailers."""

import html
from typing import NamedTuple

from PyQt6.QtCore import pyqtSignal, QEvent, Qt, QUrl
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLineEdit, QMenu, QPushButton,
    QScrollArea, QVBoxLayout, QWidget)

from .common import COLLECTION_NAMES, MAX_COLLECTION_NAME
from .theme import G, P, SP


class GameAction(NamedTuple):
    """One per-game action, rendered as a QMenu item (mouse/keyboard) or an
    ActionSheet row (controller, Theatre). `key` is what on_game_action runs."""
    key: str
    label: str
    section: str = ''
    checked: bool | None = None        # a toggle: shown ticked / unticked
    enabled: bool = True
    danger: bool = False
    desk: bool = False                 # opens on the desktop: leaves Theatre
    sub: str = ''                      # a submenu in the QMenu (Wine tools)


# The sheet's row icons (the QMenu stays text-only, like any desktop menu).
ACTION_GLYPHS = {'launch': 'play', 'open-folder': 'folder', 'settings': 'settings',
                 'refetch-art': 'image', 'restore-saves': 'save', 'check-integrity': 'shield',
                 'tool:kill': 'stop', 'view-log': 'log', 'steam-page': 'steam',
                 'steam-props': 'settings', 'steam-verify': 'shield', 'protongen': 'tune',
                 'pg-default': 'tune', 'discard-incomplete': 'trash',
                 'install-update': 'download', 'repack-page': 'update',
                 'pause-toggle': 'timer', 'benchmark': 'system',
                 'rename-local': 'pencil', 'rename-reset': 'reset',
                 'artwork': 'image', 'hide': 'eye_off', 'uninstall': 'trash',
                 'steam-uninstall': 'trash', 'col-new': 'plus'}


def action_glyph(a: 'GameAction') -> str:
    if a.key == 'favorite':
        return G['star' if a.checked else 'star_outline']
    if a.key == 'launch' and a.label.startswith(("Install", "Waiting", "Download", "Resume")):
        return G['download']
    if a.checked is not None and a.key != 'hide':  # In Steam / In Heroic
        return G['check_circle' if a.checked else 'plus']
    if a.key.startswith('tool:') and a.key != 'tool:kill':
        return G['wine']
    return G[ACTION_GLYPHS.get(a.key, 'more')]


# Desktop-only things nobody wants from the couch: file managers, Wine's
# own dialogs, another app. Not offered in Theatre at all.
COUCH_SKIP = ('open-folder', 'protongen', 'tool:')


# Actions that change a game's files or registration: not while a job is
# already doing that (busy), and the ones that rewrite what a running game is
# using not while it plays.
NOT_WHILE_BUSY = ('install-update', 'discard-incomplete', 'add-steam', 'remove-steam',
                  'add-heroic', 'remove-heroic', 'pg-default', 'restore-saves',
                  'check-integrity', 'tool:winecfg', 'tool:regedit', 'tool:winetricks',
                  'uninstall')
NOT_WHILE_PLAYING = ('install-update', 'restore-saves', 'uninstall', 'steam-verify',
                     'steam-uninstall', 'tool:winetricks', 'benchmark')


def _gate_actions(game: dict, out: list['GameAction']) -> list['GameAction']:
    playing = game.get('playing') or game.get('launching')
    busy = game.get('busy')
    res = []
    for a in out:
        why = ("not while it's running" if playing and a.key in NOT_WHILE_PLAYING else
               "busy — a job is running" if busy and a.key in NOT_WHILE_BUSY else '')
        res.append(a._replace(enabled=False, label=f"{a.label}  ·  {why}") if why else a)
    return res


def game_actions(game: dict, theatre: bool = False) -> list[GameAction]:
    """Every action for one game. What's offered depends on who owns it:
    Nexus installs get everything; Steam/Heroic games get launcher shortcuts
    (their launcher manages files and updates)."""
    src = game.get('source', 'nexus')
    A = GameAction
    if game.get('incomplete'):          # a cancelled or failed install: finish or drop it
        out = [A('launch', play_label(game), 'play', enabled=play_enabled(game)),
               A('open-folder', "Open game folder", 'play'),
               A('discard-incomplete', "Discard unfinished install", 'danger', danger=True)]
        return _gate_actions(game, [a for a in out
                                    if not (theatre and a.key.startswith(COUCH_SKIP))])
    out = [A('launch', play_label(game), 'play', enabled=play_enabled(game)),
           A('open-folder', "Open game folder", 'play')]
    if src == 'nexus' and game.get('repack_update'):
        out.insert(1, A('install-update', "Install update", 'play'))
    elif src == 'nexus' and (game.get('repack_online') or {}).get('url'):
        out.insert(1, A('repack-page', "See the update on FitGirl", 'play'))
    if src == 'nexus':
        out += [A('settings', "Game settings…", 'play', desk=True),
                A('remove-steam' if game.get('steam') else 'add-steam', "In Steam",
                  'stores', checked=bool(game.get('steam'))),
                A('remove-heroic' if game.get('heroic') else 'add-heroic', "In Heroic",
                  'stores', checked=bool(game.get('heroic'))),
                A('pg-default', "Use protongen defaults", 'tools'),
                A('refetch-art', "Re-fetch artwork", 'tools'),
                A('restore-saves', "Restore save backup", 'tools'),
                A('check-integrity', "Check file integrity", 'tools'),
                A('benchmark', "Benchmark run (MangoHud log)", 'tools', desk=True),
                A('tool:winecfg', "winecfg — Wine settings", 'tools', sub="Wine tools"),
                A('tool:regedit', "regedit — registry editor", 'tools', sub="Wine tools"),
                A('tool:winetricks', "winetricks", 'tools', sub="Wine tools"),
                A('tool:kill', "Stop everything in the prefix", 'kill', sub="Wine tools"),
                A('view-log', "View log", 'tools', desk=True)]
    if src == 'nexus' and game.get('playing'):
        out.insert(1, A('pause-toggle', "Pause or resume", 'play'))
    if 'sunshine' in game and is_installed(game):       # streaming, next to the stores
        at = next((i + 1 for i, a in enumerate(out) if a.section == 'stores'), len(out))
        out.insert(at + (1 if src == 'nexus' else 0),
                   A('remove-sunshine' if game['sunshine'] else 'add-sunshine', "In Sunshine",
                     'stores', checked=bool(game['sunshine'])))
    if src == 'steam':
        out += [A('steam-page', "Open in Steam", 'tools'),
                A('steam-props', "Steam properties…", 'tools'),
                A('steam-verify', "Verify game files", 'tools'),
                A('protongen', "Tune in protongen", 'tools')]
    if src != 'nexus':                    # a repack is renamed for real, in Game settings
        out.append(A('rename-local', "Rename in library…", 'tools'))
        if game.get('store_name'):
            out.append(A('rename-reset', f"Use the store's name ({game['store_name']})", 'tools'))
    out += [A('artwork', "Change artwork…", 'tools', desk=True),
            A('favorite', "Favourite", 'prefs', checked=bool(game.get('favorite'))),
            A('hide', "Hidden from library", 'prefs', checked=bool(game.get('hidden')))]
    mine = game.get('collections') or ()
    out += [A(f'col:{n}', n, 'prefs', checked=n in mine, sub="Collections")
            for n in COLLECTION_NAMES]
    out.append(A('col-new', "New collection…", 'prefs', sub="Collections"))
    if src == 'nexus':
        out.append(A('uninstall', "Uninstall…", 'danger', danger=True, desk=True))
    elif src == 'steam':
        out.append(A('steam-uninstall', "Uninstall in Steam…", 'danger', danger=True))
    if not is_installed(game):           # nothing on disk yet: install, or tidy up
        out = [a for a in out if a.key in ('launch', 'steam-page', 'artwork', 'favorite',
                                            'hide', 'col-new') or a.key.startswith('col:')]
    if theatre:
        out = [a for a in out if not a.key.startswith(COUCH_SKIP)]
    return _gate_actions(game, out)


def build_game_menu(parent: QWidget, game: dict, act) -> QMenu:
    """`act(name, game)` runs an action. The mouse/keyboard rendering of
    game_actions(): the tile's ⋮ button, right-click, the list view and the
    details panel (a controller gets ActionSheet instead)."""
    m = QMenu(parent)
    subs: dict[str, QMenu] = {}
    last = {}                                  # menu -> last section seen in it
    for a in game_actions(game):
        target = m
        if a.sub:
            if a.sub not in subs:
                subs[a.sub] = m.addMenu(a.sub)
            target = subs[a.sub]
        prev = last.get(id(target))
        if prev is not None and prev != a.section:
            target.addSeparator()
        last[id(target)] = a.section
        qa = target.addAction(a.label)
        qa.setEnabled(a.enabled)
        if a.checked is not None:
            qa.setCheckable(True)
            qa.setChecked(a.checked)
        if a.key == 'launch':
            f = qa.font()
            f.setWeight(QFont.Weight.DemiBold)
            qa.setFont(f)
        qa.triggered.connect(lambda _=False, k=a.key: act(k, game))
    return m


class ActionSheet(QFrame):
    """The game menu for a controller: a panel of big rows sliding over a
    dimmed page. D-pad moves, A runs, B or X closes. Used wherever a pad is
    driving, and always in Theatre — never the desktop right-click menu."""
    chosen = pyqtSignal(str, dict)
    closed = pyqtSignal()

    def __init__(self, parent: QWidget, scale: float = 1.0):
        super().__init__(parent)
        self.setObjectName("SheetScrim")
        self.scale = scale
        self.game: dict = {}
        self._back_to = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch()
        self.panel = QFrame()
        self.panel.setObjectName("Sheet")
        self.panel.setFixedWidth(int(400 * scale))
        outer.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        m = int(SP['5'] * scale)
        lay.setContentsMargins(m, m, m, m)
        lay.setSpacing(int(SP['1'] * scale))
        self.title = label("", "SheetTitle", wrap=True)
        lay.addWidget(self.title)
        self.sub = label("", "Muted")
        lay.addWidget(self.sub)
        lay.addSpacing(int(SP['3'] * scale))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.rows = QVBoxLayout(host)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(int(SP['0_5'] * scale))
        self.scroll.setWidget(host)
        lay.addWidget(self.scroll, 1)
        parent.installEventFilter(self)          # follow the parent's size
        self.hide()

    def open(self, game: dict, actions: list[GameAction], theatre: bool = False):
        self.game = game
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        clear_layout(self.rows)
        self.title.setText(game.get('display_name', ''))
        self.sub.setText(SOURCE_LABEL.get(game.get('source', 'nexus'), ''))
        last, last_sub, first = None, '', None
        px = int(18 * self.scale)
        for a in actions:
            if a.sub and a.sub != last_sub:
                self.rows.addSpacing(int(SP['2'] * self.scale))
                self.rows.addWidget(label(a.sub.upper(), "SheetSection"))
            elif last is not None and a.section != last and not (a.sub and last_sub):
                self.rows.addSpacing(int(SP['2'] * self.scale))
            last, last_sub = a.section, a.sub
            text = a.label
            if a.desk and theatre:
                text += "  ·  leaves Theatre"
            b = QPushButton(text)
            b.setObjectName("SheetRow")
            b.setProperty('danger', a.danger)
            set_glyph_icon(b, action_glyph(a), px,
                           'danger' if a.danger else 'accent' if a.checked else 'subtext',
                           hover_role=None)
            b.setMinimumHeight(int(44 * self.scale))
            b.setEnabled(a.enabled)
            b.clicked.connect(lambda _=False, k=a.key: self._run(k))
            self.rows.addWidget(b)
            if first is None and a.enabled:
                first = b
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        if first is not None:
            first.setFocus(Qt.FocusReason.TabFocusReason)

    def ask(self, title: str, text: str, yes: str, on_yes, danger: bool = True):
        """The sheet as a yes/no question. Cancel has the focus: A pressed
        out of habit never confirms anything."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self.game = {}
        clear_layout(self.rows)
        self.title.setText(title)
        self.sub.setText("")
        self.rows.addWidget(label(text, "Muted", wrap=True))
        self.rows.addSpacing(int(SP['3'] * self.scale))
        px = int(18 * self.scale)
        ok = QPushButton(yes)
        ok.setObjectName("SheetRow")
        ok.setProperty('danger', danger)
        set_glyph_icon(ok, G['check'], px, 'danger' if danger else 'accent', hover_role=None)
        ok.setMinimumHeight(int(44 * self.scale))
        ok.clicked.connect(lambda: (self.close_sheet(), on_yes()))
        no = QPushButton("Cancel")
        no.setObjectName("SheetRow")
        set_glyph_icon(no, G['close'], px, 'subtext', hover_role=None)
        no.setMinimumHeight(int(44 * self.scale))
        no.clicked.connect(lambda: self.close_sheet())
        self.rows.addWidget(ok)
        self.rows.addWidget(no)
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        no.setFocus(Qt.FocusReason.TabFocusReason)

    def choose(self, title: str, items: list, sub: str = ''):
        """The sheet as a plain list of choices — anything that isn't a game
        action (a deal's links, a game's collections). `items` are
        (text, glyph, fn) or (text, glyph, fn, checked); fn runs once the
        sheet has closed."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self.game = {}
        clear_layout(self.rows)
        self.title.setText(title)
        self.sub.setText(sub)
        px = int(18 * self.scale)
        first = None
        for text, glyph, fn, *rest in items:
            b = QPushButton(text)
            b.setObjectName("SheetRow")
            set_glyph_icon(b, glyph, px, 'accent' if rest and rest[0] else 'subtext',
                           hover_role=None)
            b.setMinimumHeight(int(44 * self.scale))
            b.clicked.connect(lambda _=False, fn=fn: (self.close_sheet(), fn()))
            self.rows.addWidget(b)
            first = first or b
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        if first is not None:
            first.setFocus(Qt.FocusReason.TabFocusReason)

    def prompt(self, title: str, initial: str, ok: str, on_ok, text: str = '',
               max_len: int = MAX_COLLECTION_NAME):
        """The sheet asking for a name (keyboard): Return or `ok` saves,
        Cancel / Escape drops it. An empty name saves nothing."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self.game = {}
        clear_layout(self.rows)
        self.title.setText(title)
        self.sub.setText("")
        if text:
            self.rows.addWidget(label(text, "Muted", wrap=True))
        ed = QLineEdit(initial)
        ed.setMaxLength(max_len)
        ed.setMinimumHeight(int(40 * self.scale))
        self.rows.addWidget(ed)
        self.rows.addSpacing(int(SP['2'] * self.scale))
        px = int(18 * self.scale)
        save = QPushButton(ok)
        save.setObjectName("SheetRow")
        set_glyph_icon(save, G['check'], px, 'accent', hover_role=None)
        save.setMinimumHeight(int(44 * self.scale))
        no = QPushButton("Cancel")
        no.setObjectName("SheetRow")
        set_glyph_icon(no, G['close'], px, 'subtext', hover_role=None)
        no.setMinimumHeight(int(44 * self.scale))

        def done():
            name = ed.text().strip()
            if name:
                self.close_sheet()
                on_ok(name)
        save.clicked.connect(done)
        ed.returnPressed.connect(done)
        no.clicked.connect(lambda: self.close_sheet())
        self.rows.addWidget(save)
        self.rows.addWidget(no)
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        ed.selectAll()
        ed.setFocus(Qt.FocusReason.TabFocusReason)

    def default_focus(self):
        return next((w for w in self.findChildren(QPushButton)
                     if w.isEnabled() and not w.isHidden()), None)

    def _run(self, key):
        game = self.game
        self.close_sheet(restore=key not in ('artwork', 'settings', 'view-log', 'uninstall'))
        self.chosen.emit(key, game)

    def close_sheet(self, restore: bool = True):
        if self.isHidden():
            return
        self.hide()
        back, self._back_to = self._back_to, None
        try:
            if restore and back is not None and back.isVisible():
                focus_and_reveal(back)
        except RuntimeError:                     # rebuilt while the sheet was open
            pass
        self.closed.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Menu, Qt.Key.Key_Back):
            self.close_sheet()
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        if not self.panel.geometry().contains(ev.position().toPoint()):
            self.close_sheet()
        super().mousePressEvent(ev)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, 115))
        pt.end()


class TrailerOverlay(QFrame):
    """A Steam trailer playing over the window (an overlay, not a dialog:
    niri floats dialogs). Esc / B / the close button end it. QtMultimedia
    is imported on first use; the FFmpeg backend plays Steam's HLS streams."""
    closed = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("SheetScrim")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.player = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['6'], SP['5'], SP['6'], SP['6'])
        lay.setSpacing(SP['2'])
        top = QHBoxLayout()
        self.title = label("", "SheetTitle")
        top.addWidget(self.title, 1)
        self.btn_close = icon_button(G['close'], "Close (Esc)", 36)
        self.btn_close.clicked.connect(self.stop)
        top.addWidget(self.btn_close)
        lay.addLayout(top)
        self.host = QVBoxLayout()
        lay.addLayout(self.host, 1)
        self.status = label("", "Muted")
        lay.addWidget(self.status)
        parent.installEventFilter(self)
        self.hide()

    def _ensure(self) -> bool:
        if self.player is not None:
            return True
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PyQt6.QtMultimediaWidgets import QVideoWidget
        except ImportError:
            return False
        self.video = QVideoWidget()
        self.host.addWidget(self.video, 1)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.6)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(self._error)
        self.player.mediaStatusChanged.connect(self._media_status)
        return True

    def play(self, trailer: dict, game_name: str = ''):
        url = str(trailer.get('url') or '')
        if not url.startswith('https://'):
            return
        self.setGeometry(self.parentWidget().rect())
        self.title.setText(" · ".join(x for x in (game_name, trailer.get('name')) if x))
        self.show()
        self.raise_()
        self.setFocus()
        if not self._ensure():
            self.status.setText("Video playback needs Qt Multimedia (qt6-multimedia-ffmpeg).")
            return
        self.status.setText("Loading…")
        self.player.setSource(QUrl(url))
        self.player.play()

    def _media_status(self, st):
        from PyQt6.QtMultimedia import QMediaPlayer
        if st in (QMediaPlayer.MediaStatus.BufferedMedia, QMediaPlayer.MediaStatus.LoadedMedia):
            self.status.setText("")
        elif st == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop()

    def _error(self, _err, text=''):
        self.status.setText(f"Couldn't play the trailer: {text or 'unknown error'}")

    def stop(self):
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Back, Qt.Key.Key_Backspace):
            self.stop()
        elif ev.key() == Qt.Key.Key_Space and self.player is not None:
            from PyQt6.QtMultimedia import QMediaPlayer
            (self.player.pause if self.player.playbackState()
             == QMediaPlayer.PlaybackState.PlayingState else self.player.play)()
        else:
            super().keyPressEvent(ev)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, 225))
        pt.end()


def about_html(store: dict | None, reviews: dict | None, hltb: dict | None) -> str:
    """Rich text for the details panel's About section; everything escaped,
    links only to the sites they came from."""
    st, rv, out = store or {}, reviews or {}, []
    if st.get('blurb'):
        out.append(f"<p>{html.escape(st['blurb'])}</p>")
    facts = [x for x in (', '.join(st.get('genres') or []), st.get('release'),
                         ', '.join(st.get('developers') or [])) if x]
    if facts:
        out.append(f"<p style='color:{P['subtext']}'>{html.escape(' · '.join(facts))}</p>")
    scores = []
    if rv.get('desc'):
        good = (rv.get('percent') or 0) >= 70
        scores.append(f"<span style='color:{P['success'] if good else P['warning']}'>"
                      f"{html.escape(rv['desc'])}</span>"
                      + (f" ({rv['percent']}% of {rv['total']:,})" if rv.get('percent')
                         is not None else ''))
    mc = st.get('metacritic') or {}
    if mc.get('score'):
        scores.append(f"Metacritic {int(mc['score'])}")
    if scores:
        out.append("<p><b>Reviews</b>: " + " · ".join(scores) + "</p>")
    if hltb and hltb.get('main'):
        parts = [f"main {hltb['main']:g} h"]
        if hltb.get('plus'):
            parts.append(f"+extras {hltb['plus']:g} h")
        if hltb.get('complete'):
            parts.append(f"100% {hltb['complete']:g} h")
        link = (f" <a href='{html.escape(hltb['url'])}' style='color:{P['accent']}'>HLTB</a>"
                if str(hltb.get('url', '')).startswith('https://howlongtobeat.com/') else '')
        out.append("<p><b>How long to beat</b>: " + " · ".join(parts) + link + "</p>")
    if str(st.get('url', '')).startswith('https://store.steampowered.com/'):
        out.append(f"<p><a href='{html.escape(st['url'])}' style='color:{P['accent']}'>"
                   "Steam store page</a></p>")
    return "".join(out)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .gameinfo import is_installed, play_enabled, play_label, SOURCE_LABEL  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .widgets import clear_layout, icon_button, label, set_glyph_icon  # noqa: E402
