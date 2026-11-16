"""Home's widgets that read the insights payload (w-138): On this day and
Game of the day (w-139), Your year and Next milestones (w-140). Each one is
a WidgetSpec in widget_spec.py plus one DashWidget here. The payload is
HomeFeed's (`hub.dashboard.feed.data`), asked for by the feeds in the spec.

The payload comes from the raw Library scan, which can hold games you hid,
so every widget checks its slugs against what Home shows. A widget with
nothing to say answers has_content() False and leaves the layout, like a
shelf. Words are kind: no streak is "No streak right now", never red."""

import random
from datetime import date, datetime, timedelta

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QProgressBar, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget)

from .art import anim_ms
from .common import alive
from .dashboard import ClickRow, DashWidget
from .daycard import card_text, DayCard, place_card
from .fmt import fmt_duration, fmt_hours
from .gameinfo import can_quick_play, is_installed
from .system import _add, _columns
from .theme import FONTS, FS, G, P, R, scaled, SP
from .widgets import clear_layout, label, plain_label

SMALL_ROWS = 3                  # Next milestones: rows at Small
WIDE_ROWS = 4                   # ... at Wide (two columns)
MORE_MEMORIES = 2               # On this day at Wide: others beside the first
CARD_DELAY_MS = 150             # a resting pointer shows the day card after this (0 with Motion off)
MAX_WEEKS = 52
MIN_WEEKS = 4
LEVEL_ALPHA = (0.0, 0.30, 0.50, 0.75, 1.0)     # heat level 0-4 of the accent
WEEKDAY_ROWS = {0: "Mon", 2: "Wed", 4: "Fri"}
ENTER_OPENS = 'game'            # what Enter on a day opens: 'game' (its top game) or 'history'


# ---- pure helpers (no Qt state) ----
def _day(iso) -> date | None:
    try:
        return date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None


def _date_text(d: date) -> str:
    return f"{d.day} {d:%b} {d.year}"          # no %-d: that is Linux only


def years_ago_text(n: int) -> str:
    return "A year ago today" if n == 1 else f"{n} years ago today"


def _years(n: int) -> str:
    return "a year" if n == 1 else f"{n} years"


def memories(data, visible: dict) -> list[dict]:
    """The payload's on_this_day entries for games Home shows, closest to
    the payload's own order (most minutes first)."""
    rows = (data or {}).get('on_this_day')
    if not isinstance(rows, list):
        return []
    return [e for e in rows if isinstance(e, dict) and e.get('slug') in visible
            and isinstance(e.get('years_ago'), int) and _day(e.get('date')) is not None]


def daily_choices(data, usable: dict, today: date) -> tuple:
    """(Game of the day, the one "Another day" brings): the payload's two
    picks that Home can use, then picks seeded by the date, so a hidden or
    removed game never leaves the card empty. Same answer all day."""
    out, seen = [], set()
    for key in ('daily', 'daily_alt'):
        e = (data or {}).get(key)
        if isinstance(e, dict) and e.get('slug') in usable and e['slug'] not in seen:
            seen.add(e['slug'])
            out.append({'slug': e['slug'], 'reason': e.get('reason') or 'pick',
                        'years': e.get('years')})
    rest = sorted(s for s in usable if s not in seen)
    random.Random(today.isoformat()).shuffle(rest)
    out += [{'slug': s, 'reason': 'pick', 'years': None} for s in rest[:2 - len(out)]]
    return (out[0] if out else None), (out[1] if len(out) > 1 else None)


def reason_text(entry: dict) -> str:
    n = entry.get('years')
    if entry.get('reason') == 'released' and isinstance(n, int):
        return f"Released {_years(n)} ago today"
    if entry.get('reason') == 'first_played' and isinstance(n, int):
        return f"You first played it {_years(n)} ago today"
    return "Today's pick"


