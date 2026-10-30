"""Formatting sizes, times and durations for display."""

import time
from datetime import datetime


def fmt_hours(minutes) -> str | None:
    if not minutes:
        return None
    if minutes < 60:
        return f"{int(minutes)} min"
    return f"{minutes / 60:.1f} h"


def fmt_duration(minutes) -> str:
    """'42 min', '1 h 42 min', '2 h': a length of time, in whole minutes."""
    m = max(0, int(round(minutes or 0)))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# --------------------------------------------------------------------------
# Deals page — Game-db: wishlist deals, upcoming releases, backlog picks
# --------------------------------------------------------------------------

def fmt_ago(ts) -> str:
    try:
        mins = int((time.time() - float(ts)) // 60)
    except (TypeError, ValueError):
        return ''
    if mins < 1:
        return 'just now'
    if mins < 60:
        return f'{mins} min ago'
    if mins < 48 * 60:
        return f'{mins // 60} h ago'
    return f'{mins // 1440} days ago'


# --------------------------------------------------------------------------
# Downloads — the torrent client (Transmission / qBittorrent) from inside
# Nexus: every torrent, with pause / resume / remove
# --------------------------------------------------------------------------

def fmt_eta(seconds) -> str:
    if not seconds:
        return ''
    s = int(seconds)
    if s < 90:
        return f"{s} s left"
    if s < 5400:
        return f"{s // 60} min left"
    if s < 172800:
        return f"{s // 3600} h {s % 3600 // 60:02d} min left"
    return f"{s // 86400} days left"


def fmt_size(disk_bytes) -> str | None:
    if not disk_bytes:
        return None
    return f"{disk_bytes / 1e9:.1f} GB"


def fmt_bytes(n) -> str:
    """Human size for small and large things alike (cleanup rows)."""
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1000 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit in ('B', 'KB') else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_relative(iso_ts: str | None) -> str | None:
    """'3d ago' / 'today' style label for an ISO timestamp, or None."""
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    days = (datetime.now() - then).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"
