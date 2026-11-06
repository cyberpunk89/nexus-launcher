"""Steam friends in the window (w-81): what `nexus-cli --friends --json` last
answered (FRIENDS), and the small widgets that show people: an avatar, a
stack of them, a person chip and the game page's Friends block. w-82 adds
who is playing what right now, grouped by game (playing_now), for Home's
"Friends playing now" and Theatre. w-83 adds Play together's picker
(FriendToggle, PlayTogetherForm).

The GUI never asks Steam: it reads the CLI's JSON (win_library._load_friends,
Settings' status row) and joins `by_app` against the library by Steam app id
(dashboard._game_store_id). Private friends are always shown, greyed."""

import html
import time
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QObject, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLineEdit, QPushButton,
    QVBoxLayout, QWidget)

from nexus import storeinfo

from .fmt import fmt_duration, fmt_hours
from .theme import FONTS, FS, G, P, PERSON_H, PILL_H, R, scaled, SP

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

    def public_ids(self) -> set[str] | None:
        """Steam ids of your public friends, for smart's friends rule and
        key (w-83): an unfriended or private friend drops out of them. None
        while friends aren't usable (the rule then matches nothing)."""
        if not self.usable():
            return None
        return {str(r['steamid']) for r in self.public()}

    def names(self) -> dict[str, str]:
        """steamid -> name, every friend (private ones too)."""
        return {sid: r.get('name') or sid for sid, r in self._by_id.items()}

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


# ---- Play together (w-83) ----
FIND_FROM = 12                 # "Find a friend" only with more friends than this
COVERS = 6


def _and(names: list[str], most: int = 3) -> str:
    """'Ana', 'Ana and Ben', 'Ana, Ben and Cy', 'Ana, Ben, Cy and 2 more'."""
    names = [n for n in names if n]
    if len(names) > most:
        return f"{', '.join(names[:most])} and {len(names) - most} more"
    return names[0] if len(names) == 1 else \
        f"{', '.join(names[:-1])} and {names[-1]}" if names else ''


class _Tick(QWidget):
    """The check box at a FriendToggle's right, painted from its button."""

    def __init__(self, button: QPushButton, d: int):
        super().__init__(button)
        self._button = button
        self.setFixedSize(d, d)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self._button.isChecked()
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        rad = min(R['sm'], r.height() / 2 - 1)
        pt.setPen(QPen(QColor(P['accent'] if on else P['overlay']), 1.5))
        pt.setBrush(QColor(P['accent']) if on else Qt.BrushStyle.NoBrush)
        pt.drawRoundedRect(r, rad, rad)
        if on:
            f = QFont(FONTS.icon)
            f.setPixelSize(max(8, int(self.height() * 0.62)))
            pt.setFont(f)
            pt.setPen(QColor(P['on_accent']))
            pt.drawText(r, Qt.AlignmentFlag.AlignCenter, G['check'])
        pt.end()


class FriendToggle(QPushButton):
    """One friend in Play together: avatar, name, a line under it ("Playing
    BG3" / "4 games in common with you"), a check box at the right. A click,
    Space, Enter (the modal clicks the focused button) or a pad's A toggle
    it. A private friend is greyed, disabled and says "private"."""

    def __init__(self, rec: dict, sub: str, live: bool = False, private: bool = False,
                 scale: float = 1.0, parent=None):
        super().__init__(parent)
        sc = lambda v: int(v * scale)                 # noqa: E731
        self.setObjectName("FriendToggle")
        self.rec = rec
        self.sid = str(rec.get('steamid'))
        self.name = rec.get('name') or self.sid
        self.private = private
        self.setCheckable(not private)
        self.setMinimumHeight(sc(48))
        self.setCursor(Qt.CursorShape.ArrowCursor if private
                       else Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(self.name)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(sc(SP['2']), sc(SP['1']), sc(SP['3']), sc(SP['1']))
        lay.setSpacing(sc(SP['2_5']))
        lay.addWidget(Avatar(rec, sc(32), dim=private, parent=self))
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.title = plain_label(self.name, "FriendName")
        self.title.setProperty('dim', private)
        col.addWidget(self.title)
        self.sub = plain_label(sub, "FriendSub")
        self.sub.setProperty('live', live)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        if private:
            self.tick = None
            pill_ = label("private", "PrivatePill")
            pill_.setFixedHeight(sc(PILL_H))
            pill_.setToolTip("Their game details aren't public on Steam. "
                             "“Friends only” counts as private for apps.")
            lay.addWidget(pill_, 0, Qt.AlignmentFlag.AlignVCenter)
            self.setEnabled(False)
        else:
            self.tick = _Tick(self, sc(22))
            lay.addWidget(self.tick, 0, Qt.AlignmentFlag.AlignVCenter)
            self.toggled.connect(self.tick.update)
        for w in self.findChildren(QWidget):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self):
        return self.layout().sizeHint()


