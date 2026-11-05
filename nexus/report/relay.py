"""Send a report anonymously, through the Nexus reports relay.

    payload(draft, selection, contact, mention_me) -> the JSON the relay takes
    status(url) -> {mode, pow_bits, min_client, repo} or {'error': ...}
    send(url, payload) -> {status, where, issue} or {'error': ...}

The relay (github.com/cyberpunk89/nexus-reports) files it in a private
inbox through a GitHub App; nothing here holds a key. The payload carries
only the groups the user kept, with removed lines already blanked, so what
the relay renders is the preview the user read. Every send is noted in
reports/sent.jsonl (what was sent and where it went), never the contact."""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import render as RD

GH_USER = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$')
DEFAULT_URL = 'https://nexus-reports-relay.vercel.app'
TIMEOUT = 20
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


def _call(req) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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


def note_sent(body: dict, answer: dict) -> None:
    """One line in reports/sent.jsonl: when, which kind and signature, where
    it went. Never the contact or the text."""
    from .crash import _dir
    try:
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / 'sent.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                'kind': body.get('kind'), 'sig': body.get('sig'),
                                'status': answer.get('status'), 'where': answer.get('where'),
                                'issue': answer.get('issue')}) + '\n')
    except OSError:
        pass
