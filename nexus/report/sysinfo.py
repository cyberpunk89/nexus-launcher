"""What a bug report says about this machine.

    collect(ctx=None, crash=None, log_lines=None, game=None) -> draft
        every group, scrubbed: App, System, Graphics, Runtime (Doctor's
        Requirements and Integrations rows; never its Games rows), Settings
        (an allow-list), plus Error, Recent log, Game run and Proton log
        tail when there is something for them. Slow (Doctor): a job.
    brief() -> [str]
        a few lines for the top of a launch log. Runs no program.

Never reads nexus/facts.py (that is the per-game facts index, all titles).
Every OS read goes through nexus.platform; Linux-only parts ask
features.has('proton'). A draft is plain JSON (render.py turns it into the
issue)."""

import os
import re
import sys
import time
from pathlib import Path

from nexus import __version__, features, platform
from . import scrub as S
from .crash import LOG_LINES

# config.toml keys a report may show, (section, key). Paths show as <path>.
# info.CONFIG_SECRETS are dropped even when listed here.
SETTINGS_ALLOW = (
    ('runner', 'backend'), ('runner', 'proton'), ('updates', 'check'),
    ('integrations', 'steam'), ('integrations', 'desktop'), ('integrations', 'heroic'),
    ('integrations', 'keep_awake'), ('install', 'base'), ('install', 'unattended'),
    ('install', 'trash_repack'), ('install', 'subvolumes'), ('news', 'enabled'),
    ('repacks', 'enabled'), ('gamedb', 'enabled'), ('gamedb', 'games'),
    ('gamedb', 'upload_sessions'), ('torrent', 'client'), ('alerts', 'atl'),
    ('alerts', 'fitgirl'), ('alerts', 'repack_updates'), ('alerts', 'proton'),
    ('snapshots', 'keep'), ('theme', 'preset'), ('theatre', 'start'),
    ('agent', 'enabled'), ('report', 'relay'),
)
# Environment variables a report may show; cli/common.SECRET_ENV never.
ENV_ALLOW = ('NEXUS_FEATURES', 'NEXUS_DEV_WINDOWS', 'QT_QPA_PLATFORM', 'QT_SCALE_FACTOR',
             'QT_ENABLE_HIGHDPI_SCALING', 'PROTON_LOG', 'WINEDEBUG', 'DXVK_HUD')
_SECRET_NAME = re.compile(r'(?i)token|password|passwd|secret|api_?key|credential')
_PROTON_ROW = re.compile(r'(?i)proton|wine|umu|protongen|winetricks|compatdata')
# Words explain_log greps a launch log for: the system block must not trip it.
_ALARM = re.compile(r'(?i)\b(err|error|fail\w*|fatal|exception|crash\w*|unhandled)\b')
RAM_BUCKETS = (4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 512)
PROTON_LINES, LAUNCH_LINES = 100, 60


# ---- small reads ----------------------------------------------------------

def _git_commit(repo: Path) -> str | None:
    """HEAD's short hash from .git's files (no git process)."""
    git = repo / '.git'
    try:
        if git.is_file():             # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding='utf-8').split(':', 1)[1].strip())
        head = (git / 'HEAD').read_text(encoding='utf-8').strip()
        if not head.startswith('ref:'):
            return head[:7] or None
        ref = head.split(':', 1)[1].strip()
        bases = [git]
        common = git / 'commondir'
        if common.is_file():
            bases.append(git / common.read_text(encoding='utf-8').strip())
        for base in bases:
            p = base / ref
            if p.is_file():
                return p.read_text(encoding='utf-8').strip()[:7]
            packed = base / 'packed-refs'
            if packed.is_file():
                for line in packed.read_text(encoding='utf-8').splitlines():
                    if line.endswith(' ' + ref):
                        return line[:7]
    except (OSError, IndexError):
        pass
    return None


def install_kind() -> str:
    """git (a checkout), source, win-installer, win-portable."""
    if getattr(sys, 'frozen', False):
        try:
            return 'win-installer' if any(Path(sys.executable).parent.glob('unins*.exe')) \
                else 'win-portable'
        except OSError:
            return 'win-portable'
    from nexus.paths import REPO
    return 'git' if (REPO / '.git').exists() else 'source'