class PlayTogetherForm(QWidget):
    """The Play together modal's body (w-83, the ShelfForm pattern): a well
    with "N games you, Ana and Ben all own" and the first covers, "Show
    games for" (More than one player / Any game), "Find a friend" with more
    than twelve friends, then one FriendToggle per friend: playing now
    first, then most games in common, then by name; private ones last,
    disabled. It never touches prefs or the filter: the window reads
    `key()` (Show N games) or `shelf_draft()` (Save as shelf).

    `friends`: FRIENDS.friends(); `pool`: the games the count is over (what
    the grid would show); `picked`: steamids ticked at the start.
    Signals for the modal (connect them to its bound methods, the body dies
    with it): `primary(text)`, `picked(any)` (Save as shelf allowed),
    `touched`, `relayout`; `changed` after every pick."""
    changed = pyqtSignal()
    touched = pyqtSignal()
    relayout = pyqtSignal()
    primary = pyqtSignal(str)
    picked = pyqtSignal(bool)

    def __init__(self, friends: list[dict], pool: list[dict], facts: dict,
                 picked=(), multi: bool = True, note: str = '', scale: float = 1.0):
        super().__init__()
        sc = self._sc = lambda v: int(v * scale)      # noqa: E731
        self.scale = scale
        self._pool, self._facts = pool, facts or {}
        self._public = FRIENDS.public_ids() or set()
        self._strip_sig = None
        self._n = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(sc(SP['3']))

        # What you'd get: the count and the first covers.
        well = QFrame()
        well.setObjectName("ModalWell")
        wl = QVBoxLayout(well)
        m = sc(SP['3'])
        wl.setContentsMargins(m, m, m, m)
        wl.setSpacing(sc(SP['2']))
        top = QHBoxLayout()
        top.setSpacing(sc(SP['2']))
        top.addWidget(icon_label(G['friends'], sc(16), P['accent']), 0,
                      Qt.AlignmentFlag.AlignVCenter)
        self.count = label("", "SmartHead", wrap=True)
        top.addWidget(self.count, 1)
        wl.addLayout(top)
        self.strip_host = QWidget()
        self.strip = QHBoxLayout(self.strip_host)
        self.strip.setContentsMargins(0, 0, 0, 0)
        self.strip.setSpacing(sc(SP['2']))
        wl.addWidget(self.strip_host)
        self.note = label(note, "Muted", wrap=True)
        wl.addWidget(self.note)                  # first: a parentless show is a window
        self.note.setVisible(bool(note))
        lay.addWidget(well)

        mode = QHBoxLayout()
        mode.setSpacing(sc(SP['2']))
        mode.addWidget(label("Show games for", "Muted"))
        self.mode = SegmentedControl('pill', sc(30))
        self.mode.add('multi', "More than one player",
                      tip="Co-op, online or local multiplayer; games Nexus knows no modes "
                          "for stay in")
        self.mode.add('any', "Any game", tip="Every game you all own")
        self.mode.set_current('multi' if multi else 'any', animate=False)
        self.mode.changed.connect(self._mode_changed)
        mode.addWidget(self.mode)
        mode.addStretch()
        lay.addLayout(mode)

        self.find = None
        if len(friends) > FIND_FROM:
            self.find = QLineEdit()
            self.find.setPlaceholderText("Find a friend")
            self.find.setAccessibleName("Find a friend")
            self.find.setClearButtonEnabled(True)
            self.find.setMinimumHeight(sc(40))
            self.find.textChanged.connect(self._filter)
            lay.addWidget(self.find)

        host = QWidget()
        self.rows_box = QVBoxLayout(host)
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(sc(SP['0_5']))
        lay.addWidget(host)
        self.rows: list[FriendToggle] = []
        self.empty = label("No friend by that name.", "Muted")
        self.empty.hide()
        lay.addWidget(self.empty)
        start = {str(s) for s in picked or ()} & self._public
        for rec, sub, live, private in self._ordered(friends):
            row = FriendToggle(rec, sub, live, private, scale)
            if not private:
                row.setChecked(row.sid in start)
                row.toggled.connect(self._toggled)
            self.rows_box.addWidget(row)
            row.show()                           # now: focus may go there at once
            self.rows.append(row)
        self._update()

    def _ordered(self, friends: list[dict]):
        """(record, line under the name, playing now, private) per friend,
        in the order they are shown."""
        fresh = FRIENDS.fresh()
        common = {}
        for g in self._pool:
            for s in g.get('friends') or ():
                common[s] = common.get(s, 0) + 1
        out = []
        for r in friends:
            sid = str(r.get('steamid'))
            private = r.get('visible') is False or sid not in self._public
            playing = (r.get('playing') or {}).get('name') if fresh and not private else None
            n = common.get(sid, 0)
            sub = "Private on Steam: their games can't be seen" if private else \
                f"Playing {playing}" if playing else \
                f"{n} game{'s' if n != 1 else ''} in common with you"
            order = (private, not playing, -n, (r.get('name') or sid).casefold())
            out.append((order, r, sub, bool(playing), private))
        out.sort(key=lambda t: t[0])
        return [t[1:] for t in out]

    # ---- what the window reads ----
    def chosen(self) -> list[str]:
        return [r.sid for r in self.rows if r.isChecked()]

    def multi(self) -> bool:
        return self.mode.current() != 'any'

    def key(self) -> str:
        """The Library filter for these picks ('' with nobody ticked)."""
        return smart.friends_key(self.chosen(), self.multi())

    def names(self) -> list[str]:
        return [r.name for r in self.rows if r.isChecked()]

    def shelf_draft(self) -> dict:
        """Save as shelf: a shelf with one "Friends own (all of)" rule."""
        return {'id': '', 'name': smart.clean_name("Play with " + names_text(self.names())),
                'match': 'all',
                'rules': [{'field': 'friends', 'op': 'all_multi' if self.multi() else 'all',
                           'value': self.chosen()}],
                'removed': False}

    def games(self) -> list[dict]:
        key = self.key()
        return smart.games_for(key, self._pool, self._facts, friends=self._public) \
            if key else []

    def can_show(self) -> bool:
        """Show N games: someone ticked, and some game to show."""
        return bool(self.key()) and self._n > 0

    def first_row(self) -> 'FriendToggle | None':
        """Where focus starts: the first friend who can be picked."""
        return next((r for r in self.rows if r.isEnabled() and not r.isHidden()), None)

    # ---- edits ----
    def _toggled(self, _on: bool):
        self.touched.emit()
        self._update()

    def _mode_changed(self, _key: str):
        self.touched.emit()
        self._update()

    def _filter(self, text: str):
        """Find a friend: rows whose name has the text. Focus leaves a row
        before it hides (w-60), then the modal fits the card again."""
        q = ' '.join(text.casefold().split())
        show = [r for r in self.rows if not q or q in r.name.casefold()]
        f = QApplication.focusWidget()
        if isinstance(f, FriendToggle) and f not in show and self.find is not None:
            self.find.setFocus(Qt.FocusReason.OtherFocusReason)
        for r in self.rows:
            r.setVisible(r in show)
        self.empty.setVisible(not show)
        self.relayout.emit()

    def _update(self):
        names = self.names()
        hit = self.games()
        n = self._n = len(hit)
        if not names:
            self.count.setText("Pick the friends you want to play with")
        else:
            who = f"you and {names[0]} both own" if len(names) == 1 else \
                f"you, {_and(names)} all own"
            self.count.setText(f"No games {who}" if not n else
                               f"{n} game{'s' if n != 1 else ''} {who}")
        sig = ([g['slug'] for g in hit[:COVERS]], max(0, n - COVERS))
        if sig != self._strip_sig:
            self._strip_sig = sig
            clear_layout(self.strip)
            from .shelfedit import cover_thumb
            for g in hit[:COVERS]:
                c = cover_thumb(g, self._sc(56), self._sc(84))
                self.strip.addWidget(c, 0, Qt.AlignmentFlag.AlignVCenter)
                c.show()                         # it has its parent now: never a window
            if len(hit) > COVERS:
                more = label(f"+{len(hit) - COVERS}", "Muted")
                self.strip.addWidget(more, 0, Qt.AlignmentFlag.AlignVCenter)
                more.show()
            self.strip.addStretch()
            self.strip_host.setVisible(bool(hit))
            self.relayout.emit()
        self.primary_text = f"Show {n} game{'s' if n != 1 else ''}" if names and n \
            else "Show games"
        self.primary.emit(self.primary_text)
        self.picked.emit(bool(names))
        self.changed.emit()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from nexus import smart  # noqa: E402
from .art import ART  # noqa: E402
from .widgets import (clear_layout, FlowLayout, icon_label, label, plain_label,  # noqa: E402
    SegmentedControl)
