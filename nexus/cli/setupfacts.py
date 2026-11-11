"""nexus-cli --setup-facts: what Setup shows in place, in one read-only query.

Drives (free space, filesystem, the folder games would go to, Steam games per
drive), the torrent client and its download folder, what blocks an install
(with a fix), and what [repacks] said before. Creates nothing: not the install
folder, not a config. Every probe that touches a drive or the network runs in
a daemon thread with a deadline, so a dead network mount or a client that
doesn't answer shows up as a warning instead of a hang."""

import json
import os
import shutil
import sys
import threading
import time
import tomllib
from pathlib import Path

from nexus import features, platform, steamlib, torrent
from nexus.steamlib import find_heroic_config

from .common import (CONFIG_PATH, STEAM_ROOT, drive_of, call_timeout, existing_ancestor)
from .doctor import LONG_PATHS_HELP
from .protoncmds import install_hint
from .manage import _disk_info
from .runner import damaged_protons, find_proton, proton_repair_tag

DRIVE_TIMEOUT = 3.0        # one shared deadline for every drive probe
GATHER_TIMEOUT = 5.0       # finding the candidate folders
TORRENT_TIMEOUT = 8.0
LOW_SPACE = 50e9           # Windows: a warning under this on the default drive


def _candidates(default_base: Path) -> list[tuple[Path, str]]:
    """(folder, kind) the drives are found from: the default base first, then
    each Steam library (named, not stat-ed: the probe thread does that), then
    (Linux) each Heroic install parent."""
    out = [(default_base, 'base')]
    out += [(lib, 'steam') for lib in steamlib.library_paths(STEAM_ROOT)]
    if features.has('heroic'):
        for g in steamlib.heroic_store_games(find_heroic_config()):
            if g.get('install_dir'):
                out.append((Path(g['install_dir']).parent, 'heroic'))
    return out


def _probe(path: Path, kind: str, default_base: Path, deadline: float) -> dict:
    """One candidate's drive: id, mount, filesystem, free/total, Steam games,
    the folder games would go to and its warnings. {} when it isn't a drive
    here (a Steam library that no longer exists)."""
    if kind == 'steam' and not path.is_dir():
        return {}
    anchor = existing_ancestor(path)
    drive = drive_of(anchor)
    if not drive:
        return {}
    disk = _disk_info(anchor) or {}
    is_default = kind == 'base'
    folder = str(default_base) if is_default else str(Path(drive['mount']) / 'Games')
    ok, warns = call_timeout(lambda: platform.install_base_warnings(Path(folder)),
                             max(0.1, deadline - time.monotonic() - 0.1))
    return {'id': drive['id'], 'path': drive['mount'],
            'fs': platform.filesystem(drive['mount']),
            'free': disk.get('free'), 'total': disk.get('total'),
            'folder': folder, 'default': is_default,
            'steam_games': steamlib.library_count(path) if kind == 'steam' else 0,
            'warnings': warns if ok else ["couldn't check this folder in time"]}


def _timed_out(path: Path, is_default: bool, why: str) -> dict:
    return {'id': f'timeout:{path}', 'path': str(path), 'fs': None, 'free': None,
            'total': None, 'folder': str(path), 'default': is_default,
            'steam_games': 0, 'warnings': [why]}


def _under(path: Path, mount: str) -> bool:
    try:
        return Path(path).is_relative_to(mount)
    except (ValueError, TypeError):
        return False


def _drives(default_base: Path) -> list[dict]:
    ok, cands = call_timeout(lambda: _candidates(default_base), GATHER_TIMEOUT)
    extra = []
    if not ok:
        why, cands = cands, [(default_base, 'base')]
        extra.append(_timed_out(STEAM_ROOT, False,
                                f"couldn't list the Steam and Heroic folders ({why})"))
    deadline = time.monotonic() + DRIVE_TIMEOUT
    slots = []
    for path, kind in cands:
        box: list = []

        def run(path=path, kind=kind, box=box):
            try:
                box.append(_probe(path, kind, default_base, deadline))
            except Exception:
                box.append({})
        t = threading.Thread(target=run, daemon=True)
        t.start()
        slots.append((path, kind, t, box))

    results = []                                  # (path, kind, drive dict | None for stuck)
    for path, kind, t, box in slots:
        t.join(max(0.0, deadline - time.monotonic()))
        results.append((path, kind, box[0] if box else None))
    mounts = [r['path'] for _p, _k, r in results if r]
    # A mount that is its own parent is "/" (or "C:\\"): everything is under it,
    # so it can't tell a dead network folder from a plain one.
    narrow = [m for m in mounts if os.path.dirname(m) != m]

    drives, by_id = [], {}
    for path, kind, got in results:
        if got == {}:                                      # not a drive on this machine
            continue
        if got is None:                                    # still stuck
            if any(_under(path, m) for m in narrow):
                continue
            got = _timed_out(path, kind == 'base',
                             f"didn't answer within {DRIVE_TIMEOUT:g} s (a network drive?)")
        known = by_id.get(got['id'])
        if known:
            known['steam_games'] += got['steam_games']
            known['default'] = known['default'] or got['default']
            continue
        by_id[got['id']] = got
        drives.append(got)
    return drives + extra


