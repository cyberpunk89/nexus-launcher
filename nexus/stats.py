"""The anonymous install count: opt-in, at most one small ping a day.

Nothing is sent until the user says yes ([stats] count = true; a missing key
means "not asked yet"). The ping has no id of any kind. Like Fedora's
countme it says which periods this is the install's first ping of: the UTC
day (d), the ISO week (w) and the month (m), so the relay can count daily,
weekly and monthly installs by adding one per field and period, without
ever keeping a ping as a row.

    state_of(cfg) -> 'on' | 'off' | 'unasked' | 'blocked'
    blocked(cfg)  -> why it can't send even when on: 'NEXUS_NO_STATS',
                     'DO_NOT_TRACK' or 'relay' ([report] relay = false)
    due(state, now) -> the periods not yet counted ([] while backing off)
    payload(periods, state, now) -> the exact JSON sent (schema 1)
    ping(cfg, now) -> what happened: {'skipped': why} or {'sent', 'ok', ...}
    load_sent() -> the last 100 pings this computer sent, newest first

State lives in DATA_DIR/stats.json ({first, last_d, last_w, last_m,
retry_after}); `first` is the ISO week of the first count and never leaves
the machine (only its age bucket does). A failed send stamps no period, only
a 3 h pause. DATA_DIR/stats.lock keeps the agent and the window from both
counting one day. Every send is logged in LOG_DIR/stats-sent.jsonl, which
`nexus-cli --stats-sent` and Settings show. Qt-free, stdlib only."""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

from nexus import net, paths, platform

DATA_DIR = paths.DATA_DIR
LOG_DIR = paths.LOG_DIR
STATE = 'stats.json'
LOCK = 'stats.lock'
SENT = 'stats-sent.jsonl'
KEEP_SENT = 100
RETRY = timedelta(hours=3)
SCHEMA = 1
PERIODS = ('d', 'w', 'm')
STAMP = '%Y-%m-%dT%H:%M:%SZ'
OFF_ENV = ('NEXUS_NO_STATS', 'DO_NOT_TRACK')

# The same lists as the relay's (nexus-reports relay/stats.py): anything else
# is sent as "other", so an unusual setup can't single anyone out.
ALLOWED = {
    'install': ('git', 'source', 'win-installer', 'win-portable'),
    'os': ('arch', 'cachyos', 'endeavouros', 'manjaro', 'garuda', 'artix',
           'fedora', 'nobara', 'bazzite', 'ultramarine', 'rhel', 'centos', 'almalinux',
           'rocky', 'ubuntu', 'pop', 'linuxmint', 'zorin', 'elementary', 'kubuntu', 'debian',
           'mx', 'kali', 'opensuse-tumbleweed', 'opensuse-leap', 'opensuse-aeon', 'nixos',
           'gentoo', 'void', 'solus', 'steamos', 'chimeraos', 'slackware', 'alpine',
           'vanilla', 'bluefin', 'aurora'),
    'desktop': ('kde', 'gnome', 'niri', 'hyprland', 'sway', 'river', 'xfce',
                'cinnamon', 'mate', 'lxqt', 'lxde', 'budgie', 'pantheon', 'deepin', 'cosmic',
                'gamescope', 'i3', 'awesome', 'qtile', 'wayfire', 'labwc', 'unity', 'enlightenment'),
    'session': ('wayland', 'x11', 'tty'),
}
# Rolling releases have no version worth sending.
ROLLING = {'arch', 'cachyos', 'endeavouros', 'manjaro', 'garuda', 'artix', 'gentoo', 'void',
           'opensuse-tumbleweed', 'opensuse-aeon', 'chimeraos', 'bluefin', 'aurora'}
# Year.month releases, where both parts are the version (Ubuntu 24.04).
YEAR_MONTH = {'ubuntu', 'pop', 'kubuntu'}
AGES = ((2, '<2w'), (5, '2-5w'), (25, '5-25w'))


# ---- who may send ---------------------------------------------------------

def _env_on(name: str) -> bool:
    v = os.environ.get(name, '').strip().lower()
    return v not in ('', '0', 'false', 'no', 'off')


def blocked(cfg: dict) -> str | None:
    """Why nothing may be sent whatever the setting says, or None."""
    for name in OFF_ENV:
        if _env_on(name):
            return name
    if not cfg.get('report_relay', True):
        return 'relay'
    return None


def state_of(cfg: dict) -> str:
    """'unasked' (no [stats] count yet), 'off', 'on', or 'blocked' (on, but
    an environment variable or [report] relay = false stops it)."""
    v = cfg.get('stats_count')
    if v is None:
        return 'unasked'
    if v is not True:
        return 'off'
    return 'blocked' if blocked(cfg) else 'on'


# ---- periods --------------------------------------------------------------

