"""The downloads pipeline: one card per game Nexus downloads, from the torrent
client through the install to Ready — Download, Verify, Install, Finish.

Pure (no OS calls, no feature checks, no Qt), so the CLI, the desktop page,
Theatre and the tray build the same cards from the same data:

- the torrent rows of `--torrents --all` (torrent.all_status: the client's
  view merged with torrents.json: install_started, installed, installed_at,
  install_failed, repack_bytes, original_bytes);
- the progress files the install CLI keeps while it runs
  (DATA_DIR/pipeline/<hash>.json, torrent.load_progress), which is how an
  install the background agent runs with no window open shows up too.

The install's progress is an estimate against the repack page's "Original
Size", so it stops at 99% until the install reports success. An update
installed on top of a game starts from a full folder and never gets a
percent. The stage weights only draw the track."""

from __future__ import annotations

from datetime import datetime

STAGES = (('download', .52), ('verify', .10), ('install', .33), ('finish', .05))
FRESH_S = 60                # a progress file older than this is a dead install's
STUCK_S = 86400             # started this long ago without a word: it stopped
READY_HOURS = 24            # an installed game stays a Ready card this long
CAP = .99                   # the most an unfinished card shows

_STATE = {'verify': 'verifying', 'install': 'installing', 'finish': 'finishing'}
# Why an install ended without the game (torrents.json install_failed_reason;
# 'stopped' is the pipeline's own: killed, no heartbeat left).
FAILED_STEPS = {'error': "The install didn't finish", 'uac': "The admin prompt was declined",
                'cancelled': "Install cancelled", 'stopped': "The install stopped"}


def download_state(r: dict) -> str:
    """The client's side of a torrent row: gone, error, done, paused, queued,
    checking or downloading (anything else the client says)."""
    if r.get('gone'):
        return 'gone'
    if r.get('error'):
        return 'error'
    if r.get('done'):
        return 'done'
    state = r.get('state')
    if state == 'paused' or (r.get('paused') and not state):
        return 'paused'
    if state in ('queued', 'checking'):
        return state
    return 'downloading'


def recent(p, now: float) -> bool:
    """A progress file written in the last minute (the install rewrites it
    every few seconds while it runs)."""
    if not isinstance(p, dict) or p.get('stage') not in _STATE:
        return False
    try:
        age = now - float(p.get('updated_at'))
    except (TypeError, ValueError):
        return False
    return -FRESH_S < age <= FRESH_S


def fresh(p, now: float, alive) -> bool:
    """Whether a progress file belongs to an install that runs now: recent,
    and written by a process that is still alive (`alive`: the pids running
    now, one snapshot the caller takes only when some file is recent)."""
    return recent(p, now) and p.get('pid') in alive


