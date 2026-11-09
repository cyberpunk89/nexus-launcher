"""Send a report anonymously, through the Nexus reports relay.

    payload(draft, selection, contact, mention_me) -> the JSON the relay takes
    status(url) -> {mode, pow_bits, min_client, repo} or {'error': ...}
    send(url, payload) -> {status, where, issue, token} or {'error': ...}
    track(url, tokens), similar(url, sig): what became of sent reports, and
        a public issue with the same crash signature
    count(url, body): the opt-in anonymous install count (nexus/stats.py)
    load_sent / refresh_sent / remove_sent / describe: reports/sent.jsonl

The relay (github.com/cyberpunk89/nexus-reports) files it in a private
inbox through a GitHub App; nothing here holds a key. The payload carries
only the groups the user kept, with removed lines already blanked, so what
the relay renders is the preview the user read. Every send is noted in
reports/sent.jsonl (what was sent, where it went and the relay's token for
it), never the contact. The token is the only handle on a sent report: the
relay answers a token with a coarse state (waiting for review, #N open,
fixed in X, closed) and nothing from the inbox. The daily update check asks
once a day; rows go after 90 days or when the user removes them."""

import json
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import render as RD

GH_USER = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$')
DEFAULT_URL = 'https://nexus-reports-relay.vercel.app'
TIMEOUT = 20
COUNT_TIMEOUT = 10                  # the install count: small, and never in a hurry
MAX_LINE = 400                      # the relay refuses longer lines


def relay_url(cfg: dict) -> str | None:
    """The relay base URL, or None when [report] relay = false."""
    if not cfg.get('report_relay', True):
        return None
    return (cfg.get('report_url') or DEFAULT_URL).rstrip('/')


def payload(draft: dict, selection: dict, contact: str | None = None,
            mention_me: bool = False) -> dict:
    from nexus import __version__
    out = RD.render(draft, selection)
    groups: dict[str, list[str]] = {}
    removed = 0
    for ln in out['lines']:
        if not ln['included']:
            continue
        removed += ln['removed']
        text = RD.REMOVED if ln['removed'] else ln['text']
        groups.setdefault(ln['group'], []).append(text[:MAX_LINE])
    contact = (contact or '').strip().lstrip('@') or None
    return {'schema': 1, 'client': __version__, 'kind': draft.get('kind') or 'user-report',
            'sig': draft.get('signature') or None,
            'version': str(draft.get('version') or __version__),
            'edition': draft.get('edition') or 'linux', 'title': out['title'],
            'groups': groups, 'removed_lines': removed,
            'description': str(selection.get('description') or '')[:RD.DESCRIPTION_MAX],
            'contact': contact, 'mention_me': bool(mention_me and contact)}


def _call(req, timeout: float = TIMEOUT) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read() or b'{}')
        except ValueError:
            data = {}
        return {'error': data.get('error') or f"http-{e.code}",
                'reason': data.get('reason') or f"The relay answered {e.code}."}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {'error': 'offline', 'reason': f"Couldn't reach the relay ({e})."}


def version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r'\d+', str(v))[:3])


def _headers() -> dict:
    from nexus import __version__
    return {'User-Agent': f"Nexus/{__version__}", 'Content-Type': 'application/json'}


def status(url: str) -> dict:
    return _call(urllib.request.Request(f"{url}/v1/status", headers=_headers()))


def send(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    return _call(urllib.request.Request(f"{url}/v1/report", data=data, method='POST',
                                        headers=_headers()))


def _post(url: str, route: str, body: dict, timeout: float = TIMEOUT) -> dict:
    return _call(urllib.request.Request(f"{url}/v1/{route}", data=json.dumps(body).encode(),
                                        method='POST', headers=_headers()), timeout)


def count(url: str, body: dict) -> dict:
    """One anonymous install-count ping (nexus/stats.py builds it):
    {'status': 'counted'}, or {'error': 'limit'|'off'|'schema'|'offline', ...}."""
    return _post(url, 'count', body, COUNT_TIMEOUT)


def track(url: str, tokens: list[str]) -> dict:
    """{token: {state, issue, url, fixed_in}} for the tokens the relay knows,
    or {'error': ...}."""
    out = _post(url, 'track', {'tokens': tokens[:TRACK_MAX]})
    return out if 'error' in out else (out.get('reports') or {})


def similar(url: str, sig: str) -> dict:
    """{'match': {state, issue, url, fixed_in} | None} or {'error': ...}."""
    out = _post(url, 'similar', {'sig': sig})
    if 'error' in out:
        return out
    m = out.get('match')
    return {'match': _clean(m) if isinstance(m, dict) and m.get('issue') else None}


# ---- sent.jsonl: the reports this computer sent -----------------------------

SENT = 'sent.jsonl'
KEEP_DAYS = 90
RECHECK = 24 * 3600                 # one look a day, with the update check
TRACK_MAX = 50
SENT_ID = re.compile(r'^[\w-]{1,40}$')
ISSUE_URL = re.compile(r'^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$')
STATES = ('held', 'open', 'fixed', 'closed')
STAMP = '%Y-%m-%dT%H:%M:%SZ'


def _now() -> str:
    return datetime.now(timezone.utc).strftime(STAMP)


def _age(stamp, now: datetime) -> float | None:
    try:
        t = datetime.strptime(str(stamp), STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - t).total_seconds()


def _clean(t: dict) -> dict:
    """Only the keys and shapes the page shows; anything odd is dropped."""
    state = t.get('state') if t.get('state') in STATES else None
    n = t.get('issue') if isinstance(t.get('issue'), int) and t['issue'] > 0 else None
    url = t.get('url') if isinstance(t.get('url'), str) and ISSUE_URL.match(t['url']) else None
    fix = t.get('fixed_in')
    fix = fix if isinstance(fix, str) and re.match(r'^[\w.+-]{1,30}$', fix) else None
    return {'state': state, 'issue': n, 'url': url if n else None, 'fixed_in': fix}


def _sent_path():
    from .crash import _dir
    return _dir() / SENT


def note_sent(body: dict, answer: dict) -> str | None:
    """One line in reports/sent.jsonl: when, which kind, signature and title,
    where it went and the relay's token for tracking it. Never the contact
    or the text. Returns the line's id."""
    sid = secrets.token_hex(6)
    issue = answer.get('issue') if ISSUE_URL.match(str(answer.get('issue') or '')) else None
    try:
        path = _sent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'id': sid, 'at': _now(), 'kind': body.get('kind'),
                                'sig': body.get('sig'), 'title': str(body.get('title') or '')[:160],
                                'status': answer.get('status'), 'where': answer.get('where'),
                                'issue': issue, 'token': answer.get('token') or None}) + '\n')
    except OSError:
        return None
    return sid