def _ram_gb() -> int | None:
    kb = platform.meminfo().get('MemTotal')
    if not kb:
        return None
    gb = kb / 1024 / 1024
    return min(RAM_BUCKETS, key=lambda b: abs(b - gb))


def _qt_versions() -> str:
    """PyQt6 and Qt from the installed packages' metadata (no Qt import)."""
    try:
        from importlib import metadata
    except ImportError:
        return 'unknown'
    out = []
    for dist, label in (('PyQt6', 'PyQt'), ('PyQt6-Qt6', 'Qt')):
        try:
            out.append(f"{label} {metadata.version(dist)}")
        except Exception:             # noqa: BLE001 — not installed / frozen
            pass
    if not out and getattr(sys, 'frozen', False):
        out.append('bundled')
    return ', '.join(out) or 'not installed'


def _base_features() -> frozenset:
    return features.ALL if platform.OS_NAME == 'linux' else features.WINDOWS


def _gpu_line(g: dict) -> str:
    bits = [g.get('vendor') or 'unknown']
    if g.get('gen'):
        bits.append(g['gen'])
    bits.append('integrated' if g.get('integrated') else 'discrete')
    drv = g.get('driver') or 'unknown'
    if g.get('driver_version'):
        drv += f" {g['driver_version']}"
    return f"{g.get('name') or 'unknown'} ({', '.join(bits)}), driver {drv}"


# ---- the groups -----------------------------------------------------------

def app_group(commit: str | None = None) -> list[str]:
    from nexus.paths import REPO
    kind = install_kind()
    commit = commit or (_git_commit(REPO) if kind == 'git' else None)
    out = [f"Version: {__version__}", f"Edition: {platform.OS_NAME}", f"Install: {kind}"]
    if commit:
        out.append(f"Commit: {commit}")
    base, now = _base_features(), features.FEATURES
    diff = [f"-{f}" for f in sorted(base - now)] + [f"+{f}" for f in sorted(now - base)]
    if diff:
        out.append(f"Features changed: {', '.join(diff)}")
    return out


def system_group() -> list[str]:
    osr, cpu = platform.os_release(), platform.cpu_info()
    out = [f"OS: {osr.get('name')} ({osr.get('id')} {osr.get('version')})",
           f"Kernel: {osr.get('kernel')}", f"Arch: {osr.get('arch')}"]
    desktop, session = os.environ.get('XDG_CURRENT_DESKTOP'), os.environ.get('XDG_SESSION_TYPE')
    if desktop or session:
        out.append(f"Desktop: {desktop or '?'}, session {session or '?'}")
    out.append(f"Python: {sys.version.split()[0]}; {_qt_versions()}")
    cores = f"{cpu['cores']} cores, " if cpu.get('cores') else ''
    out.append(f"CPU: {cpu.get('model')} ({cores}{cpu.get('threads') or '?'} threads)")
    ram = _ram_gb()
    out.append(f"RAM: {f'about {ram} GB' if ram else 'unknown'}")
    return out


def graphics_group() -> list[str]:
    gs = platform.gpus()
    if not gs:
        return ["GPU: unknown"]
    out = [f"GPU {i}: {_gpu_line(g)}" for i, g in enumerate(gs, 1)]
    if len(gs) > 1 and any(not g.get('integrated') for g in gs):
        out.append("Hybrid graphics: yes")
    return out


