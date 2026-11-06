"""For you now (w-88): what on Home needs you, ranked. Pure: home_moments()
reads plain lists and dicts and a fixed `now`, so every rule is tested with
fake data. moments_inputs(hub) is the one place that reads the window; it
only reads what the window already has (no refresh, no CLI call). The pill
and the strip that show the moments are w-89.

Order, highest first (`rank`):
  1 broken        a Health check failing, a drive under DISK_CRITICAL free
  2 running       a download or install running, or one that finished
                  READY_HOURS ago or less and wasn't played since
  3 friends       friends playing a game you own, now
  4 store         wishlist all-time low, then sale (biggest cut first),
                  then a wishlist game on FitGirl; a sale ending within
                  ENDING_HOURS goes first (when Game-db sends `endsAt`)
  5 update        a newer repack of an installed game, a store update
                  ready, a new GE-Proton (Linux only)
  6 achievements  close to 100 % (at least ACH_CLOSE)
  7 idle          pick up again: unfinished, idle IDLE_MOMENT_DAYS or more
  8 disk          a drive over DISK_FULL used (not critical yet)

Rules: one moment per game (the higher kind wins); never the Stage's game;
at most STORE_MAX store moments; more than alerts.GROUP of one alert kind is
one summary with a count; at most TOTAL_MAX; ties go to the newest.
Not now snoozes one moment's snooze_key for SNOOZE_DAYS; Quiet until
tomorrow hides all but kind 1 until QUIET_HOUR local time; kinds_off hides
whole kinds (a kind or an alert kind)."""

import time
from datetime import date, datetime, timedelta

from nexus import achievements, alerts, smart
from nexus.features import has

KINDS = ('broken', 'running', 'friends', 'store', 'update', 'achievements', 'idle', 'disk')
RANK = {k: i + 1 for i, k in enumerate(KINDS)}
URGENCY = dict(zip(KINDS, (100, 85, 70, 60, 50, 40, 30, 20)))
STORE_SUBS = ('atl', 'sale', 'fitgirl')
GROUPED = ('atl', 'sale', 'fitgirl', 'repack_updates')   # alert kinds summed past GROUP
STORE_MAX = 2
TOTAL_MAX = 8
SNOOZE_DAYS = 7
SNOOZE_MAX = 200                    # remembered snoozes (prefs)
QUIET_HOUR = 4                      # Quiet until tomorrow ends at 04:00 local
IDLE_MOMENT_DAYS = 5                # Pick up again (the Unfinished shelf waits 30)
DISK_CRITICAL = 0.05                # free share under this: something broken
DISK_FULL = 0.80                    # used share over this: almost full (System tile warns too)
READY_HOURS = 24                    # a finished install stays "Ready to play" this long
ENDING_HOURS = 48                   # a sale ending this soon goes first
ACH_CLOSE = 0.75                    # "close to 100 %" from this share

LABELS = {'open_job': "View", 'launch': "Launch", 'open_deals': "View", 'update': "Update",
          'install_ge': "Install", 'resume': "Resume", 'open_storage': "Open",
          'open_health': "Open"}
SUMMARY = {'atl': "{n} wishlist games at their all-time low",
           'sale': "{n} wishlist deals",
           'fitgirl': "{n} wishlist games on FitGirl",
           'repack_updates': "{n} newer repacks"}


# ── time helpers ─────────────────────────────────────────────────────────

def _local(dt: datetime) -> datetime:
    """`dt` as a naive local time (an aware one is converted)."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _when_ts(value) -> float | None:
    """Epoch seconds from an ISO string (naive = local) or a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    t = smart._when(value) if value else None
    return t.timestamp() if t else None


def snooze_key(kind: str, game: str) -> str:
    """What Not now stores: '<kind or alert kind>:<game key>'."""
    return f"{kind}:{game}"


def snooze_until(now: datetime) -> str:
    """The date a snooze made `now` runs out (it shows again that day)."""
    return (_local(now).date() + timedelta(days=SNOOZE_DAYS)).isoformat()