def load_sent(now: datetime | None = None) -> list[dict]:
    """The sent reports, newest first, without those older than 90 days.
    Lines from before tracking (no id, no token) still load."""
    now = now or datetime.now(timezone.utc)
    try:
        text = _sent_path().read_text(encoding='utf-8')
    except OSError:
        return []
    rows, seen = [], set()
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        age = _age(r.get('at'), now)
        if age is not None and age > KEEP_DAYS * 86400:
            continue
        if not SENT_ID.match(str(r.get('id') or '')):
            r['id'] = 'old-' + re.sub(r'\D', '', str(r.get('at') or ''))[:14] or 'old'
        while r['id'] in seen:
            r['id'] += '-'
        seen.add(r['id'])
        rows.append(r)
    return rows[::-1]


def _save_sent(rows: list[dict]) -> bool:
    path = _sent_path()
    tmp = path.with_name(SENT + '.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for r in reversed(rows):                     # file order: oldest first
                f.write(json.dumps(r) + '\n')
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def remove_sent(sid: str) -> bool:
    rows = load_sent()
    keep = [r for r in rows if r['id'] != sid]
    return len(keep) != len(rows) and _save_sent(keep)


def refresh_sent(url: str, force: bool = False, now: datetime | None = None) -> dict:
    """Ask the relay about the sent reports not looked at in the last day.
    {'checked': n} or {'error', 'reason'}; offline keeps the old states and
    stamps nothing, so the next daily check tries again."""
    now = now or datetime.now(timezone.utc)
    def stale(r):
        age = _age(r.get('checked'), now)
        return force or age is None or age > RECHECK
    due = [r['token'] for r in load_sent(now) if r.get('token') and stale(r)]
    if not due:
        return {'checked': 0}
    answer = track(url, due)
    if 'error' in answer:
        return answer
    stamp, due = now.strftime(STAMP), set(due[:TRACK_MAX])
    rows = load_sent(now)                # again: a send may have landed meanwhile
    for r in rows:
        if r.get('token') in due:
            if isinstance(answer.get(r['token']), dict):
                r['track'] = _clean(answer[r['token']])
            r['checked'] = stamp
    _save_sent(rows)
    return {'checked': len(due)}


def describe(row: dict) -> str:
    """One line for the Your reports list, e.g. '#123 fixed in 0.5.4'."""
    t = row.get('track') or {}
    n = t.get('issue')
    if not n:
        m = re.search(r'/issues/(\d+)$', str(row.get('issue') or ''))
        n = int(m.group(1)) if m else None
    state = t.get('state')
    if state == 'fixed' and n:
        return f"#{n} fixed in {t['fixed_in']}" if t.get('fixed_in') else f"#{n} fixed"
    if state == 'closed':
        return f"#{n} closed" if n else "Closed after review"
    if state == 'open' and n:
        return f"#{n} open"
    if n:
        return f"#{n}"
    if not row.get('token'):
        return "Sent, status unknown"
    return "Received, waiting for review"


def public_row(row: dict) -> dict:
    """A sent.jsonl row for the window: no token (it never leaves the CLI)."""
    t = row.get('track') or {}
    link = t.get('url') or (row.get('issue') if ISSUE_URL.match(str(row.get('issue') or ''))
                            else None)
    return {'id': row['id'], 'at': row.get('at'), 'kind': row.get('kind'),
            'title': row.get('title') or '', 'state': t.get('state'), 'url': link,
            'checked': row.get('checked'), 'text': describe(row)}
