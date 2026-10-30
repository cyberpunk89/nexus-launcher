"""Controller and keyboard navigation, and the focus ring."""

import os
import re
import time

from PyQt6.QtCore import (pyqtSignal, QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect,
    QRectF, QSizeF, Qt, QTimer, QVariantAnimation)
from PyQt6.QtGui import QBrush, QColor, QKeyEvent, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractScrollArea,
    QApplication, QComboBox, QFrame, QHBoxLayout, QLineEdit, QPlainTextEdit, QScrollArea,
    QScrollBar, QSlider, QTabBar, QTextEdit, QWidget)

from .common import alive
from .theme import GLYPHS, GRAD, pill, R, SP, TILE_LIFT


def _native_arrows(w: QWidget, key) -> bool:
    """Arrow keys this widget should keep for itself instead of moving focus."""
    horiz = key in (Qt.Key.Key_Left, Qt.Key.Key_Right)
    if isinstance(w, QLineEdit):
        return horiz and bool(w.text())
    if isinstance(w, (QSlider, QTabBar)):
        return horiz
    if isinstance(w, QAbstractItemView):
        if horiz:
            return False
        row = w.currentIndex().row()
        last = (w.model().rowCount() - 1) if w.model() else 0
        return not ((key == Qt.Key.Key_Up and row <= 0) or (key == Qt.Key.Key_Down and row >= last))
    if isinstance(w, (QPlainTextEdit, QTextEdit)):
        return not horiz
    return False


NAV_KEYS = {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
            Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Return, Qt.Key.Key_Enter,
            Qt.Key.Key_Space, Qt.Key.Key_Escape, Qt.Key.Key_Menu, Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End}


class InputMode(QObject):
    """What Nexus is being driven with right now — 'mouse', 'keyboard' or
    'pad' — so each gets its own interactions: hover buttons and right-click
    menus for the mouse; the focus ring for keys; the ring, button hints and
    controller sheets (never a desktop menu) for a pad. ControllerInput
    sets 'pad'; real key presses and mouse use set the others."""
    changed = pyqtSignal(str)
    activity = pyqtSignal()             # any key, click, wheel or real pointer move
    MOVE_SLOP = 8                       # px of real pointer travel that counts

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = 'mouse'
        self._pos = None

    def listen(self):
        """Start watching input (an app-wide filter). The window calls it once
        it's built: no input arrives before, and building it is ~100k events
        a filter would see for nothing."""
        QApplication.instance().installEventFilter(self)

    def set(self, mode: str):
        if mode != self.mode:
            self.mode = mode
            self._pos = None
            self.changed.emit(mode)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel) or (
                t == QEvent.Type.MouseMove and ev.spontaneous()):
            self.activity.emit()
        if t == QEvent.Type.KeyPress:
            # Keys we post for the pad aren't spontaneous: they keep 'pad'.
            if ev.key() in NAV_KEYS and (ev.spontaneous() or self.mode != 'pad'):
                self.set('keyboard')
        elif t in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
            self.set('mouse')
            # The mouse's own Back / Forward buttons step through pages
            # (one app-wide filter already sees every press).
            if t == QEvent.Type.MouseButtonPress and ev.button() in (
                    Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton):
                win = self.parent()
                if hasattr(win, 'go_back') and not getattr(win, '_theatre', False) \
                        and QApplication.activeModalWidget() is None:
                    if ev.button() == Qt.MouseButton.BackButton:
                        win.go_back()
                    else:
                        win.go_forward()
                    return True
        elif t == QEvent.Type.MouseMove and self.mode != 'mouse' and ev.spontaneous():
            # Qt re-sends the pointer position when things move under it;
            # only real travel means the mouse is back in use.
            p = ev.globalPosition()
            if self._pos is None:
                self._pos = p
            elif (p - self._pos).manhattanLength() > self.MOVE_SLOP:
                self.set('mouse')
        return False


INPUT: 'InputMode | None' = None        # set by MainWindow (install_input_mode)


def install_input_mode(window) -> 'InputMode':
    """The window's InputMode, made the app-wide one input_mode() reads."""
    global INPUT
    INPUT = InputMode(window)
    return INPUT


