"""Pausing a running repack, and benchmark runs."""

import json
import os
import re
import shutil
import signal
import sys
from datetime import datetime
from pathlib import Path

from nexus import features, paths as NX


def _proc_state(pid: int) -> str | None:
    try:
        stat = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
        return stat.rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def running_repacks() -> dict[str, list[int]]:
    """{slug: pids} for Nexus games with processes in their prefix now."""
    out = {}
    for slug, e in load_registry()['games'].items():
        pids = prefix_processes(_prefix_markers(e))
        if pids:
            out[slug] = pids
    return out


def cmd_pause(slug: str | None, mode: str) -> None:
    """Freeze (SIGSTOP) or thaw (SIGCONT) everything running in a repack's
    prefix; mode 'toggle' picks by the processes' state. Without a slug: the
    one repack that's running."""
    if not features.has('pause'):
        print("Pausing a game isn't available on this system.", file=sys.stderr)
        sys.exit(1)
    running = running_repacks()
    if slug is None:
        if len(running) != 1:
            print("No repack is running." if not running else
                  f"Several are running ({', '.join(sorted(running))}) — name one.",
                  file=sys.stderr)
            sys.exit(1)
        slug = next(iter(running))
    pids = running.get(slug) or prefix_processes(_prefix_markers(_require_entry(slug)))
    if not pids:
        print(f"'{slug}' isn't running.", file=sys.stderr)
        sys.exit(1)
    if mode == 'toggle':
        stopped = sum(_proc_state(p) == 'T' for p in pids)
        mode = 'resume' if stopped * 2 >= len(pids) else 'pause'
    sig = signal.SIGSTOP if mode == 'pause' else signal.SIGCONT
    n = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            n += 1
        except OSError:
            pass
    print(f"{'Paused' if mode == 'pause' else 'Resumed'} '{slug}' ({n} processes).")


BENCH_DIR = NX.DATA_DIR / 'benchmarks'


def cmd_benchmark(install_base: Path, slug: str, seconds: int, delay: int) -> None:
    """Launch a repack with MangoHud logging `seconds` long, starting `delay`
    seconds in (Shift+F2 also starts/stops it by hand). Results:
    --bench-results."""
    entry = _require_entry(slug)
    sh = Path(entry.get('launcher') or '')
    try:
        hooked = BENCH_HOOK in sh.read_text(encoding='utf-8')
    except OSError:
        hooked = False
    if not hooked:
        print(f"The launcher for '{slug}' predates benchmarks. Apply its launch settings once "
              f"(Game settings → Apply, or nexus-cli --apply-launch {slug}), then retry.",
              file=sys.stderr)
        sys.exit(1)
    if not shutil.which('mangohud'):
        print("Benchmarks need MangoHud.", file=sys.stderr)
        sys.exit(1)
    out = BENCH_DIR / slug
    out.mkdir(parents=True, exist_ok=True)
    extra = [f"output_folder={out}", f"autostart_log={max(0, delay)}",
             f"log_duration={max(5, seconds)}", "log_interval=0"]
    if not perf_state(entry.get('launch'))['mangohud']:
        extra.append('no_display')          # log without showing an overlay you didn't ask for
    os.environ['NEXUS_MANGOHUD_EXTRA'] = ','.join(extra)
    print(f"Benchmark: logging {seconds} s, starting {delay} s after launch "
          f"(Shift+F2 toggles it by hand). Results: nexus-cli --bench-results {slug}")
    cmd_launch(install_base, slug)


def parse_mangohud_csv(text: str) -> dict | None:
    """avg / 1% low / 0.1% low fps, samples and seconds from a MangoHud log."""
    lines = text.splitlines()
    try:
        i = next(n for n, l in enumerate(lines) if l.startswith('fps,frametime'))
    except StopIteration:
        return None
    cols = lines[i].split(',')
    ft_col = cols.index('frametime')
    el_col = cols.index('elapsed') if 'elapsed' in cols else None
    frametimes, elapsed = [], []
    for l in lines[i + 1:]:
        parts = l.split(',')
        try:
            ft = float(parts[ft_col])
        except (IndexError, ValueError):
            continue
        if ft > 0:
            frametimes.append(ft)
            if el_col is not None:
                try:
                    elapsed.append(float(parts[el_col]))
                except (IndexError, ValueError):
                    pass
    if len(frametimes) < 10:
        return None
    srt = sorted(frametimes)

    def low(pct):                    # fps at the pct-slowest frame time
        k = min(len(srt) - 1, int(len(srt) * (1 - pct / 100)))
        return round(1000 / srt[k], 1)
    span = (elapsed[-1] - elapsed[0]) / 1e9 if len(elapsed) > 1 else sum(frametimes) / 1000
    return {'avg_fps': round(1000 * len(frametimes) / sum(frametimes), 1),
            'low_1': low(1), 'low_01': low(0.1), 'frames': len(frametimes),
            'seconds': round(span if span > 0 else sum(frametimes) / 1000, 1)}


def cmd_bench_results(slug: str, as_json: bool) -> None:
    rows = []
    d = BENCH_DIR / slug
    for f in sorted(d.glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True) \
            if d.is_dir() else []:
        if f.name.endswith('_summary.csv'):
            continue
        try:
            r = parse_mangohud_csv(f.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            r = None
        if r:
            rows.append({'file': str(f), 'at': datetime.fromtimestamp(f.stat().st_mtime)
                         .isoformat(timespec='seconds'), **r})
    if as_json:
        print(json.dumps({'slug': slug, 'runs': rows[:20]}))
        return
    if not rows:
        print(f"No benchmark runs for '{slug}' yet (nexus-cli --benchmark {slug}).")
    for r in rows[:20]:
        print(f"  {r['at']}  avg {r['avg_fps']} fps · 1% low {r['low_1']} · 0.1% low "
              f"{r['low_01']} · {r['seconds']} s")


# The steam:// and heroic:// URLs Nexus itself builds: a known verb and an id,
# nothing a remote value could turn into another Steam command.
_OPEN_URL_RE = re.compile(
    r'^steam://(?:(?:rungameid|install|uninstall|validate|gameproperties|store'
    r'|nav/games/details)/\d+|open/keyboard)$'
    r'|^heroic://launch/\w[\w.-]*/\w[\w.-]*$'
    # Epic: namespace, catalog item and app name, as sources/epic.launch_url builds it.
    r'|^com\.epicgames\.launcher://apps/\w+%3A\w+%3A\w[\w.-]*\?action=launch&silent=true$')


def cmd_open_url(url: str) -> None:
    """Launch a Steam, Heroic or Epic game (or open a Steam page) by URL. Only
    the forms in _OPEN_URL_RE are accepted, and the pages
    Doctor's fixes open (platform.fix_page: Windows settings, help links)."""
    from nexus import platform, steamlib
    if not _OPEN_URL_RE.match(url) and not platform.fix_page(url):
        print(f"Refusing to open {url!r}: only Steam, Heroic and Epic launch URLs", file=sys.stderr)
        sys.exit(2)
    if not steamlib.open_url(url):
        print(f"Nothing here can open {url} (on Linux: no `steam` or `xdg-open`).",
              file=sys.stderr)
        sys.exit(1)
    print(f"Opened {url}")


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import _require_entry  # noqa: E402
from .launchprofile import perf_state  # noqa: E402
from .registry import load_registry  # noqa: E402
from .runner import BENCH_HOOK  # noqa: E402
from .sunshine import _prefix_markers, cmd_launch, prefix_processes  # noqa: E402
