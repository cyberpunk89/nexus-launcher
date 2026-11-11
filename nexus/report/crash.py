"""Crash files: what went wrong stays on disk until it is reported.

    record(kind, exc, flags=(), log=None, page=None) -> id | None
                                a Python exception (or a text) as
                                REPORTS_DIR/crash-<id>.json; the same
                                signature again bumps that file's count
    enable_native() -> Path | None
                                faulthandler into native-<pid>.txt for a
                                segfault or abort in this process
    native_path() -> Path | None
                                that file while it is on (a Qt message
                                handler appends qFatal's text there)
    native_note(text)           append a line to it, flushed at once
    watch_stalls() / stall_beat()
                                the UI thread's watchdog (w-101): each beat
                                re-arms faulthandler.dump_traceback_later, so
                                a UI thread busy for STALL_SECONDS dumps every
                                thread into the native file
    disable_native()            a clean exit: stop, close, delete it
    harvest() -> [id]           native files left by dead processes become
                                `native-crash` records (a freeze the process
                                never came back from is one too, typed so)
    pending() -> [summary]      harvest(), then every crash file, newest first
    load(id) / dismiss(id)      one crash file / delete it
    save_draft(draft) -> id, load_draft(id)
                                report drafts (draft-<id>.json)

Kinds: gui-exception, cli-internal, native-crash, game-failure,
user-report. Files are 0600 and hold the raw text (they never leave this
machine); collecting a report scrubs it. The signature is the exception
type plus the innermost Nexus frames as file:function (no line numbers),
so a crash still matches after an unrelated edit. The newest KEEP files
stay. Nothing here raises, and nothing writes to sys.stderr unless there is
one (the frozen Windows GUI has none)."""

import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

KEEP = 10                      # crash files kept (drafts too)
FRAMES = 5                     # innermost Nexus frames in the signature
LOG_LINES = 50

CRASH_ID = re.compile(r'^\d{8}T\d{6}Z-[0-9a-f]{4}$')
DRAFT_ID = re.compile(r'^[0-9a-f]{12}$')
KINDS = ('gui-exception', 'cli-internal', 'native-crash', 'game-failure', 'user-report')

_NEXUS_FILE = re.compile(r'(?:.*[/\\])?(nexus[/\\][\w/\\.-]+\.py)$')
_FRAME_LINE = re.compile(r'^\s*File "([^"\n]+)", line \d+,? in (\S+)')
_native = {'fh': None, 'path': None, 'was_on': False, 'pos': 0, 'watch': False,
           'beat': 0.0, 'dumped': False}

# The UI watchdog (w-101). faulthandler's own thread writes the dump; it
# can't run Python, so the dump carries faulthandler's "Timeout (0:00:15)!"
# line and our lines go round it: the UI thread's id once, before the first
# arming (so the report can show that thread first), and a line when the UI
# thread answers again after a dump. harvest() reads only what follows the
# last of those: a freeze the process came back from is no report.
# dump_traceback_later starts an OS thread per call, so the beat is slow.
STALL_SECONDS = 15
BEAT_SECONDS = 5
MARK = '# Nexus '                         # every line of ours starts so
UI_MARK = MARK + 'UI thread'
RECOVERED = MARK + 'answered again'
FREEZE_TYPE = 'Nexus stopped answering'
FREEZE = f'Freeze: {FREEZE_TYPE}'
_STALL_HEAD = re.compile(r'^Timeout \(\d+:\d\d:\d\d(?:\.\d+)?\)!', re.M)
_THREAD_HEAD = re.compile(r'^(?:Current thread|Thread) (0x[0-9a-fA-F]+)\b')
_FATAL = ('Fatal Python error', 'Windows fatal exception', 'qFatal:')
_HEADS = _FATAL + ('Qt critical:',)      # a part of the file that stands on its own
_lock = threading.RLock()                 # our writes and the position we expect


def _dir() -> Path:
    from nexus.paths import REPORTS_DIR
    return REPORTS_DIR


