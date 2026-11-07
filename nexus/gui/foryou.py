"""For you now on Home (w-89): the moments home_moments() ranks (w-88),
shown minimised.

- **On stage** (default): a glass pill at the end of the Stage's Play row
  with the top moment and its action. With more than one, the pill opens a
  list over the art that grows up out of it and takes its place while open
  (its bottom-right corner sits on the pill's); never a window, never open
  on its own, collapsed every time Home is shown.
- **Strip**: a slim card under the Stage with up to three moments in a line
  and "N more". The Stage hidden or at Banner size falls back to it.
- Not now snoozes one moment, Quiet until tomorrow all but problems, Kinds…
  (Customise) whole kinds.

ForYouModel asks home_moments over what the window already has (no CLI)
and says `changed` only when what it shows changed (a signature), so a
download's percent updates text in place and never rebuilds what has focus."""

from datetime import datetime

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from nexus.features import has

from .common import alive
from .dashboard import DashWidget, GlyphBadge
from .moments import home_moments, moments_inputs, stage_key, TOTAL_MAX
from .theme import G, P, pill, scaled, SP, STAGE_BTN_H

TONE_GRAD = {'accent': 'dusk', 'success': 'tide', 'warning': 'dawn', 'danger': 'ember'}
LIST_ROWS = 5                  # the open list's rows before "N more"
# Customise → Kinds…: the chips, checked = shown. Problems always show.
KIND_CHIPS = (('running', "Downloads"), ('friends', "Friends playing"), ('store', "Deals"),
              ('update', "Updates"), ('achievements', "Achievements"),
              ('idle', "Pick up again"), ('disk', "Storage"))
KIND_NAMES = dict(KIND_CHIPS)
KINDS_TIP = ("Which moments For you now shows. Problems (a failing check, a drive "
             "nearly out of space) always show.")


# ── pure helpers ─────────────────────────────────────────────────────────

def foryou_mode(items, editing: bool, stage_shown: bool) -> str:
    """How For you now shows: 'pill' on the Stage, 'placeholder' (its card
    in Customise while it is on the Stage), 'strip' (chosen, or the Stage is
    hidden, at Banner size or has no game), or 'off' (hidden)."""
    fy = next((i for i in items if i.get('id') == 'foryou'), None)
    if fy is None or fy.get('hidden'):
        return 'off'
    st = next((i for i in items if i.get('id') == 'stage'), None)
    on_stage = (fy.get('style', 'stage') == 'stage' and stage_shown and st is not None
                and not st.get('hidden') and st.get('size') in ('cinematic', 'wide'))
    if not on_stage:
        return 'strip'
    return 'placeholder' if editing else 'pill'


def moments_sig(moments) -> tuple:
    """What changes the rows: which moments, in which order."""
    return tuple(m['id'] for m in moments)


def moments_text(moments) -> tuple:
    """What only changes text in place (a percent, a friend's minutes)."""
    return tuple((m['title'], m['why'], m['action']['label'], tuple(m['badge']),
                  m.get('count')) for m in moments)


def split_snooze(key: str) -> tuple[str, str]:
    """A moment's snooze_key ('kind:key') as LibraryPrefs.snooze takes it."""
    kind, _sep, rest = str(key).partition(':')
    return kind, rest