def runtime_group(ctx: dict, rows=None) -> list[str]:
    """Doctor's Requirements and Integrations rows, scrubbed. Proton rows
    only where Proton is a feature; the Steam account row never says whose."""
    if rows is None:
        from nexus.cli.config import resolve_install_base
        from nexus.cli.doctor import doctor_checks
        rows = doctor_checks(resolve_install_base(None))
    out = []
    for r in rows:
        if r.get('group') not in ('Requirements', 'Integrations'):
            continue
        title = str(r.get('title') or '')
        if not features.has('proton') and _PROTON_ROW.search(title):
            continue
        detail = str(r.get('detail') or '')
        if title.lower().startswith('steam account'):
            detail = 'found' if r.get('status') == 'ok' else detail.split('—')[0][:40]
        out.append(S.scrub(f"{title}: {r.get('status')}, {detail}".rstrip(', '), ctx))
    if features.has('proton'):
        try:
            from nexus import tips
            hw = tips.hardware()['features']
            out.append(f"ntsync: {'yes' if hw.get('ntsync') else 'no'}")
        except Exception:             # noqa: BLE001 — /sys unreadable
            pass
    return out or ["(no rows)"]


def _config_data() -> dict:
    try:
        import tomllib
        from nexus.paths import CONFIG_PATH
        with open(CONFIG_PATH, 'rb') as f:
            return tomllib.loads(f.read().decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def settings_group(ctx: dict, data: dict | None = None) -> list[str]:
    from nexus.cli.common import SECRET_ENV
    from nexus.cli.info import CONFIG_SECRETS
    data = _config_data() if data is None else data
    secret = {(s, k) for s, k, _ in CONFIG_SECRETS}
    out = []
    for sec, key in SETTINGS_ALLOW:
        if (sec, key) in secret or _SECRET_NAME.search(key):
            continue
        section = data.get(sec)
        if not isinstance(section, dict) or key not in section:
            continue
        v = section[key]
        if isinstance(v, bool):
            shown = 'true' if v else 'false'
        elif isinstance(v, (int, float)):
            shown = str(v)
        elif isinstance(v, str) and (re.search(r'[/\\]', v) or v.startswith('~')):
            shown = '<path>'
        elif isinstance(v, str):
            shown = S.scrub(v[:60], ctx)
        else:
            shown = f"({type(v).__name__})"
        out.append(f"{sec}.{key} = {shown}")
    drop = {e.lower() for e in SECRET_ENV}
    for var in ENV_ALLOW:
        if var.lower() in drop or _SECRET_NAME.search(var) or var not in os.environ:
            continue
        out.append(f"env {var} = {S.scrub(os.environ[var][:80], ctx)}")
    return out or ["(all defaults)"]


def error_group(crash: dict, ctx: dict) -> list[str]:
    out = [f"Kind: {crash.get('kind')}", f"Type: {S.scrub(crash.get('type') or '?', ctx)}",
           f"Message: {S.scrub(crash.get('message') or '', ctx)}",
           f"Signature: sig:{crash.get('signature')}",
           f"Seen: {crash.get('count') or 1} time(s), first {crash.get('first')}, "
           f"last {crash.get('last')}"]
    if crash.get('flags'):
        out.append(f"Action: {' '.join(crash['flags'])}")
    if crash.get('page'):
        out.append(f"Page: {S.scrub(crash['page'], ctx)}")
    if crash.get('frames'):
        out.append("Nexus frames: " + ' > '.join(crash['frames'][-8:]))
    tb = [ln for ln in str(crash.get('traceback') or '').splitlines() if ln.strip()]
    if tb:
        out.append('Traceback:')
        out += S.scrub_lines(tb[-80:], ctx)
    return out


def tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            text = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    return [ln[:400] for ln in text.splitlines()[-n:]]


def _newest(paths) -> Path | None:
    best = None
    for p in paths:
        try:
            t = p.stat().st_mtime
        except OSError:
            continue
        if best is None or t > best[0]:
            best = (t, p)
    return best[1] if best else None


# ---- the draft ------------------------------------------------------------

def _title(crash: dict | None, kind: str) -> str:
    where = f"({__version__}, {platform.OS_NAME})"
    if crash:
        frame = (crash.get('frames') or ['?'])[-1]
        tag = 'freeze' if crash.get('freeze') else \
            {'native-crash': 'crash', 'gui-exception': 'crash', 'cli-internal': 'cli',
             'game-failure': 'game'}.get(kind, 'report')
        return f"[{tag}] {crash.get('type') or 'error'} in {frame} {where}"[:200]
    return f"[report] Problem report {where}"


def collect(ctx=None, crash=None, log_lines=None, game=None, rows=None, config=None) -> dict:
    """A draft for one report. `crash`: a crash record (crash.load);
    `log_lines`: recent log lines (else the crash's own); `game`: a registry
    entry plus 'slug' for the Game run, Proton log and Game identity groups.
    `rows` / `config` stand in for Doctor and config.toml (tests)."""
    ctx = S.context() if ctx is None else ctx
    if game:
        names = [game.get(k) for k in ('slug', 'name', 'display_name', 'title')]
        ctx = {**ctx, 'games': [*(ctx.get('games') or ()), *[n for n in names if n]]}
    kind = (crash or {}).get('kind') or ('game-failure' if game else 'user-report')
    groups = {}

    def safe(name, fn, *a):
        try:
            groups[name] = fn(*a)
        except Exception as e:        # noqa: BLE001 — one group can't stop a report
            groups[name] = [f"(couldn't be read: {type(e).__name__})"]

    safe('app', app_group)
    safe('system', system_group)
    safe('graphics', graphics_group)
    safe('runtime', runtime_group, ctx, rows)
    safe('settings', settings_group, ctx, config)
    if crash:
        safe('error', error_group, crash, ctx)
    lines = list(log_lines) if log_lines else list((crash or {}).get('log') or ())
    if lines:
        groups['log'] = S.scrub_lines(lines[-LOG_LINES:], ctx)
    if game and game.get('slug'):
        from nexus.paths import LOG_DIR
        launch = tail(LOG_DIR / f"{game['slug']}-last-launch.log", LAUNCH_LINES)
        if launch:
            groups['launch'] = S.scrub_lines(launch, ctx)
        if features.has('proton'):
            newest = _newest(LOG_DIR.glob('steam-*.log')) if LOG_DIR.is_dir() else None
            plog = tail(newest, PROTON_LINES) if newest else []
            if plog:
                groups['proton_log'] = S.scrub_lines(plog, ctx)
        title = game.get('display_name') or game.get('name') or game.get('slug')
        ident = [f"Title: {title}"]
        if game.get('steam_appid'):
            ident.append(f"Steam appid: {game['steam_appid']}")
        groups['game'] = ident
    return {'schema': 1, 'id': None, 'kind': kind,
            'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'version': __version__, 'edition': platform.OS_NAME,
            'crash': (crash or {}).get('id'), 'signature': (crash or {}).get('signature'),
            'title': S.scrub(_title(crash, kind), ctx), 'groups': groups,
            'default_exclude': ['proton_log']}


def brief() -> list[str]:
    """The system block for the top of a launch log: Nexus, OS, desktop, CPU,
    RAM, GPUs, Python. No program is run; nothing personal is in it; no
    line holds a word explain_log takes for trouble."""
    out = []

    def add(fn):
        try:
            out.extend(fn())
        except Exception:             # noqa: BLE001 — a missing line, not a missing log
            pass

    def head():
        from nexus.paths import REPO
        kind = install_kind()
        commit = _git_commit(REPO) if kind == 'git' else None
        return [f"Nexus {__version__} {platform.OS_NAME} ({kind}{' ' + commit if commit else ''})"]

    def system():
        osr, cpu = platform.os_release(), platform.cpu_info()
        lines = [f"OS {osr.get('name')}, kernel {osr.get('kernel')}, {osr.get('arch')}"]
        desktop = os.environ.get('XDG_CURRENT_DESKTOP')
        if desktop:
            lines.append(f"Desktop {desktop}, {os.environ.get('XDG_SESSION_TYPE') or '?'}")
        lines.append(f"CPU {cpu.get('model')} ({cpu.get('threads') or '?'} threads)")
        ram = _ram_gb()
        if ram:
            lines.append(f"RAM about {ram} GB")
        return lines

    def graphics():
        return [f"GPU {_gpu_line(g)}" for g in platform.gpus()]

    add(head)
    add(system)
    add(graphics)
    out.append(f"Python {sys.version.split()[0]}")
    return [_ALARM.sub('-', ln) for ln in out]