def _say(msg: str) -> None:
    err = sys.stderr
    if err is None:
        return
    try:
        print(f"nexus: {msg}", file=err)
    except Exception:                 # noqa: BLE001 — a closed or broken stream
        pass


def _write(path: Path, obj) -> None:
    from nexus import net
    net.write_atomic(path, json.dumps(obj, indent=1, ensure_ascii=False), mode=0o600)


def _read(path: Path) -> dict | None:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def nexus_file(path: str) -> str | None:
    """'nexus/cli/entry.py' for a file of ours (a checkout, the frozen
    exe's relative names, either slash), else None."""
    try:
        import nexus
        pkg = os.path.dirname(os.path.abspath(nexus.__file__))
        full = os.path.abspath(path)
        if os.path.normcase(full).startswith(os.path.normcase(pkg) + os.sep):
            return 'nexus/' + os.path.relpath(full, pkg).replace('\\', '/')
    except (ImportError, ValueError, TypeError):
        pass
    m = _NEXUS_FILE.match(path or '')
    return m.group(1).replace('\\', '/') if m else None


def frames_of(exc: BaseException) -> list[str]:
    """The Nexus frames of `exc`'s traceback, outermost first, as
    'nexus/<file>.py:function'."""
    out = []
    for fs in traceback.extract_tb(exc.__traceback__):
        f = nexus_file(fs.filename)
        if f:
            out.append(f"{f}:{fs.name}")
    return out


def frames_in(text: str) -> list[str]:
    """The Nexus frames in a traceback or faulthandler dump. faulthandler
    lists the innermost frame first, so its order is turned around."""
    out = []
    for line in (text or '').splitlines():
        m = _FRAME_LINE.match(line)
        if m and (f := nexus_file(m.group(1))):
            out.append(f"{f}:{m.group(2)}")
    if 'most recent call first' in (text or ''):
        out.reverse()
    return out


def signature(type_name: str, frames) -> str:
    """12 hex characters: the type and the innermost FRAMES Nexus frames."""
    key = '|'.join([type_name or '?', *list(frames)[-FRAMES:]])
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]


def _files(pattern: str) -> list[Path]:
    """Matching files, oldest first (mtime, then name)."""
    try:
        found = list(_dir().glob(pattern))
    except OSError:
        return []

    def key(p):
        try:
            return (p.stat().st_mtime_ns, p.name)
        except OSError:
            return (0, p.name)
    return sorted(found, key=key)


def _prune(pattern: str) -> None:
    for p in _files(pattern)[:-KEEP]:
        try:
            p.unlink()
        except OSError:
            pass


def _new_id() -> str:
    """<UTC second>-<4 hex>: the hex counts milliseconds, so ids of one
    second still sort by time; a taken one moves on."""
    t = time.time()
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(t))
    n = int(t * 1000) % 1000 * 64
    while (_dir() / f"crash-{stamp}-{n:04x}.json").exists() and n < 0xffff:
        n += 1
    return f"{stamp}-{n:04x}"


def _app() -> dict:
    try:
        from nexus import __version__, platform
        return {'version': __version__, 'edition': platform.OS_NAME}
    except Exception:                 # noqa: BLE001
        return {'version': '?', 'edition': '?'}


def _store(rec: dict) -> str | None:
    """Write `rec`, or bump the count of the file with its signature."""
    for p in reversed(_files('crash-*.json')):
        old = _read(p)
        if old and old.get('signature') == rec['signature'] and old.get('kind') == rec['kind']:
            old.update({k: rec[k] for k in ('message', 'traceback', 'flags', 'log', 'page',
                                            'version', 'edition') if k in rec})
            old['count'] = int(old.get('count') or 1) + 1
            old['last'] = rec['last']
            _write(p, old)
            return old.get('id')
    _write(_dir() / f"crash-{rec['id']}.json", rec)
    _prune('crash-*.json')
    return rec['id']


