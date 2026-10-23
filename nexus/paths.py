"""Every on-disk location Nexus uses, plus the one-time migration from the
old fitgirl-install names. Evaluated from $HOME at import time, so a test (or
a sandbox) can point HOME elsewhere before importing."""

import os
import shutil
import sys
from pathlib import Path

from . import platform

HOME = Path.home()
APP_ID = "nexus"                                   # Wayland app-id / desktop file
REPO = Path(__file__).resolve().parent.parent      # the source folder

DATA_DIR = platform.data_dir(HOME)
CONFIG_DIR = platform.config_dir(HOME)
CACHE_DIR = platform.cache_dir(HOME)
CONFIG_PATH = CONFIG_DIR / "config.toml"
NEWS_CONFIG = CONFIG_DIR / "news.toml"
GAMEDB_CACHE = CACHE_DIR / "gamedb"                # Game-db summary (0600, personal)

LOG_DIR = DATA_DIR / "logs"
ARTWORK_DIR = DATA_DIR / "artwork"
LAUNCHERS_DIR = DATA_DIR / "launchers"
SAVES_DIR = DATA_DIR / "saves"
INTEGRITY_DIR = DATA_DIR / "integrity"
ORPHAN_HOLDING_DIR = DATA_DIR / "orphaned-compatdata"
REGISTRY_PATH = DATA_DIR / "games.json"

APPLICATIONS_DIR = platform.app_menu_dir(HOME)


def find_steam_root(home: Path = HOME) -> Path:
    """Where Steam keeps its data (see platform.steam_root)."""
    return platform.steam_root(home)


def cli_command(same_python: bool = False) -> list[str]:
    """The argv that runs nexus-cli as a child process (see
    platform.cli_command). same_python: under this interpreter even on
    Linux, where the script's shebang would otherwise pick one."""
    return platform.cli_command(REPO, same_python)


STEAM_ROOT = find_steam_root()
PROTONGEN_CONFIG = HOME / ".config/protongen"

# Pre-rename locations. After migration each is a symlink to the new one, so
# paths already recorded in games.json, Heroic entries and old launchers keep
# resolving.
LEGACY = [
    (HOME / ".local/share/fitgirl-install", DATA_DIR),
    (HOME / ".config/fitgirl-install", CONFIG_DIR),
]


def migrate(quiet: bool = False) -> list[str]:
    """Move fitgirl-install's data/config dirs to the Nexus locations and leave
    a symlink behind. Idempotent: does nothing once the old path is a symlink,
    or when the new dir already exists (never merges two real dirs)."""
    done = []
    for old, new in LEGACY:
        if old.is_symlink() or not old.is_dir():
            continue
        if new.exists():
            if not quiet:
                print(f"  [!] Both {old} and {new} exist — leaving them as they are.",
                      file=sys.stderr)
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(old, new)
        except OSError:
            shutil.move(str(old), str(new))           # different filesystem
        old.symlink_to(new)
        done.append(f"{old} -> {new}")
    return done