def strip_count(width: int) -> int:
    """How many moments the Strip shows in its line at this width."""
    return max(1, min(3, (width - 232) // scaled(340)))


def list_width(cols: int, content_w: int) -> int:
    """The open list's width: fixed beside a wide Stage, the whole art in
    one column."""
    return scaled(440) if cols >= 3 else scaled(400) if cols == 2 else content_w


def badge(m: dict, size: int) -> GlyphBadge:
    glyph, tone = m['badge']
    return GlyphBadge(G.get(glyph, G['sparkles']), TONE_GRAD.get(tone, 'dusk'), size)


def _set_badge(b: GlyphBadge, m: dict):
    glyph, tone = m['badge']
    b.glyph, b.grad = G.get(glyph, G['sparkles']), TONE_GRAD.get(tone, 'dusk')
    b.update()


def _focus_in(w: QWidget) -> bool:
    f = QApplication.focusWidget()
    return f is not None and (f is w or w.isAncestorOf(f))


def _focus(w: QWidget | None):
    if w is not None and alive(w):
        focus_and_reveal(w)


# ── the model ────────────────────────────────────────────────────────────

class ForYouModel(QObject):
    """The moments now, and what the user does with them."""
    changed = pyqtSignal()

    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.moments: list[dict] = []
        self._sig = None

    def compute(self) -> list[dict]:
        prefs = self.hub.prefs
        return home_moments(moments_inputs(self.hub), datetime.now(),
                            snoozes=prefs.snoozes, quiet_until=prefs.quiet_until,
                            kinds_off=prefs.kinds_off, stage_key=stage_key(self.hub),
                            linux=has('proton'))

    def refresh(self) -> bool:
        """Ask again; `changed` (and True) only when something shown moved."""
        ms = self.compute()
        sig = (moments_sig(ms), moments_text(ms))
        if sig == self._sig:
            return False
        self._sig, self.moments = sig, ms
        self.changed.emit()
        return True

    def act(self, m: dict):
        self.hub.moment_action(m)

    def not_now(self, m: dict):
        kind, key = split_snooze(m['snooze_key'])
        self.hub.prefs.snooze(kind, key, datetime.now())
        self.refresh()

    def quiet(self):
        self.hub.prefs.quiet_tomorrow(datetime.now())
        self.refresh()

    def set_kind(self, kind: str, shown: bool):
        off = [k for k in self.hub.prefs.kinds_off if k != kind] + ([] if shown else [kind])
        if off != self.hub.prefs.kinds_off:
            self.hub.prefs.kinds_off = off
            self.hub.prefs.save()
            self.refresh()


# ── one moment ───────────────────────────────────────────────────────────

class MomentRow(QFrame):
    """One moment: badge, title and why line (both elided), one action and
    ✕ Not now. On the art (the list) or on the page (the Strip, expanded)."""
    acted = pyqtSignal(dict)
    dismissed = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)

    def __init__(self, m: dict, on_art: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("FynRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.m = m
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['1_5'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2_5'])
        if on_art:
            box = QWidget()
            box.setFixedSize(48, 28)
            bl = QHBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            self.badge = badge(m, 26)
            bl.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(box)
        else:
            self.badge = badge(m, 28)
            lay.addWidget(self.badge)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.title = ElideLabel('', "FynTitle")
        self.why = ElideLabel('', "FynWhy")
        col.addWidget(self.title)
        col.addWidget(self.why)
        lay.addLayout(col, 1)
        self.act = QPushButton()
        if on_art:
            self.act.setObjectName("MomentAct")
        self.act.setFixedHeight(scaled(28))
        self.act.setCursor(Qt.CursorShape.PointingHandCursor)
        self.act.clicked.connect(lambda: self.acted.emit(self.m))
        lay.addWidget(self.act)
        self.x = icon_button(G['close'], "Not now", scaled(28), "MomentX" if on_art else "Ghost",
                             round_=True)
        self.x.clicked.connect(lambda: self.dismissed.emit(self.m))
        lay.addWidget(self.x)
        self.set_moment(m)

    def set_moment(self, m: dict):
        self.m = m
        self.title.setText(m['title'])
        self.why.setText(m['why'])
        self.setToolTip(f"{m['title']}\n{m['why']}")
        self.act.setText(amp(m['action']['label']))
        _set_badge(self.badge, m)

    def contextMenuEvent(self, ev):
        self.menu.emit(self.m, ev.globalPos())


# ── On stage: the pill and its list ──────────────────────────────────────

class PillBody(QFrame):
    """The pill's sentence: clicking it (or A / Enter) opens the list."""
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FynPillBody")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(ev)


class ForYouPill(QFrame):
    """Collapsed For you now at the end of the Stage's Play row: the top
    moment and its action; with more than one, the sentence opens the list,
    with one, ✕ Not now sits beside the action instead."""
    opened = pyqtSignal()
    acted = pyqtSignal(dict)
    dismissed = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FynPill")
        self.moments: list[dict] = []
        self.H = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['1_5'])
        self.body = PillBody()
        bl = QHBoxLayout(self.body)
        bl.setContentsMargins(SP['1'], 0, SP['2'], 0)
        bl.setSpacing(SP['2'])
        self.badge = GlyphBadge(G['sparkles'], 'dusk', 28, round_=True)
        bl.addWidget(self.badge)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.kicker = label("", "FynKicker")
        self.title = ElideLabel('', "FynTitle")
        col.addWidget(self.kicker)
        col.addWidget(self.title)
        bl.addLayout(col, 1)
        self.ink = 'on_art'                        # muted off the art (set_ink, w-90)
        self.chevron = icon_label(G['chevron_up'], scaled(12), P[self.ink])
        bl.addWidget(self.chevron)
        self.body.clicked.connect(self.opened)
        lay.addWidget(self.body, 1)
        self.act = QPushButton()
        self.act.setObjectName("MomentAct")
        self.act.setCursor(Qt.CursorShape.PointingHandCursor)
        self.act.clicked.connect(lambda: self.moments and self.acted.emit(self.moments[0]))
        lay.addWidget(self.act)
        self.x = icon_button(G['close'], "Not now", 28, "MomentX", round_=True)
        self.x.clicked.connect(lambda: self.moments and self.dismissed.emit(self.moments[0]))
        lay.addWidget(self.x)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_height(self, h: int):
        """As tall as the Play row (Play grows with Appearance → Size); the
        radius follows, as icon_button(round_=True) does. Everything inside
        is concentric with the capsule: the action and ✕ are capsules inset
        by the same margin on every side, and the round badge sits as far
        from the left as from the top."""
        h = max(STAGE_BTN_H, h)
        if h == self.H:
            return
        self.H = h
        inner = h - 2 * SP['1'] - 2
        self.setFixedHeight(h)
        self.setStyleSheet(f"#FynPill {{ border-radius: {pill(h)}px; }}"
                           f" #FynPillBody {{ border-radius: {pill(inner)}px; }}"
                           f" #FynPill QPushButton#MomentAct {{ border-radius: {pill(inner)}px; }}")
        self.act.setFixedHeight(inner)
        self.x.setFixedSize(inner, inner)
        self.x.setStyleSheet(f"border-radius: {pill(inner)}px;")
        b = min(scaled(28), inner - 4)
        self.badge.setFixedSize(b, b)
        self.body.layout().setContentsMargins((inner - b) // 2, 0, SP['2'], 0)

    def set_moments(self, moments: list[dict]):
        self.moments = list(moments)
        if not moments:
            return
        m, many = moments[0], len(moments) > 1
        # Focus moves before the part that has it hides (w-60).
        if many and self.x.hasFocus():
            self.act.setFocus(Qt.FocusReason.OtherFocusReason)
        if not many and self.body.hasFocus():
            self.act.setFocus(Qt.FocusReason.OtherFocusReason)
        self.kicker.setText(f"FOR YOU NOW  ·  {len(moments)}" if many else "FOR YOU NOW")
        self.title.setText(m['title'])
        self.body.setToolTip(f"{m['title']}\n{m['why']}"
                             + ("\nShow all" if many else ""))
        self.act.setText(amp(m['action']['label']))
        _set_badge(self.badge, m)
        self.chevron.setVisible(many)
        self.x.setVisible(not many)
        self.body.setFocusPolicy(Qt.FocusPolicy.StrongFocus if many else Qt.FocusPolicy.NoFocus)
        self.updateGeometry()

    def set_ink(self, role: str):
        """The chevron's colour: on_art on the art, muted on the card surface.
        A new label, not setStyleSheet, which would drop its glyph font (w-54)."""
        if role == self.ink:
            return
        self.ink = role
        lay = self.body.layout()
        old = self.chevron
        new = icon_label(G['chevron_up'], scaled(12), P[role])
        lay.replaceWidget(old, new)
        new.setVisible(not old.isHidden())       # in the layout first (w-54)
        old.hide()
        old.deleteLater()
        self.chevron = new

    def first_focus(self) -> QWidget:
        """Where focus goes when the list closes: the sentence, or the action
        when there is no list."""
        return self.body if len(self.moments) > 1 else self.act

    def sizeHint(self):
        s = super().sizeHint()
        lay = self.layout()
        m = lay.contentsMargins()
        bm = self.body.layout().contentsMargins()
        text = max(self.title.fontMetrics().horizontalAdvance(self.title.text()),
                   self.kicker.fontMetrics().horizontalAdvance(self.kicker.text()))
        many = len(self.moments) > 1
        want = (m.left() + m.right() + bm.left() + bm.right() + self.badge.width()
                + SP['2'] + text + 4
                + (self.chevron.sizeHint().width() + SP['2'] if many else 0)
                + lay.spacing() + self.act.sizeHint().width()
                + (lay.spacing() + self.x.width() if not many else 0))
        s.setWidth(min(self.maximumWidth(), max(scaled(220), want)))
        return s

    def contextMenuEvent(self, ev):
        if self.moments:
            self.menu.emit(self.moments[0], ev.globalPos())


class ForYouList(QFrame):
    """The open list: a child of the Stage's art in no layout (never a
    window). It grows up out of the pill: its bottom-right corner on the
    pill's, the footer where the pill was, no higher than the art's top
    (rows scroll, whole rows at a time, past that). The pill hides while it
    is open."""
    acted = pyqtSignal(dict)
    dismissed = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    closing = pyqtSignal()
    quiet = pyqtSignal()

    def __init__(self, art: QWidget):
        super().__init__(art)
        self.setObjectName("FynList")
        self.art = art
        self.pill: QWidget | None = None
        self.rows: dict[str, MomentRow] = {}
        self.order: list[str] = []
        self.moments: list[dict] = []
        self.limit = LIST_ROWS
        self.start: str | None = None          # the row it opened on (default_focus)
        self._width = scaled(440)
        v = QVBoxLayout(self)
        v.setContentsMargins(SP['2'], SP['2'], SP['2'], SP['2'])
        v.setSpacing(SP['1'])
        self.rows_host = QWidget()
        self.rows_host.setObjectName("FynRows")
        self.rows_lay = QVBoxLayout(self.rows_host)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.rows_host)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        v.addWidget(self.scroll)
        ft = QHBoxLayout()
        ft.setContentsMargins(SP['1_5'], 0, 0, 0)
        ft.setSpacing(SP['1'])
        self.kicker = label("", "FynKicker")
        ft.addWidget(self.kicker)
        ft.addStretch()
        self.btn_more = QPushButton()
        self.btn_more.setObjectName("MomentFoot")
        self.btn_more.setFixedHeight(scaled(28))
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_more.clicked.connect(self.show_more)
        ft.addWidget(self.btn_more)
        self.btn_quiet = QPushButton("Quiet until tomorrow")
        self.btn_quiet.setObjectName("MomentFoot")
        self.btn_quiet.setFixedHeight(scaled(28))
        self.btn_quiet.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_quiet.setToolTip("Hide For you now until 04:00. Problems still show.")
        set_glyph_icon(self.btn_quiet, G['moon'], 14, 'on_art', hover_role=None)
        self.btn_quiet.clicked.connect(self.quiet)
        ft.addWidget(self.btn_quiet)
        self.btn_close = icon_button(G['chevron_down'], "Close", scaled(28), "MomentX",
                                     round_=True)
        self.btn_close.clicked.connect(self.closing)
        ft.addWidget(self.btn_close)
        v.addLayout(ft)
        self._place_timer = QTimer(self)
        self._place_timer.setSingleShot(True)
        self._place_timer.setInterval(0)
        self._place_timer.timeout.connect(self.place)
        self._outside_timer = QTimer(self)
        self._outside_timer.setSingleShot(True)
        self._outside_timer.setInterval(0)
        self._outside_timer.timeout.connect(self._outside)
        self.hide()

    # ---- rows ----
    def _row(self, m: dict) -> MomentRow:
        r = MomentRow(m, True, self.rows_host)
        r.acted.connect(self.acted)
        r.dismissed.connect(self._dismiss)
        r.menu.connect(self.menu)
        return r

    def sync(self, moments: list[dict]):
        """Show these moments, reusing the rows of the ones still there, so a
        focused row keeps its focus; a row that goes gives focus to the next
        one first."""
        self.moments = list(moments)
        shown = self.moments[:self.limit]
        want = [m['id'] for m in shown]
        gone = [i for i in self.order if i not in want]
        f = QApplication.focusWidget()
        for i in gone:
            r = self.rows[i]
            if f is not None and r.isAncestorOf(f):
                nxt = self._neighbour(i, want)
                if nxt is not None:
                    _focus(nxt.act)
        for i in gone:
            r = self.rows.pop(i)
            self.rows_lay.removeWidget(r)
            r.hide()
            r.deleteLater()
        for k, m in enumerate(shown):
            r = self.rows.get(m['id'])
            if r is None:
                r = self.rows[m['id']] = self._row(m)
            else:
                r.set_moment(m)
            if self.rows_lay.indexOf(r) != k:
                self.rows_lay.removeWidget(r)
                self.rows_lay.insertWidget(k, r)
            r.show()                        # after it is in the layout (w-57)
        self.order = want
        n = len(self.moments)
        self.kicker.setText(f"FOR YOU NOW  ·  {n}")
        more = n - len(shown)
        if more <= 0 and self.btn_more.hasFocus() and self.order:
            _focus(self.rows[self.order[-1]].act)
        self.btn_more.setText(f"{more} more")
        self.btn_more.setVisible(more > 0)
        if self.isVisible():
            self.place()

    def _neighbour(self, gone_id: str, want: list[str]) -> 'MomentRow | None':
        """The row after `gone_id` that stays, else the one before it."""
        i = self.order.index(gone_id)
        after = [x for x in self.order[i + 1:] if x in want]
        before = [x for x in self.order[:i] if x in want]
        pick = after[0] if after else before[-1] if before else None
        return self.rows.get(pick) if pick else None

    def _dismiss(self, m: dict):
        """Not now: focus to the next row before this one hides (w-60)."""
        r = self.rows.get(m['id'])
        if r is not None:
            if _focus_in(r):
                nxt = self._neighbour(m['id'], [i for i in self.order if i != m['id']])
                if nxt is not None:
                    _focus(nxt.act)
                elif self.pill is not None:
                    self.closing.emit()         # the last row: back to the pill
            r.hide()
        self.dismissed.emit(m)

    def show_more(self):
        """"N more": the list grows in place, up to TOTAL_MAX rows."""
        first_new = self.limit
        self.limit = TOTAL_MAX
        self.sync(self.moments)
        if first_new < len(self.order):
            _focus(self.rows[self.order[first_new]].act)
        elif self.order:
            _focus(self.rows[self.order[-1]].act)

    # ---- open / close ----
    def open(self, anchor: QWidget, moments: list[dict], width: int, cover: bool,
             focus_row: int = 0):
        self.pill = anchor
        self.limit = LIST_ROWS
        self._width = width
        self.set_cover(cover)
        for i in list(self.order):              # fresh rows: Appearance → Size may have moved
            r = self.rows.pop(i)
            self.rows_lay.removeWidget(r)
            r.hide()
            r.deleteLater()
        self.order = []
        for b in (self.btn_more, self.btn_quiet):
            b.setFixedHeight(scaled(28))
        self.btn_close.setFixedSize(scaled(28), scaled(28))
        self.btn_close.setStyleSheet(f"border-radius: {pill(scaled(28))}px;")
        self.sync(moments)
        self.start = self.order[min(focus_row, len(self.order) - 1)] if self.order else None
        self.art.installEventFilter(self)
        QApplication.instance().installEventFilter(self)
        self.place()
        self.show()
        self.raise_()
        target = self.default_focus()
        if target is not None:
            focus_and_reveal(target)            # into the list first, then the pill hides

    def collapse(self) -> bool:
        if self.isHidden():
            return False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.art.removeEventFilter(self)
        self.hide()
        return True

    def default_focus(self) -> QWidget | None:
        r = self.rows.get(self.start) if self.start else None
        if r is None or r.isHidden():
            r = next((self.rows[i] for i in self.order if not self.rows[i].isHidden()), None)
        return r.act if r is not None else self.btn_close

    def set_cover(self, on: bool):
        """One column: the list covers the art's width on the solid scrim."""
        if self.property('cover') != on:
            self.setProperty('cover', on)
            self.style().unpolish(self)
            self.style().polish(self)

    def place(self):
        """Bottom-right on the pill's bottom-right; as tall as it needs, no
        higher than the art's top + SP 3; rows past that scroll, whole rows."""
        pill = self.pill
        if pill is None or not alive(pill):
            return
        self.setFixedWidth(min(self._width, self.art.width()))
        br = pill.mapTo(self.art, pill.rect().bottomRight())
        self.ensurePolished()
        for r in self.rows.values():
            r.ensurePolished()
        self.rows_lay.invalidate()
        rows = [self.rows[i] for i in self.order if not self.rows[i].isHidden()]
        rh = max((r.sizeHint().height() for r in rows), default=scaled(40))
        rows_h = rh * len(rows)
        chrome = SP['2'] * 2 + SP['1'] + self.btn_close.height() + 2
        avail = br.y() + 1 - SP['3']
        area = rows_h
        if rows_h + chrome > avail and rows:
            area = max(rh, (avail - chrome) // rh * rh)
        self.scroll.setFixedHeight(area)
        h = area + chrome
        self.setFixedHeight(h)
        self.move(br.x() + 1 - self.width(), br.y() + 1 - h)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj is self.art and t in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._place_timer.start()
        elif t == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget) \
                and obj.window() is self.window() and self.isVisible():
            gp = ev.globalPosition().toPoint()
            if not self.rect().contains(self.mapFromGlobal(gp)):
                self._outside_timer.start()     # after the click is delivered
        return False

    def _outside(self):
        if self.isVisible():
            self.closing.emit()


# ── the widget ───────────────────────────────────────────────────────────

class ForYouWidget(DashWidget):
    """For you now in the grid. Its card is the Strip, or in Customise the
    placeholder that says it's on the Stage; the pill and the list live on
    the Stage's art (attach)."""
    KIND, TITLE, GLYPH, GRAD = 'foryou', "For you now", G['sparkles'], 'dusk'
    HEAD = False

    def __init__(self):
        super().__init__()
        self.layout().setContentsMargins(SP['4'], SP['2_5'], SP['3'], SP['2_5'])
        self.setMinimumHeight(0)
        self.model: ForYouModel | None = None
        self.hub = None
        self.dash = None
        self.art = None
        self.pill: ForYouPill | None = None
        self.list: ForYouList | None = None
        self.line: QHBoxLayout | None = None     # one column: the pill's own line
        self.expanded = False                    # the Strip's "N more"
        self.kinds_open = False
        self._drawn = None
        self._labels: dict[str, tuple] = {}      # strip: moment id -> (title, why, button)
        self._chips: dict[str, QPushButton] = {}  # Kinds…: kind -> chip
        self._cols = 3
        self.btn_kinds = QPushButton("Kinds…")
        self.btn_kinds.setObjectName("Ghost")
        self.btn_kinds.setCheckable(True)
        self.btn_kinds.setToolTip(KINDS_TIP)
        set_glyph_icon(self.btn_kinds, G['chevron_down'], 14)
        self.btn_kinds.toggled.connect(self._toggle_kinds)
        bar = self.editbar.layout()
        bar.insertWidget(bar.indexOf(self.btn_style) + 1, self.btn_kinds)

    def set_density(self, d: dict):
        pass                                     # its own slim margins, any density

    def set_ink(self, on_art: bool):
        """The pill's and list's glyphs: on_art on the Stage's art, muted on
        the gradient and Plain backgrounds (their fill is the card surface)."""
        if self.pill is None:
            return
        role = 'on_art' if on_art else 'muted'
        self.pill.set_ink(role)
        if getattr(self.list.btn_quiet, '_glyph_spec', (None,) * 3)[2] != role:
            set_glyph_icon(self.list.btn_quiet, G['moon'], 14, role, hover_role=None)

    def bind(self, hub):
        self.hub = hub
        self.model = ForYouModel(hub, self)
        self.model.changed.connect(self._changed)

    def attach(self, dash, stage):
        """Put the pill at the end of the Stage's Play row and the list over
        its art. Both are kept for the window's life."""
        self.dash = dash
        art = self.art = stage.art
        self.pill = ForYouPill(art)
        self.pill.hide()
        art.actions.addWidget(self.pill)
        self.line = QHBoxLayout()
        self.line.setContentsMargins(0, 0, 0, 0)
        lay = art.layout()
        lay.insertLayout(lay.indexOf(art.actions) + 1, self.line)
        self.pill.opened.connect(self.toggle_list)
        self.pill.acted.connect(self._act)
        self.pill.dismissed.connect(self._not_now)
        self.pill.menu.connect(self._menu)
        self.list = ForYouList(art)
        self.list.acted.connect(self._act)
        self.list.dismissed.connect(self._not_now)
        self.list.menu.connect(self._menu)
        self.list.closing.connect(lambda: self.close_list(refocus=True))
        self.list.quiet.connect(self._quiet)
        # The pad order along the Play row: Play, Details, More, pill, pill
        # action, though the Continue row is nearer than the pill (SpatialNav).
        art.btn_more.nav_jump = self._jump_from_more
        self.pill.body.nav_jump = self._jump_from_pill
        self.pill.act.nav_jump = self._jump_from_pill
        self.pill.x.nav_jump = self._jump_from_pill

    # ---- state ----
    @property
    def moments(self) -> list[dict]:
        return self.model.moments if self.model is not None else []

    def mode(self) -> str:
        d = self.dash
        if d is None:
            return 'off'
        return foryou_mode(d.items, d.editing, d.stage.has_content(d.hub))

    def has_content(self, hub):
        mode = self.mode()
        return mode == 'placeholder' or (mode == 'strip' and bool(self.moments))

    def list_open(self) -> bool:
        return self.list is not None and not self.list.isHidden()

    def fill(self, hub):
        if self.model is not None and not self.model.refresh():
            self._draw()                         # nothing new: the drawing may still change

    def _changed(self):
        ms = self.moments
        if self.list_open():
            if self.mode() not in ('pill', 'placeholder') or len(ms) <= 1:
                self.close_list(refocus=_focus_in(self.list))
            else:
                self.list.sync(ms)
        self.sync_pill()
        self._draw()

    # ---- the pill ----
    def _in_row(self) -> bool:
        return self.pill is not None and self.art.actions.indexOf(self.pill) >= 0 \
            and not self.pill.isHidden()

    def _jump_from_more(self, key):
        if key == Qt.Key.Key_Right and self._in_row():
            return self.pill.first_focus()
        return None

    def _jump_from_pill(self, key):
        if key != Qt.Key.Key_Left or not self._in_row():
            return None
        f = QApplication.focusWidget()
        pill = self.pill
        if f is pill.x:
            return pill.act
        if f is pill.act and len(pill.moments) > 1:
            return pill.body
        return self.art.btn_more

    def sync_pill(self, cols: int | None = None):
        """Show the pill when For you now is on the Stage and has something;
        in one column it takes its own line under the Play row."""
        if self.pill is None:
            return
        if cols is not None:
            self._cols = cols
        self.set_ink(self.art.on_art())
        on = self.mode() in ('pill', 'placeholder') and bool(self.moments) \
            and not self.art.isHidden()
        pill, art = self.pill, self.art
        if not on:
            if self.list_open():
                self.close_list(refocus=_focus_in(self.list))
            if not pill.isHidden():
                if _focus_in(pill):
                    _focus(art.btn_more)         # before it hides (w-60)
                self._retain(False)
                pill.hide()
            return
        art.ensurePolished()
        art.btn.ensurePolished()
        pill.set_height(art.btn.sizeHint().height())
        pill.set_moments(self.moments)
        one_col = self._cols <= 1
        host = self.line if one_col else art.actions
        if host.indexOf(pill) < 0:
            had = _focus_in(pill)
            f = QApplication.focusWidget()
            (art.actions if one_col else self.line).removeWidget(pill)
            host.addWidget(pill)
            if had and alive(f):
                f.setFocus(Qt.FocusReason.OtherFocusReason)
        pill.setMaximumWidth(16777215 if one_col else list_width(self._cols, 0))
        pill.setSizePolicy(QSizePolicy.Policy.Expanding if one_col else
                           QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        pill.updateGeometry()
        if not self.list_open():
            pill.show()
        elif self.list is not None:
            self.list._width = self._list_width()
            self.list.set_cover(one_col)
            self.list._place_timer.start()

    def _retain(self, on: bool):
        sp = self.pill.sizePolicy()
        sp.setRetainSizeWhenHidden(on)
        self.pill.setSizePolicy(sp)

    def _list_width(self) -> int:
        m = self.art.layout().contentsMargins()
        return list_width(self._cols, self.art.width() - m.left() - m.right())

    def toggle_list(self):
        if self.list_open():
            self.close_list(refocus=True)
        else:
            self.open_list()

    def open_list(self, focus_row: int = 0):
        """Never on its own: a click, A or Enter on the pill."""
        if self.list is None or len(self.moments) <= 1 or self.pill.isHidden():
            return
        self._retain(True)
        self.list.open(self.pill, self.moments, self._list_width(), self._cols <= 1,
                       focus_row)
        self.pill.hide()                         # focus is in the list already

    def close_list(self, refocus: bool = False):
        """Esc, B, the chevron, a click outside, leaving Home or Customise.
        The pill comes back first, then takes focus, then the list hides."""
        if not self.list_open():
            return
        on = self.mode() in ('pill', 'placeholder') and bool(self.moments)
        had = _focus_in(self.list)
        if on:
            self.pill.set_moments(self.moments)
            self.pill.show()
        if refocus or had:
            _focus(self.pill.first_focus() if on else self.art.btn_more)
        self.list.collapse()
        self._retain(False)
        if not on:
            self.pill.hide()

    # ---- what the user does ----
    def _act(self, m: dict):
        if self.list_open():
            self.close_list(refocus=True)
        self.model.act(m)

    def _not_now(self, m: dict):
        self.model.not_now(m)

    def _quiet(self):
        self.model.quiet()

    def _menu(self, m: dict, pos: QPoint):
        self.hub._moment_menu(m, pos)

    def dont_show(self, kind: str):
        self.model.set_kind(kind, False)

    @staticmethod
    def kind_name(m: dict) -> str | None:
        """The Kinds… name a moment's "Don't show" switches off; none for a problem."""
        return KIND_NAMES.get(m.get('kind')) if m.get('kind') != 'broken' else None

    # ---- the card: Strip or placeholder ----
    def set_editing(self, on: bool, size: str, shelf: str | None = None):
        super().set_editing(on, size, shelf)
        self.btn_kinds.setVisible(on)
        if not on and self.kinds_open:
            self.btn_kinds.setChecked(False)
        self._draw()

    def _toggle_kinds(self, on: bool):
        self.kinds_open = on
        set_glyph_icon(self.btn_kinds, G['chevron_up' if on else 'chevron_down'], 14)
        self._draw()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._drawn is not None and self._drawn[0] == 'strip' \
                and self._drawn[3] != strip_count(self.width()):
            self._draw()

    def _draw(self):
        """Build the card for its mode; only when what it shows changed. A
        text-only change (a percent) updates the labels in place."""
        if self.hub is None:
            return
        mode = self.mode()
        ms = self.moments
        editing = self.dash.editing if self.dash is not None else False
        cols = self.dash._cols if self.dash is not None else 3
        sig = (mode, editing, self.kinds_open, strip_count(self.width()) if mode == 'strip'
               else 0, self.expanded, cols >= 3, moments_sig(ms) if mode == 'strip' else (),
               tuple(self.hub.prefs.kinds_off) if self.kinds_open else ())
        if sig == self._drawn:
            for m in ms:
                parts = self._labels.get(m['id'])
                if parts is not None:
                    t, w, b = parts
                    t.setText(m['title'])
                    w.setText(m['why'])
                    b.setText(amp(m['action']['label']))
            return
        self._drawn = sig
        f = QApplication.focusWidget()
        focus_id = next((i for i, parts in self._labels.items() if f in parts), None)
        focus_kind = next((k for k, b in self._chips.items() if b is f), None)
        self.clear()
        self._labels, self._chips = {}, {}
        if mode in ('placeholder', 'off'):
            line = QHBoxLayout()
            line.setSpacing(SP['2'])
            line.addWidget(icon_label(G['sparkles'], 14, P['muted']))
            line.addWidget(label("Shown on the Stage, beside Play", "FynNote"))
            line.addStretch()
            self.body.addLayout(line)
        elif mode == 'strip':
            if self.expanded and len(ms) > strip_count(self.width()):
                self._strip_expanded(ms, cols)
            else:
                self.expanded = False
                self._strip_line(ms)
        if self.kinds_open:
            self.body.addWidget(self._kinds_row())
        # What had focus comes back (a rebuilt button is a new widget).
        target = self._labels[focus_id][2] if focus_id in self._labels else \
            self._chips.get(focus_kind)
        if target is not None:
            QTimer.singleShot(0, lambda b=target: alive(b) and focus_and_reveal(b))

    def _strip_line(self, ms: list[dict]):
        inner = QFrame()
        inner.setObjectName("FynStrip")
        h = QHBoxLayout(inner)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP['4'])
        h.addWidget(kicker("For you now"))
        if not ms:
            h.addWidget(label("Nothing needs you right now.", "FynNote"), 1)
            self.body.addWidget(inner)
            return
        n = strip_count(self.width())
        for i, m in enumerate(ms[:n]):
            if i:
                sep = QFrame()
                sep.setObjectName("FynSep")
                sep.setFixedSize(1, 28)
                h.addWidget(sep)
            item = StripItem(m, x=len(ms) == 1)
            item.acted.connect(self._act)
            item.dismissed.connect(self._not_now)
            item.menu.connect(self._menu)
            if len(ms) == 1:
                item.setMaximumWidth(scaled(480))
            self._labels[m['id']] = (item.title, item.why, item.act)
            h.addWidget(item, 1)
        if len(ms) == 1:
            h.addStretch(1)
        if len(ms) > n:
            more = QPushButton(f"{len(ms) - n} more")
            more.setObjectName("Ghost")
            more.setCursor(Qt.CursorShape.PointingHandCursor)
            set_glyph_icon(more, G['chevron_down'], 14)
            more.clicked.connect(lambda: self._expand(True))
            h.addWidget(more)
        self.body.addWidget(inner)

    def _strip_expanded(self, ms: list[dict], cols: int):
        inner = QFrame()
        inner.setObjectName("FynStrip")
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.addWidget(kicker("For you now"))
        head.addStretch()
        q = QPushButton("Quiet until tomorrow")
        q.setObjectName("Ghost")
        q.setToolTip("Hide For you now until 04:00. Problems still show.")
        set_glyph_icon(q, G['moon'], 14)
        q.clicked.connect(self._quiet)
        head.addWidget(q)
        less = QPushButton("Show less")
        less.setObjectName("Ghost")
        set_glyph_icon(less, G['chevron_up'], 14)
        less.clicked.connect(lambda: self._expand(False))
        head.addWidget(less)
        v.addLayout(head)
        grid = QGridLayout()
        grid.setHorizontalSpacing(SP['6'])
        grid.setVerticalSpacing(0)
        per = 2 if cols >= 3 else 1
        for i, m in enumerate(ms):
            r = MomentRow(m, on_art=False)
            r.acted.connect(self._act)
            r.dismissed.connect(self._not_now)
            r.menu.connect(self._menu)
            self._labels[m['id']] = (r.title, r.why, r.act)
            grid.addWidget(r, i // per, i % per)
        v.addLayout(grid)
        self.body.addWidget(inner)

    def _expand(self, on: bool):
        self.expanded = on
        self._draw()
        if on:
            first = next(iter(self._labels.values()), None)
            if first is not None:
                QTimer.singleShot(0, lambda b=first[2]: alive(b) and focus_and_reveal(b))
        else:
            QTimer.singleShot(0, lambda: alive(self) and self._focus_more())

    def _focus_more(self):
        more = next((b for b in self.findChildren(QPushButton) if b.text().endswith(" more")
                     and not b.isHidden()), None)
        if more is not None:
            focus_and_reveal(more)

    def _kinds_row(self) -> QWidget:
        host = QWidget()
        flow = FlowLayout(host, margin=0, spacing=SP['1_5'])
        off = set(self.hub.prefs.kinds_off)
        for key, text in KIND_CHIPS:
            b = QPushButton(text)
            b.setObjectName("Chip")
            b.setCheckable(True)
            b.setChecked(key not in off)
            b.setToolTip(KINDS_TIP)
            b.toggled.connect(lambda on, k=key: self.model.set_kind(k, on))
            flow.addWidget(b)
            self._chips[key] = b
        return host


class StripItem(QWidget):
    """One moment in the Strip's line: badge, two lines, a plain button;
    ✕ when it is the only one. Not now is in its context menu."""
    acted = pyqtSignal(dict)
    dismissed = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)

    def __init__(self, m: dict, x: bool = False):
        super().__init__()
        self.m = m
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        lay.addWidget(badge(m, 24))
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.title = ElideLabel(m['title'], "FynTitle")
        self.why = ElideLabel(m['why'], "FynWhy")
        col.addWidget(self.title)
        col.addWidget(self.why)
        lay.addLayout(col, 1)
        self.setToolTip(f"{m['title']}\n{m['why']}")
        self.act = QPushButton(amp(m['action']['label']))
        self.act.setFixedHeight(scaled(28))
        self.act.setCursor(Qt.CursorShape.PointingHandCursor)
        self.act.clicked.connect(lambda: self.acted.emit(self.m))
        lay.addWidget(self.act)
        if x:
            b = icon_button(G['close'], "Not now", scaled(28), "Ghost", round_=True)
            b.clicked.connect(lambda: self.dismissed.emit(self.m))
            lay.addWidget(b)

    def contextMenuEvent(self, ev):
        self.menu.emit(self.m, ev.globalPos())


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .details import ElideLabel, kicker  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .widgets import (amp, FlowLayout, icon_button, icon_label, label,  # noqa: E402
    set_glyph_icon)