def heat_columns(today: date, weeks: int) -> list[list]:
    """`weeks` columns, oldest first, each Monday to Sunday; the last is
    this week and days after today are None."""
    monday = today - timedelta(days=today.weekday())
    cols = []
    for w in range(weeks - 1, -1, -1):
        start = monday - timedelta(days=7 * w)
        cols.append([d if (d := start + timedelta(days=i)) <= today else None for i in range(7)])
    return cols


def heat_level(minutes: float, most: float) -> int:
    """0 for a day without play, else 1-4 by its share of the busiest day."""
    if not minutes:
        return 0
    share = minutes / most if most else 1.0
    return 1 if share <= 0.25 else 2 if share <= 0.5 else 3 if share <= 0.75 else 4


def enter_target(detail: dict | None) -> tuple[str, str | None]:
    """What Enter on a heatmap day opens: ('game', slug) for the day's top
    game, else ('history', None). The user's answer is pending: set
    ENTER_OPENS = 'history' to always open Play history."""
    games = (detail or {}).get('games') or []
    if ENTER_OPENS == 'game' and games and games[0].get('slug'):
        return 'game', games[0]['slug']
    return 'history', None


def milestone_text(m: dict) -> tuple[str, str, str]:
    """(name, "97 of 100 h", "3 h to go") for one payload milestone."""
    have, goal = m.get('have') or 0, m.get('goal') or 0
    if m.get('kind') == 'finished':
        return "Games finished", f"{have:g} of {goal:g}", f"{goal - have:g} to go"
    if m.get('kind') == 'year':
        name = "Hours this year"
    else:
        name = m.get('name') or "A game"
    left = goal - have
    return name, f"{have:g} of {goal:g} h", f"{left:.0f} h to go" if left >= 1 else "Almost there"


# ---- shared card plumbing ----
class InsightWidget(DashWidget):
    """A card that reads the insights payload and rebuilds only when what it
    shows changed (so a pad keeps its place across the hourly refresh)."""

    def __init__(self):
        super().__init__()
        self._hub = None
        self._sig = None

    def bind(self, hub):
        self._hub = hub

    @staticmethod
    def data(hub) -> dict | None:
        feed = getattr(getattr(hub, 'dashboard', None), 'feed', None)
        d = getattr(feed, 'data', None)
        return d if isinstance(d, dict) else None

    @staticmethod
    def shown(hub) -> dict:
        """The games Home shows by slug: not hidden, one copy of each."""
        return {g['slug']: g for g in getattr(hub, '_games', ())
                if g.get('slug') and not g.get('hidden') and not g.get('dup_of')}

    def signature(self, hub):
        raise NotImplementedError

    def build(self, hub):
        raise NotImplementedError

    def fill(self, hub):
        sig = (self.size_, self.signature(hub))
        if sig == self._sig and self.body.count():
            return
        self._sig = sig
        f = QApplication.focusWidget()
        had_focus = f is not None and self.isAncestorOf(f)
        self.clear()
        self.build(hub)
        if had_focus:                       # a rebuilt row must not drop a pad's place
            b = next((b for b in self.findChildren(QPushButton)
                      if b.objectName() == 'Primary' and b.isVisibleTo(self)), None)
            if b is not None:
                b.setFocus(Qt.FocusReason.OtherFocusReason)

    # shared bits
    def link_block(self, lines: list, on_click) -> ClickRow:
        """Labels stacked in one focusable, clickable row."""
        row = ClickRow()
        col = QVBoxLayout(row)
        col.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        col.setSpacing(SP['0_5'])
        for lab in lines:
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            col.addWidget(lab)
        row.clicked.connect(on_click)
        return row

    @staticmethod
    def open_page(hub, g: dict):
        hub._tile_clicked(g, Qt.KeyboardModifier.NoModifier)

    @staticmethod
    def play(hub, g: dict):
        if can_quick_play(g):
            hub.on_game_action('launch', g)
        else:
            hub._open_on_play(g)

    def play_button(self, hub, g: dict, text: str, fallback: str) -> QPushButton:
        b = QPushButton(text if can_quick_play(g) else fallback)
        b.setObjectName("Primary")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False, g=g: self.play(hub, g))
        return b