def record(kind: str, exc, flags=(), log=None, page=None, frames=None,
           freeze: bool = False) -> str | None:
    """Keep one crash: `exc` is an exception (its traceback is used) or a
    text (a native dump). `flags` are option names only (--install --json),
    never values; `log` the recent log lines; `page` the window's page;
    `frames` the Nexus frames when a text's own would mislead (a freeze:
    the UI thread's, not every thread's); `freeze` marks a native-crash
    record as a freeze (`freeze: true`; old records have no key).
    Returns the crash id, or None when nothing could be written."""
    try:
        if isinstance(exc, BaseException):
            tname = type(exc).__name__
            message = str(exc)
            tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            frames = frames_of(exc)
        else:
            tb = str(exc or '')
            first = next((ln for ln in tb.splitlines() if ln.strip()), '')
            m = re.match(r'(?:Fatal Python error|qFatal|Fatal|Freeze):\s*(.+)', first.strip())
            tname = (m.group(1) if m else first.strip() or 'unknown')[:80]
            message = first.strip()[:300]
            frames = list(frames) if frames is not None else frames_in(tb)
        now = _now()
        rec = {'schema': 1, 'id': _new_id(), 'kind': kind if kind in KINDS else 'user-report',
               'type': tname, 'message': message[:2000], 'traceback': tb[-20_000:],
               'frames': frames, 'signature': signature(tname, frames),
               'flags': [str(f) for f in flags or () if str(f).startswith('-')][:40],
               'log': [str(x)[:500] for x in (log or ())][-LOG_LINES:],
               'page': str(page) if page else None,
               'count': 1, 'first': now, 'last': now, **_app()}
        if freeze:
            rec['freeze'] = True
        return _store(rec)
    except Exception as e:            # noqa: BLE001 — the last line of defence
        _say(f"couldn't keep the crash report ({type(e).__name__})")
        return None


# ---- native crashes -------------------------------------------------------

def native_path() -> Path | None:
    return _native['path']


def enable_native() -> Path | None:
    """faulthandler for this process into REPORTS_DIR/native-<pid>.txt
    (0600). Pair with disable_native() on a clean exit (atexit does it
    too). Returns the file, or None when it couldn't be set up."""
    if _native['fh'] is not None:
        return _native['path']
    try:
        import atexit
        import faulthandler
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"native-{os.getpid()}.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        fh = os.fdopen(fd, 'w', encoding='utf-8')
        try:
            from nexus import platform
            platform.keep_private(path)
        except Exception:             # noqa: BLE001 — still better than no file
            pass
        was_on = faulthandler.is_enabled()          # python -X faulthandler
        faulthandler.enable(file=fh, all_threads=True)
        _native.update(fh=fh, path=path, was_on=was_on, pos=_fd_pos(fh))
        atexit.register(disable_native)
        return path
    except Exception as e:            # noqa: BLE001
        _say(f"native crash reports are off ({type(e).__name__})")
        return None


def native_note(text: str) -> None:
    """One line into the native file (a qFatal message before the abort).
    Any thread; a dump written since our last line is remembered first."""
    with _lock:
        fh = _native['fh']
        if fh is None:
            return
        try:
            _notice_dump(fh)
            fh.write(str(text).rstrip('\n') + '\n')
            fh.flush()
            _native['pos'] = _fd_pos(fh)
        except Exception:             # noqa: BLE001
            pass


def _fd_pos(fh) -> int:
    """Where the file's descriptor is: faulthandler writes through it too."""
    return os.lseek(fh.fileno(), 0, os.SEEK_CUR)


def _notice_dump(fh) -> None:
    """Under _lock: the descriptor moved by bytes we didn't write = a dump."""
    if _fd_pos(fh) != _native['pos']:
        _native['dumped'] = True


