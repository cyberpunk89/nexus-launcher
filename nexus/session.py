"""System tuning for the length of a play session, put back afterwards:

- a sched_ext scheduler in its gaming mode (CachyOS's scx_loader, through
  `scxctl`): started when none runs, else the running one switched to
  gaming — and on exit stopped / switched back to the mode it had;
- a LACT GPU profile (`lact cli profile set`), and the previous one again
  on exit.

Configured in config.toml, off by default:

    [session]
    scx = "lavd"              # scheduler to start when none is running
    lact_profile = "Gaming"   # a profile you made in LACT

The power profile isn't here: the game-performance wrapper (Performance
settings → Power mode) already raises and restores it. Every step is
best-effort; a failure is reported in the launch log, never fatal."""

import re
import shutil
import subprocess
import tomllib

from .paths import CONFIG_PATH

_SCX_RUNNING = re.compile(r'running\s+(?:scx_)?(\w+)\s+in\s+(\w+)\s+mode', re.I)


def settings() -> dict:
    try:
        data = tomllib.loads(CONFIG_PATH.read_text()).get('session') or {}
    except (OSError, ValueError):
        data = {}
    scx = str(data.get('scx') or '').strip().lower().removeprefix('scx_')
    lact = str(data.get('lact_profile') or '').strip()
    return {'scx': scx if re.fullmatch(r'\w+', scx) and scx != 'off' else None,
            'lact_profile': lact or None}


def _run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def scx_current() -> tuple[str, str] | None | bool:
    """(scheduler, mode) when one runs, None when none does, False when
    scx_loader can't be asked."""
    r = _run(['scxctl', 'get'])
    if r is None or r.returncode != 0:
        return False
    m = _SCX_RUNNING.search(r.stdout)
    if m:
        return m.group(1).lower(), m.group(2).lower()
    return None if 'no scx scheduler' in r.stdout.lower() else False


def lact_current() -> str | None:
    r = _run(['lact', 'cli', 'profile', 'get'])
    out = (r.stdout.strip().splitlines() or [''])[0].strip() if r and r.returncode == 0 else ''
    return out or None


def enter(cfg: dict | None = None, say=print) -> dict:
    """Apply the session tuning; returns what leave() needs to undo it."""
    cfg = cfg or settings()
    undo = {}
    if cfg.get('scx'):
        cur = scx_current()
        if cur is False:
            say("# scx: scxctl / scx_loader not available")
        elif cur is None:
            r = _run(['scxctl', 'start', '-s', cfg['scx'], '-m', 'gaming'])
            if r is not None and r.returncode == 0:
                undo['scx'] = None
                say(f"# scx: started {cfg['scx']} in gaming mode")
            else:
                say(f"# scx: couldn't start {cfg['scx']}: {(r.stderr if r else '').strip()}")
        elif cur[1] != 'gaming':
            r = _run(['scxctl', 'switch', '-s', cur[0], '-m', 'gaming'])
            if r is not None and r.returncode == 0:
                undo['scx'] = cur
                say(f"# scx: {cur[0]} switched from {cur[1]} to gaming mode")
    if cfg.get('lact_profile'):
        prev = lact_current()
        if prev is None:
            say("# LACT: not available")
        elif prev != cfg['lact_profile']:
            r = _run(['lact', 'cli', 'profile', 'set', cfg['lact_profile']])
            if r is not None and r.returncode == 0:
                undo['lact'] = prev
                say(f"# LACT: profile {cfg['lact_profile']} (was {prev})")
            else:
                say(f"# LACT: couldn't set {cfg['lact_profile']}: "
                    f"{(r.stderr if r else '').strip()}")
    return undo


def leave(undo: dict, say=print) -> None:
    if 'scx' in undo:
        prev = undo['scx']
        cmd = (['scxctl', 'stop'] if prev is None
               else ['scxctl', 'switch', '-s', prev[0], '-m', prev[1]])
        r = _run(cmd)
        say("# scx: " + ("restored" if r is not None and r.returncode == 0
                         else f"couldn't restore ({' '.join(cmd)})"))
    if undo.get('lact'):
        r = _run(['lact', 'cli', 'profile', 'set', undo['lact']])
        say(f"# LACT: back to {undo['lact']}" if r is not None and r.returncode == 0
            else f"# LACT: couldn't restore {undo['lact']}")