# ---- On this day (w-139) ----
class OnThisDayWidget(InsightWidget):
    """One year ago (or two, three...) around today's date: the game, how
    long you played and when, and Play again. Wide lists the next ones."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'onthisday', "On this day", G['history'], 'dusk', None

    def _entries(self, hub):
        return memories(self.data(hub), self.shown(hub))

    def has_content(self, hub):
        return bool(self._entries(hub))

    def signature(self, hub):
        shown = self.shown(hub)
        return tuple((e['slug'], e['years_ago'], e['date'], e['minutes'],
                      can_quick_play(shown[e['slug']])) for e in self._entries(hub))

    def build(self, hub):
        entries = self._entries(hub)
        if not entries:
            return
        shown = self.shown(hub)
        first = entries[0]
        g = shown[first['slug']]
        wide = self.size_ == 'wide'
        left, right = _columns(self.body, 2) if wide else (self.body, self.body)
        _add(left, self.link_block(
            [label(years_ago_text(first['years_ago']), "DashBig", wrap=True),
             plain_label(g.get('display_name') or first.get('name') or g['slug'], "RowTitle", wrap=True),
             label(self._when(first), "Muted", wrap=True)],
            lambda g=g: self.open_page(hub, g)))
        _add(left, self.play_button(hub, g, "Play again", "Open page"))
        if wide and len(entries) > 1:
            _add(right, label("Also around this date", "StatLabel"))
            for e in entries[1:1 + MORE_MEMORIES]:
                og = shown[e['slug']]
                _add(right, self.row(G['history'], og.get('display_name') or e.get('name') or og['slug'],
                                     f"{years_ago_text(e['years_ago']).replace(' today', '')}"
                                     f" · {fmt_duration(e['minutes'])}",
                                     on_click=lambda og=og: self.open_page(hub, og)))

    @staticmethod
    def _when(e: dict) -> str:
        d = _day(e['date'])
        return f"{fmt_duration(e['minutes'])} on {_date_text(d)}"


# ---- Game of the day (w-139) ----
class DailyWidget(InsightWidget):
    """One installed game, the same all day and a new one at midnight, with
    why. "Another day" brings the next pick, once a day (library.json's
    `daily_rerolled`, the date it was used)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'daily', "Game of the day", G['sparkles'], 'dawn', None

    def _state(self, hub):
        """(entry shown, can still re-roll, game) for today."""
        data = self.data(hub)
        day = _day((data or {}).get('today')) or date.today()
        usable = {s: g for s, g in self.shown(hub).items() if is_installed(g)}
        first, alt = daily_choices(data, usable, day)
        used = getattr(getattr(hub, 'prefs', None), 'daily_rerolled', None) == day.isoformat()
        entry = alt if used and alt else first
        return entry, bool(alt) and not used, usable.get(entry['slug']) if entry else None

    def has_content(self, hub):
        return self._state(hub)[0] is not None

    def signature(self, hub):
        entry, can, g = self._state(hub)
        return (entry and (entry['slug'], entry['reason'], entry['years']), can,
                g is not None and can_quick_play(g))

    def build(self, hub):
        entry, can, g = self._state(hub)
        if entry is None or g is None:
            return
        self.body.addWidget(self.link_block(
            [plain_label(g.get('display_name') or g['slug'], "DashTitle", wrap=True),
             label(reason_text(entry), "Muted", wrap=True)],
            lambda: self.open_page(hub, g)))
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        row.addWidget(self.play_button(hub, g, "Play", "Open page"))
        if can:
            again = QPushButton("Another day")
            again.setObjectName("Ghost")
            again.setToolTip("Another pick for today. Once a day.")
            again.setCursor(Qt.CursorShape.PointingHandCursor)
            again.clicked.connect(lambda: self._again(hub))
            row.addWidget(again)
        row.addStretch()
        self.body.addLayout(row)

    def _again(self, hub):
        """Use today's re-roll: remember the date, then draw the next pick.
        Focus moves to Play first: this button goes away."""
        data = self.data(hub)
        day = (data or {}).get('today') or date.today().isoformat()
        hub.prefs.daily_rerolled = day
        hub.prefs.save()
        play = next((b for b in self.findChildren(QPushButton) if b.objectName() == 'Primary' and not b.isHidden()), None)
        if play is not None:
            play.setFocus(Qt.FocusReason.OtherFocusReason)
        self.fill(hub)


