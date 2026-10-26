"""Jobs (every CLI action is one), JSON queries to the CLI, worker threads."""

import codecs
import json
import re
import shlex
import time

from PyQt6.QtCore import (pyqtSignal, QObject, QProcess, QProcessEnvironment, QRunnable,
    QThreadPool, QTimer)

from nexus import platform, steamlib

from .common import CLI
from .fmt import fmt_bytes


# Install progress. The CLI prints a step marker at the start of a line
# ("[1/4] Verifying files...", "[3/4] Running setup.exe ...") and, during
# --verify, indented per-file markers ("  [12/40] data.bin: OK").
_STEP_RE = re.compile(r'^\[(\d+)/(\d+)\]\s*(.*)$')
_FILE_RE = re.compile(r'^\s+\[(\d+)/(\d+)\]')
_INSTALL_STEPS = {1: "Checking files", 2: "Setting up the prefix",
                  3: "Installer running — finish it in its window",
                  4: "Adding to your launchers"}
_SILENT_STEP = "Installing — no setup window needed"
# A step that runs an installer as administrator (Windows: "[2/3] Running
# setup.exe as administrator..."). Nexus can't stop it while it runs.
_ELEVATED_RE = re.compile(r'\bas administrator\b')
ELEVATED_TIP = "The installer runs as admin; close it from its own window"
# Machine-readable lines from the CLI: "@nexus <event> <json>".
_EVENT_RE = re.compile(r'^@nexus ([\w-]+) (\{.*\})\s*$')


def _cli_env() -> 'QProcessEnvironment':
    env = QProcessEnvironment.systemEnvironment()
    env.insert('PYTHONUNBUFFERED', '1')      # stream status lines, don't block-buffer
    return env


