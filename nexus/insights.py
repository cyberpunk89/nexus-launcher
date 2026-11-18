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
DAY_GAMES = 4               # games named in a day's card
AVG_DAYS = 90               # `avg_minutes` averages the played days of the last 90
ON_THIS_DAY = 3             # +- days around today's date in earlier years
SPACE_TOP = 12
PACE_DAYS = 28
MILESTONES_TOP = 4
MILESTONE_NEAR = 0.8        # have/goal of at least this is "close"
HOUR_GOALS = (10, 25, 50, 100, 200, 300, 500, 1000, 2000)
COUNT_GOALS = (5, 10, 25, 50, 100, 250, 500, 1000)
YEAR_GOALS = (10, 25, 50, 100, 250, 500, 1000, 2000)


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


def _space(games, today: date) -> tuple[list[dict], dict]:
    """(the SPACE_TOP biggest installed games, {n, bytes} of all the rest)."""
    rows = []
    for g in games:
        b = int(g.get('disk_bytes') or 0)
        if b > 0:
            last = _date_of(g.get('last_played'))
            rows.append({'slug': g['slug'], 'bytes': b,
                         'idle_days': (today - last).days if last else None})
    rows.sort(key=lambda r: (-r['bytes'], r['slug']))
    rest = rows[SPACE_TOP:]
    return rows[:SPACE_TOP], {'n': len(rest), 'bytes': sum(r['bytes'] for r in rest)}


def _facts(games, facts) -> list[dict]:
    """Library facts for the Facts widget, in a fixed order; a kind with no
    data is left out. `n` is the number the words use; names only."""
    out = []

    def add(kind, g=None, name=None, n=0, **extra):
        out.append({'kind': kind, 'slug': g['slug'] if g else None,
                    'name': _name(g) if g else name, 'n': n, **extra})

    if games:
        add('library', n=len(games), hours=round(sum(smart._hours(g) for g in games), 1))
    top = smart.top_genres(games, facts, 1)
    if top:
        add('top_genre', name=top[0][0], n=top[0][1])
    played = [g for g in games if smart._hours(g) > 0]
    if played:
        g = max(played, key=lambda g: (smart._hours(g), g['slug']))
        add('most_played', g, n=round(smart._hours(g), 1))
    years = [(y, g['slug'], g) for g in games
             if isinstance(y := (facts.get(g['slug']) or {}).get('year'), int) and y > 0]
    if years:
        y, _s, g = min(years, key=lambda t: (t[0], t[1]))
        add('oldest', g, n=y)
    short = [(m, g['slug'], g) for g in games
             if not (g.get('playtime_minutes') or g.get('last_played'))
             and (m := smart.main_hours(facts.get(g['slug'])))]
    if short:
        m, _s, g = min(short, key=lambda t: (t[0], t[1]))
        add('shortest_unplayed', g, n=round(m, 1))
    sized = [g for g in games if int(g.get('disk_bytes') or 0) > 0]
    if sized:
        g = max(sized, key=lambda g: (int(g['disk_bytes']), g['slug']))
        add('biggest', g, n=int(g['disk_bytes']))
    studios: dict[str, int] = defaultdict(int)
    for g in games:
        for dev in dict.fromkeys((facts.get(g['slug']) or {}).get('developers') or ()):
            if isinstance(dev, str) and dev.strip():
                studios[dev.strip()] += 1
    if studios:
        name, n = max(studios.items(), key=lambda kv: (kv[1], kv[0]))
        add('top_studio', name=name, n=n)
    return out


def _next_goal(have: float, goals) -> int | None:
    return next((g for g in goals if g > have), None)


def _milestones(games, facts, year_minutes: float) -> list[dict]:
    """The round numbers you are close to: hours in one game, games
    finished, hours played this year. Closest first (have/goal), at most
    MILESTONES_TOP. `slug` is None for the library-wide ones."""
    out = []
    done = 0
    for g in games:
        p = smart.progress(g, facts.get(g['slug']))
        if p is not None and p >= smart.DONE_AT:
            done += 1
        have = smart._hours(g)
        goal = _next_goal(have, HOUR_GOALS)
        if goal and have / goal >= MILESTONE_NEAR:
            out.append({'kind': 'hours', 'slug': g['slug'], 'name': _name(g),
                        'have': round(have, 1), 'goal': goal})
    goal = _next_goal(done, COUNT_GOALS)
    if done and goal and done / goal >= MILESTONE_NEAR:
        out.append({'kind': 'finished', 'slug': None, 'name': None, 'have': done, 'goal': goal})
    hours = year_minutes / 60
    goal = _next_goal(hours, YEAR_GOALS)
    if goal and hours / goal >= MILESTONE_NEAR:
        out.append({'kind': 'year', 'slug': None, 'name': None,
                    'have': round(hours, 1), 'goal': goal})
    out.sort(key=lambda m: (-m['have'] / m['goal'], m['kind'], m['slug'] or ''))
    return out[:MILESTONES_TOP]


