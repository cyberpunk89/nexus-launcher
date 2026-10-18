"""Play sessions, one JSON line each in DATA_DIR/sessions.jsonl:
{slug, source, name, start, end, minutes}. Repacks are recorded by the
launch tracker; Steam and Heroic games by the GUI while it's open (it sees
them start and stop). Lines are appended with O_APPEND, so the two never
interleave. summary() turns them into the play-history page: the last
weeks, and a year in review."""

import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .paths import DATA_DIR

PATH = DATA_DIR / 'sessions.jsonl'
MIN_SECONDS = 60                    # shorter than this isn't a session (a crash, a misclick)
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def record(slug: str, source: str, start: datetime, end: datetime,
           name: str | None = None, path=None) -> bool:
    seconds = (end - start).total_seconds()
    if seconds < MIN_SECONDS:
        return False
    line = json.dumps({'slug': slug, 'source': source, 'name': name or slug,
                       'start': start.isoformat(timespec='seconds'),
                       'end': end.isoformat(timespec='seconds'),
                       'minutes': round(seconds / 60, 1)}) + '\n'
    path = path or PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def load(path=None) -> list[dict]:
    out = []
    try:
        with open(path or PATH, encoding='utf-8') as f:
            for line in f:
                try:
                    s = json.loads(line)
                    s['_start'] = datetime.fromisoformat(s['start'])
                    s['minutes'] = float(s['minutes'])
                except (ValueError, KeyError, TypeError):
                    continue                    # a torn or hand-edited line
                out.append(s)
    except OSError:
        pass
    return out


def summary(sessions: list[dict] | None = None, today: date | None = None,
            weeks: int = 12) -> dict:
    sessions = load() if sessions is None else sessions
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=weeks - 1)
    week_minutes = [0.0] * weeks
    names = {}
    for s in sessions:
        names[s['slug']] = s.get('name') or s['slug']
        d = s['_start'].date()
        if first <= d <= today:
            week_minutes[(d - first).days // 7] += s['minutes']
    year = today.year
    ys = [s for s in sessions if s['_start'].year == year]
    per_game = defaultdict(float)
    for s in ys:
        per_game[s['slug']] += s['minutes']
    days = {s['_start'].date() for s in ys}
    streak = best = 0
    prev = None
    for d in sorted(days):
        streak = streak + 1 if prev and d - prev == timedelta(days=1) else 1
        best = max(best, streak)
        prev = d
    longest = max(ys, key=lambda s: s['minutes'], default=None)
    weekday = Counter()
    for s in ys:
        weekday[s['_start'].weekday()] += s['minutes']
    return {
        'weeks': [{'start': (first + timedelta(weeks=i)).isoformat(),
                   'minutes': round(m, 1)} for i, m in enumerate(week_minutes)],
        'year': {
            'year': year,
            'minutes': round(sum(s['minutes'] for s in ys), 1),
            'sessions': len(ys),
            'games': len(per_game),
            'days': len(days),
            'best_streak': best,
            'longest': ({'slug': longest['slug'], 'name': names[longest['slug']],
                         'minutes': longest['minutes'], 'date': longest['start'][:10]}
                        if longest else None),
            'top': [{'slug': k, 'name': names[k], 'minutes': round(v, 1)}
                    for k, v in sorted(per_game.items(), key=lambda kv: -kv[1])[:5]],
            'busiest_day': WEEKDAYS[weekday.most_common(1)[0][0]] if weekday else None,
        },
        'since': min((s['start'][:10] for s in sessions), default=None),
        'recent': [{'slug': s['slug'], 'name': names[s['slug']], 'start': s['start'],
                    'minutes': s['minutes']}
                   for s in sorted(sessions, key=lambda s: s['start'], reverse=True)[:8]],
    }