class Job(QObject):
    """One CLI invocation: its state, a headline step, a detail line (last
    output), progress, and its own log. `kind` groups behaviour: 'install',
    'launch' (quick, runs alongside others), 'open' (a steam:// or heroic://
    hand-off that isn't a launch), 'tool', or 'task'."""
    changed = pyqtSignal()
    output = pyqtSignal(str)
    done = pyqtSignal(int)

    PARALLEL = ('launch', 'open', 'tool')
    _cancel_event = None                    # Windows: what Stop sets, see start()

    def __init__(self, argv, title, slug=None, kind='task', name=None):
        super().__init__()
        self.argv, self.title, self.slug, self.kind = argv, title, slug, kind
        self.name = name or title
        self.state = 'queued'            # queued | running | done | failed | cancelled
        self.step = 'Waiting for the current job…'
        self.detail = ''
        self.progress: tuple[int, int] | None = None   # None = indeterminate
        self.log: list[str] = []
        self.code: int | None = None
        self.started = self.ended = None
        self.proc = None
        self._decoder = None
        self._buf = ''
        self.events: dict[str, dict] = {}     # last @nexus event of each kind
        self._rate: tuple[float, int] | None = None
        self.elevated = False            # an admin installer is running: no Stop
        self._change_due = False

    # Output can arrive hundreds of times a second (an installer's log); the
    # step/detail/progress it moves is announced at most ~8 times a second.
    CHANGE_MS = 120

    def _changed_soon(self):
        if not self._change_due:
            self._change_due = True
            if not hasattr(self, '_change_timer'):   # a child: it dies with the job
                self._change_timer = QTimer(self)
                self._change_timer.setSingleShot(True)
                self._change_timer.timeout.connect(self._flush_change)
            self._change_timer.start(self.CHANGE_MS)

    def _flush_change(self):
        if self._change_due:
            self._change_due = False
            self.changed.emit()

    # ---- lifecycle ----
    def start(self):
        self.state, self.started = 'running', time.monotonic()
        self.step = {'install': "Starting…", 'launch': "Launching…"}.get(self.kind, "Working…")
        self.proc = QProcess(self)
        env = _cli_env()
        # Windows: no signal reaches the CLI alone (PyQt6 can't give it a
        # process group), so Stop sets this event, which the CLI watches.
        # No console window either way: QProcess adds CREATE_NO_WINDOW when
        # Nexus has no console of its own.
        self._cancel_event = platform.cancel_event()
        if self._cancel_event is not None:
            env.insert(platform.CANCEL_EVENT_ENV, self._cancel_event.name)
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        # Incremental: a multi-byte UTF-8 character split across two reads
        # would otherwise decode as two replacement characters.
        self._decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.proc.readyReadStandardOutput.connect(self._drain)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self._emit_text("$ " + " ".join(shlex.quote(a) for a in self.argv) + "\n")
        self.proc.start(self.argv[0], self.argv[1:])
        # EOF on stdin: any unexpected prompt takes its safe default, not a hang.
        self.proc.closeWriteChannel()
        self.changed.emit()

    def cancel(self):
        if self.state == 'queued':
            self._end('cancelled', -1)
        elif self.state == 'running' and self.proc is not None and self.stoppable:
            # SIGTERM (the cancel event on Windows): the CLI stops whatever
            # it's waiting on (umu-run/wine/an unpacker) too; a kill follows
            # if it hasn't exited in 5 s.
            self.state = 'cancelling'
            self.step = "Cancelling…"
            self.changed.emit()
            proc = self.proc
            if not (self._cancel_event is not None and self._cancel_event.set()):
                proc.terminate()
            # A child timer dies with the process object: a lambda on a bare
            # singleShot outlived it and aborted the app (deleted QProcess).
            # kill() on a process that has already exited does nothing.
            kill = QTimer(proc)
            kill.setSingleShot(True)
            kill.timeout.connect(proc.kill)
            kill.start(5000)

    @property
    def stoppable(self) -> bool:
        """False while an elevated installer runs: the CLI can't stop it, and
        stopping the CLI would leave it running unwatched."""
        return not self.elevated

    @property
    def active(self) -> bool:
        return self.state in ('queued', 'running', 'cancelling')

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return (self.ended or time.monotonic()) - self.started

    # ---- process plumbing ----
    def _drain(self):
        if self.proc is None or self._decoder is None:
            return
        text = self._decoder.decode(bytes(self.proc.readAllStandardOutput()))
        if text:
            self._emit_text(text)

    def _emit_text(self, text: str):
        self.log.append(text)
        if len(self.log) > 4000:
            del self.log[:1000]
        self.output.emit(text)
        self._buf += text.replace('\r', '\n')
        *lines, self._buf = self._buf.split('\n')
        before = (self.step, self.detail, self.progress)
        for line in lines:
            self._parse(line)
        if (self.step, self.detail, self.progress) != before:
            self._changed_soon()

    def _parse(self, line: str):
        m = _EVENT_RE.match(line)
        if m:
            try:
                data = json.loads(m.group(2))
            except ValueError:
                return
            self.events[m.group(1)] = data
            if m.group(1) == 'progress':
                self._on_progress(int(data.get('written') or 0))
            return
        m = _STEP_RE.match(line)
        if m:
            n, total = int(m.group(1)), int(m.group(2))
            elevated = bool(_ELEVATED_RE.search(m.group(3)))
            # The installer's own step: [3/4] of a repack on Linux; on Windows
            # the one that runs as administrator ([2/3] of a repack, [1/2] of
            # --import, silent only when it says so).
            running = self.kind == 'install' and ((total == 4 and n == 3) or elevated)
            silent = running and '--interactive' not in self.argv and (
                not elevated or '--import' not in self.argv or 'silently' in m.group(3))
            named = self.kind == 'install' and (total == 4 or (n == 1 and '--import'
                                                               not in self.argv))
            self.step = (_SILENT_STEP if silent else _INSTALL_STEPS[3] if running else
                         _INSTALL_STEPS.get(n) if named else None) \
                or m.group(3).strip().rstrip('.')
            self.progress = None if running else (n, total)
            self.detail = ''
            if elevated != self.elevated:
                self.elevated = elevated
                self._changed_soon()
            return
        m = _FILE_RE.match(line)
        if m:
            self.progress = (int(m.group(1)), int(m.group(2)))
            self.detail = line.strip()
            return
        text = line.strip()
        if text and not text.startswith(('$ ', '=')):
            self.detail = text[:160]

    def _on_progress(self, written: int):
        """Silent installs report bytes written; show that and the speed."""
        now = time.monotonic()
        text = f"{fmt_bytes(written)} written"
        if self._rate and now > self._rate[0] and written >= self._rate[1]:
            speed = (written - self._rate[1]) / (now - self._rate[0])
            if speed > 0:
                text += f" · {fmt_bytes(speed)}/s"
        self._rate = (now, written)
        self.detail = text

    def _finished(self, code, _status):
        self._drain()
        if self._buf.strip():
            self._parse(self._buf)
        state = ('cancelled' if self.state == 'cancelling' else
                 'done' if code == 0 else 'failed')
        self._end(state, code)

    def _error(self, err):
        # FailedToStart never emits finished(); without this the job would
        # stay "running" forever.
        if err == QProcess.ProcessError.FailedToStart:
            self._emit_text(f"[!] Could not start {self.argv[0]}: {self.proc.errorString()}\n")
            self._end('failed', -1)

    def _end(self, state, code):
        if not self.active:
            return
        self.state, self.code, self.ended = state, code, time.monotonic()
        self.step = {'done': "Done", 'failed': f"Failed (exit {code})",
                     'cancelled': "Cancelled"}[state]
        if state == 'failed' and self.detail:
            self.step = f"Failed — {self.detail}"
        self.progress = None
        if self._cancel_event is not None:
            self._cancel_event.close()
            self._cancel_event = None
        self._change_due = False               # the final state goes out now
        self.changed.emit()
        self.done.emit(code)


