"""The background agent, `nexus-cli --agent`: what the Nexus window does on
its timers, for when no window is open. It watches the repack folders and
the torrent client (and installs a finished download that asked for it),
keeps the FitGirl feed and catalogue fresh, fills missing artwork once a
day, fills the game facts index a few games at a time (nexus/facts.py), and
sends alerts (nexus/alerts.py). Meant to run as a systemd user
service (`--agent-enable`); one at a time (an flock on DATA_DIR/agent.lock).

With a Nexus window open, the agent hands it what it finds: a finished
download (`--agent-ready`) or an install to start (`--agent-install`), so
those show as the window's own toasts and jobs. Without one it says so with
a desktop notification (platform.notify_local; alerts push to the phone)
and installs by itself. Both sides decide through the same locked files
(watch.py, torrent.claim_install, alerts.json), so nothing is announced or
installed twice.

Windows has no systemd: `--agent-enable` there puts a shortcut in the
Startup folder that starts the window in the tray (`nexus --hidden`), and
the window does this work on its own timers while hidden. `--agent` still
runs this console agent for anyone who wants one."""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from nexus import features, instance, paths as NX, platform

from .common import CLI_BIN, DATA_DIR, LOG_DIR

AGENT_LOCK = instance.AGENT_LOCK
AGENT_STATE = DATA_DIR / 'agent-state.json'
UNIT_NAME = 'nexus-agent.service'
UNIT_DIR = NX.CONFIG_DIR.parent / 'systemd' / 'user'

# Seconds between runs of each task (torrents set their own pace).
EVERY = {'scan': 60, 'scan_waiting': 30, 'scan_full': 600, 'feed': 1800, 'index': 1800,
         'deals': 2700, 'proton': 6 * 3600, 'art': 6 * 3600, 'facts': 900, 'facts_idle': 3600}
FACTS_BATCH = 10          # per site per run: 40 an hour at EVERY['facts']
TICK = 1.0


def _take_lock():
    """The agent's exclusive lock, or None when another agent has it. A few
    tries: a GUI probing agent_running() holds it for a moment."""
    AGENT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(AGENT_LOCK, 'a')
    for _ in range(10):
        try:
            platform.lock(fh, blocking=False)
            return fh
        except OSError:
            time.sleep(0.2)
    fh.close()
    return None


def _say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def _desktop(title: str, body: str) -> None:
    try:
        platform.notify_local(title, body)
    except OSError as e:
        _say(f"[!] desktop notification: {e}")


def playing() -> bool:
    """A game is running (Steam's reaper or a Nexus launch tracker)."""
    from nexus import steamlib
    try:
        r = steamlib.running()
    except OSError:
        return False
    return bool(r.get('appids') or r.get('slugs'))


def install_cmd(path: str, h: str, config: dict) -> list[str]:
    """The install the window's one-click toast would start (its Settings
    defaults), for a finished download."""
    cmd = [*NX.cli_command(), path, '--yes', '--log', '--defer-steam']
    if not config.get('unattended', True):
        cmd.append('--interactive')
    if protongen_default_config() is not None:
        cmd.append('--use-protongen-default')
    return cmd + ['--from-torrent', h]