def _streak_pos(all_days) -> dict:
    """{day: (k, n)} for every day that sits in a run of 2+ played days."""
    out, run = {}, []
    ordered = sorted(all_days)
    for i, d in enumerate(ordered):
        run.append(d)
        if i + 1 == len(ordered) or ordered[i + 1] - d != timedelta(days=1):
            if len(run) > 1:
                for k, x in enumerate(run, 1):
                    out[x] = (k, len(run))
            run = []
    return out


def _day_games(per_day: dict, day_n: dict, day_t: dict, pos: dict) -> dict:
    """{day: {'n': sessions, 'games': [top DAY_GAMES by minutes], 'more': others,
    'first'/'last': HH:MM of the first and last session START (local),
    'streak_pos': {'day': k, 'of': n} or None}} for the heatmap's card, played
    days only. Each game has `source` (the one with most of its minutes), and `via:
    'steam-log'` when most of its minutes were read from Steam's game log."""
    out = {}
    for d, games in sorted(per_day.items()):
        ranked = sorted(games.items(), key=lambda kv: (-kv[1][0], kv[0]))
        lo, hi = day_t[d]
        k = pos.get(d)
        out[d.isoformat()] = {
            'n': day_n[d],
            'games': [{'slug': g, 'name': v[1], 'minutes': round(v[0], 1),
                       'source': max(v[2].items(), key=lambda kv: (kv[1], kv[0]))[0],
                       **({'via': 'steam-log'} if v[3] * 2 > v[0] else {})}
                      for g, v in ranked[:DAY_GAMES]],
            'more': max(0, len(ranked) - DAY_GAMES),
            'first': lo.strftime('%H:%M'), 'last': hi.strftime('%H:%M'),
            'streak_pos': {'day': k[0], 'of': k[1]} if k else None}
    return out


def build(sessions: list[dict], games: list[dict], facts: dict, today: date,
          hidden=()) -> dict:
    """`hidden`: slugs the user hid; they count nowhere in the library parts."""
    hidden = set(hidden or ())
    games = [g for g in games if isinstance(g, dict) and g.get('slug') and g['slug'] not in hidden]
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
    per_day: dict[date, dict[str, list]] = defaultdict(dict)   # day -> slug -> [minutes, name]
    day_n: dict[date, int] = defaultdict(int)
    day_t: dict[date, list] = {}
    avg_from = today - timedelta(days=AVG_DAYS - 1)
    avg_days: dict[date, float] = defaultdict(float)
    for s in sessions:
        t = _start(s)
        if t is None:
            continue
        d, m, slug = t.date(), s['minutes'], s['slug']
        all_days.add(d)
        if first <= d <= today:
            by_day[d] += m
            day_n[d] += 1
            cell = per_day[d].setdefault(slug, [0.0, s.get('name') or slug, defaultdict(float), 0.0])
            cell[0] += m
            if s.get('via') == 'steam-log':
                cell[3] += m                    # minutes that came from Steam's game log
            cell[2][s.get('source') or 'nexus'] += m
            lh = day_t.get(d)
            if lh is None:
                day_t[d] = [t, t]
            elif t < lh[0]:
                lh[0] = t
            elif t > lh[1]:
                lh[1] = t
            if d >= avg_from:
                avg_days[d] += m
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
    space, space_more = _space(games, today)
    return {
        'v': V,
        'today': today.isoformat(),
        'days': {k.isoformat(): round(v, 1) for k, v in sorted(by_day.items())},
        'day_games': _day_games(per_day, day_n, day_t, _streak_pos(all_days)),
        'avg_minutes': round(sum(avg_days.values()) / len(avg_days), 1) if avg_days else 0.0,
        'streak': {'now': now, 'best': history.best_streak(year_days)},
        'busiest_day': (history.WEEKDAYS[max(weekday.items(), key=lambda kv: (kv[1], -kv[0]))[0]]
                        if weekday else None),
        'year_minutes': round(year_minutes, 1),
        'on_this_day': _on_this_day(rows, today),
        'daily': daily,
        'daily_alt': alt,
        'backlog': _backlog(games, facts, week_minutes, today),
        'space': space,
        'space_more': space_more,
        'facts': _facts(games, facts),
        'milestones': _milestones(games, facts, year_minutes),
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


def hidden_slugs(path=None) -> frozenset:
    """The slugs the GUI's prefs hide (library.json `hidden`); none when the
    file is missing or broken (a plain read, the GUI writes it)."""
    from .paths import CONFIG_DIR
    try:
        blob = json.loads((path or CONFIG_DIR / 'library.json').read_text(encoding='utf-8'))
        rows = blob.get('hidden')
    except (OSError, ValueError, AttributeError):
        return frozenset()
    return frozenset(s for s in rows if isinstance(s, str)) if isinstance(rows, list) else frozenset()


def gather(today: date | None = None, sessions: list[dict] | None = None) -> dict:
    """build() from what is on disk: sessions.jsonl (or the merged `sessions`
    the caller made with history.all_sessions), the Library cache and the
    facts index."""
    from . import facts
    games = library_games()
    return build(history.load() if sessions is None else sessions, games,
                 facts.read_index().get('games') or {}, today or date.today(), hidden_slugs())