# ---- Your year (w-140) ----
class HeatGrid(QWidget):
    """A calendar heatmap: Monday to Sunday down, one column per week, as
    many weeks as fit (up to 52), this week last, today ringed. Painted, so
    the card keeps its Open button for a pad. The day under the pointer (or the
    keyboard) gets a ring and a painted DayCard beside it, inside the window."""

    OWN_FOCUS_RING = True       # the grid rings its own cell (input.FocusRing skips it)
    NATIVE_ARROWS = 'all'       # arrows move the cell, never the window's focus (input._native_arrows)
    activated = pyqtSignal(object)      # Enter on a day (a date)
    leave_requested = pyqtSignal()      # Escape or the pad's B: back to the Open button

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Your year, play calendar")
        self.keyboard = False                     # has focus: the cell gets the focus ring
        self.days: dict[str, float] = {}
        self.detail: dict[str, dict] = {}         # day -> payload day_games entry (the card)
        self.avg = 0.0                            # the payload's avg_minutes (the card's footer)
        self.games: dict = {}                     # slug -> game, for the covers
        self.card: DayCard | None = None
        self.show_timer = QTimer(self)
        self.show_timer.setSingleShot(True)
        self.show_timer.timeout.connect(self._timed_show)
        self.hovered: date | None = None
        self.today: date | None = None
        self.cell, self.gap = scaled(12), scaled(3)
        self.setMouseTracking(True)
        self.setMinimumWidth(self.left_pad() + MIN_WEEKS * (self.cell + self.gap))
        self.setMinimumHeight(self.top_pad() + 7 * (self.cell + self.gap) - self.gap)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def left_pad(self) -> int:
        return scaled(30)

    def top_pad(self) -> int:
        return scaled(16)

    def set_data(self, days: dict, today: date, detail: dict | None = None,
                 avg: float = 0.0, games: dict | None = None):
        self.days, self.today, self.detail = days or {}, today, detail or {}
        self.avg, self.games = avg or 0.0, games or {}
        self.hovered = None
        self._hide_card()
        self.update()

    def weeks_fit(self) -> int:
        n = (self.width() - self.left_pad() + self.gap) // (self.cell + self.gap)
        return max(MIN_WEEKS, min(MAX_WEEKS, n))

    def _columns(self):
        return heat_columns(self.today, self.weeks_fit()) if self.today else []

    def _rect(self, c: int, r: int) -> QRectF:
        step = self.cell + self.gap
        return QRectF(self.left_pad() + c * step, self.top_pad() + r * step, self.cell, self.cell)

    def cell_rect(self, d: date) -> QRectF | None:
        for c, col in enumerate(self._columns()):
            if d in col:
                return self._rect(c, col.index(d))
        return None

    def today_cell(self) -> date | None:
        return self.today if self.today and self.cell_rect(self.today) is not None else None

    def day_at(self, pos) -> date | None:
        p = QPointF(pos)
        for c, col in enumerate(self._columns()):
            for r, d in enumerate(col):
                if d is not None and self._rect(c, r).contains(p):
                    return d
        return None

    def card_text(self, d: date):
        """What the day card says for `d` (also its accessibleDescription)."""
        return card_text(d, self.days.get(d.isoformat(), 0), self.detail.get(d.isoformat()),
                         self.avg, self.today)

    def _card(self) -> DayCard:
        win = self.window()
        if self.card is None or not alive(self.card):
            self.card = DayCard(win)
            # The card lives in the window, not in the grid: it goes with the grid.
            self.destroyed.connect(lambda _=None, c=self.card: alive(c) and c.deleteLater())
        elif self.card.parent() is not win:
            self.card.setParent(win)
        return self.card

    def _show_card(self, d: date):
        """Put the day card beside `d`'s cell, inside the window; flipped left
        near the right edge and moved up near the bottom."""
        rect = self.cell_rect(d)
        if rect is None:
            return self._hide_card()
        self.show_timer.stop()
        card = self._card()
        win = self.window()
        card.set_content(self.card_text(d), self.games)
        top_left = self.mapTo(win, rect.topLeft().toPoint())
        cell = QRectF(float(top_left.x()), float(top_left.y()), rect.width(), rect.height())
        proper = card.card_rect()
        at = place_card(cell, proper.size().toSize(), QRectF(win.rect()), float(SP['2']),
                        float(SP['1']))
        card.move(int(round(at.x() - proper.left())), int(round(at.y() - proper.top())))
        if not card.isVisible():
            card.appear()
        else:
            card.raise_()

    def _hide_card(self):
        self.show_timer.stop()
        if self.card is not None and alive(self.card):
            self.card.vanish()

    def _timed_show(self):
        if self.hovered is not None:
            self._show_card(self.hovered)

    def _card_for_hover(self):
        d = self.hovered
        if d is None:
            return self._hide_card()
        if self.card is not None and alive(self.card) and self.card.isVisible():
            return self._show_card(d)                  # moving along the grid: no new delay
        delay = anim_ms(CARD_DELAY_MS)
        if delay:
            self.show_timer.start(delay)
        else:
            self._show_card(d)

    # ---- keyboard and pad (w-149) ----
    def _move_to(self, d: date | None):
        """Put the ring and the card on `d` (a cell of the grid), at once."""
        if d is None or self.cell_rect(d) is None:
            return
        self.hovered = d
        self._describe(d)
        self.update()
        self._show_card(d)

    def _describe(self, d: date):
        self.setAccessibleDescription(self.card_text(d).text())

    def _step(self, d: date, key) -> date | None:
        """The cell a key moves to from `d`: a week sideways, a day up or down,
        Home/End to the first/last week (same weekday); None when there is none."""
        cols = self._columns()
        if not cols:
            return None
        row = d.weekday()
        if key == Qt.Key.Key_Left:
            return d - timedelta(days=7)
        if key == Qt.Key.Key_Right:
            return d + timedelta(days=7)
        if key == Qt.Key.Key_Up:
            return d - timedelta(days=1)
        if key == Qt.Key.Key_Down:
            return d + timedelta(days=1)
        if key == Qt.Key.Key_Home:
            return cols[0][row]
        if key == Qt.Key.Key_End:
            return cols[-1][row] or self.today
        return None

    def keyPressEvent(self, ev):
        key = ev.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                   Qt.Key.Key_Home, Qt.Key.Key_End):
            cur = self.hovered or self.today_cell()
            if cur is not None:
                self._move_to(self._step(cur, key))      # off the grid: stays
            ev.accept()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.hovered is not None:
                self.activated.emit(self.hovered)
            ev.accept()
        elif key == Qt.Key.Key_Escape:                   # the pad's B arrives as Escape
            self.leave_requested.emit()
            ev.accept()
        else:
            super().keyPressEvent(ev)

    def focusInEvent(self, ev):
        self.keyboard = True
        if ev.reason() != Qt.FocusReason.MouseFocusReason or self.hovered is None:
            self._move_to(self.hovered or self.today_cell())     # Tab lands on today
        elif self.hovered is not None:
            self._describe(self.hovered)
        self.update()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        self.keyboard = False
        self.hovered = None
        self._hide_card()
        self.update()
        super().focusOutEvent(ev)

    def hover_at(self, pos):
        d = self.day_at(pos)
        if d != self.hovered:
            self.hovered = d
            self.update()
            self._card_for_hover()

    def mouseMoveEvent(self, ev):
        self.hover_at(ev.position().toPoint())
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):
        if not self.keyboard:                           # a focused grid keeps its keyboard cell
            if self.hovered is not None:
                self.hovered = None
                self.update()
            self._hide_card()
        if ev is not None:
            super().leaveEvent(ev)

    def hideEvent(self, ev):
        self._hide_card()
        super().hideEvent(ev)

    def resizeEvent(self, ev):
        self._hide_card()                              # the cells moved under it
        super().resizeEvent(ev)

    def paintEvent(self, ev):
        cols = self._columns()
        if not cols:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(FONTS.ui)
        f.setPixelSize(FS['2xs'])
        pt.setFont(f)
        most = max((self.days.get(d.isoformat(), 0) for c in cols for d in c if d), default=0)
        radius = R['sm'] / 2
        pt.setPen(QColor(P['muted']))
        for r, name in WEEKDAY_ROWS.items():
            pt.drawText(QRectF(0, self._rect(0, r).top() - 2, self.left_pad() - self.gap,
                               self.cell + 4), Qt.AlignmentFlag.AlignVCenter, name)
        last_label, prev_month = -9, None
        for c, col in enumerate(cols):
            first = next(d for d in col if d)
            if first.month != prev_month and c - last_label >= 3:
                pt.setPen(QColor(P['muted']))
                pt.drawText(QRectF(self._rect(c, 0).left(), 0, self.cell * 4, self.top_pad() - 2),
                            Qt.AlignmentFlag.AlignVCenter, f"{first:%b}")
                last_label = c
            prev_month = first.month
            for r, d in enumerate(col):
                if d is None:
                    continue
                level = heat_level(self.days.get(d.isoformat(), 0), most)
                color = QColor(P['accent'] if level else P['overlay'])
                if level:
                    color.setAlphaF(LEVEL_ALPHA[level])
                pt.setPen(Qt.PenStyle.NoPen)
                pt.setBrush(color)
                rect = self._rect(c, r)
                pt.drawRoundedRect(rect, radius, radius)
                if d == self.today:                    # today: a ring just outside the cell
                    pt.setBrush(Qt.BrushStyle.NoBrush)
                    pt.setPen(QPen(QColor(P['text']), 1.5))
                    pt.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius + 1, radius + 1)
                if d == self.hovered and self.keyboard:     # focus: 2 px accent, next to today's ring
                    pt.setBrush(Qt.BrushStyle.NoBrush)
                    pt.setPen(QPen(QColor(P['accent']), 2.0))
                    pt.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), radius + 2, radius + 2)
                elif d == self.hovered and d != self.today:   # under the pointer: a softer ring
                    pt.setBrush(Qt.BrushStyle.NoBrush)
                    pt.setPen(QPen(QColor(P['accent']), 1.5))
                    pt.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius + 1, radius + 1)
        pt.end()


