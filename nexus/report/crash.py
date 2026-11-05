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
    disable_native()            a clean exit: stop, close, delete it
    harvest() -> [id]           native files left by dead processes become
                                `native-crash` records
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
_native = {'fh': None, 'path': None, 'was_on': False}


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


def record(kind: str, exc, flags=(), log=None, page=None) -> str | None:
    """Keep one crash: `exc` is an exception (its traceback is used) or a
    text (a native dump). `flags` are option names only (--install --json),
    never values; `log` the recent log lines; `page` the window's page.
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
            m = re.match(r'(?:Fatal Python error|qFatal|Fatal):\s*(.+)', first.strip())
            tname = (m.group(1) if m else first.strip() or 'unknown')[:80]
            message = first.strip()[:300]
            frames = frames_in(tb)
        now = _now()
        rec = {'schema': 1, 'id': _new_id(), 'kind': kind if kind in KINDS else 'user-report',
               'type': tname, 'message': message[:2000], 'traceback': tb[-20_000:],
               'frames': frames, 'signature': signature(tname, frames),
               'flags': [str(f) for f in flags or () if str(f).startswith('-')][:40],
               'log': [str(x)[:500] for x in (log or ())][-LOG_LINES:],
               'page': str(page) if page else None,
               'count': 1, 'first': now, 'last': now, **_app()}
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
        _native.update(fh=fh, path=path, was_on=was_on)
        atexit.register(disable_native)
        return path
    except Exception as e:            # noqa: BLE001
        _say(f"native crash reports are off ({type(e).__name__})")
        return None


def native_note(text: str) -> None:
    """One line into the native file (a qFatal message before the abort)."""
    fh = _native['fh']
    if fh is None:
        return
    try:
        fh.write(str(text).rstrip('\n') + '\n')
        fh.flush()
    except Exception:                 # noqa: BLE001
        pass


def disable_native() -> None:
    fh, path = _native['fh'], _native['path']
    if fh is None:
        return
    _native.update(fh=None, path=None)
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
            if text.strip():
                cid = record('native-crash', text)
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


SUMMARY = ('id', 'kind', 'type', 'signature', 'count', 'first', 'last', 'version', 'edition')


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
