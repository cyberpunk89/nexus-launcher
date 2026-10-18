"""Every on-disk location Nexus uses, plus the one-time migration from the
old fitgirl-install names. Evaluated from $HOME at import time, so a test (or
a sandbox) can point HOME elsewhere before importing."""

import os
import shutil
import sys
from pathlib import Path

HOME = Path.home()
APP_ID = "nexus"                                   # Wayland app-id / desktop file

DATA_DIR = HOME / ".local/share/nexus"
CONFIG_DIR = HOME / ".config/nexus"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "nexus"
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

APPLICATIONS_DIR = HOME / ".local/share/applications"


def find_steam_root(home: Path = HOME) -> Path:
    """Where Steam keeps its data: the usual ~/.local/share/Steam, Debian and
    Ubuntu's packaged Steam (~/.steam/debian-installation), or Flatpak Steam.
    The first that exists; the usual one when none does."""
    candidates = (home / ".local/share/Steam",
                  home / ".steam/debian-installation",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam")
    return next((c for c in candidates if (c / "steamapps").is_dir()),
                next((c for c in candidates if c.is_dir()), candidates[0]))


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