def _torrent() -> dict | None:
    s = torrent.settings()
    detected = None
    if not s['client']:
        detected = torrent.detect()
        if not detected:
            return None
        s = dict(s, client=detected['client'], url=detected['url'])
    out = {'client': s['client'], 'url': s['url'], 'configured': detected is None,
           'download_dir': s.get('download_dir'), 'error': None}
    try:
        out['download_dir'] = torrent.client(s).info().get('download_dir') or out['download_dir']
    except torrent.TorrentError as e:
        out['error'] = str(e)
    except Exception as e:
        out['error'] = f"{type(e).__name__}: {e}"
    return out


def _needs(drives: list[dict]) -> list[dict]:
    out = []

    def add(status, title, detail, fix, actions=None):
        out.append({'status': status, 'title': title, 'detail': detail, 'fix': fix,
                    'actions': actions or []})
    if features.has('proton'):
        if not shutil.which('wine'):
            add('fail', 'Wine', 'not found', f"Install it: {install_hint('wine', 'wine')}")
        if not find_proton():
            add('fail', 'Proton', 'none installed', 'Install GE-Proton',
                [{'label': 'Install GE-Proton', 'args': ['--proton-install', 'latest'],
                  'steam': False}])
        for name, why in sorted(damaged_protons().items()):
            tag = proton_repair_tag(name)
            add('fail', f'{name} is damaged', f"{why}. Every game or installer run with it "
                "aborts at once.",
                "Reinstall it (downloaded and verified again; the damaged copy goes to "
                "the Trash)" if tag else "Reinstall it with your package manager",
                [{'label': 'Reinstall', 'args': ['--proton-install', tag, '--force'],
                  'steam': False}] if tag else None)
        return out
    if not platform.long_paths_enabled():
        add('warn', 'Long paths', 'off',
            "Games with deep folders can fail to install past 260 characters. Keep the "
            "install folder short, or turn on \"Enable Win32 long paths\". Nexus doesn't "
            "change it.",
            [{'label': 'How to turn it on', 'args': ['--open-url', LONG_PATHS_HELP],
              'steam': False}])
    default = next((d for d in drives if d['default']), None)
    if default and default['free'] is not None and default['free'] < LOW_SPACE:
        add('warn', 'Free space', f"{default['free'] / 1e9:.0f} GB free",
            'Big repacks need as much again to unpack',
            [{'label': 'Storage settings', 'args': ['--open-url', 'ms-settings:storagesense'],
              'steam': False}])
    return out


def _repacks_was() -> bool | None:
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    rp = data.get('repacks')
    return None if not isinstance(rp, dict) else rp.get('enabled', True) is not False


def setup_facts(install_base: Path) -> dict:
    """The facts Setup shows. Read-only, bounded in time, never creates a folder."""
    drives = _drives(install_base)
    ok, tor = call_timeout(_torrent, TORRENT_TIMEOUT)
    if not ok:                                       # timeout or an exception: say which
        try:
            s = torrent.settings()
        except Exception:
            s = {'client': None, 'url': None}
        tor = {'client': s['client'], 'url': s['url'], 'configured': bool(s['client']),
               'download_dir': s.get('download_dir'),
               'error': tor.replace("didn't finish", "didn't answer")}
    ok, needs = call_timeout(lambda: _needs(drives), GATHER_TIMEOUT)
    if not ok:
        needs = [{'status': 'warn', 'title': "Some checks didn't finish", 'detail': needs,
                  'fix': 'Open Health for the full list', 'actions': []}]
    return {'edition': 'linux' if features.has('proton') else 'windows',
            'default_base': str(install_base),
            'drives': drives,
            'torrent': tor,
            'needs': needs,
            'repacks_was': _repacks_was()}


def cmd_setup_facts(install_base: Path, as_json: bool = False) -> None:
    facts = setup_facts(install_base)
    if as_json:
        print(json.dumps(facts))
        sys.stdout.flush()
        return
    print(f"Install folder: {facts['default_base']}")
    for d in facts['drives']:
        free = f"{d['free'] / 1e9:.0f} GB free" if d['free'] is not None else 'free space unknown'
        print(f"  {d['path']}{' (default)' if d['default'] else ''}  {free}"
              + (f"  {d['fs']}" if d['fs'] else '')
              + (f"  {d['steam_games']} Steam games" if d['steam_games'] else ''))
        for w in d['warnings']:
            print(f"    ! {w}")
    t = facts['torrent']
    if t:
        print(f"Torrent client: {t['client']} at {t['url']}"
              + (f", downloads to {t['download_dir']}" if t['download_dir'] else '')
              + (f" ({t['error']})" if t['error'] else '')
              + ('' if t['configured'] else ' (found running, not configured)'))
    else:
        print("Torrent client: none found")
    for n in facts['needs']:
        print(f"  [{n['status']}] {n['title']}: {n['detail']}. {n['fix']}")
    sys.stdout.flush()
