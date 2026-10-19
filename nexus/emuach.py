"""Achievements for repacks: the files the bundled Steam emulator writes
inside the game's prefix (gbe_fork / Goldberg, CODEX, RUNE, EMPRESS,
OnlineFix, SKIDROW), read-only, matched to the game's achievement list.

The list comes from Steam's own stats cache when the account has the game,
else from the game's public Steam Community achievements page (the page
embeds the full schema — API names, titles, descriptions, icons — as JSON;
no key needed), cached for a month.

Watcher polls the files while a game runs and calls back with each new
unlock, so the launch tracker can show a notification."""

import configparser
import html
import json
import os
import re
import threading
import time
from pathlib import Path

from . import net
from .paths import CACHE_DIR

COMMUNITY = "https://steamcommunity.com/stats/{appid}/achievements/?l=english"
SCHEMA_AGE = 30 * 86400
EMPTY_SCHEMA_AGE = 6 * 3600      # an empty one may be an age gate or an error page

# (emulator, glob under drive_c, file kind). <id> is the appid directory.
LOCATIONS = (
    ('gbe_fork', 'users/*/AppData/Roaming/GSE Saves/*/achievements.json', 'json'),
    ('Goldberg', 'users/*/AppData/Roaming/Goldberg SteamEmu Saves/*/achievements.json', 'json'),
    ('CODEX', 'users/Public/Documents/Steam/CODEX/*/achievements.ini', 'ini'),
    ('CODEX', 'users/*/AppData/Roaming/Steam/CODEX/*/achievements.ini', 'ini'),
    ('RUNE', 'users/Public/Documents/Steam/RUNE/*/achievements.ini', 'ini'),
    ('EMPRESS', 'users/*/AppData/Roaming/EMPRESS/*/remote/*/achievements.json', 'json'),
    ('EMPRESS', 'users/Public/Documents/EMPRESS/*/remote/*/achievements.json', 'json'),
    ('OnlineFix', 'users/Public/Documents/OnlineFix/*/Stats/Achievements.ini', 'ini'),
    ('SKIDROW', 'users/*/AppData/Local/SKIDROW/*/SteamEmu/UserStats/achiev.ini', 'ini'),
)
_TRUE = {'1', 'true', 'yes'}


def _appid_of(path: Path, drive_c: Path) -> int | None:
    for part in path.relative_to(drive_c).parts:
        if part.isdigit() and 0 < int(part) < 0x80000000:
            return int(part)
    return None


def find(prefix: Path) -> list[dict]:
    """[{emulator, appid, path, kind}] for every emulator file in a prefix,
    newest first."""
    drive_c = Path(prefix) / 'drive_c'
    out = []
    if not drive_c.is_dir():
        return out
    for emu, pattern, kind in LOCATIONS:
        for p in drive_c.glob(pattern):
            appid = _appid_of(p, drive_c)
            if appid and p.is_file():
                out.append({'emulator': emu, 'appid': appid, 'path': p, 'kind': kind})
    out.sort(key=lambda f: f['path'].stat().st_mtime, reverse=True)
    return out


