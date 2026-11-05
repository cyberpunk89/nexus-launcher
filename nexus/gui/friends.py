"""Steam friends in the window (w-81): what `nexus-cli --friends --json` last
answered (FRIENDS), and the small widgets that show people: an avatar, a
stack of them, a person chip and the game page's Friends block. w-82 adds
who is playing what right now, grouped by game (playing_now), for Home's
"Friends playing now" and Theatre.

The GUI never asks Steam: it reads the CLI's JSON (win_library._load_friends,
Settings' status row) and joins `by_app` against the library by Steam app id
(dashboard._game_store_id). Private friends are always shown, greyed."""

import html
import time
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QObject, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from nexus import storeinfo

from .fmt import fmt_duration, fmt_hours
from .theme import FONTS, FS, P, PERSON_H, scaled, SP

FRIENDS_ARGS = ['--friends', '--json']
# Only who plays what (GetPlayerSummaries), asked while Home is on screen.
PRESENCE_ARGS = ['--friends', '--presence', '--json']
PRESENCE_MS = 120_000          # how often, while Home shows and the window is active
PRESENCE_AGE = 60              # s: older than this, Home asks as soon as it shows
MAX_CARDS = 8                  # games in "Friends playing now"
PLAYING_STALE = 600            # s: "playing now" older than this misleads (the CLI's rule)
# Errors that mean "not set up": the answer still carries the old cache, but
# nothing is shown from it. A refused key, no network or a throttle keep it.
UNSET_ERRORS = ('no-key', 'no-account')
HUES = ('accent', 'success', 'attention', 'warning', 'danger', 'info')
MAX_CHIPS = 8


def _ts(value) -> float | None:
    """An ISO time or epoch seconds (the CLI's `playing.since`) as epoch."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def names_text(names: list[str], most: int = 2) -> str:
    """'Ana', 'Ana and Ben', 'Ana, Ben +2'."""
    names = [n for n in names if n]
    if len(names) <= most:
        return names[0] if len(names) == 1 else " and ".join(names) if names else ''
    return f"{', '.join(names[:most])} +{len(names) - most}"


def _game_store_id(g: dict | None) -> int | None:
    """dashboard._game_store_id, imported when first used: tiles import this
    module early, before dashboard can be imported."""
    from .dashboard import _game_store_id as store_id
    return store_id(g) if g else None


class FriendsData(QObject):
    """The last `--friends --json` answer. `changed` fires only when what the
    window shows from it changed (owners, names, avatars, who plays), so a
    refresh that brings nothing new rebuilds nothing. `avatar_ready` fires
    when an avatar arrived or was decoded: painters ask again."""
    changed = pyqtSignal()
    avatar_ready = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.data: dict | None = None
        self._by_id: dict[str, dict] = {}
        self._sig = None
        self._asked: set = set()          # avatar downloads / decodes on their way

    # ---- the answer ----
    def set_answer(self, data):
        """Take a new answer; None (the CLI failed) keeps the last one."""
        if not isinstance(data, dict):
            return
        self.data = data
        self._by_id = {str(r['steamid']): r for r in data.get('friends') or ()
                       if isinstance(r, dict) and r.get('steamid')}
        sig = self._signature()
        if sig != self._sig:
            self._sig = sig
            self.changed.emit()

    def reset(self):
        """Forget everything (the feature was switched off; tests)."""
        had = self.data is not None
        self.data, self._by_id, self._sig = None, {}, None
        if had:
            self.changed.emit()

    def _signature(self):
        if not self.usable():
            return None
        by_app = self.data.get('by_app') or {}
        people = tuple(sorted((sid, r.get('name'), r.get('avatar'), r.get('visible'),
                               str((r.get('playing') or {}).get('appid') or ''))
                              for sid, r in self._by_id.items()))
        return (tuple(sorted((str(k), tuple(v)) for k, v in by_app.items()
                             if isinstance(v, list))), people)

    def usable(self) -> bool:
        """On, set up, and with an answer: a refused key or no network still
        show the last good copy; no key or no account show nothing."""
        d = self.data
        return bool(d and d.get('enabled')
                    and (d.get('error') or {}).get('kind') not in UNSET_ERRORS)

    # ---- questions ----
    def record(self, sid) -> dict | None:
        return self._by_id.get(str(sid))

    def friends(self) -> list[dict]:
        return list(self._by_id.values()) if self.usable() else []

    def owner_ids(self, appid) -> list[str]:
        """Steam ids of the public friends who own appid, most played first."""
        if not self.usable() or appid is None:
            return []
        ids = (self.data.get('by_app') or {}).get(str(appid)) or []
        return [str(s) for s in ids if str(s) in self._by_id]

    def owners(self, appid) -> list[dict]:
        return [self._by_id[s] for s in self.owner_ids(appid)]

    def owner_ids_of(self, g: dict) -> list[str]:
        return self.owner_ids(_game_store_id(g))

    def owners_of(self, g: dict) -> list[dict]:
        return self.owners(_game_store_id(g))

    def private(self) -> list[dict]:
        """Friends whose game details Nexus can't see."""
        return [r for r in self.friends() if r.get('visible') is False]

    def public(self) -> list[dict]:
        return [r for r in self.friends() if r.get('visible') is not False]

    def age(self, now: float | None = None) -> float | None:
        """Seconds since presence was last asked, None when never."""
        t = _ts((self.data or {}).get('updated'))
        if t is None:
            return None
        return max(0.0, (time.time() if now is None else now) - t)

    def error_kind(self) -> str | None:
        return ((self.data or {}).get('error') or {}).get('kind')

    def fresh(self, now: float | None = None) -> bool:
        """Was presence asked recently enough to say who plays now?"""
        t = _ts((self.data or {}).get('updated'))
        now = time.time() if now is None else now
        return t is not None and now - t < PLAYING_STALE

    def playing_in(self, appid, now: float | None = None) -> list[dict]:
        """Friends in appid right now (none once the answer is stale)."""
        if appid is None or not self.usable() or not self.fresh(now):
            return []
        return [r for r in self.public()
                if str((r.get('playing') or {}).get('appid') or '') == str(appid)]

    def has_owners(self) -> bool:
        return self.usable() and any(self.owner_ids(a) for a in self.data.get('by_app') or {})

    def owned_by_text(self, ids) -> str:
        """'Owned by Ana, Ben +2' ('' for nobody)."""
        names = [(self.record(s) or {}).get('name') or '' for s in ids or ()]
        who = names_text([n for n in names if n])
        return f"Owned by {who}" if who else ''

    # ---- avatars ----
    def avatar_pixmap(self, rec: dict | None, d: int):
        """The friend's Steam avatar decoded at d x d, or None for now (it is
        asked for, and avatar_ready fires when it's there)."""
        url = (rec or {}).get('avatar')
        if not url or not str(url).startswith('https://'):
            return None
        path = ART.cached(url)
        if path is None:
            if url not in self._asked:
                self._asked.add(url)
                ART.fetch(url, self._avatar_arrived)
            return None
        pm = ART.peek(path, d, d)
        if pm is None and (path, d) not in self._asked:
            self._asked.add((path, d))
            ART.request(path, d, d, self._avatar_decoded)
        return pm

    def _avatar_arrived(self, _path):
        self._asked = {a for a in self._asked if not isinstance(a, str)}
        self.avatar_ready.emit()

    def _avatar_decoded(self, _pm):
        self._asked = {a for a in self._asked if isinstance(a, str)}
        self.avatar_ready.emit()


