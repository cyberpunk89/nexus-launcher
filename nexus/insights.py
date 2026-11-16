"""What `nexus-cli --insights --json` answers: a local read of your play
history, library and facts for Home's widgets (w-138). build() is pure; the
payload is v1 and only grows by new keys. A session counts on its START
date (as history._period does); aware timestamps are made local first.
Nothing here touches the network."""

import json
import random
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import history, smart

V = 1
DAYS = 371                  # the heatmap: 53 weeks and a day
ON_THIS_DAY = 3             # +- days around today's date in earlier years
SPACE_TOP = 12
PACE_DAYS = 28


def _local(t: datetime) -> datetime:
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _start(s: dict) -> datetime | None:
    t = s.get('_start')
    if t is None:
        t = smart._when(s.get('start'))
    return None if t is None else _local(t)


def _anchor(y: int, today: date) -> date:
    try:
        return today.replace(year=y)
    except ValueError:                                   # Feb 29 in a common year
        return date(y, 2, 28)


def _date_of(iso) -> date | None:
    t = smart._when(iso)
    return None if t is None else t.date()


def _release_date(fact: dict | None) -> date | None:
    try:
        return date.fromisoformat(str((fact or {}).get('release') or '')[:10])
    except ValueError:
        return None                                      # '2020-11' or '2027': no day


def _name(g: dict) -> str:
    return g.get('display_name') or g.get('name') or g['slug']


def _on_this_day(rows, today: date) -> list[dict]:
    """rows: (date, slug, name, minutes). Per game and year, the busiest
    stretch within +-3 days of today's date in an earlier year."""
    best: dict[tuple, dict] = {}
    for d, slug, name, minutes in rows:
        if d.year > today.year:
            continue
        for ay in (d.year - 1, d.year, d.year + 1):
            if ay >= today.year:
                continue
            if abs((d - _anchor(ay, today)).days) > ON_THIS_DAY:
                continue
            e = best.setdefault((slug, ay), {'slug': slug, 'name': name, 'years_ago': today.year - ay,
                                             'minutes': 0.0, 'per_day': defaultdict(float)})
            e['minutes'] += minutes
            e['per_day'][d] += minutes
            e['name'] = name
            break
    out = []
    for e in best.values():
        day = max(e['per_day'].items(), key=lambda kv: (kv[1], kv[0]))[0]
        out.append({'slug': e['slug'], 'name': e['name'], 'years_ago': e['years_ago'],
                    'date': day.isoformat(), 'minutes': round(e['minutes'], 1)})
    out.sort(key=lambda e: (-e['minutes'], e['years_ago'], e['slug']))
    return out[:3]


def _daily(games, facts, first_played: dict, today: date) -> tuple:
    """Game of the day: a release anniversary first, then a first-play
    anniversary, then a seeded pick; the seed is the date (never hash())."""
    rng = random.Random(today.isoformat())
    tiers = {'released': [], 'first_played': [], 'pick': []}
    for g in sorted(games, key=lambda g: g['slug']):
        slug = g['slug']
        r = _release_date(facts.get(slug))
        if r and r.year < today.year and r.month == today.month and r.day == today.day:
            tiers['released'].append((slug, today.year - r.year))
        f = first_played.get(slug)
        if f and f.year < today.year and (f.month, f.day) == (today.month, today.day):
            tiers['first_played'].append((slug, today.year - f.year))
        tiers['pick'].append((slug, None))
    seen, ordered = set(), []
    for reason, items in tiers.items():
        rng.shuffle(items)
        for slug, years in items:
            if slug not in seen:
                seen.add(slug)
                ordered.append({'slug': slug, 'reason': reason, 'years': years})
    return (ordered[0] if ordered else None), (ordered[1] if len(ordered) > 1 else None)