def input_mode() -> str:
    return INPUT.mode if INPUT is not None else 'mouse'


class SpatialNav(QObject):
    """Application event filter: arrow keys move focus geometrically."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.win = window

    def _focusable(self):
        out = []
        scope = self.win.nav_scope() if hasattr(self.win, 'nav_scope') else self.win
        for w in scope.findChildren(QWidget):
            if not w.isVisible() or not w.isEnabled() or w.window() is not self.win:
                continue
            if not (w.focusPolicy() & Qt.FocusPolicy.TabFocus):
                continue
            if isinstance(w, QAbstractScrollArea) and not isinstance(w, QAbstractItemView):
                continue
            if isinstance(w, QScrollBar) or w.size().isEmpty():
                continue
            out.append(w)
        return out

    def _rect(self, w):
        tl = w.mapTo(self.win, QPoint(0, 0))
        return QRectF(tl.x(), tl.y(), w.width(), w.height())

    def move(self, cur: QWidget, key) -> QWidget | None:
        r0 = self._rect(cur)
        c0 = r0.center()
        best, best_score = None, None
        for w in self._focusable():
            if w is cur or cur.isAncestorOf(w) or w.isAncestorOf(cur):
                continue
            r = self._rect(w)
            c = r.center()
            dx, dy = c.x() - c0.x(), c.y() - c0.y()
            if key == Qt.Key.Key_Right:
                primary, overlap = r.left() - r0.right() + r.width() / 2, \
                    min(r.bottom(), r0.bottom()) - max(r.top(), r0.top())
                ok = dx > 8
                off = abs(dy)
            elif key == Qt.Key.Key_Left:
                primary, overlap = r0.left() - r.right() + r.width() / 2, \
                    min(r.bottom(), r0.bottom()) - max(r.top(), r0.top())
                ok = dx < -8
                off = abs(dy)
            elif key == Qt.Key.Key_Down:
                primary, overlap = r.top() - r0.bottom() + r.height() / 2, \
                    min(r.right(), r0.right()) - max(r.left(), r0.left())
                ok = dy > 8
                off = abs(dx)
            else:
                primary, overlap = r0.top() - r.bottom() + r.height() / 2, \
                    min(r.right(), r0.right()) - max(r.left(), r0.left())
                ok = dy < -8
                off = abs(dx)
            if not ok:
                continue
            # Things in line with the current control win; then the nearest.
            score = max(primary, 0) + (0 if overlap > 0 else off * 2.5)
            if best_score is None or score < best_score:
                best, best_score = w, score
        return best

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.KeyPress:
            return False
        key = ev.key()
        if key not in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            return False
        if ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier or QApplication.activePopupWidget():
            return False
        # Another window (a message box, Qt's own file dialog) keeps its
        # arrow keys; only a focusless main window gets a starting point.
        if QApplication.activeModalWidget() is not None:
            return False
        cur = QApplication.focusWidget()
        if cur is not None and cur.window() is not self.win:
            return False
        if cur is None:
            if not self.win.isActiveWindow():
                return False
            target = self.win.default_focus()
            if target:
                focus_and_reveal(target)
            return target is not None
        if _native_arrows(cur, key):
            return False
        nxt = self.move(cur, key)
        if nxt is not None:
            focus_and_reveal(nxt)
            return True
        # At an edge: stay put — on a tile, and anywhere inside an overlay
        # (Qt's own arrow handling would tab out of it, behind the overlay).
        return isinstance(cur, GameTile) or self.win.nav_scope() is not self.win


def focus_and_reveal(w: QWidget | None):
    if not alive(w):
        return
    w.setFocus(Qt.FocusReason.TabFocusReason)
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QScrollArea) and p.widget() is not None and p.widget().isAncestorOf(w):
            p.ensureWidgetVisible(w, 24, 24)
        p = p.parentWidget()


class ControllerInput(QObject):
    """Polls SDL game controllers (only while the window is focused) and turns
    them into navigation: D-pad / left stick = arrows (with repeat), A = select,
    B = back, X = menu, Y = search, LB/RB = previous/next page, triggers = page
    scroll, right stick = scroll, View/Share = switch library view, Menu/Options
    (Start) and Guide (Xbox / PS button) = the window decides: Theatre's quick
    menu, or into Theatre from the desktop."""
    connected = pyqtSignal(str, str)       # name, kind
    disconnected = pyqtSignal(str)
    used = pyqtSignal()
    action = pyqtSignal(str)               # page-prev/next, view, start, quick, search

    REPEAT_DELAY, REPEAT_RATE = 0.38, 0.085
    DIRS = {'up': Qt.Key.Key_Up, 'down': Qt.Key.Key_Down,
            'left': Qt.Key.Key_Left, 'right': Qt.Key.Key_Right}

    IDLE_POLL = 500                     # ms between checks while no pad is connected

    def __init__(self, parent):
        super().__init__(parent)
        from nexus.gamepad import Gamepads
        # Inert until open(): SDL takes ~0.1 s to start, so that happens
        # once the window is up (and never with NEXUS_NO_GAMEPAD).
        self.pads = Gamepads(load=False)
        self.held: dict[str, float] = {}   # direction -> next repeat time
        self.stick = {'lx': 0.0, 'ly': 0.0, 'rx': 0.0, 'ry': 0.0, 'lt': 0.0, 'rt': 0.0}
        self.trig_down = {'lt': False, 'rt': False}
        self.synth: dict[int, float] = {}  # key -> time we posted it (Steam desktop dedupe)
        self.timer = QTimer(self)
        self.timer.setInterval(self.IDLE_POLL)
        self.timer.timeout.connect(self._tick)
        QApplication.instance().aboutToQuit.connect(self._shutdown)

    def open(self):
        if os.environ.get('NEXUS_NO_GAMEPAD') or self.pads.available:
            return
        from nexus.gamepad import Gamepads
        self.pads = Gamepads()
        if self.pads.available:
            QApplication.instance().applicationStateChanged.connect(self._app_state)
            QApplication.instance().installEventFilter(self)
            if QApplication.applicationState() != Qt.ApplicationState.ApplicationActive:
                self.pads.suspend()            # a game has the focus already
            else:
                self.timer.start()

    @property
    def kind(self):
        return self.pads.kind

    def _shutdown(self):
        self.timer.stop()
        self.pads.shutdown()

    def _app_state(self, state):
        if state == Qt.ApplicationState.ApplicationActive:
            self.pads.resume()
            self.timer.start()
        else:
            # A game (or anything else) has focus: hand the controller back.
            self.timer.stop()
            self.held.clear()
            self.pads.suspend()

    # Steam's "Desktop" controller layout also types arrow/enter/escape keys;
    # when the same key arrives from both within a moment, keep one. Return,
    # Enter and Space are one "accept" (A posts Space on buttons, Steam
    # sends Return).
    ACCEPT = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)

    @classmethod
    def _same(cls, key):
        return 'accept' if key in cls.ACCEPT else key

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.KeyPress and ev.spontaneous():
            k = self._same(ev.key())
            t = self.synth.get(k)
            if t and time.monotonic() - t < 0.12:
                return True
            self.synth.pop(k, None)
            self._last_real = (k, time.monotonic())
        return False

    def _post(self, key, text=''):
        last = getattr(self, '_last_real', None)
        if last and last[0] == self._same(key) and time.monotonic() - last[1] < 0.12:
            return
        target = QApplication.activePopupWidget() or QApplication.focusWidget() \
            or self.parent()
        self.synth[self._same(key)] = time.monotonic()
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.postEvent(target, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier,
                                                     text))

    def _press_dir(self, d):
        self._post(self.DIRS[d])
        self.held[d] = time.monotonic() + self.REPEAT_DELAY

    def _tick(self):
        now = time.monotonic()
        for ev in self.pads.poll():
            kind = ev[0]
            if kind == 'added':
                self.connected.emit(ev[1], ev[2])
                continue
            if kind == 'removed':
                self.disconnected.emit(ev[1])
                continue
            if not self.parent().isActiveWindow():
                continue
            if kind == 'button':
                _k, name, down = ev
                if name in self.DIRS:
                    if down:
                        self.used.emit()
                        self._press_dir(name)
                    else:
                        self.held.pop(name, None)
                elif down:
                    self.used.emit()
                    self._button(name)
            elif kind == 'axis':
                self.stick[ev[1]] = ev[2]
                if ev[1] in ('lx', 'ly'):
                    self._stick_dirs()
                elif ev[1] in ('lt', 'rt'):
                    on = ev[2] > 0.6
                    if on and not self.trig_down[ev[1]]:
                        self.used.emit()
                        self._scroll(-0.8 if ev[1] == 'lt' else 0.8, pages=True)
                    self.trig_down[ev[1]] = on
        # 60 Hz while a controller is there; twice a second to notice one.
        want = 16 if self.pads.pads else self.IDLE_POLL
        if self.timer.interval() != want:
            self.timer.setInterval(want)
        for d, t in list(self.held.items()):
            if now >= t:
                self._post(self.DIRS[d.removeprefix('stick-')])
                self.held[d] = now + self.REPEAT_RATE
        ry = self.stick['ry']
        if abs(ry) > 0.25:
            self._scroll(ry * 0.05)

    def _stick_dirs(self):
        x, y = self.stick['lx'], self.stick['ly']
        want = None
        if max(abs(x), abs(y)) > 0.55:
            want = ('right' if x > 0 else 'left') if abs(x) > abs(y) else \
                ('down' if y > 0 else 'up')
        for d in ('up', 'down', 'left', 'right'):
            key = f'stick-{d}'
            if d == want and key not in self.held:
                self.used.emit()
                self._post(self.DIRS[d])
                self.held[key] = time.monotonic() + self.REPEAT_DELAY
            elif d != want:
                self.held.pop(key, None)

    def _button(self, name):
        focus = QApplication.focusWidget()
        if name == 'a':
            if isinstance(focus, QLineEdit) and not focus.isReadOnly():
                self.action.emit('keyboard')       # Steam's on-screen keyboard, if any
            elif isinstance(focus, (QAbstractButton, QComboBox)):
                self._post(Qt.Key.Key_Space, ' ')
            else:
                self._post(Qt.Key.Key_Return, '\r')
        elif name == 'b':
            self._post(Qt.Key.Key_Escape)
        elif name == 'x':
            self._post(Qt.Key.Key_Menu)
        elif name == 'y':
            self.action.emit('search')
        elif name == 'lb':
            self.action.emit('page-prev')
        elif name == 'rb':
            self.action.emit('page-next')
        elif name == 'back':
            self.action.emit('view')
        elif name == 'start':
            self.action.emit('start')
        elif name == 'guide':
            self.action.emit('quick')

    def _scroll(self, amount, pages=False):
        w = QApplication.focusWidget()
        area = None
        while w is not None:
            if isinstance(w, QAbstractScrollArea) and w.verticalScrollBar().maximum() > 0:
                area = w
                break
            w = w.parentWidget()
        if area is None:
            return
        bar = area.verticalScrollBar()
        step = bar.pageStep() if pages else 40
        bar.setValue(int(bar.value() + amount * step * (1 if pages else 10)))
        self._refocus(area)

    @staticmethod
    def _refocus(area):
        """Scrolling moved the view, not focus: once the focused control has
        left the view, focus the control nearest the middle of what's shown,
        so the next D-pad press starts from what you're looking at."""
        vp = area.viewport()
        view = QRect(vp.mapToGlobal(QPoint(0, 0)), vp.size())
        f = QApplication.focusWidget()
        if f is not None and view.contains(QRect(f.mapToGlobal(QPoint(0, 0)), f.size()).center()):
            return
        mid = view.center()
        best, dist = None, None
        for w in vp.findChildren(QWidget):
            if not (w.focusPolicy() & Qt.FocusPolicy.TabFocus) or not w.isVisible() \
                    or not w.isEnabled() or isinstance(w, (QScrollBar, QAbstractScrollArea)):
                continue
            c = QRect(w.mapToGlobal(QPoint(0, 0)), w.size()).center()
            if not view.contains(c):
                continue
            d = abs(c.y() - mid.y()) * 2 + abs(c.x() - mid.x())
            if dist is None or d < dist:
                best, dist = w, d
        if best is not None:
            best.setFocus(Qt.FocusReason.TabFocusReason)