class HeatmapWidget(InsightWidget):
    """Your year: the play calendar, this year's total, the streak now and
    the best one, the busiest weekday. Open goes to Play history."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'heatmap', "Your year", G['calendar'], 'tide', 'history'

    def __init__(self):
        super().__init__()
        self.grid: HeatGrid | None = None

    def signature(self, hub):
        d = self.data(hub)
        if d is None:
            return None
        return (d.get('today'), repr(d.get('days')), repr(d.get('day_games')),
                d.get('avg_minutes'), repr(d.get('streak')), d.get('busiest_day'), d.get('year_minutes'))

    def clear(self):
        self.grid = None
        super().clear()

    def build(self, hub):
        d = self.data(hub)
        if d is None:
            self.body.addWidget(label("Your play days show here as you play.", "Muted", wrap=True))
            return
        today = _day(d.get('today')) or date.today()
        self.grid = HeatGrid()
        self.grid.activated.connect(lambda day: self._enter(day))
        self.grid.leave_requested.connect(self._leave_grid)
        self.grid.set_data(d.get('days') or {}, today, d.get('day_games') or {},
                           d.get('avg_minutes') or 0.0, self.shown(hub))
        self.body.addWidget(self.grid)
        streak = d.get('streak') or {}
        now, best = streak.get('now') or 0, streak.get('best') or 0
        stats = QHBoxLayout()
        stats.setSpacing(SP['6'])
        for cap, value in (
                ("Streak", f"{now} day streak" if now else "No streak right now"),
                ("Best this year", (f"{best} day{'s' if best != 1 else ''}") if best else "–"),
                ("Busiest day", d.get('busiest_day') or "–"),
                ("Played this year", fmt_hours(d.get('year_minutes')) or "–")):
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(label(cap, "StatLabel"))
            col.addWidget(label(value, "StatValue"))
            stats.addLayout(col)
        stats.addStretch()
        self.body.addLayout(stats)


    def _enter(self, day: date):
        """Enter on a day: its top game's page, else Play history (enter_target)."""
        hub = self._hub
        kind, slug = enter_target(((self.data(hub) or {}).get('day_games') or {}).get(day.isoformat()))
        g = self.shown(hub).get(slug) if kind == 'game' else None
        if g is not None:
            self.open_page(hub, g)
        else:
            self.go.emit('history')

    def _leave_grid(self):
        btn = getattr(self, 'open_btn', None)
        if btn is not None and alive(btn):
            btn.setFocus(Qt.FocusReason.TabFocusReason)