def watch_stalls() -> bool:
    """Start the UI watchdog. Call from the UI thread after enable_native(),
    then stall_beat() from a timer on that thread every BEAT_SECONDS. False
    when there is no native file."""
    if _native['fh'] is None:
        return False
    try:
        if not _native['watch']:
            native_note(f"{UI_MARK} {threading.get_ident():#x}: a dump after "
                        f"\"Timeout\" means it didn't answer for {STALL_SECONDS} s")
            _native['watch'] = True
        stall_beat()
        return True
    except Exception as e:            # noqa: BLE001
        _say(f"the freeze watchdog is off ({type(e).__name__})")
        return False


def stall_beat() -> None:
    """The UI thread is answering: push the dump STALL_SECONDS further off.
    If one was written since the last beat, say that it answered again.
    Cancelling first waits for a dump still being written, so our line
    never lands inside one."""
    with _lock:
        fh = _native['fh']
        if fh is None or not _native['watch']:
            return
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
            now = time.monotonic()
            _notice_dump(fh)
            if _native['dumped']:
                native_note(f"{RECOVERED} after {now - _native['beat']:.0f} s")
                _native['dumped'] = False
            _native['beat'] = now
            faulthandler.dump_traceback_later(STALL_SECONDS, repeat=False, file=fh,
                                              exit=False)
        except Exception:             # noqa: BLE001
            pass


def stall_cancel() -> None:
    """Stop the watchdog (right after the event loop ends, and before the
    file closes under its thread)."""
    with _lock:
        if not _native['watch']:
            return
        _native['watch'] = False
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
        except Exception:             # noqa: BLE001
            pass


def disable_native() -> None:
    fh, path = _native['fh'], _native['path']
    if fh is None:
        return
    stall_cancel()                    # before the file closes under its thread
    _native.update(fh=None, path=None, pos=0, dumped=False)
    try:
        import faulthandler
        faulthandler.disable()
        if _native['was_on'] and sys.__stderr__ is not None:
            faulthandler.enable(file=sys.__stderr__, all_threads=True)
    except Exception:                 # noqa: BLE001
        pass
    for step in (fh.close, lambda: path.unlink(missing_ok=True)):
        try:
            step()
        except Exception:             # noqa: BLE001
            pass


def native_reading(text: str):
    """What a dead process's native file says, as (text, frames) for
    record(), or None when there's nothing to report. Only what follows the
    last "answered again" line counts, without our own '# Nexus' lines. A
    faulthandler timeout dump with no crash after it is a freeze the user
    had to end: typed "Nexus stopped answering", with the UI thread's frames
    and stack first. A crash after a dump is the crash."""
    text = text or ''
    if MARK not in text and not _STALL_HEAD.search(text):
        return (text, None) if text.strip() else None      # as before the watchdog
    # Windows: the fd is in text mode, so our notes end \r\r\n, a dump \r\n.
    text = text.replace('\r\r\n', '\n').replace('\r\n', '\n')
    ui = re.search(rf'^{re.escape(UI_MARK)} (0x[0-9a-fA-F]+)', text, re.M)
    at = text.rfind(RECOVERED)
    tail = text if at < 0 else text[at:].partition('\n')[2]
    lines = [ln for ln in tail.splitlines() if not ln.startswith(MARK)]
    # Lines before the first header are the rest of a dump an answer line
    # raced into: no report of their own.
    first = next((i for i, ln in enumerate(lines)
                  if _STALL_HEAD.match(ln) or ln.lstrip().startswith(_HEADS)), None)
    if first is None:
        return None
    body = '\n'.join(lines[first:])
    stall = _STALL_HEAD.search(body)
    if stall is None:
        return body.strip('\n') + '\n', None
    before, dump = body[:stall.start()].strip(), body[stall.start():]
    lines = dump.splitlines()
    fatal = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(_FATAL)), None)
    if fatal is not None:                 # it crashed while stuck: the crash it is
        crash_part = '\n'.join(lines[fatal:])
        earlier = '\n'.join(x for x in (before, '\n'.join(lines[:fatal]).strip()) if x)
        return (crash_part + '\n\nBefore it, Nexus had stopped answering:\n' + earlier
                + '\n'), frames_in(crash_part)
    section = _thread_section(dump, int(ui.group(1), 16)) if ui else ''
    head = (f"{FREEZE}\nThe window didn't answer for {STALL_SECONDS} s or more and was "
            f"closed before it did. Every thread's stack at {STALL_SECONDS} s:")
    text_out = head + '\n' + (section + '\n\n' if section else '') + dump.rstrip() + '\n'
    if before:                            # a Qt message just before, say
        text_out += '\nBefore it:\n' + before + '\n'
    return text_out, frames_in(section or dump)


