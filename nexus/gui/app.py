"""Start-up: the app icon and desktop entry, one instance per user, main()."""

import json
import os
import shutil
import subprocess
import sys

from PyQt6.QtCore import pyqtSignal, QObject, QRectF, Qt, QTimer
from PyQt6.QtGui import QFont, QIcon, QPainter, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication

from nexus import paths as NX
from nexus.instance import INSTANCE, INSTANCE_MAX

from .common import (_REPO, APPLICATIONS_DIR, DATA_DIR, DESKTOP_PATH, ICON_PATH,
    LEGACY_DESKTOP, LOGO_SVG)
from .theme import apply_appearance, build_stylesheet, FONTS, FS, resolve_fonts


def ensure_icon() -> str:
    """The app icon: assets/nexus.svg rendered to a 256 px PNG (docks,
    notifications and the desktop entry want a raster file). Re-rendered
    whenever the SVG is newer than the PNG. Needs a live QApplication."""
    try:
        svg_mtime = LOGO_SVG.stat().st_mtime
    except OSError:
        svg_mtime = None
    try:
        if ICON_PATH.exists() and (svg_mtime is None or ICON_PATH.stat().st_mtime >= svg_mtime):
            return str(ICON_PATH)
    except OSError:
        pass
    if svg_mtime is None:
        return str(ICON_PATH) if ICON_PATH.exists() else "applications-games"
    try:
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:                       # qt6-svg missing: use the theme's icon
        return str(ICON_PATH) if ICON_PATH.exists() else "applications-games"
    renderer = QSvgRenderer(str(LOGO_SVG))
    if not renderer.isValid():
        return str(ICON_PATH) if ICON_PATH.exists() else "applications-games"
    size = 256
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(pt, QRectF(0, 0, size, size))
    pt.end()
    # Qt's SVG renderer ignores the rounded clip-path, so round the corners
    # here (the logo's radius is 8 of 64).
    vb = renderer.viewBoxF()
    pm = rounded_pixmap(pm, size * 8 / (vb.width() or 64))
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ICON_PATH.with_suffix('.tmp.png')
        if pm.save(str(tmp), 'PNG'):
            os.replace(tmp, ICON_PATH)
            return str(ICON_PATH)
    except OSError:
        pass
    return "applications-games"


def manager_exec() -> str:
    """Desktop-entry Exec for this app: `nexus` on PATH if there is one (e.g.
    the ~/.local/bin symlink), else the repo's bin/nexus — quoted per the
    Desktop Entry spec."""
    path = shutil.which('nexus') or str(_REPO / 'bin' / 'nexus')
    for ch in ('\\', '"', '`', '$'):
        path = path.replace(ch, '\\' + ch)
    return f'"{path}"'.replace('\\', '\\\\').replace('%', '%%')


# One Nexus per user. A second launch (nirinit restoring the session, Sunshine
# starting Theatre, a double click on the icon) hands its arguments to the
# running window and exits instead of opening another one.
# The socket path is INSTANCE (nexus/instance.py: the background agent
# forwards to it without Qt). No socket options on purpose: with them Qt
# builds the socket elsewhere and renames it over ours, so a second listen()
# would silently steal the name instead of failing.


