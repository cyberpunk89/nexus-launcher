"""The Friends card's ladder (w-161): draws nexus.social.friends_digest.
Now (friends in a game) > Play together (friends online, games you all own) >
Friends' picks (your games friends put hours into) > quiet. The digest decides
what leads; this only lays it out and wires the one action on each row.

Small shows the top rung, Wide the top rung and the picks, Full all of them
(plus who is online, with Message handing off to the Steam client, and the
first wishlist game a friend owns). The picks are the card's tail: height
the grid gives over the base rows shows more of them (GrowHost, w-155).
`w` is the FriendsWidget; the module is used from its fill and has_content."""

from datetime import datetime

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QPushButton

from nexus import smart, social, storeinfo

from .common import alive
from .fmt import fmt_duration
from .friends import _and, _ts, FRIENDS
from .theme import SP
from .widgets import label

PICK_POOL = 8                # the digest's picks: PICK_ROWS shown, the rest as the card grows
SHOWN = {                    # rows per rung: {size: (now, together, picks)}
    'small': (2, 1, 2), 'wide': (2, 2, 3), 'full': (4, 3, 3)}
OFFLINE_KINDS = ('offline', 'rate-limited', 'private-list')


def digest(w, hub) -> dict:
    """The digest for what the window holds now (the gallery seeds `w._clock`)."""
    wishlist = []
    page = getattr(hub, 'dealspage', None)
    if page is not None and hub.config.get('gamedb_enabled', True):
        wishlist = list((page._data() or {}).get('wishlist') or [])
    facts = hub._smart_facts() if hasattr(hub, '_smart_facts') else {}
    return social.friends_digest(FRIENDS.data, getattr(hub, '_games', None) or (), facts,
                                 wishlist, now=getattr(w, '_clock', None), limit=PICK_POOL)


def state_of(w, hub) -> tuple[str, dict | None]:
    """(the card's state, its digest): setup, loading, bad-key, now,
    together, picks or quiet."""
    if not hub.config.get('friends_enabled'):
        return 'setup', None
    if FRIENDS.data is None:
        return 'loading', None
    d = digest(w, hub)
    if d['state'] in ('setup', 'hidden'):
        return 'setup', d
    if FRIENDS.error_kind() == 'bad-key':
        return 'bad-key', d
    return d['state'], d


def has_content(w, hub) -> bool:
    """On Home while there is something to say (or Steam refused the key);
    setup, loading and quiet leave it (Customise still shows them)."""
    state, _d = state_of(w, hub)
    return state in ('now', 'together', 'picks', 'bad-key')


def _name(i) -> str:
    return (FRIENDS.record(i) or {}).get('name') or str(i)


def _names(ids, most=3) -> str:
    return _and([_name(i) for i in ids], most)


def _hhmm(t) -> str:
    return datetime.fromtimestamp(t).strftime('%H:%M')


def _structure(state, d, size, online) -> tuple:
    """What a rebuild depends on; the minutes in a game are not part of it."""
    if d is None:
        return (size, state)
    people = {i for e in d['now'] for i in e['people']}
    people |= set((d['together'] or {}).get('people') or ())
    people |= {i for p in d['picks'] for i in p['minutes']}
    people |= {i for e in d['wishlist'] for i in e['owners']}
    people |= {r.get('steamid') for r in online}
    return (size, state, d['stale'], d['updated'] if d['stale'] else None,
            FRIENDS.error_kind(), tuple(sorted(d['counts'].items())),
            tuple((r.get('steamid'), (r.get('playing') or {}).get('appid')) for r in online),
            tuple((e['appid'], e['slug'], e['title'], tuple(e['people'])) for e in d['now']),
            tuple((g['appid'], tuple(g['with'])) for g in (d['together'] or {}).get('games', ())),
            tuple((p['appid'], p['why']) for p in d['picks']),
            tuple((e['appid'], tuple(e['owners'])) for e in d['wishlist']),
            tuple((i, _name(i), (FRIENDS.record(i) or {}).get('avatar'))
                  for i in sorted(people, key=str)))


def fill(w, hub):
    """Build the card for its state, or only move the minutes on."""
    state, d = state_of(w, hub)
    online = FRIENDS.online(getattr(w, '_clock', None)) if d is not None else []
    sig = _structure(state, d, w.size_, online)
    if sig == w._sig:
        _minutes(w, d)
        return
    w._sig = sig
    focus = _focus_key(w)
    w.clear()
    w.cards, w._subs, w.footer, w.state = [], {}, None, state
    w.head = None
    if state == 'setup':
        w.body.addWidget(label("See what your Steam friends play.", "Muted", wrap=True))
        b = QPushButton("Set up in Settings")
        b.clicked.connect(lambda: hub._open_settings('accounts', 'friends'))
        w.body.addWidget(b, 0, _left())
        return
    if state == 'loading':
        w.body.addWidget(label("Looking for your friends…", "Muted"))
        return
    if state == 'bad-key':
        w._error("Steam refused the key", hub._reload_friends)
        return
    if state == 'quiet':
        _quiet(w, d)
        return
    _ladder(w, hub, state, d, online)
    _foot(w, d)
    _refocus(w, focus)


