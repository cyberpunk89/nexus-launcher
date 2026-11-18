"""A play-time ledger (w-151): what Nexus saw of each game's total play time,
so days it missed (Heroic, Epic, other devices, Steam while Nexus was closed
and the log didn't say) still count as play, marked Estimated.

The library scan (nexus-cli --library) calls snapshot(); it appends a line
`{date, at, slug, name, source, total, last_played}` to
DATA_DIR/playtime-ledger.jsonl for each game whose whole-minute total changed
since that game's last line (the first line of a game is its baseline, no
estimate). estimates() turns consecutive lines into session-shaped records
(`kind: 'estimate'`, dated at midnight) that history.all_sessions() adds at
read time. The file is never uploaded to Game-db and turning the setting off
hides the estimates without losing the lines. Times are naive local, like
sessions.jsonl's."""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import history
from .paths import DATA_DIR

PATH = DATA_DIR / 'playtime-ledger.jsonl'
ESTIMATE_MIN = 5                    # minutes: a smaller remainder is rounding, not play


def _total(g: dict) -> int | None:
    """The whole-minute total to note. A game Nexus launches itself carries
    `external_minutes` (what Heroic timed): its own timed play is already in
    sessions.jsonl, so only the part Nexus did not record is noted."""
    m = g.get('external_minutes', g.get('playtime_minutes'))
    return m if isinstance(m, int) and not isinstance(m, bool) and m >= 0 else None


def _when(text) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text))
    except (ValueError, TypeError):
        return None
    return t.replace(tzinfo=None) if t.tzinfo is None else t.astimezone().replace(tzinfo=None)


def load(path=None) -> list[dict]:
    """The ledger lines with `_at` (a datetime); torn lines are skipped."""
    out = []
    try:
        with open(path or PATH, encoding='utf-8') as f:
            for raw in f:
                try:
                    x = json.loads(raw)
                    x['_at'] = _when(x['at'])
                    if x['_at'] is None or not (isinstance(x['slug'], str) and isinstance(x['total'], int)):
                        continue
                except (ValueError, KeyError, TypeError):
                    continue
                out.append(x)
    except OSError:
        pass
    return out


def snapshot(games, path=None, now: datetime | None = None) -> int:
    """Append a line for each game in `games` (the Library list) whose
    playtime_minutes changed since its last line, or that has none yet.
    Returns how many were written. Raises OSError for an unwritable file."""
    path, now = Path(path or PATH), now or datetime.now()
    last: dict[str, int] = {}
    for x in sorted(load(path), key=lambda x: x['_at']):
        last[x['slug']] = x['total']
    rows = []
    for g in games or ():
        slug, total = g.get('slug'), _total(g)
        if not slug or total is None or last.get(slug) == total:
            continue
        last[slug] = total
        rows.append(json.dumps({
            'date': now.date().isoformat(), 'at': now.isoformat(timespec='seconds'),
            'slug': slug, 'name': g.get('display_name') or g.get('name') or slug,
            'source': g.get('source') or g.get('kind') or 'nexus',
            'total': total, 'last_played': g.get('last_played') or None}) + '\n')
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, ''.join(rows).encode())
        finally:
            os.close(fd)
    return len(rows)


def estimates(lines: list[dict], real_sessions) -> list[dict]:
    """Session-shaped estimates from `lines` (load()'s): per game, each pair of
    consecutive lines gives the minutes the total grew by, less the minutes of
    that game's real sessions that ended between the two lines. A total that
    fell starts a new baseline; a growth bigger than the time between the
    lines (another Steam account, a restored save) is dropped, as is a
    remainder under ESTIMATE_MIN. The estimate is dated at last_played when
    that falls between the lines, else at the later line."""
    ends: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for s in real_sessions:
        if history.is_real(s) and s.get('_start') is not None:
            ends[s['slug']].append((s['_start'] + timedelta(minutes=s['minutes']), s['minutes']))
    by_slug: dict[str, list[dict]] = defaultdict(list)
    for x in lines:
        by_slug[x['slug']].append(x)
    out = []
    for slug, rows in sorted(by_slug.items()):
        rows.sort(key=lambda x: x['_at'])
        for prev, cur in zip(rows, rows[1:]):
            delta = cur['total'] - prev['total']
            elapsed = (cur['_at'] - prev['_at']).total_seconds() / 60
            if delta <= 0 or delta > elapsed:
                continue
            seen = sum(m for end, m in ends.get(slug, ()) if prev['_at'] < end <= cur['_at'])
            rest = delta - seen
            if rest < ESTIMATE_MIN:
                continue
            lp = _when(cur.get('last_played'))
            day = (lp if lp and prev['_at'] < lp <= cur['_at'] else cur['_at']).date()
            start = datetime(day.year, day.month, day.day)
            out.append({'slug': slug, 'name': cur.get('name') or slug,
                        'start': start.isoformat(timespec='seconds'), '_start': start,
                        'minutes': float(rest), 'kind': 'estimate',
                        'source': cur.get('source') or 'nexus'})
    return out
