"""The command palette (Ctrl+K): one search box for games, pages, Settings
sections and commands, and — on a game — everything its menu offers. Type
a few letters ("hdes", "move noc", "appear"), ↑/↓ to choose, Enter to run;
Tab (or →) on a game lists its actions. Desktop only: Theatre has its own
search. An overlay over the window, never a dialog (niri floats those)."""

from dataclasses import dataclass, field
from typing import Callable

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter
from PyQt6.QtWidgets import (QFrame, QLineEdit, QListWidget, QListWidgetItem,
    QVBoxLayout, QWidget)

from .theme import G, P, R, SP

RESULTS = 12                     # rows shown at once
RECENT_MAX = 8


def _norm(s: str) -> str:
    return ''.join(c for c in (s or '').casefold()
                   if c.isalnum() or c.isspace()).strip()


def fuzzy_score(query: str, text: str) -> float | None:
    """How well `query` matches `text`, or None when it doesn't: every
    letter of the query in order. Contiguous runs, word starts and an early
    first hit score higher; a plain substring beats a scattered match."""
    q, t = _norm(query).replace(' ', ''), _norm(text)
    if not q:
        return 0.0
    if not t:
        return None
    flat = t.replace(' ', '')
    if flat.startswith(q):
        return 100.0 + len(q) / len(flat)
    starts = {0} | {i + 1 for i, c in enumerate(t) if c == ' '}
    score, ti, prev, first = 0.0, 0, -2, None
    for ch in q:
        ti = t.find(ch, ti)
        while ti != -1 and t[ti] == ' ':
            ti = t.find(ch, ti + 1)
        if ti == -1:
            return None
        if first is None:
            first = ti
            # A match has to start at a word, or be there as a whole: "ho"
            # finds Hollow Knight and Home, not "debug log".
            if ti not in starts and q not in flat:
                return None
        score += 3.0 if ti in starts else 0.0
        score += 2.0 if ti == prev + 1 else 0.0
        prev, ti = ti, ti + 1
    if q in flat:
        score += 20.0
    return score - first * 0.1 + len(q) / len(flat)


@dataclass
class Entry:
    kind: str                     # game | page | setting | command | action
    title: str
    hint: str = ''
    glyph: str = ''
    run: Callable | None = None
    game: dict | None = None
    keywords: str = ''
    enabled: bool = True
    key: str = field(default='')  # remembered for "recent"

    def text(self) -> str:
        return f"{self.title} {self.keywords}"


def rank(entries: list[Entry], query: str, recent: list[str] = (),
         limit: int = RESULTS) -> list[Entry]:
    """Best matches first; with no query, recent picks then the list as given."""
    if not query.strip():
        pos = {k: i for i, k in enumerate(recent)}
        return sorted(entries, key=lambda e: pos.get(e.key, len(pos)))[:limit]
    words = query.split()
    scored = []
    for i, e in enumerate(entries):
        # "move noc": a command word and a game word both have to match.
        s = fuzzy_score(query, e.text())
        if s is None and len(words) > 1:
            parts = [fuzzy_score(w, e.text()) for w in words]
            s = None if None in parts else sum(parts) - 5
        if s is None:
            continue
        s += 4.0 if e.key in recent else 0.0
        s += {'game': 1.5, 'command': 1.0, 'page': 1.0}.get(e.kind, 0.0)
        s -= 0 if e.enabled else 50
        scored.append((-s, i, e))
    scored.sort(key=lambda x: x[:2])
    return [e for *_x, e in scored[:limit]]


