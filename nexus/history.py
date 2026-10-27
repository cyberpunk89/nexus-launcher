"""Play sessions, one JSON line each in DATA_DIR/sessions.jsonl:
{slug, source, name, start, end, minutes}. Repacks are recorded by the
launch tracker; Steam and Heroic games by the GUI while it's open (it sees
them start and stop). Lines are appended with O_APPEND, so the two never
interleave. summary() turns them into the play-history page: the last
weeks, and a year in review; periods() into the Home Play time widget's
week and month."""

import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .paths import DATA_DIR

PATH = DATA_DIR / 'sessions.jsonl'
MIN_SECONDS = 60                    # shorter than this isn't a session (a crash, a misclick)
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def is_session(seconds: float) -> bool:
    """The one rule for what counts as play: the launch tracker's play time
    and sessions.jsonl both use it, so the two never disagree."""
    return seconds >= MIN_SECONDS


def record(slug: str, source: str, start: datetime, end: datetime,
           name: str | None = None, path=None) -> bool:
    seconds = (end - start).total_seconds()
    if not is_session(seconds):
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


def _period(sessions: list[dict], first: date, days: int, names: dict) -> dict:
    """Minutes per day from `first` for `days` days (days after today stay 0),
    and per game, most played first. A session counts on its start date."""
    last = first + timedelta(days=days - 1)
    by_day = [0.0] * days
    per_game: dict[str, float] = defaultdict(float)
    count = 0
    for s in sessions:
        d = s['_start'].date()
        if first <= d <= last:
            by_day[(d - first).days] += s['minutes']
            per_game[s['slug']] += s['minutes']
            count += 1
    return {
        'start': first.isoformat(),
        'minutes': round(sum(by_day), 1),
        'sessions': count,
        'days': [{'date': (first + timedelta(days=i)).isoformat(), 'minutes': round(m, 1)}
                 for i, m in enumerate(by_day)],
        'games': [{'slug': k, 'name': names.get(k, k), 'minutes': round(v, 1)}
                  for k, v in sorted(per_game.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


def periods(sessions: list[dict] | None = None, today: date | None = None) -> dict:
    """This week (Monday on) and this month, by day and by game: what the
    Home Play time widget charts."""
    sessions = load() if sessions is None else sessions
    today = today or date.today()
    names = {}
    for s in sorted(sessions, key=lambda s: s['start']):
        names[s['slug']] = s.get('name') or s['slug']      # the newest name wins
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return {'today': today.isoformat(),
            'week': _period(sessions, monday, 7, names),
            'month': _period(sessions, first, (nxt - first).days, names)}


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
        'periods': periods(sessions, today),
        'since': min((s['start'][:10] for s in sessions), default=None),
        'recent': [{'slug': s['slug'], 'name': names[s['slug']], 'start': s['start'],
                    'minutes': s['minutes']}
                   for s in sorted(sessions, key=lambda s: s['start'], reverse=True)[:8]],
    }
