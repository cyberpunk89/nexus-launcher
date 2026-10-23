"""The tray icon (opt-in, Settings → Background): closing the window then
hides it to the tray instead of quitting, so the window's own watchers keep
going. QSystemTrayIcon: the notification area on Windows; on Linux a
StatusNotifierItem — on niri that needs a tray host such as waybar's;
without one the option is unavailable. `nexus --hidden` starts here (how
"Start with Windows" opens it at sign-in)."""

from PyQt6.QtCore import QObject
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from .common import ICON_PATH

RECENT = 5                      # games under "Continue playing"
READY_MAX = 5                   # finished downloads remembered for the menu
MESSAGE_MS = 8000               # how long a tray notification stays


def tray_available() -> bool:
    return QSystemTrayIcon.isSystemTrayAvailable()


def recent_games(games: list[dict], n: int = RECENT) -> list[dict]:
    """The installed, playable games played most recently."""
    played = [g for g in games if g.get('last_played') and is_installed(g)
              and play_enabled(g) and not g.get('stale')]
    played.sort(key=lambda g: g['last_played'], reverse=True)
    return played[:n]


def tray_message(title: str, body: str) -> bool:
    """Show a notification from the tray icon (a toast on Windows), when
    there is one showing. False: no tray, the caller notifies another way."""
    tray = Tray.current
    if tray is None or not tray.icon.isVisible() or not QSystemTrayIcon.supportsMessages():
        return False
    tray.icon.showMessage(title, body, QIcon(str(ICON_PATH)), MESSAGE_MS)
    return True


class Tray(QObject):
    """The icon and its menu; the menu is rebuilt each time it opens, from
    the window's current state. Its entries carry what they do as data, and
    one slot handles them all: no lambda slots on short-lived actions."""
    current: 'Tray | None' = None          # the window's tray, for tray_message

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ready: list[dict] = []           # finished downloads offered while hidden
        self.icon = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        self.icon.setToolTip("Nexus")
        self.menu = QMenu(win)
        self.menu.aboutToShow.connect(self._fill)
        self.menu.triggered.connect(self._chosen)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.messageClicked.connect(self._message_clicked)
        self._fill()
        self.icon.show()
        Tray.current = self

    def close(self):
        if Tray.current is self:
            Tray.current = None
        self.icon.hide()
        self.icon.setContextMenu(None)
        self.menu.deleteLater()
        self.deleteLater()

    def add_ready(self, c: dict):
        self.ready = [r for r in self.ready if r['path'] != c['path']][-(READY_MAX - 1):] + [c]

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.win.isVisible() and not self.win.isMinimized():
                self.win.hide()
            else:
                self.win.show_from_tray()

    def _message_clicked(self):
        self.win.show_from_tray()

    def _add(self, text: str, what: str, arg=None):
        self.menu.addAction(text).setData((what, arg))

    def _fill(self):
        m = self.menu
        m.clear()
        self._add("Show Nexus", 'show')
        games = recent_games(getattr(self.win, '_games', []))
        if games:
            m.addSection("Continue playing")
            for g in games:
                self._add(g.get('display_name') or g['slug'], 'play', g)
        active = self.win.downloads.active_count() if hasattr(self.win, 'downloads') else 0
        if active or self.ready:
            m.addSection("Downloads")
        if active:
            self._add(f"{active} downloading…", 'downloads')
        for c in self.ready:
            self._add(f"Install {c['name']}", 'install', c)
        m.addSeparator()
        self._add("Quit Nexus", 'quit')

    def _chosen(self, action):
        what, arg = action.data() or (None, None)
        if what == 'show':
            self.win.show_from_tray()
        elif what == 'play':
            self.win.on_game_action('launch', arg)
        elif what == 'downloads':
            self.win.show_from_tray(self.win.DOWNLOADS)
        elif what == 'install':
            self._install(arg)
        elif what == 'quit':
            self.win.quit_from_tray()

    def _install(self, c: dict):
        self.ready = [r for r in self.ready if r['path'] != c['path']]
        self.win.show_from_tray()
        self.win._start_install(self.win._default_install_cmd(c['path']), c['name'],
                                c.get('slug') or '')


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .gameinfo import is_installed, play_enabled  # noqa: E402
