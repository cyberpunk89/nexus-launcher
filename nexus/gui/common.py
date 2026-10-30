"""Shared base of the GUI: paths, the CLI it drives, alive(), a safe deleteLater()."""

from functools import partial
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import QObject

from nexus import paths as NX, platform


HOME = Path.home()
# The repo this package lives in (nexus/gui/), so GUI and CLI are always the
# same version; bin/nexus and bin/nexus-cli are thin stubs that import us.
_REPO = Path(__file__).resolve().parents[2]


def cli_args(argv: list) -> list:
    """A job's arguments after the CLI itself ([*CLI, '--x'] -> ['--x']);
    other commands unchanged."""
    return argv[len(CLI):] if argv[:len(CLI)] == CLI else argv


def alive(*objs) -> bool:
    """Whether these Qt objects still exist. A deferred call (a 0 ms timer, a
    lambda slot) can outlive the widget it touches, and an exception in a
    slot aborts the whole app under PyQt6."""
    return all(o is not None and not sip.isdeleted(o) for o in objs)


# deleteLater() from Python hands the object to Qt, and sip lets go of its
# wrapper. A lambda slot holding `self` then makes a cycle the garbage
# collector may clear (the lambda loses its closure) while Qt still delivers
# timers and signals to the object until the deferred delete runs: calling
# that lambda is a native crash (the Windows CI's access violation, a test
# or two after the widget's own). So every deleteLater() keeps the wrapper
# here until Qt has destroyed the object.
_DOOMED: dict[int, QObject] = {}
_qt_delete_later = QObject.deleteLater


def _delete_later(self):
    key = id(self)
    if key not in _DOOMED:
        _DOOMED[key] = self
        self.destroyed.connect(partial(_DOOMED.pop, key, None))
    _qt_delete_later(self)


QObject.deleteLater = _delete_later


CLI_BIN = str(_REPO / 'bin' / 'nexus-cli')      # the Linux script itself (autostart, desktop files)
CLI = NX.cli_command()                          # argv that runs the CLI: [*CLI, '--list']
DATA_DIR = NX.DATA_DIR
CONFIG_PATH = NX.CONFIG_PATH
APPLICATIONS_DIR = NX.APPLICATIONS_DIR
ICON_PATH = DATA_DIR / 'nexus-icon.png'
LOGO_SVG = _REPO / 'assets' / 'nexus.svg'           # the app logo; ICON_PATH is rendered from it
WORDMARK_SVG = _REPO / 'assets' / 'wordmark.svg'    # "Nexus" lettering, painted in the accent
DESKTOP_PATH = APPLICATIONS_DIR / f'{NX.APP_ID}.desktop'
LEGACY_DESKTOP = APPLICATIONS_DIR / 'fitgirl-manager.desktop'
# Where games go when config has no [install] base: ~/Games/FitGirl; on
# Windows \Games on the roomiest non-system drive (the CLI's own default).
INSTALL_BASE_DEFAULT = str(platform.default_install_base(HOME))


# The user's collections, in their order: set by MainWindow whenever they
# change, read by game_actions() (which only gets a game) and Theatre's rows.
COLLECTION_NAMES: list[str] = []
# Each one's icon, colour and top-bar pin (LibraryPrefs.collection_look), set
# with COLLECTION_NAMES: {name: {'icon': lucide name, 'color': accent key,
# 'pinned': True}}, only what was chosen.
COLLECTION_LOOK: dict[str, dict] = {}
MAX_COLLECTION_NAME = 40
MAX_TITLE = 120
ATTRACT_MINUTES = (0, 3, 5, 10, 20)
MAX_NOTE = 20000
