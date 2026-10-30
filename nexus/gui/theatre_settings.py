"""Theatre's own settings and the Your week card (w-63).

`TheatreSettings` is a couch-sized sheet on the right, opened from the top
bar's gear and the quick menu's Settings row: nine rows in four groups, each
a `CouchRow` the D-pad walks (Up/Down move, Left/Right step and stop at the
ends, A steps and wraps). Every change goes out through `changed` at once;
the window saves it (prefs.controls, or the appearance) and keeps desktop
Settings in step. `WeekCard` is the play time of this week on Theatre's
Home; Theatre fetches nothing itself, the window hands it `--history`."""

from datetime import date

from PyQt6.QtCore import pyqtSignal, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .actions import ActionSheet
from .common import ATTRACT_MINUTES, THEATRE_STARTS, VOLUME_STEPS
from .theme import G, LOOK, P, SP, THEMES

SWITCH = ((False, "Off"), (True, "On"))

# The sheet's rows, top to bottom: (group, key, title, choices, help). The
# choices are (value, text); SWITCH rows show a switch, 'volume' ten pills.
# 'theme' and 'art_tint' are the appearance; the rest are prefs.controls.
ROWS = (
    ("Controller", 'pad_a', "A on a game",
     (('details', "Opens its page"), ('play', "Plays it"))),
    ("Controller", 'theatre_on_pad', "Switch to Theatre when a controller connects", SWITCH),
    ("Theatre", 'theatre_start', "Start Nexus in Theatre",
     tuple(zip(THEATRE_STARTS, ("Never", "On a TV", "Always")))),
    ("Theatre", 'attract', "Screensaver",
     tuple((m, f"After {m} minutes" if m else "Never") for m in ATTRACT_MINUTES)),
    ("Theatre", 'week_home', "Your week on Home", SWITCH),
    ("Sound", 'sounds', "Menu sounds", SWITCH),
    ("Sound", 'volume', "Volume", tuple((v, f"{v}%") for v in VOLUME_STEPS)),
    ("Look", 'theme', "Theme", tuple((k, t[0]) for k, t in THEMES.items())),
    ("Look", 'art_tint', "Colour from the game's art", SWITCH),
)
HELP = {
    'pad_a': "A busy, running or not installed game always opens its page.",
    'theatre_on_pad': "Only when you plug a controller in or turn it on.",
    'theatre_start': "On a TV: when the screen Nexus opens on is 32\" or bigger.",
    'attract': "Shows off your games when Theatre sits untouched.",
    'week_home': "Your play time this week, under Jump back in.",
    'sounds': "Soft clicks as you move and select, in Theatre only.",
    'volume': "How loud the menu sounds are.",
    'theme': "Changing it restyles Nexus, which takes a moment.",
    'art_tint': "The background takes its colour from the game's art.",
}
APPEARANCE_KEYS = ('theme', 'art_tint')


class VolumeMeter(QWidget):
    """Volume as ten short pills (no slider: a pad can't drag)."""

    def __init__(self, scale: float = 1.0):
        super().__init__()
        self.value = 50
        self.setFixedSize(int(140 * scale), int(18 * scale))

    def set_value(self, v: int):
        self.value = v
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(Qt.PenStyle.NoPen)
        n, w = self.value // 10, self.width() / 10
        for i in range(10):
            c = QColor(P['accent'] if i < n else P['muted'])
            if i >= n:
                c.setAlphaF(0.30)
            if not self.isEnabled():
                c.setAlphaF(c.alphaF() * 0.4)
            pt.setBrush(c)
            pt.drawRoundedRect(QRectF(i * w + 2, 2, w - 4, self.height() - 4), 3.0, 3.0)
        pt.end()