def forward_to_running(argv: list[str]) -> bool:
    """Give argv to an already-running Nexus. True if one took it."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE)
    if not sock.waitForConnected(300):
        return False
    sock.write(json.dumps({'argv': argv}).encode() + b'\n')
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


class InstanceServer(QObject):
    """The running Nexus's end: emits each later launch's argv."""
    request = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept)

    def listen(self, argv: list[str]) -> bool:
        """Become the instance. False if another one answered meanwhile (two
        launches at once) and took argv instead."""
        if os.path.isabs(INSTANCE):
            try:
                os.makedirs(os.path.dirname(INSTANCE), mode=0o700, exist_ok=True)
            except OSError:
                pass
        if self.server.listen(INSTANCE):
            return True
        if forward_to_running(argv):
            return False
        QLocalServer.removeServer(INSTANCE)     # a stale socket from a crash
        self.server.listen(INSTANCE)
        return True

    def _accept(self):
        while (sock := self.server.nextPendingConnection()) is not None:
            buf = bytearray()

            def read(sock=sock, buf=buf):
                buf.extend(bytes(sock.readAll()))
                if len(buf) > INSTANCE_MAX:         # not a launch: drop it
                    buf.clear()
                    sock.abort()
                    return
                if b'\n' in buf:
                    line = bytes(buf).split(b'\n', 1)[0]
                    buf.clear()
                    try:
                        argv = json.loads(line).get('argv') or []
                    except (ValueError, AttributeError):
                        argv = []
                    self.request.emit([str(a) for a in argv])
            sock.readyRead.connect(read)
            sock.disconnected.connect(sock.deleteLater)
            if sock.bytesAvailable():
                read()


def install_desktop_entry(icon: str):
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_PATH.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Nexus\n"
        "GenericName=Game launcher\n"
        "Comment=Play and manage your games, install repacks, update Proton\n"
        f"Exec={manager_exec()}\n"
        f"Icon={icon}\n"
        "Categories=Game;\n"
        "Keywords=games;launcher;steam;proton;fitgirl;\n"
        "Terminal=false\n"
        f"StartupWMClass={NX.APP_ID}\n"
        "SingleMainWindow=true\n"
        "Actions=Theatre;\n"
        "\n"
        "[Desktop Action Theatre]\n"
        "Name=Theatre mode (TV)\n"
        f"Exec={manager_exec()} --theatre\n"
    )
    DESKTOP_PATH.chmod(0o644)
    try:
        LEGACY_DESKTOP.unlink()           # the pre-rename "FitGirl Manager" entry
    except OSError:
        pass
    if shutil.which('update-desktop-database'):
        try:
            subprocess.run(['update-desktop-database', str(APPLICATIONS_DIR)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass


def install_excepthook(win: 'MainWindow'):
    """An exception escaping a Qt slot aborts PyQt6 (qFatal) unless an
    excepthook is set. Log it (stderr, the Activity debug log) and say so in
    a toast; the app keeps running."""
    import traceback
    busy = []

    def hook(etype, value, tb):
        if issubclass(etype, KeyboardInterrupt):
            sys.__excepthook__(etype, value, tb)
            return
        text = ''.join(traceback.format_exception(etype, value, tb))
        sys.stderr.write(text)
        if busy:                             # failing inside the hook: stop here
            return
        busy.append(1)
        try:
            win.activity._debug_append(None, f"\n[GUI error]\n{text}")
            win.toast(f"Something went wrong: {value or etype.__name__}", 'error',
                      ("Details", lambda: (win.go(win.ACTIVITY),
                                           win.activity.show_debug(True))), 10000)
        except Exception:                    # noqa: BLE001 — never raise from the hook
            pass
        finally:
            busy.clear()
    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    argv = sys.argv[1:]
    instance = None
    if "--install-desktop" not in argv:
        if forward_to_running(argv):
            return
        if "--leave-theatre" in argv:         # nothing running: nothing to leave
            return
        instance = InstanceServer(app)
        if not instance.listen(argv):
            return
    app.setStyle('Fusion')
    NX.migrate()                          # fitgirl-install dirs -> nexus (once)
    app.setApplicationName("Nexus")
    # The Wayland app-id: lets niri/docks match the icon and lets users write
    # window rules for app-id="nexus".
    app.setDesktopFileName(NX.APP_ID)
    resolve_fonts()                       # before anything reads FONTS.ui
    apply_appearance(LibraryPrefs().appearance)   # colours before the stylesheet
    base = QFont(FONTS.ui)
    base.setPixelSize(FS['sm'])
    app.setFont(base)
    app.setStyleSheet(build_stylesheet())
    app.popup_rounder = PopupRounder(app)
    app.installEventFilter(app.popup_rounder)

    icon = ensure_icon()
    app.setWindowIcon(QIcon(icon))
    stale = DESKTOP_PATH.exists() and 'SingleMainWindow' not in DESKTOP_PATH.read_text()
    if not DESKTOP_PATH.exists() or stale or LEGACY_DESKTOP.exists() \
            or "--install-desktop" in sys.argv:
        install_desktop_entry(icon)
    if "--install-desktop" in sys.argv:
        print(f"Installed {DESKTOP_PATH}")
        return

    win = MainWindow()
    install_excepthook(win)
    win.setWindowIcon(QIcon(icon))
    if "--hidden" not in argv or win.tray is None:    # --hidden: start in the tray
        win.show()
    QTimer.singleShot(0, win.ctrl.open)       # controllers once the window is up
    if instance is not None:
        instance.request.connect(win.handle_remote)
    if "--theatre" in argv:
        win.set_theatre(True)
    code = app.exec()
    # Tear the window down while Qt is still fully alive: left to PyQt's
    # atexit cleanup, a large widget tree can segfault in sip on the way out.
    win.deleteLater()
    app.processEvents()
    del win
    sys.exit(code)


if __name__ == '__main__':
    main()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import rounded_pixmap  # noqa: E402
from .prefs import LibraryPrefs  # noqa: E402
from .widgets import PopupRounder  # noqa: E402
from .window import MainWindow  # noqa: E402