# Hint strip helpers ---------------------------------------------------------

class HintBar(QFrame):
    """Controller button hints along the bottom, shown while a controller is
    connected: what A/B/X/Y do right now."""

    def __init__(self):
        super().__init__()
        self.setObjectName("HintBar")
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(SP['5'], SP['1_5'], SP['5'], SP['1_5'])
        self.lay.setSpacing(SP['4'])
        self.kind = 'xbox'
        self.battery = ''                       # "Controller 64%", when a pad reports one
        self.setVisible(False)

    def set_battery(self, text: str):
        if text != self.battery:
            self.battery = text
            hints = getattr(self, '_shown', (None, ()))[1]
            self._shown = None
            self.show_hints(list(hints))

    def show_hints(self, hints: list[tuple[str, str]]):
        key = (self.kind, tuple(hints), self.battery)
        if key == getattr(self, '_shown', None):
            return                              # every focus move asks; most change nothing
        self._shown = key
        clear_layout(self.lay)
        g = GLYPHS.get(self.kind, GLYPHS['xbox'])
        for btn, text in hints:
            glyph = g.get(btn, btn.upper() if len(btn) <= 2 else btn)
            pill = label(glyph, "HintGlyph")
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pill.setMinimumWidth(22)
            row = QHBoxLayout()
            row.setSpacing(SP['1_5'])
            row.addWidget(pill)
            row.addWidget(label(text, "Muted"))
            self.lay.addLayout(row)
        self.lay.addStretch()
        if self.battery:
            self.lay.addWidget(label(self.battery, "Muted"))