class CommandPalette(QFrame):
    """The overlay. `source()` gives the entries (built fresh each time it
    opens, so nothing goes stale); `actions(game)` a game's entries."""
    closed = pyqtSignal()
    picked = pyqtSignal(str)              # the entry's key, for "recent"

    def __init__(self, parent: QWidget, source, actions):
        super().__init__(parent)
        self.setObjectName("PaletteScrim")
        self.source, self.actions = source, actions
        self.recent: list[str] = []
        self._all: list[Entry] = []
        self._game: dict | None = None
        self.panel = QFrame(self)
        self.panel.setObjectName("Palette")
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(SP['3'], SP['3'], SP['3'], SP['3'])
        lay.setSpacing(SP['2'])
        self.edit = QLineEdit()
        self.edit.setObjectName("PaletteSearch")
        self.edit.textChanged.connect(self._refill)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)
        self.list = QListWidget()
        self.list.setObjectName("PaletteList")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.itemClicked.connect(lambda it: self._run(it))
        lay.addWidget(self.list)
        parent.installEventFilter(self)
        self.hide()

    def _style(self):
        self.panel.setStyleSheet(f"""
            #Palette {{ background: {P['surface_solid']}; border: 1px solid {P['border']};
                        border-radius: {R['card']}px; }}
            QLineEdit#PaletteSearch {{ background: {P['surface_1']}; color: {P['text']};
                border: 1px solid {P['accent']}; border-radius: {R['control']}px;
                padding: {SP['2']}px {SP['3']}px; font-size: 16px; }}
            QListWidget#PaletteList {{ background: transparent; border: none; color: {P['text']};
                outline: 0; }}
            QListWidget#PaletteList::item {{ padding: {SP['1_5']}px {SP['2']}px;
                border-radius: {R['control']}px; }}
            QListWidget#PaletteList::item:selected {{ background: {P['accent_16']};
                color: {P['text']}; }}
            QListWidget#PaletteList::item:disabled {{ color: {P['muted']}; }}""")

    # ---- open / close ----
    def open(self, game: dict | None = None):
        self._style()
        self._game = None
        self.setGeometry(self.parentWidget().rect())
        self._place()
        if game is not None:
            self._open_game(game)
        else:
            self._all = self.source()
            self.edit.setPlaceholderText("Search games, pages, settings and commands…")
            self.edit.clear()
            self._refill()
        self.raise_()
        self.show()
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _open_game(self, game: dict):
        self._game = game
        self._all = self.actions(game)
        self.edit.setPlaceholderText(f"{game.get('display_name', game['slug'])} — what to do?"
                                     "   (Backspace: back)")
        self.edit.blockSignals(True)
        self.edit.clear()
        self.edit.blockSignals(False)
        self._refill()

    def escape(self):
        """Esc: from a game's actions back to everything, else close."""
        if self._game is not None:
            self.open()
        else:
            self.close_palette()

    def close_palette(self):
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def _place(self):
        w = min(620, max(360, self.width() - 2 * SP['8']))
        self.panel.setFixedWidth(w)
        rows = RESULTS * 34 + 60
        self.panel.setGeometry((self.width() - w) // 2, max(SP['8'], self.height() // 8), w,
                               min(rows, self.height() - 2 * SP['8']))

    # ---- results ----
    def _refill(self, *_):
        q = self.edit.text()
        self.list.clear()
        limit = len(self._all) if self._game else RESULTS      # a game's actions: all of them
        for e in rank(self._all, q, [] if self._game else self.recent, limit):
            it = QListWidgetItem(e.title + (f"   ·   {e.hint}" if e.hint else ''))
            if e.glyph:
                it.setIcon(QIcon(glyph_pixmap(e.glyph, P['accent'] if e.kind == 'game'
                                              else P['subtext'], 16)))
            it.setData(Qt.ItemDataRole.UserRole, e)
            if not e.enabled:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(it)
        for i in range(self.list.count()):
            if self.list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(i)
                break

    def current(self) -> Entry | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _move(self, step: int):
        n = self.list.count()
        if not n:
            return
        i = self.list.currentRow()
        for _ in range(n):
            i = (i + step) % n
            if self.list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(i)
                return

    def _run(self, it=None):
        e = it.data(Qt.ItemDataRole.UserRole) if it is not None else self.current()
        if e is None or not e.enabled:
            return
        if e.kind == 'game' and e.run is None:
            self._open_game(e.game)
            return
        self.hide()
        if e.key and not self._game:
            self.recent = [e.key] + [k for k in self.recent if k != e.key][:RECENT_MAX - 1]
            self.picked.emit(e.key)
        self.closed.emit()
        if e.run is not None:
            e.run()

    # ---- keys and size ----
    def eventFilter(self, obj, ev):
        if obj is self.parentWidget() and ev.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(obj.rect())
            self._place()
        elif obj is self.edit and ev.type() == QEvent.Type.KeyPress:
            k = ev.key()
            if k == Qt.Key.Key_Escape:
                self.escape()
                return True
            if k in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move(1 if k == Qt.Key.Key_Down else -1)
                return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run()
                return True
            e = self.current()
            if k in (Qt.Key.Key_Tab, Qt.Key.Key_Right) and e is not None and e.game \
                    and self._game is None and (k == Qt.Key.Key_Tab or
                                                self.edit.cursorPosition() == len(self.edit.text())):
                self._open_game(e.game)
                return True
            if k == Qt.Key.Key_Backspace and self._game is not None and not self.edit.text():
                self.open()
                return True
        return False

    def mousePressEvent(self, ev):
        if not self.panel.geometry().contains(ev.position().toPoint()):
            self.close_palette()
        super().mousePressEvent(ev)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, 115))
        pt.end()