def is_snoozed(snoozes, key: str, now: datetime) -> bool:
    """True while snoozes[key] (a 'YYYY-MM-DD' date) is after today. A bad
    value or a bad snoozes dict snoozes nothing."""
    if not isinstance(snoozes, dict):
        return False
    until = snoozes.get(key)
    if not isinstance(until, str):
        return False
    try:
        return _local(now).date() < date.fromisoformat(until)
    except ValueError:
        return False


def quiet_until_next(now: datetime) -> datetime:
    """The next QUIET_HOUR:00 on the local wall clock after `now` (naive).
    The hour is set on the date, so the night the clocks change it is still
    04:00, not 03:00 or 05:00."""
    now = _local(now)
    at = now.replace(hour=QUIET_HOUR, minute=0, second=0, microsecond=0)
    if at <= now:
        nxt = now.date() + timedelta(days=1)
        at = datetime(nxt.year, nxt.month, nxt.day, QUIET_HOUR)
    return at


def _quiet(quiet_until, now: datetime) -> bool:
    if isinstance(quiet_until, str):
        try:
            quiet_until = datetime.fromisoformat(quiet_until)
        except ValueError:
            return False
    if not isinstance(quiet_until, datetime):
        return False
    return _local(now) < _local(quiet_until)


# ── the ranking ──────────────────────────────────────────────────────────

def _moment(kind, key, title, why, action, arg, badge, when, sub=None, count=1,
            game=True, order=()) -> dict:
    return {'id': f"{kind}:{key}", 'kind': kind, 'sub': sub, 'rank': RANK[kind], 'key': key,
            'title': title, 'why': why,
            'action': {'id': action, 'label': LABELS[action], 'arg': arg},
            'badge': badge, 'urgency': URGENCY[kind], 'count': count, 'when': when,
            'snooze_key': snooze_key(sub or kind, key),
            '_game': game and bool(key), '_order': order}


def _visible(games) -> dict:
    return {g['slug']: g for g in games or () if isinstance(g, dict) and g.get('slug')
            and not g.get('hidden') and not g.get('dup_of')}


def _name(g: dict | None, fallback: str) -> str:
    return (g or {}).get('display_name') or fallback


def _broken(inputs, now_ts) -> list[dict]:
    out = []
    for c in inputs.get('checks') or ():
        if isinstance(c, dict) and c.get('status') == 'fail':
            key = f"health-{c.get('id') or c.get('title') or ''}"
            out.append(_moment('broken', key, c.get('title') or "A health check failed",
                               c.get('fix') or "Open Health to see what's wrong",
                               'open_health', c.get('id'), ('fail', 'danger'), now_ts,
                               sub='health', game=False))
    for d in inputs.get('disks') or ():
        total = (d or {}).get('total') or 0
        if not total:
            continue
        free = d.get('free') if d.get('free') is not None else total - (d.get('used') or 0)
        path = d.get('path') or '/'
        name = path.rstrip('/').rsplit('/', 1)[-1] or path
        if free / total < DISK_CRITICAL:
            out.append(_moment('broken', f"disk-{path}", f"{name} is nearly out of space",
                               f"{int(100 * free / total)}% free: installs and saves can fail",
                               'open_storage', path, ('storage', 'danger'), now_ts,
                               sub='disk', game=False))
        elif (d.get('used') or total - free) / total > DISK_FULL:
            out.append(_moment('disk', f"disk-{path}", f"{name} is almost full",
                               f"{int(100 * free / total)}% free",
                               'open_storage', path, ('storage', 'warning'), now_ts,
                               sub='disk', game=False))
    return out


