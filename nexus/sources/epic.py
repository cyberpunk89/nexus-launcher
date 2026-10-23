"""Epic Games Launcher: the installed games, from the launcher's manifest
files (one JSON `.item` per install). Only installed games: Epic's owned list
needs a login, which Heroic or Game-db cover."""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

BROKEN = "Couldn't read Epic's install list (the launcher may have changed its format)"


def default_manifests() -> Path | None:
    root = os.environ.get('ProgramData') or os.environ.get('PROGRAMDATA')
    return Path(root, 'Epic', 'EpicGamesLauncher', 'Data', 'Manifests') if root else None


def launch_url(item: dict) -> str:
    return ('com.epicgames.launcher://apps/'
            f"{item['CatalogNamespace']}%3A{item['CatalogItemId']}%3A{item['AppName']}"
            '?action=launch&silent=true')


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _game(item: dict) -> dict | None:
    """The library entry for one manifest, or None for DLC and unfinished
    installs. KeyError/TypeError when a required field is missing."""
    app = item['AppName']
    if item.get('bIsIncompleteInstall') or item.get('MainGameAppName', app) != app:
        return None
    folder = item['InstallLocation']
    exe = item.get('LaunchExecutable')
    return {
        'slug': f'epic-{app}',
        'source': 'epic',
        'app_name': app,
        'display_name': item.get('DisplayName') or app,
        'install_dir': folder,
        'exe': str(Path(folder, exe)) if exe else None,
        'launcher': launch_url(item),
        'installed': True,
        'disk_bytes': _int(item.get('InstallSize')),
        'last_played': None,
        'playtime_minutes': None,
        'cover_url': None,
        'hero_url': None,
        'wide_url': None,
        'logo_url': None,
    }


class Epic:
    name = 'epic'

    def __init__(self, manifests: Path | None = None):
        self.manifests = manifests if manifests is not None else default_manifests()
        self.error: str | None = None
        self.count = 0

    def available(self) -> bool:
        return self.manifests is not None and self.manifests.is_dir()

    def installed(self) -> list[dict]:
        """Never raises: a manifest that can't be read is skipped, logged and
        reported through health()."""
        self.error, games, seen = None, [], set()
        try:
            files = sorted(self.manifests.glob('*.item'))
        except OSError:
            log.exception("epic: can't list %s", self.manifests)
            self.error = BROKEN
            return []
        for f in files:
            try:
                g = _game(json.loads(f.read_text(encoding='utf-8-sig')))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                log.warning("epic: unreadable manifest %s", f.name, exc_info=True)
                self.error = BROKEN
                continue
            if g is None:
                continue
            # The same game twice (a leftover manifest) shows once; paths
            # compare without regard to case or slash direction.
            key = str(g['install_dir']).replace('\\', '/').rstrip('/').casefold()
            if key not in seen:
                seen.add(key)
                games.append(g)
        self.count = len(games)
        return games

    def health(self) -> dict:
        return {'name': self.name, 'label': 'Epic Games', 'ok': self.error is None,
                'games': self.count, 'message': self.error}