def page_entries(win) -> list[Entry]:
    """Pages, System sections and the commands the window has."""
    E = Entry
    out = []
    names = {win.HOME: ("Home", G['home']), win.LIBRARY: ("Library", G['library']),
             win.DEALS: ("Deals", G['deals']), win.NEWS: ("News", G['news']),
             win.PROTON: ("Proton builds", G['proton']),
             win.DOWNLOADS: ("Downloads", G['download']),
             win.SETTINGS: ("Settings", G['settings'])}
    for key, (title, glyph) in names.items():
        b = win.nav.get(key) if hasattr(win, 'nav') else None
        if b is not None and b.isHidden():
            continue
        out.append(E('page', title, "page", glyph, lambda k=key: win.go(k), key=f"page:{key}"))
    for key, title, glyph in (('activity', "Activity", G['activity']),
                              ('storage', "Storage · Clean up", G['storage']),
                              ('health', "Health · Doctor", G['health']),
                              ('play', "Play history", G['history'])):
        out.append(E('page', title, "System", glyph, lambda k=key: win.go(k) if k != 'play'
                     else (win.go(win.SYSTEM), win.system.show_section('play')),
                     key=f"page:{key}", keywords="system"))
    for key, title, glyph, fn, kw in (
            ('install', "Install a repack…", G['plus'], win.open_install, "add game"),
            ('import', "Add another game (installer or folder)…", G['plus'],
             lambda: (win.open_install(), win.install._browse_installer()), "gog import"),
            ('refresh', "Refresh the library", G['refresh'], win.refresh_library, "reload"),
            ('surprise', "Surprise me", G['dice'], win.surprise, "random pick"),
            ('theatre', "Theatre mode", G['tv'], lambda: win.set_theatre(True), "tv couch"),
            ('sizes', "Measure disk usage", G['storage'],
             lambda: win.run(['--list', '--sizes'], "Measuring disk usage"), "size"),
            ('doctor', "Run the health check", G['health'],
             lambda: (win.go('health'), win.health.refresh()), "doctor"),
            ('debug', "Show the debug log", G['log'],
             lambda: (win.go('activity'), win.activity.show_debug(True)), "console")):
        out.append(E('command', title, "", glyph, fn, key=f"cmd:{key}", keywords=kw))
    st = getattr(win, 'settings', None)
    for key, sec in (getattr(st, 'sections', None) or {}).items():
        out.append(E('setting', sec.title, "Settings", G.get(sec.glyph, G['settings']),
                     lambda k=key: win._open_settings(k), key=f"set:{key}",
                     keywords="settings " + getattr(sec, '_keys', '')))
    return out


def game_entries(games: list[dict]) -> list[Entry]:
    """One per game; Enter (or Tab) lists its actions."""
    return [Entry('game', g.get('display_name', g['slug']),
                  {'nexus': "Repack", 'steam': "Steam", 'heroic': "Heroic"}.get(
                      g.get('source', 'nexus'), ''), G['gamepad'], None, g,
                  keywords=g['slug'].replace('-', ' '), key=f"game:{g['slug']}")
            for g in games if not g.get('hidden')]


def action_entries(win, game: dict) -> list[Entry]:
    from .actions import action_glyph, game_actions
    out = [Entry('action', a.label, a.sub, action_glyph(a),
                 lambda k=a.key: win.on_game_action(k, win.game(game['slug']) or game),
                 enabled=a.enabled, key=a.key)
           for a in game_actions(game)]
    return out


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .widgets import glyph_pixmap  # noqa: E402