class JobManager(QObject):
    """Runs jobs one at a time (installs and registry edits must not overlap),
    except quick 'launch'/'tool' jobs, which start immediately."""
    added = pyqtSignal(object)
    finished = pyqtSignal(object)
    changed = pyqtSignal()
    output = pyqtSignal(object, str)
    pruned = pyqtSignal(list)                  # finished jobs dropped from the list

    KEEP_FINISHED = 40                         # a long session doesn't grow forever

    def __init__(self):
        super().__init__()
        self.jobs: list[Job] = []

    def submit(self, job: Job) -> Job:
        self.jobs.append(job)
        job.changed.connect(self.changed)
        job.output.connect(lambda text, j=job: self.output.emit(j, text))
        job.done.connect(lambda _c, j=job: self._on_done(j))
        self.added.emit(job)
        if job.kind in Job.PARALLEL:
            job.start()
        else:
            self._pump()
        self.changed.emit()
        return job

    def _on_done(self, job):
        self.finished.emit(job)
        self._pump()
        done = [j for j in self.jobs if not j.active]
        if len(done) > self.KEEP_FINISHED:
            old = done[:len(done) - self.KEEP_FINISHED]
            gone = set(map(id, old))
            self.jobs = [j for j in self.jobs if id(j) not in gone]
            self.pruned.emit(old)
        self.changed.emit()

    def _pump(self):
        if any(j.state in ('running', 'cancelling') and j.kind not in Job.PARALLEL
               for j in self.jobs):
            return
        nxt = next((j for j in self.jobs if j.state == 'queued'), None)
        if nxt:
            nxt.start()

    def active(self) -> list[Job]:
        return [j for j in self.jobs if j.active and j.kind not in Job.PARALLEL]

    def for_slug(self, slug) -> Job | None:
        """The job to show on a game's tile: its queued/running task, or a
        launch that is still starting (a quick spinner while the game boots)."""
        return next((j for j in self.jobs if j.active and j.slug == slug
                     and j.kind not in ('tool', 'open')), None)

    def tile_jobs(self) -> dict:
        """for_slug() for every slug at once, in one pass over the jobs — the
        views ask for every tile several times a second while a job runs."""
        out = {}
        for j in self.jobs:
            if j.active and j.slug is not None and j.kind not in ('tool', 'open'):
                out.setdefault(j.slug, j)
        return out

    def busy_slugs(self) -> frozenset:
        """Every slug busy() is true for, in one pass."""
        return frozenset(j.slug for j in self.jobs
                         if j.active and j.slug is not None and j.kind in ('install', 'task'))

    def find(self, argv) -> Job | None:
        """An active job running exactly this command — a double click, or the
        same action asked for from two places."""
        argv = list(argv)
        return next((j for j in self.jobs if j.active and j.argv == argv), None)

    def busy(self, slug) -> bool:
        """Whether a queued or running job is changing this game's files or
        registration (an install, update, uninstall, restore…)."""
        return any(j.active and j.slug == slug and j.kind in ('install', 'task')
                   for j in self.jobs)

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.active]
        self.changed.emit()


def _error_body(out: str) -> tuple[str, str]:
    """(message, kind) of a JSON `{"error": "text" | {"kind", "message"}}`
    body, else ('', '')."""
    try:
        e = json.loads(out).get('error')
    except (ValueError, AttributeError):
        return '', ''
    if isinstance(e, dict):
        msg, kind = e.get('message') or e.get('kind'), e.get('kind') or ''
    else:
        msg, kind = e, ''
    return (msg, kind) if isinstance(msg, str) and msg else ('', '')


class CliQuery(QObject):
    """Run a short read-only CLI command (e.g. --list --json) without blocking
    the GUI thread; `callback(stdout_text or None, error_text)` runs on
    completion. Kept alive by a class-level set until it finishes."""
    _live: set = set()

    def __init__(self, args, callback, timeout_ms=60000, error_body=False):
        super().__init__()
        self._cb = callback
        self._done = False
        self._error_body = error_body
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(_cli_env())
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        CliQuery._live.add(self)
        QTimer.singleShot(timeout_ms, self._timeout)
        self.proc.start(CLI[0], [*CLI[1:], *args])

    def _complete(self, out, err):
        if self._done:
            return
        self._done = True
        CliQuery._live.discard(self)
        try:
            self._cb(out, err)
        finally:
            self.deleteLater()

    def _finished(self, code, _status):
        out = bytes(self.proc.readAllStandardOutput()).decode('utf-8', 'replace')
        err = bytes(self.proc.readAllStandardError()).decode('utf-8', 'replace').strip()
        if code != 0 and 'Traceback (most recent call last)' in err:
            err = err.splitlines()[-1]         # the one-line summary, not the stack
        msg, kind = _error_body(out) if code != 0 and self._error_body else ('', '')
        if msg:
            # A --json command that failed but said why on stdout: hand that
            # body on with its message. A crash ('internal') has no useful
            # body, so callers keep seeing None for it.
            self._complete(None if kind == 'internal' else out, msg)
            return
        self._complete(out if code == 0 else None, err or f"exit {code}")

    def _error(self, e):
        if e == QProcess.ProcessError.FailedToStart:
            self._complete(None, f"could not run {CLI[-1]}: {self.proc.errorString()}")

    def _timeout(self):
        if not self._done:
            self.proc.kill()
            self._complete(None, "timed out")