def _left():
    from PyQt6.QtCore import Qt
    return Qt.AlignmentFlag.AlignLeft


def _quiet(w, d):
    c = d['counts']
    if not c['total']:
        w.body.addWidget(label("No friends on this Steam account.", "Muted", wrap=True))
    elif not c['public']:
        n = c['private']
        w.body.addWidget(label(
            f"None of your {n} friend{'s' if n != 1 else ''} show{'s' if n == 1 else ''} their "
            "game details on Steam, so Nexus can't see what they play.", "Muted", wrap=True))
    else:
        w.body.addWidget(label("Nothing to show yet", "StatValue"))
        w.body.addWidget(label("Shows when a friend is on, or plays games you own.", "Muted",
                               wrap=True))


def _foot(w, d):
    """Presence age when stale, and how many friends share their games."""
    c = d['counts']
    if d['stale'] and d['updated']:
        t = _ts(d['updated'])
        if t is not None:
            text = f"Presence as of {_hhmm(t)}"
            if FRIENDS.error_kind() in OFFLINE_KINDS:
                text += " · Steam is not answering"
            w.body.addWidget(label(text, "Muted"))
    if c['private']:
        w.footer = label(f"{c['public']} of {c['total']} friends share their games", "Muted")
        w.footer.setToolTip(w.PRIVATE_TIP)
        w.body.addWidget(w.footer)


def _ladder(w, hub, state, d, online):
    size = w.size_
    n_now, n_tog, n_pick = SHOWN[size]
    stale = d['stale']
    full, wide = size == 'full', size == 'wide'
    if state in ('now', 'together'):
        w.head = label(f"{len(online)} online", "StatValue")
    else:
        w.head = label("Offline" if stale else "Nobody's on", "StatValue")
    w.body.addWidget(w.head)
    shown_picks = d['picks'] if (state == 'picks' or wide or full) else []
    tail = None                                  # the rows the card may grow into
    if state == 'now' or (full and d['now']):
        _caption(w, "Right now", wide or full)
        rows = d['now']
        for e in rows[:n_now]:
            _add(w, _now_row(w, hub, e))
        tail = (rows[n_now:], lambda e: _now_row(w, hub, e), " in a game")
    if state == 'together' or (full and d['together']):
        t = d['together']
        _together_head(w, t, state == 'together')
        for i, g in enumerate(t['games'][:n_tog]):
            _add(w, _together_row(w, hub, g, t, first=i == 0))
        tail = (t['games'][n_tog:], lambda g: _together_row(w, hub, g, t, False), "")
    if full:
        _who_is_on(w, hub, online)
    if shown_picks:
        if state == 'picks':
            _caption(w, "Games in your library your friends put hours into"
                     + (" · nobody is on right now" if full and not stale else "")
                     if (wide or full) else "Friends' picks", True)
        else:
            _caption(w, "Friends' picks", True)
        for p in shown_picks[:n_pick]:
            _add(w, _pick_row(w, hub, p))
        tail = (shown_picks[n_pick:], lambda p: _pick_row(w, hub, p), "")
    if tail and tail[0]:
        w._tail_more(*tail)
    if full and d['wishlist']:
        _wish_line(w, d['wishlist'][0])


def _caption(w, text, on):
    if on:
        w.body.addWidget(label(text, "Muted"))


def _add(w, row):
    w.body.addWidget(row)
    if row.key[0] != 'wish':
        w.cards.append(row)


def _row(w, key, title, sub, right, text, act, on_click, header=None):
    """A game row (art, title, a muted line) with its one button."""
    from . import dashboard as D
    row = D.game_row({'title': title, 'headerImage': header}, right, on_click, sub=sub)
    row.key, row.appid = key, key[1]
    btn = row.button = QPushButton(text)
    btn.clicked.connect(act)
    row.layout().addWidget(btn)
    if row.sub_label is not None:
        w._subs[key] = row.sub_label
    return row


def _store(appid):
    from . import dashboard as D
    D.open_url(storeinfo.STORE_PAGE.format(appid=appid))


def _open(hub, slug, appid):
    if slug:
        hub._reveal_game(slug)
    else:
        _store(appid)


def _now_sub(e) -> str:
    return f"{fmt_duration(e['minutes'])} · " + ("in your library" if e['owned']
                                                  else "not in your library")