def _thread_section(dump: str, ident: int) -> str:
    """One thread's part of a faulthandler dump (its header to the blank line)."""
    out, on = [], False
    for ln in dump.splitlines():
        m = _THREAD_HEAD.match(ln)
        if m:
            on = int(m.group(1), 16) == ident
            if on:
                out = [ln]
            continue
        if on:
            if not ln.strip():
                break
            out.append(ln)
    return '\n'.join(out)


def _alive_pids() -> set[int]:
    from nexus import platform
    return {p.pid for p in platform.processes()}


def harvest() -> list[str]:
    """native-<pid>.txt files: a dead process's non-empty file becomes a
    native-crash record and goes; an empty one just goes; a live process's
    file (this one, another window, a CLI) stays untouched."""
    out = []
    try:
        files = [p for p in _files('native-*.txt')
                 if re.fullmatch(r'native-\d+\.txt', p.name)]
        if not files:
            return out
        alive = _alive_pids() | {os.getpid()}
        for p in files:
            pid = int(p.stem.split('-', 1)[1])
            if pid in alive:
                continue
            try:
                text = p.read_bytes().decode('utf-8', 'replace')
            except OSError:
                continue
            reading = native_reading(text)
            if reading is not None:
                cid = record('native-crash', reading[0], frames=reading[1],
                             freeze=reading[0].startswith(FREEZE))
                if cid is None:
                    continue              # keep it for the next try
                out.append(cid)
            try:
                p.unlink()
            except OSError:
                pass
    except Exception as e:            # noqa: BLE001
        _say(f"couldn't read old crash files ({type(e).__name__})")
    return out


# ---- reading them back ----------------------------------------------------

def load(crash_id: str) -> dict | None:
    if not isinstance(crash_id, str) or not CRASH_ID.match(crash_id):
        return None
    return _read(_dir() / f"crash-{crash_id}.json")


def dismiss(crash_id: str) -> bool:
    """Delete one crash file (reported, or the user said no)."""
    if not isinstance(crash_id, str) or not CRASH_ID.match(crash_id):
        return False
    try:
        (_dir() / f"crash-{crash_id}.json").unlink()
        return True
    except OSError:
        return False


SUMMARY = ('id', 'kind', 'type', 'signature', 'count', 'first', 'last', 'version', 'edition',
           'freeze')


def pending() -> list[dict]:
    """harvest(), then a summary of every crash file, newest first."""
    harvest()
    out = []
    for p in reversed(_files('crash-*.json')):
        rec = _read(p)
        if rec and CRASH_ID.match(str(rec.get('id') or '')):
            out.append({k: rec.get(k) for k in SUMMARY})
    return out


def save_draft(draft: dict) -> str | None:
    try:
        did = draft.get('id') if DRAFT_ID.match(str(draft.get('id') or '')) \
            else os.urandom(6).hex()
        draft['id'] = did
        _write(_dir() / f"draft-{did}.json", draft)
        _prune('draft-*.json')
        return did
    except Exception as e:            # noqa: BLE001
        _say(f"couldn't save the report draft ({type(e).__name__})")
        return None


def load_draft(draft_id: str) -> dict | None:
    if not isinstance(draft_id, str) or not DRAFT_ID.match(draft_id):
        return None
    return _read(_dir() / f"draft-{draft_id}.json")
