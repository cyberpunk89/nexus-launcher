"""Hand keyboard/controller focus to a game's window once it opens, and back
to Nexus when it closes.

Wayland doesn't let one app focus another's window, but the compositor can.
This uses niri's IPC (`niri msg`) when Nexus runs under niri; elsewhere it
reports itself unavailable and the caller falls back to stepping aside
(minimising). Read-only apart from the focus action itself.
"""

import functools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# Windows that are never "the game": Nexus, launchers, their helper windows.
IGNORE_APP_IDS = {'nexus', 'steam', 'steamwebhelper', 'heroic', 'electron',
                  'com.heroicgameslauncher.hgl', 'protontricks', 'winetricks',
                  'zenity', 'yad', 'kdialog'}


@functools.cache
def _has_niri() -> bool:
    return shutil.which('niri') is not None      # a PATH walk: asked every hand-off tick


def available() -> bool:
    return bool(os.environ.get('NIRI_SOCKET')) and _has_niri()


def windows() -> list[dict]:
    """niri's window list (id, app_id, title, pid, is_focused…), or []."""
    if not available():
        return []
    try:
        out = subprocess.run(['niri', 'msg', '--json', 'windows'], capture_output=True,
                             text=True, timeout=2)
        data = json.loads(out.stdout or '[]')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return data if isinstance(data, list) else []


def focus(window_id: int) -> bool:
    if not available():
        return False
    try:
        return subprocess.run(['niri', 'msg', 'action', 'focus-window', '--id',
                               str(int(window_id))], capture_output=True,
                              timeout=2).returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _words(s: str) -> set[str]:
    return {w for w in re.split(r'[^a-z0-9]+', (s or '').lower()) if len(w) > 2}


def pick_game_window(wins: list[dict], before_ids: set, game: dict) -> int | None:
    """The window that most likely belongs to `game` among the windows that
    weren't there before it was launched. Steam games run with WM_CLASS
    steam_app_<appid>; other games are matched by exe or title; failing
    that, the newest unknown window wins."""
    new = [w for w in wins if w.get('id') not in before_ids
           and (w.get('app_id') or '').lower() not in IGNORE_APP_IDS]
    if not new:
        return None
    appid = game.get('appid') if game.get('source') == 'steam' else None
    exe = Path(game.get('exe') or '').stem.lower()
    name = _words(game.get('display_name') or '')

    def score(w):
        app = (w.get('app_id') or '').lower()
        title = (w.get('title') or '').lower()
        s = 0
        if appid and app == f'steam_app_{appid}':
            s += 100
        if app.startswith('steam_app_'):
            s += 20
        if exe and (exe in app or exe in title):
            s += 50
        s += 10 * len(name & _words(title))
        return (s, w.get('id') or 0)
    best = max(new, key=score)
    return best.get('id')


def own_window(wins: list[dict], app_id: str = 'nexus', pid: int | None = None) -> int | None:
    """Our own window: the one with our pid when niri reports pids (so a stale
    or second Nexus never gets the focus back), else the first with our app-id."""
    mine = [w for w in wins if (w.get('app_id') or '') == app_id]
    if pid is not None:
        exact = next((w.get('id') for w in mine if w.get('pid') == pid), None)
        if exact is not None or any('pid' in w for w in mine):
            return exact
    return next((w.get('id') for w in mine), None)