class CouchRow(QPushButton):
    """One couch setting: its title left, its value right (a word between
    ‹ › while focused, a switch, or the volume pills). A or a click steps
    forward and wraps round; Left/Right step either way and stop at the
    ends (the row keeps those arrows: input._native_arrows)."""
    NATIVE_ARROWS = True
    stepped = pyqtSignal(str, object)        # key, the new value
    focused = pyqtSignal(object)             # this row got focus

    def __init__(self, key: str, title: str, choices: tuple, scale: float = 1.0):
        super().__init__()
        self.key, self.choices = key, choices
        self.value = choices[0][0]
        self.setObjectName("CouchRow")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(int(44 * scale))
        h = QHBoxLayout(self)
        h.setContentsMargins(int(SP['3'] * scale), 0, int(SP['3'] * scale), 0)
        h.setSpacing(int(SP['2'] * scale))
        h.addWidget(label(title, "CouchTitle"), 1)
        self.switch = self.meter = self.lt = self.gt = None
        self.text = label("", "CouchValue")
        if choices is SWITCH:
            from .settings import Switch
            self.switch = Switch(title)
            self.switch.setFixedSize(int(Switch.W * scale), int(Switch.H * scale))
            self.switch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.switch.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            h.addWidget(self.switch)
            self.text.hide()
        elif key == 'volume':
            self.meter = VolumeMeter(scale)
            h.addWidget(self.meter)
            self.text.setMinimumWidth(int(48 * scale))
            self.text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            h.addWidget(self.text)
        else:
            self.lt, self.gt = label("‹", "CouchValue"), label("›", "CouchValue")
            for w in (self.lt, self.text, self.gt):
                h.addWidget(w)
            self.lt.hide()
            self.gt.hide()
        self.clicked.connect(self._forward)

    def set_value(self, v):
        """Show `v` (unknown values show the first choice); no signal."""
        values = [c[0] for c in self.choices]
        self.value = v if v in values else values[0]
        self.text.setText(dict(self.choices)[self.value])
        if self.switch is not None:
            self.switch.setChecked(bool(self.value))
        if self.meter is not None:
            self.meter.set_value(self.value)

    def step(self, d: int, wrap: bool = False) -> bool:
        """One choice on (d=1) or back (-1); at an end it stays unless
        `wrap`. True if the value changed (and was sent)."""
        values = [c[0] for c in self.choices]
        i = values.index(self.value) + d
        i = i % len(values) if wrap else max(0, min(len(values) - 1, i))
        if values[i] == self.value:
            return False
        self.set_value(values[i])
        self.stepped.emit(self.key, self.value)
        return True

    def _forward(self):
        self.step(1, wrap=True)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.step(-1 if ev.key() == Qt.Key.Key_Left else 1)
        elif ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._forward()
        else:
            super().keyPressEvent(ev)

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self._chevrons(True)
        self.focused.emit(self)

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self._chevrons(False)

    def _chevrons(self, on: bool):
        for w in (self.lt, self.gt):
            if w is not None:
                w.setVisible(on)


class TheatreSettings(ActionSheet):
    """Theatre's settings sheet: the ActionSheet's panel (scrim, title,
    close_sheet and its focus return, X and a click outside close it),
    wider, with CouchRows built once and synced from the prefs on every
    open. A help line under the rows speaks for the focused one; the foot
    leaves Theatre for the desktop's Settings."""
    changed = pyqtSignal(str, object)        # key, value: the window saves it
    all_settings = pyqtSignal()

    def __init__(self, parent: QWidget, scale: float = 1.0):
        super().__init__(parent, scale)
        self.setObjectName("SettingsScrim")
        sc = lambda v: int(v * scale)            # noqa: E731
        self.panel.setFixedWidth(sc(620))
        self.title.setText("Settings")
        self.sub.setText("Changes are saved as you make them.")
        self.rows_by_key: dict[str, CouchRow] = {}
        self._last: str | None = None            # the row used last (this session)
        group = None
        for grp, key, title, choices in ROWS:
            if grp != group:
                if group is not None:
                    self.rows.addSpacing(sc(SP['2']))
                self.rows.addWidget(label(grp.upper(), "SheetSection"))
                group = grp
            row = CouchRow(key, title, choices, scale)
            row.stepped.connect(self._stepped)
            row.focused.connect(self._row_focused)
            self.rows.addWidget(row)
            self.rows_by_key[key] = row
        self.rows.addStretch()
        self.help = label("", "CouchHelp", wrap=True)
        lay = self.panel.layout()
        lay.insertWidget(lay.indexOf(self.foot), self.help)
        self.btn_all = QPushButton("All settings  ·  leaves Theatre")
        self.btn_all.setObjectName("SheetRow")
        set_glyph_icon(self.btn_all, G['settings'], sc(18), 'subtext', hover_role=None)
        self.btn_all.setMinimumHeight(sc(44))
        self.btn_all.clicked.connect(self._all)
        self.foot_rows.addWidget(self.btn_all)
        self.foot.show()

    def open(self, controls: dict, appearance: dict):
        """Show the sheet with these values; focus the row used last."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self.sync(controls, appearance)
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        t = self.default_focus()
        if t is not None:
            focus_and_reveal(t)

    def sync(self, controls: dict, appearance: dict):
        """The rows' values from the prefs (no `changed`)."""
        for key, row in self.rows_by_key.items():
            row.set_value((appearance if key in APPEARANCE_KEYS else controls).get(key))
        self._sync_volume()

    def _sync_volume(self):
        self.rows_by_key['volume'].setEnabled(bool(self.rows_by_key['sounds'].value))

    def _stepped(self, key: str, value):
        if key == 'sounds':
            self._sync_volume()                  # greyed while sounds are off
        self.changed.emit(key, value)

    def _row_focused(self, row: CouchRow):
        self._last = row.key
        self.help.setText(HELP.get(row.key, ''))

    def _all(self):
        self.close_sheet(restore=False)
        self.all_settings.emit()

    def default_focus(self):
        row = self.rows_by_key.get(self._last) if self._last else None
        if row is None or not row.isEnabled():
            row = next(iter(self.rows_by_key.values()))
        return row

    def paintEvent(self, ev):
        pt = QPainter(self)                      # a lighter dim over a light theme
        pt.fillRect(self.rect(), QColor(0, 0, 0, 89 if LOOK['light'] else 115))
        pt.end()