class Agent:
    def __init__(self, base: Path):
        self.base = base
        self.stop = False
        self.due: dict[str, float] = {}         # task -> monotonic time it runs next
        self.sizes: dict[str, int] = {}         # watch.triage's size memory
        self.skip: set[str] = set()             # folders being installed from a torrent
        self.scan_sig = None
        self.scan_waiting = False
        self.last_full_scan = 0.0
        self.children: list[tuple[subprocess.Popen, str]] = []
        self.ran: dict[str, str] = {}

    # ---- the loop ----
    def run(self) -> None:
        _say(f"Nexus agent started (pid {os.getpid()})")
        now = time.monotonic()
        # Staggered first runs, like the window's startup timers.
        for i, task in enumerate(('torrents', 'scan', 'feed', 'index', 'deals', 'proton',
                                  'art', 'facts')):
            self.due[task] = now + 5 + 10 * i
        last_state = 0.0
        while not self.stop:
            now = time.monotonic()
            for task, when in sorted(self.due.items(), key=lambda kv: kv[1]):
                if self.stop or when > now:
                    continue
                try:
                    delay = getattr(self, 'task_' + task)()
                except Exception as e:          # noqa: BLE001 — one task never stops the agent
                    _say(f"[!] {task}: {type(e).__name__}: {e}")
                    delay = 300
                self.due[task] = time.monotonic() + delay
                self.ran[task] = time.strftime('%Y-%m-%dT%H:%M:%S')
            self.reap()
            if now - last_state >= 60:
                self.write_state()
                last_state = now
            time.sleep(TICK)
        self.write_state(stopped=True)
        _say("Nexus agent stopped")

    def write_state(self, stopped: bool = False) -> None:
        from nexus import net
        net.save_json(AGENT_STATE, {
            'pid': None if stopped else os.getpid(),
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'ran': self.ran,
            'installing': [name for _p, name in self.children]})

    def reap(self) -> None:
        """Say how installs the agent ran by itself ended."""
        for proc, name in list(self.children):
            rc = proc.poll()
            if rc is None:
                continue
            self.children.remove((proc, name))
            if instance.window_running():
                continue                    # the window reloads and says so itself
            if rc == 0:
                _desktop(f"{name} is installed", "Open Nexus to play it.")
            else:
                _desktop(f"Installing {name} failed", "Open Nexus to see what went wrong.")

    # ---- tasks: each returns seconds until it runs again ----
    def task_torrents(self) -> float:
        from nexus import torrent
        s = torrent.settings()
        if not s['client']:
            return 300
        try:
            rows = torrent.status(s)
        except torrent.TorrentError as e:
            _say(f"[!] torrent client: {e}")
            return 120
        config = load_config()
        for r in rows:
            path = r.get('path')
            if r.get('done') and r.get('auto_install') and not r.get('installed') \
                    and not r.get('install_started') and path and Path(path).is_dir() \
                    and torrent.claim_install(r['hash']):
                self.install(r, config)
        busy = any(not r.get('done') and not r.get('gone') for r in rows)
        return 10 if busy else 60

    def install(self, r: dict, config: dict) -> None:
        path, h = r['path'], r['hash']
        name = r.get('title') or r.get('name') or Path(path).name
        self.skip.add(str(Path(path)))
        if instance.forward(['--agent-install', path, h]):
            _say(f"Handed {name} to the Nexus window to install")
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOG_DIR / f"agent-install-{time.strftime('%Y%m%d-%H%M%S')}.log", 'ab')
        with log:
            proc = subprocess.Popen(install_cmd(path, h, config), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    **platform.new_group())
        self.children.append((proc, name))
        _say(f"Installing {name} (pid {proc.pid})")
        _desktop(f"Installing {name}", "The download finished; Nexus is installing it.")

    def task_scan(self) -> float:
        from nexus import watch
        dirs = [d for d in resolve_scan_dirs(None) if d.is_dir()]
        sig = tuple((str(d), d.stat().st_mtime_ns) for d in dirs)
        now = time.monotonic()
        # A new folder changes its parent's mtime; a download filling one up
        # doesn't, so a waiting folder (and every 10 min, anything) is looked
        # at again regardless.
        if sig == self.scan_sig and not self.scan_waiting \
                and now - self.last_full_scan < EVERY['scan_full']:
            return EVERY['scan']
        self.scan_sig, self.last_full_scan = sig, now
        cands = [c for d in dirs for c in scan_repacks(d)]
        with watch.edit_seen() as box:
            ready, box['seen'], self.scan_waiting = watch.triage(
                cands, box['seen'], self.sizes, self.skip)
        for c in ready:
            if instance.forward(['--agent-ready', json.dumps(c)]):
                continue
            what = "A new release of" if c.get('update_for') else ""
            _desktop(f"{what} {c['name']} is ready to install".strip(),
                     "Open Nexus to install it.")
        return EVERY['scan_waiting'] if self.scan_waiting else EVERY['scan']

    def task_feed(self) -> float:
        from nexus import alerts as A, net, repackfeed
        if not load_config().get('repacks', True):
            return EVERY['feed']
        try:
            data = repackfeed.refresh()
        except net.NetError as e:
            _say(f"[!] FitGirl feed: {e}")
            return EVERY['feed']
        found = repackfeed.updates(load_registry()['games'].values(), data)
        send_alerts(A.fresh('repack_updates', A.update_alerts(found)),
                    desktop=not instance.window_running())
        return EVERY['feed']

    def task_index(self) -> float:
        from nexus import gamedb
        if not gamedb.configured() or not load_config().get('repacks', True):
            return EVERY['index']
        if playing():
            return 600                      # a full rebuild is a few minutes of requests
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_repack_index(False, True, desktop=not instance.window_running())
        return EVERY['index']

    def task_deals(self) -> float:
        from nexus import alerts as A, gamedb
        if not gamedb.configured():
            return EVERY['deals']
        try:
            res = gamedb.summary()
        except gamedb.GameDbError as e:
            _say(f"[!] Game-db: {e}")
            return EVERY['deals']
        wishlist = ((res or {}).get('data') or {}).get('wishlist') or []
        s = A.settings()
        new = []
        for kind, items in A.deal_alerts(wishlist, s).items():
            new += A.fresh(kind, items, s)
        send_alerts(new, desktop=not instance.window_running())
        if not playing():
            res = gamedb.sync_games()                   # itself at most every 6 h
            if res['status'] == 'failed':
                _say(f"[!] Game-db games: {res['error']['message']}")
        return EVERY['deals']

    def task_proton(self) -> float:
        from nexus import alerts as A
        latest, newer = ge_update()
        send_alerts(A.fresh('proton', A.proton_alerts(latest, newer)),
                    desktop=not instance.window_running())
        return EVERY['proton']

    def task_art(self) -> float:
        if not load_config().get('api_key') or playing():
            return EVERY['art']
        # Its own process (it's long, and stamps itself: at most once a day).
        subprocess.run([*NX.cli_command(), '--fill-art', '--if-stale', '--json'],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=1800)
        return EVERY['art']

    def task_facts(self) -> float:
        from nexus import facts
        if playing():
            return 600
        wait = facts.wait_seconds('steam')
        if wait:
            return max(wait, 60)
        # Its own process, like the art fill: a batch is a minute of requests.
        r = subprocess.run([*NX.cli_command(), '--game-facts', '--fill', str(FACTS_BATCH),
                            '--json'], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=900)
        try:
            left = (json.loads(r.stdout).get('fill') or {}).get('left')
        except (ValueError, AttributeError):
            left = None
        return EVERY['facts_idle'] if left == 0 else EVERY['facts']