def _now_row(w, hub, e):
    from . import dashboard as D
    recs = [r for r in map(FRIENDS.record, e['people']) if r]
    who = _names(e['people'])
    verb = "is" if len(e['people']) == 1 else "are"
    slug, appid = e['slug'], e['appid']

    def play():
        g = hub.game(slug) if slug else None
        if g is not None:
            hub._play_lead(g)
        else:
            _open(hub, slug, appid)
    row = _row(w, ('now', appid), f"{who} {verb} in {e['title']}", _now_sub(e),
               [D.AvatarStack(recs, 22, 3, ring='surface')],
               "Play" if slug else "Steam page", play, lambda: _open(hub, slug, appid),
               storeinfo.HEADER.format(appid=appid))
    row.setToolTip("Open its page" if slug else "Not in your library · opens its Steam page ↗")
    row.now_entry = e
    return row


def _together_head(w, t, lead):
    w.body.addWidget(label(f"{_names(t['people'])} {'is' if len(t['people']) == 1 else 'are'} "
                           "online", "DashSub" if lead else "Muted", wrap=True))
    n = sum(1 for g in t['games'] if g['all'])
    w.body.addWidget(label("Nobody is in a game" + (
        f" · you all own {n} game{'s' if n != 1 else ''}" if n else ""), "Muted", wrap=True))


def _together_row(w, hub, g, t, first):
    from . import dashboard as D
    recs = [r for r in map(FRIENDS.record, g['with']) if r]
    owners = _names(g['with'])
    sub = f"{owners} {'owns' if len(g['with']) == 1 else 'own'} it"
    slug, appid = g['slug'], g['appid']
    if first:
        text, act = "Play together", lambda: hub._play_together(
            smart.friends_key(g['with'], True))
    else:
        text, act = "Play", lambda: hub._play_lead(hub.game(slug))
    row = _row(w, ('together', appid), g['title'], sub,
               [D.AvatarStack(recs, 22, 3, ring='surface')], text, act,
               lambda: _open(hub, slug, appid), storeinfo.HEADER.format(appid=appid))
    row.setToolTip("Open its page")
    return row


def _pick_row(w, hub, p):
    slug, appid = p['slug'], p['appid']
    row = _row(w, ('pick', appid), p['title'], p['why'], [], "Open",
               lambda: _open(hub, slug, appid), lambda: _open(hub, slug, appid),
               storeinfo.HEADER.format(appid=appid))
    row.setToolTip("Open its page")
    return row


def _who_is_on(w, hub, online):
    """Full: each friend online, with Message (Steam's own chat; Nexus
    sends nothing)."""
    from . import dashboard as D
    if not online:
        return
    w.body.addWidget(label("On now", "Muted"))
    for r in online:
        line = QHBoxLayout()
        line.setSpacing(SP['2'])
        line.addWidget(D.Avatar(r, 22, live=True))
        playing = r.get('playing') or {}
        what = f"In {playing.get('name') or 'a game'}" if playing else "Online"
        line.addWidget(label(f"{r.get('name') or ''} · {what}", "DashSub"), 1)
        b = QPushButton("Message")
        b.setToolTip(f"Message {r.get('name') or 'them'} in the Steam client")
        b.clicked.connect(lambda _=False, i=r.get('steamid'):
                          D.open_url(f"steam://friends/message/{i}"))
        line.addWidget(b)
        w.body.addLayout(line)


def _wish_line(w, e):
    line = QHBoxLayout()
    line.setSpacing(SP['2'])
    verb = "owns" if len(e['owners']) == 1 else "own"
    line.addWidget(label(f"{_names(e['owners'])} {verb} {e['title']}, on your wishlist",
                         "Muted", wrap=True), 1)
    b = QPushButton("Store page")
    b.clicked.connect(lambda: _store(e['appid']))
    line.addWidget(b)
    w.body.addLayout(line)


def _minutes(w, d):
    """Only the minutes moved on: write them into the rows that show them."""
    for e in (d or {}).get('now') or ():
        sub = w._subs.get(('now', e['appid']))
        if sub is not None:
            sub.setText(_now_sub(e))


def _focus_key(w):
    """(row key, whether its button had focus) of the focused row."""
    f = QApplication.focusWidget()
    for c in w.cards:
        if f is c or (f is not None and f is getattr(c, 'button', None)):
            return c.key, f is not c
    return None


def _refocus(w, focus):
    if focus is None:
        return
    key, on_button = focus
    row = next((c for c in w.cards if c.key == key), None)
    if row is None:
        return
    from .input import focus_and_reveal
    target = row.button if on_button else row
    QTimer.singleShot(0, lambda t=target: alive(t) and focus_and_reveal(t))