def _epoch(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def _num(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _track(stage: str, *, verified: bool, frac, failed: bool = False,
           ready: bool = False, pending: bool = False) -> list[dict]:
    """The four segments. A Verify that didn't run (Nexus's MD5 check is
    opt-in) is "skipped" and its share folds into Download."""
    names = [s for s, _w in STAGES]
    at = names.index(stage) if stage in names else len(names)
    out = []
    for i, (name, weight) in enumerate(STAGES):
        if name == 'download' and not verified:
            weight += dict(STAGES)['verify']
        if name == 'verify' and not verified:
            out.append({'stage': name, 'weight': 0, 'frac': None, 'status': 'skipped'})
            continue
        if ready or i < at:
            status, f = 'done', 1.0
        elif i == at:
            status, f = ('failed' if failed else 'todo' if pending else 'active'), frac
        else:
            status, f = 'todo', 0.0
        out.append({'stage': name, 'weight': round(weight, 4), 'frac': f, 'status': status})
    return out


def overall(card: dict) -> float | None:
    """0..1 across the stages: 1.0 only once installed, at most 0.99 before;
    None for an update installed on top (no total to measure against)."""
    if card.get('state') == 'ready':
        return 1.0
    if card.get('state') == 'updating' or (card.get('install') or {}).get('update'):
        return None
    total = sum(t['weight'] * (t['frac'] or 0) for t in card.get('track') or ()
                if t['status'] in ('done', 'active', 'failed'))
    return round(min(CAP, total), 4)


def cards(rows: list, progress: dict, now: float, alive, drive_ids: dict | None = None
          ) -> list[dict]:
    """A card for every tracked torrent not yet installed, and a Ready card
    for a day after an install. `progress` is {hash: progress file}; files
    that aren't fresh (see fresh) are ignored. `drive_ids` names the drive
    each card writes to: {'install': id, 'download': {folder: id}}."""
    ids = drive_ids or {}
    out = []
    for r in rows:
        if not r.get('tracked'):
            continue
        c = _card(r, progress.get(r.get('hash')), now, alive)
        if c is None:
            continue
        c['drives'] = {'download': (ids.get('download') or {}).get(r.get('dir')),
                       'install': ids.get('install')}
        out.append(c)
    return out


def _card(r: dict, p, now: float, alive) -> dict | None:
    h = str(r.get('hash') or '')
    live = p if fresh(p, now, alive) else None
    size = _num(r.get('size')) or _num(r.get('repack_bytes')) or 0
    pct = max(0.0, min(100.0, float(r.get('percent') or 0)))
    expected = _num(r.get('original_bytes'))
    dl = download_state(r)
    started, failed_at = r.get('install_started'), r.get('install_failed')
    error, eta, eta_est, install, step, reason = None, None, False, None, '', None
    left_behind = None                  # a progress file of this install, not current
    if isinstance(p, dict) and not live and started:
        began = _epoch(started)
        try:
            # (an older run's leftover, from before this install started, isn't)
            if began is None or float(p.get('started_at')) >= began - 1:
                left_behind = p
        except (TypeError, ValueError):
            pass
    verified = bool(live and (live.get('stage') == 'verify' or live.get('total') is not None))
    frac = None
    if r.get('installed'):
        at = _epoch(r.get('installed_at'))
        if at is None or now - at > READY_HOURS * 3600:
            return None
        stage = state = 'ready'
        step = "Ready to play"
    elif live:
        stage = live['stage']
        update = bool(live.get('update'))
        state = 'updating' if stage == 'install' and update else _STATE[stage]
        step = str(live.get('step') or '')
        written, exp, rate = (_num(live.get(k)) for k in ('written', 'expected', 'rate'))
        if stage == 'verify':
            done, total = _num(live.get('done')), _num(live.get('total'))
            frac = min(1.0, done / total) if done is not None and total else 0.0
        elif stage == 'install':
            if exp and written is not None and not update:
                frac = min(CAP, written / exp)
                if rate:
                    eta, eta_est = int(max(0, exp - written) / rate), True
        else:
            frac = 0.0
        if stage in ('install', 'finish'):
            install = {'written': written, 'expected': None if update else exp,
                       'rate': rate, 'update': update}
    elif dl == 'gone':
        return None
    elif started and failed_at and str(failed_at) >= str(started):
        stage, state = 'install', 'failed'
        reason = str(r.get('install_failed_reason') or 'error')
        step = FAILED_STEPS.get(reason, FAILED_STEPS['error'])
    elif started and (left_behind or now - (_epoch(started) or now) > STUCK_S):
        stage, state, reason = 'install', 'failed', 'stopped'     # killed: no exit mark
        step = FAILED_STEPS['stopped']
    elif started:
        stage, state, frac, step = 'install', 'waiting', 0.0, "Waiting to install"
    elif dl == 'done':
        stage, state, frac, step = 'install', 'downloaded', 0.0, "Downloaded"
    else:
        stage, state = 'download', dl
        frac = pct / 100
        if dl == 'error':
            error = str(r.get('error'))
            step = "Error: " + error
        else:
            step = {'paused': "Paused", 'queued': "Queued", 'checking': "Checking",
                    'downloading': "Downloading"}[dl]
            if dl == 'downloading' and r.get('eta') is not None:
                eta = _num(r.get('eta'))
    ready = state == 'ready'
    left = 0 if ready or dl == 'done' or stage != 'download' else int(size * (1 - pct / 100))
    if ready or state == 'downloaded' or (install or {}).get('update'):
        need_install = 0
    elif install and install.get('expected') and install.get('written') is not None:
        need_install = max(0, install['expected'] - install['written'])
    else:
        need_install = expected or 0
    c = {'key': f'torrent:{h}', 'hash': h,
         'title': r.get('title') or r.get('name') or h[:12],
         'slug': r.get('slug'), 'appid': r.get('appid'),
         'stage': stage, 'state': state, 'step': step, 'error': error, 'reason': reason,
         'track': _track(stage, verified=verified, frac=frac,
                         failed=state in ('failed', 'error'), ready=ready,
                         pending=state == 'downloaded'),
         'overall': None, 'eta': eta, 'eta_est': eta_est,
         'download': {'done': int(size * pct / 100), 'total': size or None,
                      'rate': r.get('rate') or 0, 'eta': _num(r.get('eta'))},
         'install': install,
         'need': {'download': left, 'install': need_install},
         'drives': {'download': None, 'install': None},
         'repack_bytes': _num(r.get('repack_bytes')), 'original_bytes': expected,
         'installed_at': r.get('installed_at')}
    c['overall'] = overall(c)
    return c


def forecast(cards: list, drives: list) -> list[dict]:
    """Each drive with what the cards in flight still write there (download
    left on the download drive, the install's remainder on the install
    drive): to_write, free_after and short_by. An estimate ("about"): the
    install size is a label, and a client that preallocates has already
    taken its space."""
    out = []
    for d in drives:
        to_write = sum(c['need'][role] for c in cards for role in ('download', 'install')
                       if (c.get('drives') or {}).get(role) == d['id'])
        free = d.get('free')
        after = free - to_write if free is not None else None
        out.append({**d, 'to_write': to_write, 'free_after': after,
                    'short_by': max(0, -after) if after is not None else 0, 'about': True})
    return out


def grab_fit(page: dict) -> dict:
    """Whether a Grab fits, from `--repack-page --json`: one line per drive
    ({role, where, need, free, committed, short_by}; role 'both' when the
    download and the install share a drive), counting what the downloads
    and installs already in flight still need there (`committed`).
    `install_known` is False when the page had no Original Size."""
    committed = page.get('committed') or {}
    size, orig = _num(page.get('size_bytes')) or 0, _num(page.get('original_bytes'))
    lines = []

    def line(role, drive, where, need, free):
        if free is None or not where:
            return
        busy = int(committed.get(drive) or 0) if drive is not None else 0
        lines.append({'role': role, 'where': where, 'need': need, 'free': free,
                      'committed': busy, 'short_by': max(0, need + busy - free)})
    dl_where = page.get('dest_mount') or page.get('dest')
    in_where = page.get('install_mount') or page.get('install_dest')
    same = (page.get('dest_drive') is not None
            and page.get('dest_drive') == page.get('install_drive'))
    if same:
        line('both', page['dest_drive'], dl_where, size + (orig or 0), page.get('free_bytes'))
    else:
        line('download', page.get('dest_drive'), dl_where, size, page.get('free_bytes'))
        if orig is not None:
            line('install', page.get('install_drive'), in_where, orig, page.get('install_free'))
    return {'same': same, 'drives': lines, 'install_known': orig is not None,
            'short_by': max((d['short_by'] for d in lines), default=0)}


def _gb(n) -> str:
    n = (n or 0) / 1e9
    return f"{n:.1f}" if n < 10 else f"{n:.0f}"


def _eta(s) -> str:
    m = max(1, round(s / 60))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def describe(c: dict) -> str:
    """One line for a card, for the CLI's text mode:
    "Test Game · Installing 9.4 of about 38 GB · est. 6 min"."""
    state, inst = c['state'], c.get('install') or {}
    if state == 'downloading':
        what = f"Downloading {c['track'][0]['frac'] * 100:.0f}%"
    elif state == 'paused':
        what = f"Paused at {c['track'][0]['frac'] * 100:.0f}%"
    elif state == 'verifying':
        what = f"Checking files {(c['track'][1]['frac'] or 0) * 100:.0f}%"
    elif state == 'installing' and inst.get('expected') and inst.get('written') is not None:
        what = f"Installing {_gb(inst['written'])} of about {_gb(inst['expected'])} GB"
    elif state in ('installing', 'updating') and inst.get('written') is not None:
        what = f"{'Updating' if state == 'updating' else 'Installing'} · " \
               f"{_gb(inst['written'])} GB written"
    else:
        what = c.get('step') or state.capitalize()
    if c.get('eta'):
        what += f" · est. {_eta(c['eta'])}" if c.get('eta_est') else f" · {_eta(c['eta'])} left"
    return f"{c['title']} · {what}"
