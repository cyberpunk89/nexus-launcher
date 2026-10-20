"""The tray icon (opt-in, Settings → Background): closing the window then
hides it to the tray instead of quitting, so the window's own watchers keep
going. Uses StatusNotifierItem through QSystemTrayIcon — on niri that needs
a tray host such as waybar's; without one the option is unavailable."""

from PyQt6.QtCore import QObject
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from .common import ICON_PATH

RECENT = 5                      # games under "Continue playing"
READY_MAX = 5                   # finished downloads remembered for the menu


def tray_available() -> bool:
    return QSystemTrayIcon.isSystemTrayAvailable()


def recent_games(games: list[dict], n: int = RECENT) -> list[dict]:
    """The installed, playable games played most recently."""
    played = [g for g in games if g.get('last_played') and is_installed(g)
              and play_enabled(g) and not g.get('stale')]
    played.sort(key=lambda g: g['last_played'], reverse=True)
    return played[:n]


class Tray(QObject):
    """The icon and its menu; the menu is rebuilt each time it opens, from
    the window's current state."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ready: list[dict] = []           # finished downloads offered while hidden
        self.icon = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        self.icon.setToolTip("Nexus")
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._fill)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self._fill()
        self.icon.show()

    def close(self):
        self.icon.hide()
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

    def _fill(self):
        m = self.menu
        m.clear()
        m.addAction("Show Nexus", self.win.show_from_tray)
        games = recent_games(getattr(self.win, '_games', []))
        if games:
            m.addSection("Continue playing")
            for g in games:
                m.addAction(g.get('display_name') or g['slug'],
                            lambda g=g: self.win.on_game_action('launch', g))
        active = self.win.downloads.active_count() if hasattr(self.win, 'downloads') else 0
        if active or self.ready:
            m.addSection("Downloads")
        if active:
            m.addAction(f"{active} downloading…", lambda: self.win.show_from_tray(
                self.win.DOWNLOADS))
        for c in self.ready:
            m.addAction(f"Install {c['name']}", lambda c=c: self._install(c))
        m.addSeparator()
        m.addAction("Quit Nexus", self.win.quit_from_tray)

    def _install(self, c: dict):
        self.ready = [r for r in self.ready if r is not c]
        self.win.show_from_tray()
        self.win._start_install(self.win._default_install_cmd(c['path']), c['name'],
                                c.get('slug') or '')


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .gameinfo import is_installed, play_enabled  # noqa: E402