FRIENDS = FriendsData()


def playing_now(games, now: float | None = None, limit: int | None = MAX_CARDS) -> list[dict]:
    """Who of your friends is in which game right now, one entry per game:
    {appid, title, slug (the library game, or None), people (longest in
    the game first), minutes, headerImage}; the most friends first, then
    the longest. Nothing once presence is stale (10 min) or friends are
    off. A hidden game counts as not in your library."""
    if not FRIENDS.usable() or not FRIENDS.fresh(now):
        return []
    now = time.time() if now is None else now
    groups: dict[str, list[dict]] = {}
    for r in FRIENDS.public():
        appid = (r.get('playing') or {}).get('appid')
        if appid is None or not str(appid).isdigit():
            continue
        groups.setdefault(str(int(appid)), []).append(r)
    if not groups:
        return []
    owned = {}
    for g in games or ():
        if g.get('hidden') or g.get('dup_of'):
            continue
        sid = _game_store_id(g)
        if sid is not None:
            owned.setdefault(str(sid), g)

    def since(r):
        t = _ts((r.get('playing') or {}).get('since'))
        return now if t is None else min(t, now)
    out = []
    for appid, people in groups.items():
        people.sort(key=lambda r: (since(r), (r.get('name') or '').casefold()))
        g = owned.get(appid)
        name = next(((r.get('playing') or {}).get('name') for r in people
                     if (r.get('playing') or {}).get('name')), None)
        start = since(people[0])
        out.append({'appid': int(appid),
                    'title': (g or {}).get('display_name') or name or f"App {appid}",
                    'slug': (g or {}).get('slug'), 'people': people,
                    'minutes': max(0, int((now - start) // 60)),
                    'headerImage': storeinfo.HEADER.format(appid=appid)})
    out.sort(key=lambda e: (-len(e['people']), -e['minutes'], e['title'].casefold()))
    return out[:limit] if limit else out


def who_text(entry: dict) -> str:
    """'Ana and Ben · 1 h 20 min'."""
    who = names_text([r.get('name') or '' for r in entry.get('people') or ()])
    return f"{who} · {fmt_duration(entry.get('minutes'))}" if who else \
        fmt_duration(entry.get('minutes'))


def hue_of(rec: dict | None) -> str:
    sid = str((rec or {}).get('steamid') or '0')
    return HUES[int(sid) % len(HUES)] if sid.isdigit() else HUES[0]


def paint_avatar(pt: QPainter, r: QRectF, rec: dict | None, dim: bool = False,
                 live: bool = False, ring: str | None = None):
    """One round avatar in r: Steam's picture, else the first letter on a
    colour of its own. dim = private (greyed); live = in this game now (a
    success ring); ring = a gap ring in that colour role (in a stack)."""
    pt.save()
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    pt.setPen(Qt.PenStyle.NoPen)
    if ring:
        pt.setBrush(QColor(P[ring]))
        pt.drawEllipse(r)
        r = r.adjusted(1.5, 1.5, -1.5, -1.5)
    pm = FRIENDS.avatar_pixmap(rec, max(1, round(r.width())))
    if pm is not None:
        pt.save()
        clip = QPainterPath()
        clip.addEllipse(r)
        pt.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        if dim:
            pt.setOpacity(pt.opacity() * 0.4)
        pt.drawPixmap(r, pm, QRectF(pm.rect()))
        pt.restore()
    else:
        pt.setBrush(QColor(P['surface_3'] if dim else P[hue_of(rec)]))
        pt.drawEllipse(r)
        f = QFont(FONTS.ui, -1, QFont.Weight.DemiBold)
        f.setPixelSize(max(8, int(r.height() * 0.45)))
        pt.setFont(f)
        pt.setPen(QColor(P['muted'] if dim else P['on_accent']))
        name = ((rec or {}).get('name') or '?').strip() or '?'
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, name[0].upper())
    if live:
        pt.setBrush(Qt.BrushStyle.NoBrush)
        pt.setPen(QPen(QColor(P['success']), 2.0))
        pt.drawEllipse(r.adjusted(1.0, 1.0, -1.0, -1.0))
    pt.restore()


class Avatar(QWidget):
    """A round avatar widget (paint_avatar)."""

    def __init__(self, rec: dict | None, d: int, dim: bool = False, live: bool = False,
                 ring: str | None = None, parent=None):
        super().__init__(parent)
        self.rec, self.dim, self.live, self.ring = rec, dim, live, ring
        self.setFixedSize(d, d)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        FRIENDS.avatar_ready.connect(self.update)

    def paintEvent(self, ev):
        pt = QPainter(self)
        paint_avatar(pt, QRectF(self.rect()), self.rec, self.dim, self.live, self.ring)
        pt.end()


class AvatarStack(QWidget):
    """Up to n overlapping avatars, then "+N", painted in one widget."""

    def __init__(self, recs: list[dict], d: int, n: int = 3, dim: bool = False,
                 ring: str = 'bg', parent=None):
        super().__init__(parent)
        self.recs, self.d, self.dim, self.ring = list(recs[:n]), d, dim, ring
        self.step = d - round(d * 0.3)
        self.more = f"+{len(recs) - n}" if len(recs) > n else ''
        self._font = QFont(FONTS.ui, -1, QFont.Weight.Medium)
        self._font.setPixelSize(FS['2xs'])
        more_w = QFontMetrics(self._font).horizontalAdvance(self.more) + SP['1'] \
            if self.more else 0
        self.setFixedSize(d + self.step * max(0, len(self.recs) - 1) + more_w, d)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        FRIENDS.avatar_ready.connect(self.update)

    def paintEvent(self, ev):
        pt = QPainter(self)
        d = float(self.d)
        for i in reversed(range(len(self.recs))):      # the first one on top
            paint_avatar(pt, QRectF(i * self.step, 0.0, d, d), self.recs[i], self.dim,
                         ring=self.ring)
        if self.more:
            pt.setFont(self._font)
            pt.setPen(QColor(P['muted']))
            x = d + self.step * (len(self.recs) - 1) + SP['1']
            pt.drawText(QRectF(x, 0.0, self.width() - x, d),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.more)
        pt.end()


class PersonChip(QFrame):
    """Avatar and name in a capsule; display only (not a focus stop).
    Playing now: a success ring and "playing now"."""

    def __init__(self, rec: dict, minutes=None, live: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("PersonChip")
        self.rec = rec
        h = scaled(PERSON_H)
        self.setFixedHeight(h)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 0, SP['3'], 0)
        lay.setSpacing(SP['1_5'])
        lay.addWidget(Avatar(rec, h - 6, live=live, parent=self))
        name = rec.get('name') or rec.get('steamid') or '?'
        lay.addWidget(plain_label(name, "PersonName"))
        self.sub = None
        if live:
            self.sub = label("playing now", "PersonSub")
            self.sub.setProperty('live', True)
            lay.addWidget(self.sub)
        played = fmt_hours(minutes)
        tip = " · ".join(x for x in (
            name, "playing now" if live else '', f"{played} played" if played else '') if x)
        self.setToolTip(html.escape(tip))


class FriendsBlock(QWidget):
    """Overview's "Friends": who of your friends owns this game (chips,
    playing now first, at most 8 then "+N more"), and one greyed line for
    the friends whose game details are private. Hidden while friends are
    off, for a game Nexus can't match to a Steam app id, and without
    friends."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        from .details import kicker
        head.addWidget(kicker("Friends"))
        head.addStretch()
        self.count = label("", "Muted")
        head.addWidget(self.count)
        v.addLayout(head)
        self.host = QWidget(self)
        self.flow = FlowLayout(self.host, margin=0, spacing=SP['1_5'])
        v.addWidget(self.host)
        self.none = label("None of your friends with public game details own this.", "Muted",
                          wrap=True)
        v.addWidget(self.none)
        self.private_row = QWidget(self)
        self._private_lay = QHBoxLayout(self.private_row)
        self._private_lay.setContentsMargins(0, 0, 0, 0)
        self._private_lay.setSpacing(SP['2'])
        self.private_text = label("", "Muted", wrap=True)
        self.private_text.setToolTip("Their game details aren't public on Steam. "
                                     "“Friends only” counts as private for apps.")
        self._private_lay.addWidget(self.private_text, 1)
        v.addWidget(self.private_row)
        self._stack = None
        self.chips: list[PersonChip] = []
        self.more = None
        self._shown = None
        self._expanded_for = None
        self._game = None
        self.setVisible(False)

    def set_game(self, g: dict | None):
        self._game = g
        sid = _game_store_id(g) if g else None
        if not FRIENDS.usable() or sid is None or not FRIENDS.friends():
            self._shown = None
            self.setVisible(False)
            return
        if self._expanded_for != sid:
            self._expanded_for = None
        expanded = self._expanded_for == sid
        live = {r['steamid'] for r in FRIENDS.playing_in(sid)}
        owners = FRIENDS.owners(sid)
        owners.sort(key=lambda r: r['steamid'] not in live)      # stable: minutes after
        minutes = {r['steamid']: (r.get('owned') or {}).get(str(sid)) for r in owners}
        private = FRIENDS.private()
        state = (sid, expanded, tuple((r['steamid'], r.get('name'), r.get('avatar'),
                                       r['steamid'] in live, minutes[r['steamid']])
                                      for r in owners),
                 tuple((r['steamid'], r.get('name'), r.get('avatar')) for r in private),
                 bool(FRIENDS.public()))
        if state != self._shown:
            self._shown = state
            self._fill(sid, owners, live, minutes, private, expanded)
        self.setVisible(True)

    def _fill(self, sid, owners, live, minutes, private, expanded):
        clear_layout(self.flow)
        self.chips, self.more = [], None
        shown = owners if expanded or len(owners) <= MAX_CHIPS else owners[:MAX_CHIPS]
        for r in shown:
            chip = PersonChip(r, minutes.get(r['steamid']), r['steamid'] in live,
                              parent=self.host)
            self.flow.addWidget(chip)
            self.chips.append(chip)
        if len(shown) < len(owners):
            self.more = QPushButton(f"+{len(owners) - len(shown)} more", self.host)
            self.more.setObjectName("TagChip")
            self.more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.more.setCursor(Qt.CursorShape.PointingHandCursor)
            self.more.setToolTip("Show everyone who owns it")
            self.more.clicked.connect(self._expand)
            self.flow.addWidget(self.more)
        self.count.setText(f"{len(owners)} own it" if owners else "")
        self.count.setVisible(bool(owners))
        self.host.setVisible(bool(owners))
        self.none.setVisible(not owners and bool(FRIENDS.public()))
        if self._stack is not None:
            self._private_lay.removeWidget(self._stack)
            self._stack.hide()
            self._stack.deleteLater()
            self._stack = None
        if private:
            self._stack = AvatarStack(private, 20, 3, dim=True, parent=self.private_row)
            self._private_lay.insertWidget(0, self._stack)
            n = len(private)
            self.private_text.setText(
                f"{n} friend{'s are' if n != 1 else ' is'} private on Steam: "
                "can't tell if they own it")
        self.private_row.setVisible(bool(private))

    def _expand(self):
        """'+N more': everyone, in place (until another game is shown)."""
        self._expanded_for = self._shown[0] if self._shown else None
        self.set_game(self._game)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import ART  # noqa: E402
from .widgets import clear_layout, FlowLayout, label, plain_label  # noqa: E402