def _running(inputs, games, now_ts) -> list[dict]:
    out = []

    def ready(slug, ended):
        g = games.get(slug)
        if g is None or ended is None or now_ts - ended > READY_HOURS * 3600:
            return
        played = _when_ts(g.get('last_played'))
        if played is not None and played >= ended:
            return                                     # played since: done with
        out.append(_moment('running', slug, _name(g, slug), "Installed · Ready to play",
                           'launch', slug, ('check_circle', 'success'), ended))

    for j in inputs.get('jobs') or ():
        if not isinstance(j, dict) or j.get('kind') != 'install':
            continue                                   # failed jobs are not moments
        slug = j.get('slug') or ''
        if j.get('active'):
            pct = j.get('progress')
            why = " · ".join(x for x in (f"{pct}%" if pct is not None else '',
                                         j.get('step') or '') if x) or "Installing"
            out.append(_moment('running', slug, j.get('title') or _name(games.get(slug), slug),
                               why, 'open_job', slug, ('download', 'accent'), now_ts))
        elif j.get('state') == 'done' and slug:
            ready(slug, _when_ts(j.get('ended')))
    for slug, t in (inputs.get('ready_at') or {}).items():
        ready(slug, _when_ts(t))
    for r in inputs.get('downloads') or ():
        if not isinstance(r, dict) or not r.get('tracked') or r.get('gone') \
                or r.get('installed') or r.get('done') or r.get('error'):
            continue
        key = f"torrent-{str(r.get('hash') or '')[:12]}"
        title = r.get('name') or r.get('title') or key
        pct = float(r.get('percent') or 0)
        out.append(_moment('running', key, title, f"Downloading · {pct:.0f}%", 'open_job', key,
                           ('download', 'accent'), now_ts, game=False))
    for slug, g in games.items():                      # Steam and Heroic downloads
        inst = g.get('installing')
        if isinstance(inst, dict):
            done, total = int(inst.get('done') or 0), int(inst.get('total') or 0)
            why = f"Downloading · {int(100 * done / total)}%" if total else "Downloading"
            out.append(_moment('running', slug, _name(g, slug), why, 'open_job', slug,
                               ('download', 'accent'), now_ts))
    return out


def _friends(inputs, now_ts) -> list[dict]:
    out = []
    for e in inputs.get('friends_now') or ():
        slug = (e or {}).get('slug')
        if not slug:
            continue                                   # not a game you own
        names = [p.get('name') or "A friend" for p in e.get('people') or ()]
        who = (", ".join(names) if len(names) <= 2 else
               f"{names[0]} and {len(names) - 1} more") or "Friends"
        minutes = int(e.get('minutes') or 0)
        out.append(_moment('friends', slug, f"{who} in {e.get('title') or slug}",
                           f"Playing now · {minutes} min" if minutes else "Playing now",
                           'launch', slug, ('friends', 'success'), now_ts - minutes * 60))
    return out


def _store(inputs, now_ts) -> list[dict]:
    out = []
    ids = inputs.get('store_ids') or {}
    wl = {str(w.get('appid')): w for w in inputs.get('deals') or ()
          if isinstance(w, dict) and w.get('appid') is not None}
    s = inputs.get('alert_settings') or {}
    deals = alerts.deal_alerts(list(wl.values()), s)

    def key_of(appid):
        return ids.get(str(appid)) or f"steam-{appid}"

    for sub in ('atl', 'sale'):
        if s.get(sub, True) is False:
            continue
        for a in deals[sub]:
            p = (wl.get(str(a['appid'])) or {}).get('price') or {}
            ends = _when_ts(p.get('endsAt'))
            soon = ends is not None and now_ts <= ends <= now_ts + ENDING_HOURS * 3600
            key = key_of(a['appid'])
            why = (f"{p.get('currentStr') or ''}"
                   + (f" · -{p['cutPct']}%" if p.get('cutPct') else '')
                   + (" · ends soon" if soon else '')).strip(' ·')
            out.append(_moment('store', key, a['title'], why or a['text'], 'open_deals',
                               a['appid'], ('deals', 'accent'), now_ts, sub=sub,
                               order=(not soon, STORE_SUBS.index(sub),
                                      -(p.get('cutPct') or 0))))
    if s.get('fitgirl', True) is not False:
        for appid, match in (inputs.get('repacks') or {}).items():
            match = match if isinstance(match, dict) else {}
            title = (wl.get(str(appid)) or {}).get('title') or match.get('title') \
                or f"App {appid}"
            out.append(_moment('store', key_of(appid), title, "On your wishlist · now on FitGirl",
                               'open_deals', appid, ('magnet', 'accent'),
                               _when_ts(match.get('date')) or now_ts, sub='fitgirl',
                               order=(True, STORE_SUBS.index('fitgirl'), 0)))
    return out


