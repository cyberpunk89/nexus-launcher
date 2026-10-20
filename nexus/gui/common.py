"""Shared base of the GUI: paths, the CLI it drives, alive()."""

from pathlib import Path

from PyQt6 import sip

from nexus import paths as NX


HOME = Path.home()
# The repo this package lives in (nexus/gui/), so GUI and CLI are always the
# same version; bin/nexus and bin/nexus-cli are thin stubs that import us.
_REPO = Path(__file__).resolve().parents[2]


def alive(*objs) -> bool:
    """Whether these Qt objects still exist. A deferred call (a 0 ms timer, a
    lambda slot) can outlive the widget it touches, and an exception in a
    slot aborts the whole app under PyQt6."""
    return all(o is not None and not sip.isdeleted(o) for o in objs)


CLI_BIN = str(_REPO / 'bin' / 'nexus-cli')
DATA_DIR = NX.DATA_DIR
CONFIG_PATH = NX.CONFIG_PATH
APPLICATIONS_DIR = NX.APPLICATIONS_DIR
ICON_PATH = DATA_DIR / 'nexus-icon.png'
LOGO_SVG = _REPO / 'assets' / 'nexus.svg'           # the app logo; ICON_PATH is rendered from it
WORDMARK_SVG = _REPO / 'assets' / 'wordmark.svg'    # "Nexus" lettering, painted in the accent
DESKTOP_PATH = APPLICATIONS_DIR / f'{NX.APP_ID}.desktop'
LEGACY_DESKTOP = APPLICATIONS_DIR / 'fitgirl-manager.desktop'
INSTALL_BASE_DEFAULT = str((HOME / 'Games' / 'FitGirl'))  # autodetected; config [install] base overrides


# The user's collections, in their order: set by MainWindow whenever they
# change, read by game_actions() (which only gets a game) and Theatre's rows.
COLLECTION_NAMES: list[str] = []
MAX_COLLECTION_NAME = 40
MAX_TITLE = 120
ATTRACT_MINUTES = (0, 3, 5, 10, 20)
MAX_NOTE = 20000