# --------------------------------------------------------------------------
# Focus ring — one console-style indicator for keyboard / controller focus
# --------------------------------------------------------------------------

class FocusRing(QWidget):
    """Draws focus *around* the focused control — a soft gradient ring that
    follows its shape and glides from control to control — instead of a box
    inside every button. Shown only while navigating with keys or a
    controller; a mouse click hides it (the pointer is its own indicator).
    Text fields keep their own highlight, and lists show their selected row."""
    PAD = 4
    SKIP = (QLineEdit, QPlainTextEdit, QAbstractItemView, QAbstractScrollArea, QScrollBar)

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.keyboard = False
        self.scale = 1.0
        self.target: QWidget | None = None
        self._rect = QRectF()
        self._radius = float(R['control'])
        self._clip = QRectF()
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._moved)
        self._follow = QTimer(self)
        self._follow.setInterval(33)          # tracks scrolling / hover lift
        self._follow.timeout.connect(self._track)
        QApplication.instance().focusChanged.connect(self._focus_changed)
        # A game in front: nothing moves under the ring, so no 30 Hz ticks
        # for the whole session.
        QApplication.instance().applicationStateChanged.connect(self._app_state)
        if INPUT is not None:
            INPUT.changed.connect(self._mode)
        self.hide()

    def _app_state(self, state):
        if state != Qt.ApplicationState.ApplicationActive:
            self._follow.stop()
        elif self.isVisible() and self.target is not None:
            self._follow.start()

    # ---- mode ----
    def _mode(self, mode):
        if mode in ('keyboard', 'pad'):
            if not self.keyboard:
                self.keyboard = True
                QTimer.singleShot(0, lambda: self._focus_changed(None,
                                                                 QApplication.focusWidget()))
        elif self.keyboard:
            self.keyboard = False
            self._hide()

    def nudge(self):
        """The controller was used: show focus even before a key arrives."""
        if not self.keyboard:
            self.keyboard = True
            self._focus_changed(None, QApplication.focusWidget())

    # ---- target ----
    def _focus_changed(self, _old, new):
        try:
            ok = (self.keyboard and new is not None and new.window() is self.win
                  and not isinstance(new, self.SKIP))
        except RuntimeError:
            ok = False
        if not ok:
            self._hide()
            return
        first = self.target is None or not self.isVisible()
        self.target = new
        geo = self._geometry()
        if geo is None:
            self._hide()
            return
        rect, radius, clip = geo
        self._radius, self._clip = radius, clip
        self.setGeometry(self.win.rect())
        self.raise_()
        self.show()
        self._follow.start()
        if first or not MOTION:
            self._anim.stop()
            self._moved(rect)
        else:
            self._anim.stop()
            self._anim.setDuration(120)
            self._anim.setStartValue(QRectF(self._rect))
            self._anim.setEndValue(rect)
            self._anim.start()

    def _hide(self):
        self.target = None
        self._follow.stop()
        self._anim.stop()
        self.hide()

    def _geometry(self):
        w = self.target
        try:
            if w is None or not w.isVisible():
                return None
            r = QRectF(w.rect())
            if isinstance(w, GameTile):              # the art, not the lift room
                top = TILE_LIFT - TILE_LIFT * w._hover
                r = QRectF(0, top, w.tw, w.th)
            pad = self.PAD * self.scale
            tl = QPointF(w.mapTo(self.win, r.topLeft().toPoint()))
            rect = QRectF(tl, r.size()).adjusted(-pad, -pad, pad, pad)
            radius = self._shape_radius(w, r) + pad
            # Don't draw over the rest of the UI when it's scrolled half out.
            clip = QRectF(self.win.rect())
            p = w.parentWidget()
            while p is not None and p is not self.win:
                if isinstance(p, QAbstractScrollArea):
                    vp = p.viewport()
                    o = QPointF(vp.mapTo(self.win, QPoint(0, 0)))
                    m = pad + 8
                    clip = clip.intersected(QRectF(o, QSizeF(vp.size())).adjusted(-m, -m, m, m))
                p = p.parentWidget()
            if not clip.intersects(rect):
                return None
            return rect, radius, clip
        except RuntimeError:
            return None

    @staticmethod
    def _shape_radius(w, r) -> float:
        m = re.search(r'border-radius:\s*(\d+)', w.styleSheet() or '')
        if m:
            return float(m.group(1))
        name = w.objectName()
        if name in ('Chip', 'SegItem', 'TopIcon', 'ShelfArrow', 'GlassIcon', 'PlayRound',
                    'Switch'):
            return r.height() / 2
        if isinstance(w, (GameTile, ArtThumb)) or name == 'TheatreShot':
            return float(R['tile'])
        if name in ('Card', 'DashCard', 'TheatreNewsRow') or isinstance(w, (DealCard, NewsTile, NewsFeature)):
            return float(R['card'])
        if name == 'DashRow':
            return float(R['sm'])
        return float(R['control'])

    def _track(self):
        if self.target is None:
            return
        geo = self._geometry()
        if geo is None:
            self.hide()
            return
        rect, self._radius, self._clip = geo
        if not self.isVisible():
            self.show()
        if self._anim.state() == QVariantAnimation.State.Running:
            self._anim.setEndValue(rect)
        elif rect != self._rect:
            self._moved(rect)

    def _moved(self, r):
        # Repaint only where the ring was and where it is now (glow included):
        # this overlay covers the window, and a bare update() would repaint
        # everything under it on every frame of the glide.
        m = 8 * self.scale
        dirty = QRectF(self._rect).united(QRectF(r)) if not self._rect.isNull() else QRectF(r)
        self._rect = QRectF(r)
        self.update(dirty.adjusted(-m, -m, m, m).toAlignedRect())

    def paintEvent(self, ev):
        if self._rect.isNull():
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setClipRect(self._clip)
        r, rad = self._rect, self._radius
        a, b = (QColor(c) for c in GRAD['dusk'])
        # Soft glow first (two wide, faint strokes), then the crisp ring.
        for width, alpha in ((10.0, 0.07), (6.0, 0.14)):
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            ga, gb = QColor(a), QColor(b)
            ga.setAlphaF(alpha); gb.setAlphaF(alpha)
            g.setColorAt(0.0, ga); g.setColorAt(1.0, gb)
            pt.setPen(QPen(QBrush(g), width * self.scale))
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.drawRoundedRect(r, rad, rad)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, a); g.setColorAt(1.0, b)
        pt.setPen(QPen(QBrush(g), 2.5 * self.scale))
        pt.drawRoundedRect(r, rad, rad)
        pt.end()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import MOTION  # noqa: E402
from .artpicker import ArtThumb  # noqa: E402
from .deals import DealCard  # noqa: E402
from .newspage import NewsFeature, NewsTile  # noqa: E402
from .tiles import GameTile  # noqa: E402
from .widgets import clear_layout, label  # noqa: E402