def cmd_agent(base: Path) -> int:
    fh = _take_lock()
    if fh is None:
        print("The Nexus agent is already running.", file=sys.stderr)
        return 1
    agent = Agent(base)

    def stop(_sig, _frame):
        agent.stop = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, 'SIGBREAK'):           # Windows: Ctrl+Break to our group
        signal.signal(signal.SIGBREAK, stop)
    with fh:
        agent.run()
    return 0


# ── the systemd user service ─────────────────────────────────────────────

def unit_text() -> str:
    exe = str(CLI_BIN).replace('%', '%%')
    return f"""[Unit]
Description=Nexus background agent (downloads, repack updates, alerts)
After=graphical-session.target network-online.target

[Service]
Type=simple
ExecStart="{exe}" --agent
Restart=on-failure
RestartSec=30
Nice=10

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    if not shutil.which('systemctl'):
        return None
    try:
        return subprocess.run(['systemctl', '--user', *args], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


# ── Windows: "Start with Windows", a Startup-folder shortcut ─────────────

STARTUP_NAME = 'Nexus.lnk'          # its name in Task Manager's Startup apps


def startup_link() -> Path | None:
    """Where "Start with Windows" puts its shortcut; None on Linux."""
    d = platform.startup_dir(NX.HOME)
    return d / STARTUP_NAME if d else None


def set_startup(on: bool) -> None:
    """Create or remove the shortcut that starts Nexus in the tray at
    sign-in (`nexus --hidden`). Raises OSError."""
    link = startup_link()
    if link is None:
        raise OSError("there's no Startup folder here")
    if not on:
        link.unlink(missing_ok=True)
        return
    exe, *args = platform.gui_command(NX.REPO)
    platform.create_shortcut(link, Path(exe), args=subprocess.list2cmdline([*args, '--hidden']),
                             workdir=NX.REPO if args else Path(exe).parent,
                             icon=None if args else Path(exe))


def _agent_state() -> dict:
    try:
        return json.loads(AGENT_STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def agent_status() -> dict:
    state = _agent_state()
    if not features.has('systemd_agent'):
        link = startup_link()
        on = bool(link and link.is_file())
        return {'startup': True, 'systemd': False, 'installed': on, 'current': on,
                'enabled': on, 'active': on, 'path': str(link) if link else None,
                'running': instance.agent_running() or instance.window_running(),
                'updated_at': state.get('updated_at'), 'ran': state.get('ran') or {},
                'installing': state.get('installing') or []}
    unit = UNIT_DIR / UNIT_NAME
    enabled = _systemctl('is-enabled', UNIT_NAME)
    active = _systemctl('is-active', UNIT_NAME)
    try:
        current = unit.read_text(encoding='utf-8') == unit_text()
    except OSError:
        current = False
    return {'installed': unit.exists(), 'current': current,
            'systemd': shutil.which('systemctl') is not None,
            'enabled': bool(enabled and enabled.stdout.strip() == 'enabled'),
            'active': bool(active and active.stdout.strip() == 'active'),
            'running': instance.agent_running(),
            'updated_at': state.get('updated_at'), 'ran': state.get('ran') or {},
            'installing': state.get('installing') or []}


def cmd_agent_enable(enable: bool, as_json: bool) -> int:
    unit = UNIT_DIR / UNIT_NAME
    errors = []
    if not features.has('systemd_agent'):
        try:
            set_startup(enable)
        except OSError as e:
            errors.append(f"couldn't {'add' if enable else 'remove'} the Startup shortcut: {e}")
    elif enable:
        try:
            if unit.exists():
                backup_file(unit)
            atomic_write(unit, unit_text())
        except OSError as e:
            errors.append(f"couldn't write {unit}: {e}")
        for args in (('daemon-reload',), ('enable', '--now', UNIT_NAME)):
            if errors:
                break
            r = _systemctl(*args)
            if r is None or r.returncode != 0:
                errors.append(f"systemctl --user {' '.join(args)}: "
                              f"{(r.stderr.strip() if r else 'systemctl not found') or 'failed'}")
    else:
        r = _systemctl('disable', '--now', UNIT_NAME)
        if unit.exists() and (r is None or r.returncode != 0):
            errors.append(f"systemctl --user disable: "
                          f"{(r.stderr.strip() if r else 'systemctl not found') or 'failed'}")
    status = agent_status()
    if as_json:
        print(json.dumps({**status, 'errors': errors}))
    else:
        for e in errors:
            print(f"  [!] {e}", file=sys.stderr)
        if not errors and status.get('startup'):
            print("Nexus " + ("starts in the tray when you sign in to Windows." if enable
                              else "no longer starts with Windows."))
        elif not errors:
            print("The Nexus agent is " + ("on: it runs in the background from now on."
                                           if enable else "off."))
    return 1 if errors else 0


def cmd_agent_status(as_json: bool) -> None:
    s = agent_status()
    if as_json:
        print(json.dumps(s))
        return
    if s.get('startup'):
        print(f"Start with Windows: {'on (' + s['path'] + ')' if s['enabled'] else 'off'}")
    else:
        print(f"Service: {'installed' if s['installed'] else 'not installed'}, "
              f"{'enabled' if s['enabled'] else 'disabled'}, "
              f"{'active' if s['active'] else 'inactive'}")
    print(f"Agent running: {'yes' if s['running'] else 'no'}"
          + (f" (last seen {s['updated_at']})" if s['updated_at'] else ''))
    for task, when in sorted(s['ran'].items()):
        print(f"  {task:<9} {when}")


def cmd_check_alerts(as_json: bool, dry_run: bool) -> None:
    """Check the deal and Proton alerts now (the agent does this on its own
    schedule; the window does it when no agent runs, so on Windows this is
    also where Game-db's game details sync, at most every 6 h). FitGirl and
    repack update alerts ride on --repack-index and the feed."""
    from nexus import alerts as A, gamedb, repackfeed
    s = A.settings()
    found: list[dict] = []
    if gamedb.configured():
        try:
            res = gamedb.summary(cache_only=dry_run)
        except gamedb.GameDbError:
            res = None
        for items in A.deal_alerts(((res or {}).get('data') or {}).get('wishlist') or [],
                                   s).values():
            found += items
        if not dry_run:
            gamedb.sync_games()
    found += A.update_alerts(repackfeed.updates(load_registry()['games'].values()))
    found += A.proton_alerts(*ge_update())
    if dry_run:
        new = found
    else:
        new = []
        for kind in A.KINDS:
            new += A.fresh(kind, [i for i in found if i['kind'] == kind], s)
        send_alerts(new)
    if as_json:
        print(json.dumps({'alerts': new, 'dry_run': dry_run}))
        return
    if not new:
        print("Nothing new to alert about." if not dry_run else "No alerts would apply.")
    for i in new:
        print(f"  [{i['kind']}] {i['text']}")


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .common import atomic_write, backup_file  # noqa: E402
from .config import load_config, resolve_scan_dirs  # noqa: E402
from .downloads import cmd_repack_index, send_alerts  # noqa: E402
from .gamecmds import scan_repacks  # noqa: E402
from .launchprofile import protongen_default_config  # noqa: E402
from .protoncmds import ge_update  # noqa: E402
from .registry import load_registry  # noqa: E402