def _backlog(games, facts, week_minutes: float, today: date) -> dict:
    done = playing = started = never = 0
    left = 0.0
    idle = 0
    for g in games:
        fact = facts.get(g['slug'])
        p = smart.progress(g, fact)
        played = bool(g.get('playtime_minutes') or g.get('last_played'))
        last = _date_of(g.get('last_played'))
        days = (today - last).days if last else None
        if p is not None and p >= smart.DONE_AT:
            done += 1
        elif not played:
            never += 1
        elif days is not None and days <= smart.RECENT_DAYS:
            playing += 1
        else:
            started += 1
        main = smart.main_hours(fact)
        if main and not (p is not None and p >= smart.DONE_AT):
            left += max(0.0, main - smart._hours(g))
        if g.get('installed', True) is not False and days is not None and days > 365:
            idle += int(g.get('disk_bytes') or 0)
    return {'done': done, 'playing': playing, 'started': started, 'never': never,
            'main_hours_left': round(left, 1),
            'pace_hours_week': round(week_minutes / 60 / (PACE_DAYS / 7), 1),
            'idle_year_bytes': idle}


def _space(games, today: date) -> list[dict]:
    rows = []
    for g in games:
        b = int(g.get('disk_bytes') or 0)
        if b > 0:
            last = _date_of(g.get('last_played'))
            rows.append({'slug': g['slug'], 'bytes': b,
                         'idle_days': (today - last).days if last else None})
    rows.sort(key=lambda r: (-r['bytes'], r['slug']))
    return rows[:SPACE_TOP]


def build(sessions: list[dict], games: list[dict], facts: dict, today: date) -> dict:
    games = [g for g in games if isinstance(g, dict) and g.get('slug')]
    facts = facts or {}
    first = today - timedelta(days=DAYS - 1)
    pace_from = today - timedelta(days=PACE_DAYS - 1)
    by_day: dict[date, float] = defaultdict(float)
    year_days = set()
    weekday = defaultdict(float)
    year_minutes = week_minutes = 0.0
    first_played: dict[str, date] = {}
    rows = []
    all_days = set()
    for s in sessions:
        t = _start(s)
        if t is None:
            continue
        d, m, slug = t.date(), s['minutes'], s['slug']
        all_days.add(d)
        if first <= d <= today:
            by_day[d] += m
        if d.year == today.year:
            year_days.add(d)
            year_minutes += m
            weekday[d.weekday()] += m
        if pace_from <= d <= today:
            week_minutes += m
        if slug not in first_played or d < first_played[slug]:
            first_played[slug] = d
        rows.append((d, slug, s.get('name') or slug, m))
    d = today if today in all_days else today - timedelta(days=1)
    now = 0
    while d in all_days:
        now += 1
        d -= timedelta(days=1)
    daily, alt = _daily(games, facts, first_played, today)
    return {
        'v': V,
        'today': today.isoformat(),
        'days': {k.isoformat(): round(v, 1) for k, v in sorted(by_day.items())},
        'streak': {'now': now, 'best': history.best_streak(year_days)},
        'busiest_day': (history.WEEKDAYS[max(weekday.items(), key=lambda kv: (kv[1], -kv[0]))[0]]
                        if weekday else None),
        'year_minutes': round(year_minutes, 1),
        'on_this_day': _on_this_day(rows, today),
        'daily': daily,
        'daily_alt': alt,
        'backlog': _backlog(games, facts, week_minutes, today),
        'space': _space(games, today),
    }


def library_games(path=None) -> list[dict]:
    """The games in the GUI's last Library scan; none when the cache is
    missing or unreadable (the GUI writes it, this only reads)."""
    from .paths import LIBRARY_CACHE
    try:
        blob = json.loads((path or LIBRARY_CACHE).read_text(encoding='utf-8'))
        games = (blob.get('data') or {}).get('games')
    except (OSError, ValueError, AttributeError):
        return []
    return games if isinstance(games, list) else []


def gather(today: date | None = None) -> dict:
    """build() from what is on disk: sessions.jsonl, the Library cache and
    the facts index."""
    from . import facts
    return build(history.load(), library_games(), facts.read_index().get('games') or {},
                 today or date.today())