def _updates(inputs, games, now_ts, linux) -> list[dict]:
    out = []
    for slug in inputs.get('repack_updates') or {}:
        g = games.get(slug)
        if g is not None:
            out.append(_moment('update', slug, _name(g, slug), "A newer repack is downloaded",
                               'update', slug, ('update', 'warning'), now_ts,
                               sub='repack_updates'))
    for slug, g in games.items():
        if g.get('update_pending') and slug not in (inputs.get('repack_updates') or {}):
            out.append(_moment('update', slug, _name(g, slug), "An update is ready",
                               'update', slug, ('update', 'warning'), now_ts))
    ge = inputs.get('proton') or {}
    if linux and isinstance(ge, dict) and ge.get('update') and ge.get('latest'):
        m = _moment('update', '', f"{ge['latest']} is out",
                    "Installs next to your current Proton builds", 'install_ge',
                    ge['latest'], ('proton', 'warning'), now_ts, sub='proton', game=False)
        m['id'] = f"update:proton-{ge['latest']}"
        m['snooze_key'] = snooze_key('proton', ge['latest'])
        out.append(m)
    return out


def _achievements(inputs, games) -> list[dict]:
    counts = {k: v for k, v in (inputs.get('counts') or {}).items()
              if k in games and isinstance(v, (list, tuple)) and len(v) == 2
              and all(isinstance(x, int) and not isinstance(x, bool) for x in v) and v[1] > 0}
    out = []
    for slug in achievements.closest(counts, len(counts)):
        got, total = counts[slug]
        if got / total < ACH_CLOSE:
            break
        g = games[slug]
        out.append(_moment('achievements', slug, _name(g, slug),
                           f"{got} of {total} achievements · {total - got} to go",
                           'resume', slug, ('trophy', 'accent'),
                           _when_ts(g.get('last_played')) or 0))
    return out


def _idle(inputs, games, now) -> list[dict]:
    facts = inputs.get('facts') or {}
    out = []
    for slug, g in games.items():
        if g.get('installed') is False:
            continue
        p = smart.progress(g, facts.get(slug))
        days = smart._days_since(g, now)
        if p is None or p >= smart.DONE_AT or days is None or days < IDLE_MOMENT_DAYS \
                or (g.get('playtime_minutes') or 0) < smart.UNFINISHED_MIN:
            continue
        out.append(_moment('idle', slug, _name(g, slug),
                           f"Last played {int(days)} days ago · about {int(p * 100)}% through",
                           'resume', slug, ('history', 'accent'),
                           _when_ts(g.get('last_played')) or 0))
    return out


def _sort(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda m: (m['rank'], m['_order'], -m['when']))


def _summary(sub: str, items: list[dict]) -> dict:
    first = items[0]
    n = len(items)
    action = 'open_deals' if first['kind'] == 'store' else 'update'
    m = _moment(first['kind'], '', SUMMARY[sub].format(n=n),
                ", ".join(i['title'] for i in items[:3]) + ("…" if n > 3 else ""),
                action, [i['action']['arg'] for i in items], first['badge'],
                max(i['when'] for i in items), sub=sub, count=n, game=False,
                order=first['_order'])
    m['id'] = f"{first['kind']}:{sub}"
    m['action']['label'] = "View"
    return m