# ---- Next milestones (w-140) ----
class MilestonesWidget(InsightWidget):
    """The round numbers you are close to, each with a bar and a link: a
    game's page, the Library (games finished) or Play history (this year)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'milestones', "Next milestones", G['trophy'], 'aurora', None

    def __init__(self):
        super().__init__()
        self.rows: list[ClickRow] = []

    def _items(self, hub) -> list[dict]:
        rows = (self.data(hub) or {}).get('milestones')
        shown = self.shown(hub)
        return [m for m in (rows if isinstance(rows, list) else []) if isinstance(m, dict)
                and m.get('goal') and (m.get('slug') is None or m['slug'] in shown)]

    def has_content(self, hub):
        return bool(self._items(hub))

    def signature(self, hub):
        return repr(self._items(hub))

    def clear(self):
        self.rows = []
        super().clear()

    def build(self, hub):
        items = self._items(hub)[:WIDE_ROWS if self.size_ == 'wide' else SMALL_ROWS]
        if not items:
            return
        wide = self.size_ == 'wide' and len(items) > 1
        cols = _columns(self.body, 2) if wide else [self.body]
        for i, m in enumerate(items):
            _add(cols[i % len(cols)], self._row(hub, m))

    def _row(self, hub, m: dict) -> ClickRow:
        name, have, left = milestone_text(m)
        row = ClickRow()
        row.setProperty('kind', m.get('kind'))
        col = QVBoxLayout(row)
        col.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        col.setSpacing(SP['1'])
        head = QHBoxLayout()
        n = plain_label(name, None)
        n.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        n.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(n, 1)
        head.addWidget(label(have, "Muted"))
        col.addLayout(head)
        bar = QProgressBar()
        bar.setObjectName("CardProgress")
        bar.setTextVisible(False)
        bar.setFixedHeight(4)
        bar.setRange(0, 1000)
        bar.setValue(int(1000 * min(1.0, (m.get('have') or 0) / m['goal'])))
        col.addWidget(bar)
        col.addWidget(label(left, "Muted"))
        for w in (n, bar, *row.findChildren(type(n))):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.clicked.connect(lambda m=m: self._open(hub, m))
        self.rows.append(row)
        return row

    def _open(self, hub, m: dict):
        if m.get('slug'):
            g = self.shown(hub).get(m['slug'])
            if g is not None:
                self.open_page(hub, g)
        else:
            hub.go('history' if m.get('kind') == 'year' else 'library')


INSIGHT_WIDGETS = (OnThisDayWidget, DailyWidget, HeatmapWidget, MilestonesWidget)