def _num(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse(path: Path, kind: str) -> dict[str, int]:
    """{api name: unlock time (0 when not recorded)} for unlocked ones."""
    out = {}
    try:
        text = path.read_text(encoding='utf-8-sig', errors='replace')
    except OSError:
        return out
    if kind == 'json':
        try:
            data = json.loads(text)
        except ValueError:
            return out
        if isinstance(data, list):               # some builds write a list of objects
            data = {d.get('name'): d for d in data if isinstance(d, dict) and d.get('name')}
        for name, v in (data.items() if isinstance(data, dict) else ()):
            if not isinstance(v, dict):
                continue
            lv = {k.lower(): x for k, x in v.items()}
            done = lv.get('earned', lv.get('achieved', lv.get('unlocked')))
            if done is True or str(done).lower() in _TRUE:
                out[str(name)] = _num(lv.get('earned_time') or lv.get('unlocktime')
                                      or lv.get('time') or 0)
        return out
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read_string(text)
    except configparser.Error:
        return out
    for sec in cp.sections():
        if sec in ('SteamAchievements', 'Steam', 'AchievementsUnlockTimes'):
            continue
        lv = {k.lower(): v for k, v in cp.items(sec)}
        done = lv.get('achieved', lv.get('unlocked', lv.get('state')))
        if str(done).strip().lower() in _TRUE:
            out[sec] = _num(lv.get('unlocktime') or lv.get('timestamp') or lv.get('time'))
    if cp.has_section('AchievementsUnlockTimes'):           # SKIDROW
        for k, v in cp.items('AchievementsUnlockTimes'):
            if _num(v):
                out[k] = _num(v)
    return out


def parse_community(page: str) -> list[dict]:
    """The schema embedded in a Steam Community achievements page."""
    m = re.search(r'<script type="application/json" id="valve-ssr-data"[^>]*>(.*?)</script>',
                  page, re.S)
    if not m:
        return []
    found: list = []

    def walk(o):
        if found:
            return
        if isinstance(o, dict):
            ach = o.get('achievements')
            if isinstance(ach, list) and ach and isinstance(ach[0], dict) \
                    and 'api_name' in ach[0]:
                found.extend(ach)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and 'api_name' in o and o.lstrip()[:1] in '{[':
            try:
                walk(json.loads(o))
            except ValueError:
                pass
    try:
        walk(json.loads(m.group(1)))
    except ValueError:
        return []
    out = []
    for a in found:
        if a.get('archived'):
            continue
        out.append({'name': str(a.get('api_name') or ''),
                    'title': html.unescape(str(a.get('name') or a.get('api_name') or '')),
                    'desc': html.unescape(str(a.get('description') or '')),
                    'hidden': bool(a.get('hidden')),
                    'icon_url': a.get('icon_achieved') if str(a.get('icon_achieved', ''))
                    .startswith('https://') else None,
                    'icon_gray_url': a.get('icon_unachieved') if str(
                        a.get('icon_unachieved', '')).startswith('https://') else None})
    return out


def schema(appid: int, steam_root: Path | None = None, cache_only: bool = False) -> list[dict]:
    """[{name (api), title, desc, hidden, icon_url, icon_gray_url}] in order."""
    if steam_root is not None:
        from . import achievements as A
        local = A.schema(steam_root, appid)
        if local:
            return [{'name': a['name'], 'title': a['title'], 'desc': a['desc'],
                     'hidden': a['hidden'],
                     'icon_url': A.ICON_URL.format(appid=appid, icon=a['icon'])
                     if a['icon'] else None,
                     'icon_gray_url': A.ICON_URL.format(appid=appid, icon=a['icon_gray'])
                     if a['icon_gray'] else None} for a in local]
    path = CACHE_DIR / 'achschema' / f"{appid}.json"
    try:
        cached = json.loads(path.read_text())
        # An empty schema is as likely an age gate or a changed page as a game
        # with no achievements, so it's only trusted for a few hours.
        if cache_only or time.time() - path.stat().st_mtime < (SCHEMA_AGE if cached
                                                               else EMPTY_SCHEMA_AGE):
            return cached
    except (OSError, ValueError):
        if cache_only:
            return []
    try:
        page = net.get(COMMUNITY.format(appid=appid), timeout=20, limit=8 << 20)
        items = parse_community(page.decode('utf-8', 'replace'))
    except net.NetError:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return []
    if not items:
        try:                                    # never replace a good schema with nothing
            old = json.loads(path.read_text())
            if old:
                os.utime(path)                  # and don't re-ask on every call
                return old
        except (OSError, ValueError):
            pass
    net.save_json(path, items)
    return items


def status(prefix: Path, steam_root: Path | None = None, cache_only: bool = False,
           appid: int | None = None) -> dict | None:
    """{appid, emulator, total, unlocked, items} like Steam's own list
    (unlocked first, newest first), or None when the prefix has no
    emulator achievement file."""
    files = find(prefix)
    if appid:
        files = [f for f in files if f['appid'] == appid] or files
    if not files:
        return None
    f = files[0]
    got = {}
    for other in files:                       # one game can have two emulators' files
        if other['appid'] == f['appid']:
            for k, v in parse(other['path'], other['kind']).items():
                got[k] = max(v, got.get(k, 0))
    items = schema(f['appid'], steam_root, cache_only)
    known = {a['name'] for a in items}
    for name in got:
        if name not in known:                   # no schema (offline): show the raw name
            items.append({'name': name, 'title': name.replace('_', ' ').strip().capitalize(),
                          'desc': '', 'hidden': False, 'icon_url': None, 'icon_gray_url': None})
    order = {a['name']: i for i, a in enumerate(items)}
    for a in items:
        t = got.get(a['name'])
        a['unlocked'] = t is not None
        a['time'] = t or None
        if not a['unlocked']:
            a['icon_url'] = a.get('icon_gray_url') or a.get('icon_url')
    items.sort(key=lambda a: (not a['unlocked'], -(a['time'] or 0), order[a['name']]))
    return {'appid': f['appid'], 'emulator': f['emulator'], 'total': len(items),
            'unlocked': len(got), 'items': items}


class Watcher(threading.Thread):
    """Polls a prefix's emulator files while a game runs; calls
    on_unlock(item) once for every achievement unlocked since it started."""

    def __init__(self, prefix: Path, on_unlock, steam_root: Path | None = None,
                 interval: float = 5.0):
        super().__init__(daemon=True)
        self.prefix, self.on_unlock, self.steam_root = Path(prefix), on_unlock, steam_root
        self.interval = interval
        self.stop_event = threading.Event()
        self._seen: set[str] | None = None
        self._mtimes: dict = {}

    def _poll(self):
        files = find(self.prefix)
        mt = {str(f['path']): f['path'].stat().st_mtime for f in files}
        if self._seen is not None and mt == self._mtimes:
            return
        self._mtimes = mt
        got = set()
        for f in files:
            got |= set(parse(f['path'], f['kind']))
        if self._seen is None:
            self._seen = got                       # what was unlocked before this session
            return
        new = got - self._seen
        self._seen = got
        if new and files:
            names = {a['name']: a for a in schema(files[0]['appid'], self.steam_root)}
            for n in sorted(new):
                self.on_unlock(names.get(n) or {'name': n, 'title': n, 'desc': ''})

    def run(self):
        while not self.stop_event.is_set():
            try:
                self._poll()
            except OSError:
                pass
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()
