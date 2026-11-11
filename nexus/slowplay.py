"""Slow the torrent client while a game runs (opt-in).

    [downloads]
    slow_while_playing = true

Nexus switches the client's own alternative speed limits ("turtle mode", the
limits the user set there) on when a game starts and off when it ends. It
never sets a limit of its own, and only undoes what it did:

- the undo record is a file (DATA_DIR/slow-while-playing.json), written
  atomically under a lock BEFORE alt speed is turned on, so a Nexus that quits
  or crashes mid-game still knows to restore it (the agent, the window's next
  start or the quit hook picks it up);
- no file when alt speed was already on, or the client's own schedule is on:
  Nexus leaves the client alone then;
- restoring turns alt speed off only if it is still on (the user may have
  switched it off meanwhile), and runs even if the option was switched off
  since. A client that can't be reached keeps the file for the next try.

sync() is idempotent, so every trigger can just call it. No OS-specific calls
here."""

import json
import time
import tomllib

from . import net, platform, torrent
from .paths import CONFIG_PATH, DATA_DIR

STATE = DATA_DIR / 'slow-while-playing.json'


def option() -> bool:
    """[downloads] slow_while_playing: off unless a real TOML boolean true."""
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig')).get('downloads')
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get('slow_while_playing') is True


def _load() -> dict | None:
    """The record; None when there is no file, {} when it is unreadable (it
    then names no client, so a restore drops it)."""
    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pending() -> bool:
    """A slowdown is in place that still has to be undone."""
    return STATE.exists()


def _drop() -> None:
    try:
        STATE.unlink(missing_ok=True)
    except OSError:
        pass


def _start(c, s: dict) -> str:
    try:
        if c.alt_scheduled() or c.alt_speed():
            return 'left'                     # the client's own doing: not ours to undo
    except torrent.TorrentError:
        return 'failed'
    record = {'client': s['client'], 'url': s['url'], 'was': False, 'slowed': False,
              'at': time.time()}
    if not _save(record):
        return 'failed'                       # no record, no slowdown
    return _slow(c, record)


def _save(record: dict) -> bool:
    """Atomic and fsynced: a record lost to a power cut would leave alt speed on."""
    try:
        net.write_atomic(STATE, json.dumps(record), fsync=True)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _slow(c, record: dict) -> str:
    try:
        c.set_alt_speed(True)
    except torrent.TorrentError:
        return 'failed'                       # the record stays: restore sorts out what happened
    _save({**record, 'slowed': True})         # (if that fails, the next sync just sets it again)
    return 'slowed'


def _kept(c, record: dict) -> str:
    """The game still runs and a record exists. Our switch-on may have failed
    (then retry it); if it succeeded and alt speed is off, the user turned it
    off: respect that."""
    if record.get('slowed'):
        return 'kept'
    try:
        if c.alt_speed():
            _save({**record, 'slowed': True})
            return 'kept'
    except torrent.TorrentError:
        return 'failed'
    return _slow(c, record)


def _end(c, s: dict, record: dict) -> str:
    # An unreadable/empty record ({}) is "unknown": whatever the current client
    # has on is put right; a record naming another client is dropped.
    if record and (record.get('client') != s.get('client') or record.get('url') != s.get('url')):
        _drop()                               # another client now; the old one is out of reach
        return 'idle'
    try:
        still_on = c.alt_speed()
        if still_on:
            c.set_alt_speed(False)
    except torrent.TorrentError:
        return 'failed'                       # keep the record, try again at the next trigger
    _drop()
    return 'restored' if still_on else 'left'


def sync(playing: bool, on: bool | None = None, c=None) -> str:
    """Bring the client in line with `playing`. Returns 'slowed' (turned alt
    speed on), 'restored' (turned it off again), 'kept' (a slowdown is in
    place and the game still runs), 'idle' (nothing to do), 'left' (the
    client's own setting was respected) or 'failed' (client unreachable; a
    record, if any, is kept for the next call)."""
    on = option() if on is None else on
    record = _load()
    if not playing and record is None:
        return 'idle'
    if playing and record is None and not on:
        return 'idle'
    s = torrent.settings()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with platform.file_lock(STATE.with_name(STATE.name + '.lock')):
        record = _load()      # again, now that we hold the lock
        if playing:
            if record == {}:
                _drop()                       # unreadable: nothing to go on, start over
                record = None
            if record is not None:
                try:
                    c = c or torrent.client(s)
                except torrent.TorrentError:
                    return 'kept'
                return _kept(c, record)
            if not on or not s.get('client'):
                return 'idle'
        elif record is None:
            return 'idle'
        try:
            c = c or torrent.client(s)
        except torrent.TorrentError:
            if playing:
                return 'idle'
            if not s.get('client') or (record and record.get('client') != s.get('client')):
                _drop()
                return 'idle'
            return 'failed'
        return _start(c, s) if playing else _end(c, s, record)
