"""Play sessions from Steam's own game log (w-150), so games played while
Nexus was closed still count. Steam writes gameprocess_log.txt (Linux:
~/.local/share/Steam/logs/) whenever a game's process starts or ends. Only
three line shapes are read, matched exactly; everything else is dropped, and
the command Steam launched (it can hold paths) is never kept.

    [ts] AppID <id> adding PID <pid> as a tracked process[ "<command>"]
    [ts] Remove <id> from running list
    [ts] Client version: <n>              (Steam restarted: open games are gone)

Imported sessions live in DATA_DIR/steam-log.jsonl, never in sessions.jsonl:
the Game-db upload reads only that one, and turning the import off must hide
them without losing them. history.all_sessions() merges both at read time.
Timestamps are naive local time, like sessions.jsonl's."""

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import history
from .paths import DATA_DIR

PATH = DATA_DIR / 'steam-log.jsonl'
CURSOR_PATH = DATA_DIR / 'steam-log.cursor.json'
LOG_NAME = 'gameprocess_log.txt'
PREVIOUS_NAME = 'gameprocess_log.previous.txt'

_TS = r'\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] '
_ADD = re.compile(_TS + r'AppID (\d+) adding PID \d+ as a tracked process(?: ".*")?$')
_REMOVE = re.compile(_TS + r'Remove (-?\d+) from running list$')
_CLIENT = re.compile(_TS + r'Client version: \d+$')


def _time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def _app32(appid: int) -> int:
    """The 32-bit appid, from any id the log or a game list shows: a store
    game's own, a shortcut's 64-bit id (appid in the top half) or the
    negative 32-bit one `Remove` uses."""
    return appid >> 32 if appid >= 1 << 32 else appid & 0xFFFFFFFF


def _scan(lines) -> tuple[list[dict], int | None]:
    """(sessions, index of the earliest `adding` line still open at the end)."""
    running: dict[int, tuple[datetime, int]] = {}
    out = []
    for i, raw in enumerate(lines):
        line = raw.rstrip('\r\n')
        m = _ADD.match(line)
        if m:
            t = _time(m.group(1))
            key = _app32(int(m.group(2)))
            if t is not None and key not in running:
                running[key] = (t, i)
            continue
        m = _REMOVE.match(line)
        if m:
            t = _time(m.group(1))
            key = _app32(int(m.group(2)))
            started = running.pop(key, None) if t else None
            if started and history.is_session((t - started[0]).total_seconds()):
                out.append({'appid': key, 'start': started[0], 'end': t})
            continue
        if _CLIENT.match(line):
            running.clear()
    return out, min((v[1] for v in running.values()), default=None)


def parse(lines) -> list[dict]:
    """[{appid, start, end}] for the games that started and stopped in `lines`."""
    return _scan(lines)[0]


def _read_lines(path, offset: int = 0) -> tuple[list[bytes], list[int]]:
    """The raw lines of `path` from byte `offset`, and each one's start offset."""
    with open(path, 'rb') as f:
        f.seek(offset)
        data = f.read()
    lines, offsets, pos = [], [], offset
    for raw in data.splitlines(True):
        if not raw.endswith(b'\n'):
            break                       # still being written: read it next time
        lines.append(raw)
        offsets.append(pos)
        pos += len(raw)
    return lines, offsets


def _text(lines: list[bytes]) -> list[str]:
    return [b.decode('utf-8', errors='replace') for b in lines]


def _game_for(appid: int, games) -> tuple[str, str]:
    """(slug, name): the Nexus game that has this Steam appid (a Steam game,
    or a shortcut Nexus made), else steam-<appid>."""
    for g in games or ():
        try:
            mine = _app32(int(g.get('steam_appid')
                             or (g.get('appid') if g.get('source') == 'steam' else 0) or 0))
        except (TypeError, ValueError):
            continue
        if mine == appid and g.get('slug'):
            return g['slug'], g.get('display_name') or g.get('name') or f'Steam app {appid}'
    return f'steam-{appid}', f'Steam app {appid}'


def _head(path) -> str:
    """A fingerprint of the file's first complete line: Steam starts a new log
    (and moves the old one aside) on every restart, whatever the sizes."""
    try:
        with open(path, 'rb') as f:
            first = f.readline(512)
    except OSError:
        return ''
    return hashlib.sha1(first).hexdigest() if first.endswith(b'\n') else ''


def _read_cursor(path) -> dict:
    try:
        c = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(c, dict) and all(isinstance(c.get(k), int) and c[k] >= 0
                                       for k in ('size', 'offset')):
            return c
    except (OSError, ValueError):
        pass
    return {'size': 0, 'offset': 0}


def _write_cursor(path, cursor: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(cursor), encoding='utf-8')
    os.replace(tmp, path)


def import_new(log_path, out_path, cursor_path, games=()) -> int:
    """Read what Steam logged since the last import and append its finished
    sessions to `out_path`; returns how many were added. Raises OSError for
    an unwritable output. A session still open stays unread: the cursor stops
    at its `adding` line and the next import starts there (what was already
    written is skipped by (appid, start)). A log smaller than the cursor was
    rotated (smaller, or its first line changed): the rest of gameprocess_log.previous.txt, then this one from the
    top."""
    log_path, out_path, cursor_path = Path(log_path), Path(out_path), Path(cursor_path)
    cur = _read_cursor(cursor_path)
    head = _head(log_path)
    try:
        size = log_path.stat().st_size
    except OSError:
        return 0
    found: list[dict] = []
    offset = cur['offset']
    if size < cur['size'] or (head and cur.get('head') and head != cur['head']):
        try:
            prev, _ = _read_lines(log_path.with_name(PREVIOUS_NAME), offset)
            found += parse(_text(prev))             # Steam restarted: nothing stays open
        except OSError:
            pass
        offset = 0
    lines, offsets = _read_lines(log_path, offset)
    sessions, open_at = _scan(_text(lines))
    found += sessions
    new_offset = offsets[open_at] if open_at is not None else offset + sum(map(len, lines))
    have = {(s['appid'], s['_start']) for s in load(out_path)}
    rows = []
    for s in sorted(found, key=lambda s: s['start']):
        if (s['appid'], s['start']) in have:
            continue
        have.add((s['appid'], s['start']))
        slug, name = _game_for(s['appid'], games)
        rows.append(json.dumps({
            'appid': s['appid'], 'slug': slug, 'name': name,
            'start': s['start'].isoformat(timespec='seconds'),
            'end': s['end'].isoformat(timespec='seconds'),
            'minutes': round((s['end'] - s['start']).total_seconds() / 60, 1),
            'source': 'steam', 'via': 'steam-log'}) + '\n')
    if rows:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, ''.join(rows).encode())
        finally:
            os.close(fd)
    _write_cursor(cursor_path, {'size': size, 'offset': new_offset, 'head': head or cur.get('head', '')})
    return len(rows)


def load(path=None) -> list[dict]:
    """The imported sessions as history.load() returns its own ('_start',
    float minutes), each (appid, start) once; torn lines are skipped."""
    out, seen = [], set()
    try:
        with open(path or PATH, encoding='utf-8') as f:
            for line in f:
                try:
                    s = json.loads(line)
                    s['_start'] = datetime.fromisoformat(s['start'])
                    s['minutes'] = float(s['minutes'])
                    key = (s['appid'], s['_start'])
                except (ValueError, KeyError, TypeError):
                    continue
                if key not in seen:
                    seen.add(key)
                    out.append(s)
    except OSError:
        pass
    return out