class WeekCard(QFrame):
    """Your week on Theatre's Home: two wide tiles wide, one tall. Left: the
    week's total, days played and the streak, the most played game as a
    line; right: Monday to Sunday, today bright. A opens the most played
    game (`open`)."""
    open = pyqtSignal()

    def __init__(self, w: int, h: int, scale: float = 1.0):
        super().__init__()
        self.setObjectName("WeekCard")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(w, h)
        self.game_name: str | None = None        # the most played, when it's in the library
        sc = lambda v: int(v * scale)            # noqa: E731
        lay = QHBoxLayout(self)
        lay.setContentsMargins(sc(SP['5']), sc(SP['4']), sc(SP['5']), sc(SP['3']))
        lay.setSpacing(sc(SP['6']))
        col = QVBoxLayout()
        col.setSpacing(sc(SP['1']))
        col.addWidget(label("THIS WEEK", "WeekKicker"))
        self.total = label("", "WeekBig")
        col.addWidget(self.total)
        self.days = label("", "WeekLine")
        col.addWidget(self.days)
        col.addStretch()
        col.addWidget(label("MOST PLAYED", "WeekKicker"))
        self.top = label("", "WeekGame")
        col.addWidget(self.top)
        lay.addLayout(col)
        self.bars = MinuteBars(h - sc(SP['4']) - sc(SP['3']), labels_every=1, scale=scale)
        lay.addWidget(self.bars, 1)

    def set_week(self, week: dict, streak: int, today: str | None = None):
        """`week`: history.periods()['week']; `streak`: its current_streak."""
        days = week.get('days') or []
        self.total.setText(fmt_hours(week.get('minutes') or 0) or "0 h")
        played = sum(1 for d in days if d.get('minutes'))
        facts = [f"{played} day{'s' if played != 1 else ''}"]
        if streak >= 2:
            facts.append(f"{streak}-day streak")
        self.days.setText("  ·  ".join(facts))
        top = (week.get('games') or [{}])[0]
        self.top.setText(f"{top.get('name') or top.get('slug') or ''}  ·  "
                         f"{fmt_hours(top.get('minutes') or 0)}" if top else "")
        bars, hi = [], set()
        for i, d in enumerate(days):
            try:
                day = date.fromisoformat(d['date'])
            except (KeyError, TypeError, ValueError):
                day = None
            if day is not None and today and day.isoformat() == today:
                hi.add(i)
            bars.append({'minutes': d.get('minutes') or 0,
                         'label': day.strftime('%a')[:2] if day else ''})
        self.bars.set_bars(bars, hi)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.open.emit()
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.open.emit()
        super().mouseReleaseEvent(ev)


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .fmt import fmt_hours  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .system import MinuteBars  # noqa: E402
from .widgets import label, set_glyph_icon  # noqa: E402