def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _week(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def keys(now: datetime | None = None) -> dict:
    """{'d': '2026-11-09', 'w': '2026-W46', 'm': '2026-11'} for `now` in UTC."""
    d = _utc(now).date()
    return {'d': d.isoformat(), 'w': _week(d), 'm': d.strftime('%Y-%m')}


def _parse(stamp) -> datetime | None:
    try:
        return datetime.strptime(str(stamp), STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def due(state: dict, now: datetime | None = None) -> list[str]:
    """The periods this install hasn't been counted in yet, in d, w, m
    order; [] while a failed send's pause runs."""
    now = _utc(now)
    retry = _parse(state.get('retry_after'))
    if retry is not None and now < retry:
        return []
    k = keys(now)
    return [p for p in PERIODS if state.get('last_' + p) != k[p]]


def stamped(state: dict, periods, now: datetime | None = None) -> dict:
    """`state` after a counted ping for `periods`."""
    k = keys(now)
    out = {key: v for key, v in state.items() if key != 'retry_after'}
    for p in periods:
        out['last_' + p] = k[p]
    out.setdefault('first', k['w'])
    return out


def age(first, now: datetime | None = None) -> str:
    """The countme-style age bucket from the ISO week of the first count."""
    m = re.match(r'^(\d{4})-W(\d{2})$', str(first or ''))
    if not m:
        return AGES[0][1]
    try:
        start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return AGES[0][1]
    d = _utc(now).date()
    weeks = ((d - timedelta(days=d.weekday())) - start).days // 7
    return next((name for n, name in AGES if weeks < n), '25w+')


# ---- the ping -------------------------------------------------------------

def _pick(field: str, v) -> str | None:
    if not v:
        return None
    v = str(v).strip().lower()
    return v if v in ALLOWED[field] else 'other'


def _os_ver(os_id: str, version: str) -> str | None:
    if os_id in ROLLING or os_id == 'other':
        return None
    m = re.match(r'^(\d{1,4})(?:\.(\d{1,2}))?', str(version or ''))
    if not m:
        return None
    return m.group(0) if os_id in YEAR_MONTH and m.group(2) else m.group(1)


def _desktop() -> str | None:
    """The first known desktop in XDG_CURRENT_DESKTOP ("ubuntu:GNOME" is
    gnome, "X-Cinnamon" cinnamon); 'other' when none is known."""
    raw = os.environ.get('XDG_CURRENT_DESKTOP', '').strip()
    if not raw:
        return None
    for tok in raw.lower().split(':'):
        tok = tok.strip()
        tok = tok[2:] if tok.startswith('x-') else tok
        if tok in ALLOWED['desktop']:
            return tok
    return 'other'


def payload(periods, state: dict | None = None, now: datetime | None = None) -> dict:
    """What a ping carries: the allow-listed facts below and nothing else
    (a new key is refused by the relay)."""
    from nexus import __version__
    from nexus.report import sysinfo
    rel = platform.os_release() or {}
    edition = 'windows' if platform.OS_NAME == 'windows' else 'linux'
    if edition == 'windows':
        os_id, desktop, session = 'windows', 'windows', None
        os_ver = rel.get('version') if rel.get('version') in ('10', '11') else None
    else:
        os_id = _pick('os', rel.get('id')) or 'other'
        os_ver = _os_ver(os_id, rel.get('version'))
        desktop = _desktop()
        session = _pick('session', os.environ.get('XDG_SESSION_TYPE', ''))
    lang = platform.ui_language()
    return {'schema': SCHEMA, 'client': __version__,
            'periods': [p for p in PERIODS if p in periods],
            'edition': edition, 'install': _pick('install', sysinfo.install_kind()) or 'other',
            'os': os_id, 'os_ver': os_ver, 'desktop': desktop, 'session': session,
            'lang': lang if isinstance(lang, str) and re.match(r'^[a-z]{2}$', lang) else None,
            'py': f"{sys.version_info[0]}.{sys.version_info[1]}",
            'age': age((state or {}).get('first'), now)}


# ---- state and the log ----------------------------------------------------

def load_state() -> dict:
    try:
        data = json.loads((DATA_DIR / STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> bool:
    return net.save_json(DATA_DIR / STATE, state)


def load_sent() -> list[dict]:
    """The logged pings, newest first: {at, payload, answer}."""
    try:
        text = (LOG_DIR / SENT).read_text(encoding='utf-8')
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get('payload'), dict):
            rows.append({'at': r.get('at'), 'payload': r['payload'],
                         'answer': r.get('answer') if isinstance(r.get('answer'), dict) else {}})
    return rows[::-1][:KEEP_SENT]


def _note_sent(now: datetime, body: dict, answer: dict) -> None:
    rows = load_sent()[::-1][-(KEEP_SENT - 1):]
    rows.append({'at': now.strftime(STAMP), 'payload': body,
                 'answer': {k: answer[k] for k in ('status', 'error', 'reason') if k in answer}})
    try:
        net.write_atomic(LOG_DIR / SENT, ''.join(json.dumps(r) + '\n' for r in rows))
    except OSError:
        pass


def ping(cfg: dict, now: datetime | None = None) -> dict:
    """Count this install for the periods that are due, if the user said
    yes. Never raises; most calls return {'skipped': 'not-due'} at once."""
    from nexus.report import relay
    st = state_of(cfg)
    if st != 'on':
        return {'skipped': st}
    url = relay.relay_url(cfg)
    if not url:
        return {'skipped': 'blocked'}
    now = _utc(now)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with platform.file_lock(DATA_DIR / LOCK, blocking=False):
            state = load_state()
            periods = due(state, now)
            if not periods:
                return {'skipped': 'not-due'}
            body = payload(periods, state, now)
            answer = relay.count(url, body)
            ok = answer.get('status') == 'counted'
            if ok:
                state = stamped(state, periods, now)
            else:
                state = {**state, 'retry_after': (now + RETRY).strftime(STAMP)}
            save_state(state)
            _note_sent(now, body, answer)
            return {'sent': True, 'ok': ok, 'periods': body['periods'],
                    'error': None if ok else answer.get('error') or 'failed'}
    except OSError:                         # held by the other process (or no disk)
        return {'skipped': 'busy'}