class _Dispatch(QObject):
    """Carries pool results back to the GUI thread. One, created on the GUI
    thread and never deleted, so a worker never owns (or frees) a GUI-thread
    object; callbacks stay on the GUI thread, keyed by a token."""
    done = pyqtSignal(int, object, object)

    def __init__(self):
        super().__init__()
        self.callbacks: dict[int, object] = {}
        self.next = 0
        self.done.connect(self._fire)

    def _fire(self, token, result, error):
        callback = self.callbacks.pop(token, None)
        if callback is None:
            return
        try:
            callback(result, error)
        except RuntimeError:              # its window was closed meanwhile
            pass


class _Task(QRunnable):
    def __init__(self, fn, token):
        super().__init__()
        self.fn, self.token = fn, token

    def run(self):
        try:
            result, error = self.fn(), None
        except Exception as e:            # handed to the callback, never lost
            result, error = None, e
        self.fn = None                    # nothing of the caller's is freed here later
        _DISPATCH.done.emit(self.token, result, error)


_DISPATCH: '_Dispatch | None' = None


def in_thread(fn, callback):
    """Run fn() on Qt's thread pool and callback(result, error) on the GUI
    thread — for in-process reads (/proc, Steam manifests) that shouldn't
    cost a subprocess or block the window. fn must not touch widgets."""
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = _Dispatch()
    _DISPATCH.next += 1
    _DISPATCH.callbacks[_DISPATCH.next] = callback
    QThreadPool.globalInstance().start(_Task(fn, _DISPATCH.next))


def match_running(games: list[dict], r: dict) -> set:
    """Slugs of the library games in steamlib.running()'s snapshot: Steam's
    reaper AppId (Steam games and repack shortcuts), Nexus's own session
    trackers, a repack's exe name, or a Heroic game's install folder."""
    appids, slugs, exes = set(r['appids']), set(r['slugs']), set(r['exes'])
    out = set()
    for g in games:
        src = g.get('source', 'nexus')
        if src == 'nexus':
            exe = (g.get('exe') or '').replace('\\', '/').rsplit('/', 1)[-1].lower()
            if g['slug'] in slugs or int(g.get('steam_appid') or 0) in appids \
                    or (exe and exe in exes):
                out.add(g['slug'])
        elif src == 'steam':
            if g.get('appid') in appids:
                out.add(g['slug'])
        elif src == 'heroic':
            d = g.get('install_dir')
            if d and steamlib.in_dir_cmdline(d, r['paths']):
                out.add(g['slug'])
    return out


_CLI_MEMO: dict[tuple, tuple[float, dict]] = {}     # args -> (when, answer)
_CLI_WAITING: dict[tuple, list] = {}                 # args -> callbacks of a query in flight


def cli_cache_clear():
    """Forget remembered answers — after any job, which may have changed them."""
    _CLI_MEMO.clear()


def cli_json(args, callback, timeout_ms=60000, ttl: float = 0):
    """CliQuery that parses stdout as JSON: callback(dict_or_None, error).
    With `ttl` (seconds), a recent answer is handed back at once and an
    identical query already running is shared, so moving between pages or
    games doesn't start the same subprocess again."""
    key = tuple(args)
    if ttl:
        hit = _CLI_MEMO.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            callback(hit[1], '')
            return None
        if key in _CLI_WAITING:
            _CLI_WAITING[key].append(callback)
            return None
        _CLI_WAITING[key] = [callback]

    def deliver(data, err):
        cbs = _CLI_WAITING.pop(key, [callback]) if ttl else [callback]
        if ttl and data is not None and not err:
            _CLI_MEMO[key] = (time.monotonic(), data)
        for cb in cbs:
            try:
                cb(data, err)
            except RuntimeError:               # its widget is gone
                pass

    def done(out, err):
        if out is None:
            deliver(None, err)
            return
        try:
            data = json.loads(out)
        except ValueError as e:
            deliver(None, f"bad JSON from CLI: {e}")
            return
        deliver(data, err)            # err is set when the CLI failed with a JSON error body
    return CliQuery(args, done, timeout_ms, error_body=True)