def home_moments(inputs: dict, now: datetime, *, snoozes=None, quiet_until=None,
                 kinds_off=(), stage_key=None, linux=True, limit=TOTAL_MAX) -> list[dict]:
    """For you now, highest first (see the module docstring). `inputs` holds
    plain data: jobs, ready_at, downloads, disks, checks, games, friends_now,
    deals (the wishlist), alert_settings, repacks, repack_updates, store_ids,
    proton (the GE family), counts, facts. Missing keys count as empty."""
    inputs = inputs if isinstance(inputs, dict) else {}
    now = _local(now)
    now_ts = now.timestamp()
    games = _visible(inputs.get('games'))
    cands = (_broken(inputs, now_ts) + _running(inputs, games, now_ts)
             + _friends(inputs, now_ts) + _store(inputs, now_ts)
             + _updates(inputs, games, now_ts, linux) + _achievements(inputs, games)
             + _idle(inputs, games, now))
    off = set(kinds_off or ())
    quiet = _quiet(quiet_until, now)

    def shown(m):
        return not (m['kind'] in off or m['sub'] in off
                    or (quiet and m['kind'] != 'broken')
                    or is_snoozed(snoozes, m['snooze_key'], now))

    seen, kept = set(), []
    for m in _sort(c for c in cands if shown(c) and not (stage_key and c['key'] == stage_key)):
        if m['_game']:
            if m['key'] in seen:
                continue                               # a higher kind has this game
            seen.add(m['key'])
        kept.append(m)
    for sub in GROUPED:
        group = [m for m in kept if m['sub'] == sub]
        if len(group) > alerts.GROUP:
            kept = [m for m in kept if m['sub'] != sub]
            summary = _summary(sub, group)
            if shown(summary):
                kept.append(summary)
    out, store = [], 0
    for m in _sort(kept):
        if m['kind'] == 'store':
            store += 1
            if store > STORE_MAX:
                continue
        del m['_game'], m['_order']
        out.append(m)
    return out[:max(0, min(limit, TOTAL_MAX))]


# ── reading the window ───────────────────────────────────────────────────

def _playing_now(games, now: float) -> list[dict]:
    from .friends import playing_now                   # the window's friends data
    return playing_now(games, now, limit=None)


def stage_key(hub) -> str | None:
    """The slug of the game on Home's Stage, if there is one."""
    stage = getattr(getattr(hub, 'dashboard', None), 'stage', None)
    game = getattr(getattr(stage, 'art', None), 'game', None)
    return game.get('slug') if isinstance(game, dict) and game.get('slug') else None


def moments_inputs(hub) -> dict:
    """home_moments' inputs from what the window holds now: no refresh, no
    CLI call, no widget touched. Job.ended is time.monotonic(): turned into
    epoch seconds here."""
    from .dashboard import _game_store_id, job_facts
    games = list(getattr(hub, '_games', None) or [])
    wall, mono = time.time(), time.monotonic()
    jobs = []
    for f in job_facts(getattr(getattr(hub, 'jobs', None), 'jobs', None) or []):
        j = f.pop('job')
        ended = getattr(j, 'ended', None)
        jobs.append({**f, 'kind': getattr(j, 'kind', None), 'slug': getattr(j, 'slug', None),
                     'ended': wall - (mono - ended) if ended is not None else None})
    deals = getattr(hub, 'dealspage', None)
    summary = deals._data() if deals is not None else {}
    proton = None
    if has('proton'):
        info = getattr(getattr(hub, 'protonpage', None), 'info', None) or {}
        ge = (info.get('families') or {}).get('ge')
        proton = dict(ge) if isinstance(ge, dict) else None
    facts = getattr(hub, '_smart_facts', None)
    store_ids = {}
    for g in games:
        sid = _game_store_id(g)
        if sid is not None and not g.get('hidden') and not g.get('dup_of'):
            store_ids.setdefault(str(sid), g['slug'])
    return {
        'jobs': jobs,
        'ready_at': dict(getattr(hub, '_ready_at', None) or {}),
        'downloads': list(getattr(hub, '_mine_torrents', None) or []),
        'disks': list(getattr(hub, '_disks', None) or []),
        'checks': list(getattr(getattr(hub, 'health', None), 'checks', None) or []),
        'games': games,
        'friends_now': _playing_now(games, wall),
        'deals': list((summary or {}).get('wishlist') or []),
        'alert_settings': alerts.settings(),
        'repacks': dict(deals.repacks()) if deals is not None else {},
        'repack_updates': dict(getattr(hub, '_repack_updates', None) or {}),
        'store_ids': store_ids,
        'proton': proton,
        'counts': {g['slug']: list(g['achievements']) for g in games
                   if g.get('source') == 'steam' and isinstance(g.get('achievements'),
                                                                  (list, tuple))
                   and len(g['achievements']) == 2},
        'facts': facts() if callable(facts) else {},
    }
