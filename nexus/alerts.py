"""Alerts worth a push to the phone (config [notify]) and, with no Nexus
window open, a desktop notification: a wishlist game at its all-time low
(or on a big sale), a wishlist game appearing on FitGirl, a newer repack of
an installed game, a new GE-Proton. Each is sent once — remembered by a key
in DATA_DIR/alerts.json, edited under a lock so the GUI and the background
agent never both send one.

    [alerts]
    atl = true              # wishlist games at their all-time low
    sale_min_cut = 0        # also any sale of at least this % (0 = off)
    fitgirl = true          # wishlist games that get a FitGirl repack
    repack_updates = true   # newer repacks of games you installed
    proton = true           # a new GE-Proton release

The first time a kind is checked, what's already true is only recorded:
switching alerts on doesn't push forty games that were at their low all
along."""

import contextlib
import json
import time
import tomllib

from . import net, platform
from .paths import CONFIG_PATH, DATA_DIR

STATE = DATA_DIR / 'alerts.json'
KINDS = ('atl', 'sale', 'fitgirl', 'repack_updates', 'proton')
# Kinds whose condition can stop being true (a sale ends): their keys are
# forgotten once they are, so the next sale or low alerts again.
TRANSIENT = ('atl', 'sale')
KEEP = 2000                         # remembered keys of the other kinds
GROUP = 3                           # more new alerts of a kind than this: one summary


def settings() -> dict:
    try:
        whole = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        whole = {}
    data = whole.get('alerts') or {}
    rp = whole.get('repacks')
    repacks = not isinstance(rp, dict) or rp.get('enabled', True) is not False
    try:
        cut = max(0, min(100, int(data.get('sale_min_cut', 0))))
    except (TypeError, ValueError):
        cut = 0
    out = {k: data.get(k, True) is not False for k in ('atl', 'fitgirl', 'repack_updates',
                                                        'proton')}
    if not repacks:                       # Settings > Repack features off
        out['fitgirl'] = out['repack_updates'] = False
    out['sale'] = cut > 0
    out['sale_min_cut'] = cut
    return out


def _load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault('sent', {})
    data.setdefault('primed', [])
    return data


@contextlib.contextmanager
def _edit():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with platform.file_lock(STATE.with_name(STATE.name + '.lock')):
        data = _load()
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            net.save_json(STATE, data)


# ── what's true now, per kind ────────────────────────────────────────────

def deal_alerts(wishlist: list, s: dict) -> dict[str, list[dict]]:
    """{'atl': [...], 'sale': [...]} for the wishlist as Game-db sees it."""
    out = {'atl': [], 'sale': []}
    for g in wishlist or ():
        p = g.get('price') or {}
        appid, title = g.get('appid'), g.get('title') or f"App {g.get('appid')}"
        if appid is None:
            continue
        price = p.get('currentStr') or ''
        cut = f" (-{p['cutPct']}%)" if p.get('cutPct') else ''
        # A low at full price (never discounted yet, a pre-order) isn't news.
        if p.get('atHistoricalLow') and (p.get('onSale') or p.get('cutPct')):
            out['atl'].append({'key': f"atl:{appid}:{price}", 'kind': 'atl', 'title': title,
                               'text': f"{title} is at its all-time low: {price}{cut}",
                               'appid': appid})
        elif p.get('onSale') and s.get('sale') and (p.get('cutPct') or 0) >= s['sale_min_cut']:
            out['sale'].append({'key': f"sale:{appid}:{p.get('cutPct')}", 'kind': 'sale',
                                'title': title, 'text': f"{title} is on sale: {price}{cut}",
                                'appid': appid})
    return out


def fitgirl_alerts(news: list[dict]) -> list[dict]:
    """New wishlist matches from repackindex (already new: it keeps its own
    'alerted' list, and says when the first sync was a summary)."""
    return [{'key': f"fitgirl:{n['appid']}", 'kind': 'fitgirl',
             'title': n.get('title') or n.get('slug'),
             'text': f"{n.get('title') or n.get('slug')} is on FitGirl", 'appid': n['appid']}
            for n in news or ()]


def update_alerts(updates: dict) -> list[dict]:
    """{slug: post} from repackfeed.updates()."""
    return [{'key': f"update:{slug}:{post.get('url') or post.get('date')}",
             'kind': 'repack_updates', 'title': post.get('title') or slug, 'slug': slug,
             'text': f"FitGirl posted an update: {post.get('title') or slug}"}
            for slug, post in sorted((updates or {}).items())]


def proton_alerts(latest_ge: str | None, update: bool) -> list[dict]:
    if not (latest_ge and update):
        return []
    return [{'key': f"proton:{latest_ge}", 'kind': 'proton', 'title': latest_ge,
             'text': f"{latest_ge} is out — install it from Nexus's Proton page"}]


# ── deciding what's new, and sending it ──────────────────────────────────

def fresh(kind: str, items: list[dict], s: dict | None = None,
          prime: bool = True) -> list[dict]:
    """The items of `kind` not sent before (and records them as sent). With
    the kind switched off, or the first time it is checked (`prime`), it
    only records."""
    s = s or settings()
    with _edit() as data:
        sent = data['sent']
        keys = {i['key'] for i in items}
        if kind in TRANSIENT:              # forget lows/sales that ended
            for k in [k for k in sent if k.startswith(kind + ':') and k not in keys]:
                del sent[k]
        new = [i for i in items if i['key'] not in sent]
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        for i in new:
            sent[i['key']] = now
        if len(sent) > KEEP:
            for k in sorted(sent, key=sent.get)[:len(sent) - KEEP]:
                del sent[k]
        first = kind not in data['primed']
        if first:
            data['primed'].append(kind)
        if not s.get(kind, True) or (first and prime):
            return []
        return new


def summarize(kind: str, new: list[dict]) -> tuple[str, str]:
    """(title, body) for one push about `new` alerts of one kind."""
    if len(new) == 1:
        return "Nexus", new[0]['text']
    names = ", ".join(i['title'] for i in new[:6]) + ("…" if len(new) > 6 else "")
    head = {'atl': f"{len(new)} wishlist games are at their all-time low",
            'sale': f"{len(new)} wishlist games are on sale",
            'fitgirl': f"{len(new)} wishlist games are on FitGirl",
            'repack_updates': f"FitGirl posted {len(new)} updates",
            'proton': "New Proton releases"}.get(kind, f"{len(new)} alerts")
    return head, names


def messages(new: list[dict]) -> list[tuple[str, str]]:
    """(title, body) for each notification about `new`: one per alert, or
    one summary per kind once there are more than GROUP of it."""
    by_kind: dict[str, list] = {}
    for i in new:
        by_kind.setdefault(i['kind'], []).append(i)
    return [msg for kind, items in by_kind.items()
            for msg in ([summarize(kind, items)] if len(items) > GROUP else
                        [summarize(kind, [i]) for i in items])]


def deliver(new: list[dict], desktop: bool = False, push: bool = True) -> list[str]:
    """Push `new` (see messages) and, with `desktop`, show it as a desktop
    notification (platform.notify_local). Returns errors."""
    from . import notify
    errs = []
    s = notify.settings()
    for title, body in messages(new):
        if push and notify.configured(s):
            errs += notify.push(title, body, True, s)
        if desktop:
            try:
                platform.notify_local(title, body)
            except OSError as e:
                errs.append(f"desktop notification: {e}")
    return errs
