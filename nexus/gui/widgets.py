"""Small reusable widgets and helpers: labels, flow layout, toasts, banners."""

import json
import re
import shlex
import shutil
from pathlib import Path

from PyQt6.QtCore import (pyqtSignal, QEasingCurve, QEvent, QObject, QPoint, QProcess,
    QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation)
from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPen,
    QPixmap, QRadialGradient)
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QBoxLayout, QComboBox, QFrame,
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QMenu, QPushButton, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget)

from nexus import paths as NX, platform

from .common import ICON_PATH
from .theme import FONTS, G, GLYPHS, GRAD, LOOK, P, PAL, pill, pill_qss, SP


class PopupRounder(QObject):
    """Menus and combo drop-downs are top-level windows; their QSS
    radius only shows if the window itself is translucent (otherwise the
    square window background paints the corners). Set that as each one is
    polished, before its native surface exists; on Windows also drop the
    system's square shadow, which would frame the rounded corners."""
    # Not tooltips: a translucent QTipLabel can come up as its own black window
    # under niri/Wayland instead of a popup.
    POPUPS = ('QComboBoxPrivateContainer',)

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.Polish:     # every event in the app passes here
            return False
        if isinstance(obj, QWidget) and obj.isWindow() \
                and (isinstance(obj, QMenu) or obj.metaObject().className() in self.POPUPS) \
                and not obj.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
            obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            if platform.SQUARE_POPUP_SHADOW:
                obj.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        return False


class GradientBackground(QWidget):
    """Paints `effect.gradient.bg`: soft radial glows over the base colour.

    QSS carries only one background per widget, so the layered gradient is
    painted directly. Everything above it uses transparent backgrounds so this
    shows through, which is also how the translucent-card look is achieved
    without a real backdrop blur (Qt has no `backdrop-filter`).

    The main glow takes on a tint (the featured game's dominant colour) and
    cross-fades when it changes — an event-driven animation, never a
    per-frame loop, since repainting this repaints the whole window.
    """

    def __init__(self):
        super().__init__()
        self._tint = QColor(P['accent'])
        self._from = QColor(self._tint)
        self._to = QColor(self._tint)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.valueChanged.connect(self._step)
        self._base: tuple | None = None          # (key, pixmap): the layers that don't fade

    def set_tint(self, color: QColor | None):
        if not LOOK['art_tint']:
            color = None                         # Appearance: glow stays the accent
        target = QColor(color) if color is not None and color.isValid() else QColor(P['accent'])
        if target == self._to:
            return
        self._from, self._to = QColor(self._tint), target
        self._anim.stop()
        self._anim.setDuration(anim_ms(800))
        if self._anim.duration() == 0:
            self._step(1.0)
        else:
            self._anim.start()

    def _step(self, t):
        t = float(t)
        a, b = self._from, self._to
        self._tint = QColor(int(a.red() + (b.red() - a.red()) * t),
                            int(a.green() + (b.green() - a.green()) * t),
                            int(a.blue() + (b.blue() - a.blue()) * t))
        self.update()

    def paintEvent(self, ev):
        # The base colour and the three fixed glows are painted once per size
        # and theme; only the tinted glow is painted live (it cross-fades, and
        # each fade frame repaints the whole window).
        w, h = self.width(), self.height()
        k = LOOK['glow']
        key = (w, h, k, P['bg'], P['accent'], GRAD['dusk'][1], PAL['pine'])
        if self._base is None or self._base[0] != key:
            pm = QPixmap(max(1, w), max(1, h))
            bp = QPainter(pm)
            bp.fillRect(pm.rect(), QColor(P['bg']))
            if k > 0:
                self._glows(bp, w, h, k, fixed=True)
            bp.end()
            self._base = (key, pm)
        pt = QPainter(self)
        pt.drawPixmap(ev.rect(), self._base[1], ev.rect())
        if k > 0:
            pt.setClipRect(ev.rect())
            self._glows(pt, w, h, k, fixed=False)
        pt.end()

    def _glows(self, pt, w, h, k, fixed):
        rect = QRect(0, 0, w, h)

        def radial(cx, cy, radius, color, alpha, stop):
            # Note the all-float overload: PyQt6 has no QPoint form of this
            # constructor, and the TypeError it raises inside paintEvent comes
            # back as a hard abort rather than a traceback.
            g = QRadialGradient(float(cx), float(cy), float(radius))
            c = QColor(color); c.setAlphaF(alpha)
            end = QColor(color); end.setAlphaF(0.0)
            g.setColorAt(0.0, c)
            g.setColorAt(stop, end)
            g.setColorAt(1.0, end)
            pt.fillRect(rect, QBrush(g))

        if not fixed:
            # The featured game's colour, top right.
            radial(w * 0.80, h * -0.10, 1150, self._tint, 0.13 * k, 0.60)
            return
        # The accent, top left — the brand glow.
        radial(w * -0.10, h * 0.10, 950, P['accent'], 0.08 * k, 0.55)
        # Faint pools of the gradient partner and pine at the bottom.
        radial(w * 0.30, h * 1.15, 900, GRAD['dusk'][1], 0.05 * k, 0.60)
        radial(w * 1.05, h * 0.95, 800, PAL['pine'], 0.07 * k, 0.60)


# --------------------------------------------------------------------------
# FlowLayout (reflowing grid) — adapted from the Qt example
# --------------------------------------------------------------------------

class WrapRow(QWidget):
    """Items on the left (`left`, a QHBoxLayout) and one control on the right
    (set_right). When the two don't fit side by side (a narrow window, or
    150 % display scaling), the control moves under them, still on the right,
    instead of being squeezed until its labels are cut off."""

    def __init__(self, spacing: int = 0):
        super().__init__()
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setSpacing(spacing)
        # The sizes asked for below don't depend on the arrangement: if they
        # did, each would get the room that keeps it (wide stays wide, stacked
        # stays stacked once the game panel takes the difference).
        self._box.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.left = QHBoxLayout()
        self.left.setContentsMargins(0, 0, 0, 0)
        self._box.addLayout(self.left, 1)
        self.right: QWidget | None = None

    def set_right(self, w: QWidget):
        self.right = w
        self._box.addWidget(w, 0, Qt.AlignmentFlag.AlignRight)
        w.installEventFilter(self)                  # shown or hidden later
        self._fit()

    def stacked(self) -> bool:
        return self._box.direction() == QBoxLayout.Direction.TopToBottom

    def _shown(self) -> bool:
        return self.right is not None and not self.right.isHidden()

    def _side_by_side(self) -> int:
        """The width both need on one line."""
        m = self._box.contentsMargins()
        right = self._box.spacing() + self.right.sizeHint().width() if self._shown() else 0
        return self.left.minimumSize().width() + right + m.left() + m.right()

    def sizeHint(self):
        return QSize(self._side_by_side(), super().sizeHint().height())

    def minimumSizeHint(self):
        m = self._box.contentsMargins()
        right = self.right.minimumSizeHint().width() if self._shown() else 0
        return QSize(max(self.left.minimumSize().width(), right) + m.left() + m.right(),
                     super().minimumSizeHint().height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def eventFilter(self, obj, ev):
        if obj is self.right and ev.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self._fit()
        return False

    def _fit(self):
        d = (QBoxLayout.Direction.TopToBottom if self._shown()
             and self.width() < self._side_by_side() else QBoxLayout.Direction.LeftToRight)
        if self._box.direction() != d:
            self._box.setDirection(d)


class FlowLayout(QLayout):
    """Wrapping row layout. With `justify`, full rows spread their leftover
    width into the gaps (the grid then reaches both edges) and the last row
    reuses that gap so its items stay in the same columns."""

    def __init__(self, parent=None, margin=0, spacing=14, justify=False):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._justify = justify
        self._hfw: dict[int, int] = {}

    def addItem(self, item):
        self._items.append(item)
        self._hfw = {}

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        self._hfw = {}
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def invalidate(self):
        self._hfw = {}
        super().invalidate()

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        # Asked many times per layout pass with the same width: remember it
        # until the items change (a few hundred tiles made this the hot spot).
        h = self._hfw.get(width)
        if h is None:
            h = self._hfw[width] = self._do_layout(QRect(0, 0, width, 0), True)
        return h

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        outer = rect
        rect = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        lines, line, x = [], [], rect.x()
        for item in self._items:
            hint = item.sizeHint()                 # once per item per pass
            w = hint.width()
            if line and x + w > rect.right() + 1:
                lines.append(line)
                line, x = [], rect.x()
            line.append((item, hint))
            x += w + self._spacing
        if line:
            lines.append(line)
        y, gap = rect.y(), float(self._spacing)
        for i, line in enumerate(lines):
            widths = [h.width() for _it, h in line]
            last = i == len(lines) - 1
            if self._justify and not last and len(line) > 1:
                gap = max(float(self._spacing),
                          (rect.width() - sum(widths)) / (len(line) - 1))
            line_gap = gap if self._justify else self._spacing
            line_height = max(h.height() for _it, h in line)
            if not test_only:
                x = float(rect.x())
                for (it, hint), w in zip(line, widths):
                    it.setGeometry(QRect(QPoint(round(x), y), hint))
                    x += w + line_gap
            y += line_height + (self._spacing if not last else 0)
        return y - outer.y() + m.bottom()


def _role_of(color: str | None) -> str | None:
    """The palette role a colour came from, so it can follow a theme change."""
    if not color:
        return None
    return next((k for k, v in P.items() if v == color), None)


def icon_label(glyph: str, px: int = 14, color: str | None = None) -> QLabel:
    lab = QLabel(glyph)
    lab._icon_spec = (px, _role_of(color), color)
    _style_icon_label(lab)
    lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return lab


def _style_icon_label(lab: QLabel):
    # Via the stylesheet: the app-wide QSS font rules beat setFont().
    px, role, color = lab._icon_spec
    c = P[role] if role else color
    lab.setStyleSheet(f'font-family: "{FONTS.icon}"; font-size: {px}px;'
                      + (f" color: {c};" if c else ""))


def glyph_pixmap(glyph: str, color: str, px: int, dpr: float = 2.0) -> QPixmap:
    pm = QPixmap(int(px * dpr), int(px * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    pt.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    f = QFont(FONTS.icon)
    f.setPixelSize(px)
    pt.setFont(f)
    pt.setPen(QColor(color))
    pt.drawText(QRectF(0, 0, px, px), Qt.AlignmentFlag.AlignCenter, glyph)
    pt.end()
    return pm


def set_glyph_icon(btn: QAbstractButton, glyph: str, px: int = 16, role: str = 'subtext',
                   on_role: str | None = None, hover_role: str | None = 'text'):
    """Give a text button a real icon drawn from the icon font — crisper and
    better aligned than a glyph pasted into its label — coloured per state
    (checked = on_role). Remembered, so a theme change can redraw it."""
    btn._glyph_spec = (glyph, px, role, on_role, hover_role)
    icon = QIcon()
    icon.addPixmap(glyph_pixmap(glyph, P[role], px), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(glyph_pixmap(glyph, P[on_role or role], px), QIcon.Mode.Normal,
                   QIcon.State.On)
    if hover_role:
        icon.addPixmap(glyph_pixmap(glyph, P[hover_role], px), QIcon.Mode.Active,
                       QIcon.State.Off)
    icon.addPixmap(glyph_pixmap(glyph, P['muted'], px), QIcon.Mode.Disabled, QIcon.State.Off)
    btn.setIcon(icon)
    btn.setIconSize(QSize(px, px))


def refresh_icons():
    """Re-colour every remembered icon after the theme changed."""
    for w in QApplication.allWidgets():
        spec = getattr(w, '_glyph_spec', None)
        if spec:
            set_glyph_icon(w, *spec)
        elif getattr(w, '_icon_spec', None):
            _style_icon_label(w)


def icon_button(glyph: str, tip: str, size: int = 30, name: str | None = None,
                round_: bool = False) -> QToolButton:
    b = QToolButton()
    b.setText(glyph)
    b.setToolTip(tip)
    b.setFixedSize(size, size)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        b.setObjectName(name)
    if round_ or name in ('ShelfArrow', 'GlassIcon', 'TopIcon'):
        b.setStyleSheet(f"border-radius: {pill(size)}px;")
    return b


def label(text: str = '', name: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def amp(text: str) -> str:
    """Button and menu text shown as is: Qt reads a lone & as a shortcut
    marker, so "Back up & move" drew "Back up _move" and a game called
    "Ratchet & Clank" lost its ampersand."""
    return text.replace('&', '&&')


def plain_label(text: str = '', name: str | None = None, wrap: bool = False) -> QLabel:
    """A label for text from outside (game, news, torrent and achievement
    names): never interpreted as rich text, whatever it starts with."""
    lab = label(text, name, wrap)
    lab.setTextFormat(Qt.TextFormat.PlainText)
    return lab


class NoWheelCombo(QComboBox):
    """A combo box the scroll wheel passes over unless it has the focus:
    scrolling a page must never change a setting (or, worse, run an action
    wired to `activated`) just because the pointer crossed a combo."""

    def __init__(self, *a):
        super().__init__(*a)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()                      # on to the scroll area underneath


def ask(widget: QWidget, title: str, text: str, yes: str, on_yes, danger: bool = True):
    """Confirm through the main window's sheet (MainWindow.confirm); a
    widget outside one (a test) just runs it."""
    win = widget.window()
    if hasattr(win, 'confirm'):
        win.confirm(title, text, yes, on_yes, danger)
    else:
        on_yes()


def protongen_code_preview(text: str) -> str | None:
    """What a pasted protongen:v1: share code would set, for a confirmation
    (None when the text isn't one). The CLI validates it again on apply."""
    import base64
    code = re.sub(r'\s+', '', text or '')
    if not code.startswith('protongen:v1:'):
        return None
    try:
        raw = json.loads(base64.b64decode(code[len('protongen:v1:'):], validate=True))
        cfg = raw['config']
        env = [f"{k}={v}" for k, v in cfg.get('env') or [] if v != '']
        env += shlex.split(str(cfg.get('extra_env') or ''))
        wrappers = [f"{k} {a}".strip() for k, a in cfg.get('wrappers') or []]
    except (ValueError, KeyError, TypeError, AttributeError):
        return "This preset code looks damaged. Apply it anyway?"
    lines = [f"“{str(raw.get('name') or 'protongen preset')[:64]}” sets:"]
    lines += [f"  {e}" for e in env] or ["  no environment variables"]
    lines += [f"  runs through {w}" for w in wrappers]
    if cfg.get('game_args'):
        lines.append(f"  game arguments: {cfg['game_args']}")
    return '\n'.join(lines)


_SOFT_BREAK = re.compile(r'([/,=;_:])')


def soft_wrap(text: str) -> str:
    """Let a word-wrapped label break long paths and env strings: a wrapped
    QLabel is never narrower than its longest unbreakable token, so a single
    MANGOHUD_CONFIG=… would otherwise widen the whole panel. Display only —
    copy the raw value, not this."""
    return _SOFT_BREAK.sub('\\1\u200b', text or '')


def short_path(path: str, keep: int = 3) -> str:
    """/home/me/Games/SteamLibrary/steamapps/common/X → …/steamapps/common/X"""
    parts = Path(path).parts if path else ()
    if len(parts) <= keep + 1:
        return path or ''
    return '…/' + '/'.join(parts[-keep:])


def clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def start_steam() -> None:
    QProcess.startDetached('steam', [])


def notify_desktop(summary: str, body: str = '') -> None:
    """Desktop notification (only used while the window isn't focused).
    notify-send reaches whatever notification daemon the session runs; on a
    bare compositor with none, it silently does nothing. Without notify-send
    (Windows) the tray icon shows it, or with the tray off a toast
    (platform.notify_local)."""
    if shutil.which('notify-send'):
        QProcess.startDetached('notify-send', [
            '-a', 'Nexus', '-i', str(ICON_PATH) if ICON_PATH.exists()
            else 'applications-games', '-h', f'string:desktop-entry:{NX.APP_ID}',
            '--', summary, body])
    elif not tray_message(summary, body):
        try:
            platform.notify_local(summary, body)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Toasts and the "Steam is running" banner — in-window, never extra windows
# --------------------------------------------------------------------------

def empty_state_text(filtered: str | None) -> tuple[str, str]:
    """(title, hint) for an empty library view."""
    if filtered:
        return filtered, "Clear the search or pick another filter."
    return ("Your library is empty",
            "Install a repack with ＋ Install, or pick up games you already have with Scan.")


class EmptyState(QFrame):
    """Centred icon + title + hint (+ optional action) for a page or list
    with nothing to show — instead of one grey sentence at the top."""
    action = pyqtSignal()

    def __init__(self, glyph: str, title: str, hint: str = '', action: str = '',
                 role: str = 'muted'):
        super().__init__()
        self.setObjectName("EmptyState")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['6'], SP['8'] * 2, SP['6'], SP['8'])
        lay.setSpacing(SP['2'])
        self.icon = icon_label(glyph, 34)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tint(role)
        lay.addWidget(self.icon)
        lay.addSpacing(SP['1'])
        self.title = label(title, "EmptyTitle", wrap=True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title)
        self.hint = label(hint, "Muted", wrap=True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)
        self.hint.setVisible(bool(hint))      # after addWidget: a parentless show is a window
        self.button = QPushButton(action)
        self.button.clicked.connect(self.action)
        lay.addSpacing(SP['1'])
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignCenter)
        self.button.setVisible(bool(action))

    def set(self, title: str | None = None, hint: str | None = None,
            glyph: str | None = None, role: str | None = None):
        if title is not None:
            self.title.setText(title)
        if hint is not None:
            self.hint.setText(hint)
            self.hint.setVisible(bool(hint))
        if glyph is not None:
            self.icon.setText(glyph)
        if role is not None:
            self._tint(role)

    def _tint(self, role):
        # The app-wide QSS font-size beats setFont(), so size the glyph here.
        self.icon.setStyleSheet(f'font-family: "{FONTS.icon}"; font-size: 34px;'
                                f' color: {P.get(role, P["muted"])};')


class SegmentedControl(QWidget):
    """One-of-N switch with a painted track and a thumb that slides to the
    current item. The items are real (transparent) buttons on top, so focus,
    SpatialNav, tooltips and the controller keep working.

    style='pill'      — capsule track, gradient thumb (view switchers, toggles)
    style='underline' — no track, a gradient bar under the current item (tabs)
    """
    changed = pyqtSignal(str)

    def __init__(self, style: str = 'pill', height: int = 32, parent=None):
        super().__init__(parent)
        self.style_ = style
        self._h = height
        self._items: dict[str, QAbstractButton] = {}
        self._badges: dict[str, QLabel] = {}
        self._current = ''
        self._thumb = QRectF()
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_thumb)
        # The items can move mid-slide (a Size change restyles the app): land on
        # where the current item is now, not where it was when the slide began.
        self._anim.finished.connect(self._sync_later)
        lay = QHBoxLayout(self)
        inset = 3 if style == 'pill' else 0
        lay.setContentsMargins(inset, inset, inset, inset)
        lay.setSpacing(2 if style == 'pill' else SP['1'])
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    # ---- building ----
    def add(self, key: str, text: str | None = None, glyph: str | None = None,
            tip: str = '') -> QAbstractButton:
        item_h = self._h - 6 if self.style_ == 'pill' else self._h
        if text is None:
            b = QToolButton()
            b.setText(glyph or '')
            b.setFixedSize(item_h + 6, item_h)
        else:
            b = QPushButton(text)
            b.setFixedHeight(item_h)
            b._label, b._glyph = text, glyph
            if glyph:
                tab = self.style_ == 'underline'
                set_glyph_icon(b, glyph, 18 if tab else 15, 'muted',
                               on_role='text' if tab else 'on_accent')
        b.setObjectName("SegItem" if self.style_ == 'pill' else "TabItem")
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        if self.style_ == 'pill':
            b.setStyleSheet(f"border-radius: {pill(item_h)}px;")
        b.clicked.connect(lambda _=False, k=key: self._clicked(k))
        b.installEventFilter(self)
        self.layout().addWidget(b)
        self._items[key] = b
        if not self._current:
            self._current = key
            b.setChecked(True)
        return b

    def item(self, key: str) -> QAbstractButton | None:
        return self._items.get(key)

    def keys(self) -> list[str]:
        return list(self._items)

    def set_item_visible(self, key: str, on: bool):
        b = self._items.get(key)
        if b is not None:
            b.setVisible(on)
            self._sync_later()

    def set_badge(self, key: str, text: str, role: str = 'accent'):
        """A small count pill at the item's top-right corner."""
        b = self._items.get(key)
        if b is None:
            return
        lab = self._badges.get(key)
        if lab is None:
            lab = QLabel(b)
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setFixedHeight(16)
            self._badges[key] = lab
        lab.setStyleSheet(pill_qss(P.get(f'{role}_22', P['accent_22']), P.get(role, P['accent']),
                                   16, 5))
        lab.setText(text)
        lab.adjustSize()
        lab.setVisible(bool(text))
        if self.style_ == 'underline':
            # Tabs: the count sits after the label, so make room for it.
            b.setStyleSheet(f"padding-right: {lab.width() + SP['3'] + 4}px;" if text else "")
        self._place_badge(b, lab)

    def _place_badge(self, b, lab):
        if self.style_ == 'underline':
            lab.move(b.width() - lab.width() - SP['2'], (b.height() - lab.height()) // 2)
        else:
            lab.move(b.width() - lab.width() - 2, 2)

    def set_compact(self, on: bool):
        """Icon-only items (glyph kept, text hidden into the tooltip)."""
        for b in self._items.values():
            if isinstance(b, QPushButton) and getattr(b, '_glyph', None):
                b.setText("" if on else b._label)
                if on and not b.toolTip():
                    b.setToolTip(b._label)
        self._sync_later()

    # ---- state ----
    def current(self) -> str:
        return self._current

    def set_current(self, key: str | None, animate: bool = True, emit: bool = False):
        if key is None:                        # nothing selected (e.g. a non-tab page)
            self._current = ''
            for b in self._items.values():
                b.setChecked(False)
            self._anim.stop()
            self._set_thumb(QRectF())
            return
        if key not in self._items:
            return
        changed = key != self._current
        self._current = key
        for k, b in self._items.items():
            b.setChecked(k == key)
        target = self._target(key)
        if animate and self.isVisible() and not self._thumb.isNull() and MOTION:
            self._anim.stop()
            self._anim.setDuration(180)
            self._anim.setStartValue(QRectF(self._thumb))
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._set_thumb(target)
        if changed and emit:
            self.changed.emit(key)

    def _clicked(self, key):
        self.set_current(key, emit=True)
        self._items[key].setChecked(True)

    # ---- geometry / paint ----
    def _target(self, key) -> QRectF:
        b = self._items.get(key) if key else None
        if b is None or not b.isVisible() and self.isVisible():
            return QRectF()
        g = QRectF(b.geometry())
        if self.style_ == 'underline':
            w = max(16.0, g.width() - 2 * SP['3'])
            return QRectF(g.center().x() - w / 2, g.bottom() - 2.0, w, 3.0)
        return g

    def _set_thumb(self, r):
        self._thumb = QRectF(r)
        self.update()

    def _sync_later(self):
        QTimer.singleShot(0, lambda: self._alive() and self._set_thumb(self._target(self._current)))

    def _alive(self):
        try:
            self.objectName()
            return True
        except RuntimeError:
            return False

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
            if self._anim.state() == QVariantAnimation.State.Running:
                self._anim.setEndValue(self._target(self._current))
            else:
                self._set_thumb(self._target(self._current))
            lab = next((l for k, l in self._badges.items() if self._items[k] is obj), None)
            if lab is not None:
                self._place_badge(obj, lab)
        return False

    def showEvent(self, ev):
        super().showEvent(ev)
        self._sync_later()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(Qt.PenStyle.NoPen)
        a, b = (QColor(c) for c in GRAD['dusk'])
        if self.style_ == 'pill':
            track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            pt.setBrush(QColor(P['surface_2']))
            pt.setPen(QPen(QColor(P['border']), 1.0))
            pt.drawRoundedRect(track, track.height() / 2, track.height() / 2)
            pt.setPen(Qt.PenStyle.NoPen)
        if not self._thumb.isNull():
            g = QLinearGradient(self._thumb.topLeft(), self._thumb.topRight())
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.setBrush(QBrush(g))
            r = self._thumb.height() / 2
            pt.drawRoundedRect(self._thumb, r, r)
        pt.end()


class Toast(QFrame):
    closed = pyqtSignal(object)

    COLORS = {'info': 'accent', 'success': 'success', 'error': 'danger', 'warn': 'warning'}
    GLYPHS = {'info': G['info'], 'success': G['check'], 'error': G['fail'], 'warn': G['warn']}

    def __init__(self, text, kind='info', action=None, timeout=6000):
        super().__init__()
        self.setObjectName("Toast")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['3'], SP['2_5'], SP['2'], SP['2_5'])
        lay.setSpacing(SP['2_5'])
        lay.addWidget(icon_label(self.GLYPHS[kind], 14, P[self.COLORS[kind]]))
        msg = label(text, wrap=True)
        msg.setTextFormat(Qt.TextFormat.PlainText)       # names come from repacks/torrents
        msg.setMaximumWidth(320)
        lay.addWidget(msg, 1)
        self._buttons = []
        for text, fn in (action if isinstance(action, list) else [action] if action else []):
            b = QPushButton(amp(text))
            b.clicked.connect(lambda _=False, fn=fn: self._act(fn))
            lay.addWidget(b)
            self._buttons.append(b)
        x = icon_button(G['close'], "Dismiss", 24, "Ghost")
        x.clicked.connect(self.dismiss)
        lay.addWidget(x)
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b'opacity', self)
        self._anim.setDuration(160)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer = None
        if timeout:                                 # a child timer: it dies with the toast
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self.dismiss)
            self._timer.start(timeout)

    def _act(self, fn):
        # Once: a double click on Retry/Install mustn't queue it twice
        # while the toast fades out.
        if getattr(self, '_closing', False):
            return
        for b in self._buttons:
            b.setEnabled(False)
        self.dismiss()
        fn()

    def enterEvent(self, ev):                       # being read: don't go away
        if self._timer is not None:
            self._timer.stop()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self._timer is not None and not getattr(self, '_closing', False):
            self._timer.start(2500)
        super().leaveEvent(ev)

    def dismiss(self):
        if getattr(self, '_closing', False):
            return
        self._closing = True
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(0.0)
        if not getattr(self, '_wired', False):     # a bound method: dropped if we're gone
            self._wired = True
            self._anim.finished.connect(self._faded_out)
        self._anim.start()

    def _faded_out(self):
        if self._closing:
            self.closed.emit(self)


class ToastHost(QWidget):
    """Stack of toasts pinned to the parent's bottom-right corner. Sized to
    its toasts only, so it never blocks clicks on the rest of the window."""

    def __init__(self, parent):
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(SP['2'])
        self.hide()
        parent.installEventFilter(self)

    def show_toast(self, text, kind='info', action=None, timeout=6000):
        if kind == 'error':
            timeout = 0                       # errors stay until dismissed
        t = Toast(text, kind, action, timeout)
        t.closed.connect(self._remove)
        self.lay.addWidget(t)
        if self.lay.count() > 4:                  # at most a few at once: drop the oldest
            self.lay.itemAt(0).widget().dismiss()
        # Shown now, not when the layout gets round to it: a hidden toast has
        # no size, so the host stayed one toast tall and squashed the rest.
        t.show()
        self.show()
        self.raise_()
        self._place()

    def _remove(self, t):
        self.lay.removeWidget(t)
        t.deleteLater()
        if self.lay.count() == 0:
            self.hide()
        self._place()

    def _place(self):
        self.adjustSize()
        p = self.parentWidget()
        self.move(p.width() - self.width() - SP['5'], p.height() - self.height() - SP['5'])

    def eventFilter(self, obj, ev):
        if ev.type() == ev.Type.Resize and self.isVisible():
            self._place()
        return False


class SteamGate(QFrame):
    """In-window replacement for the old "Steam is running" message box.

    Steam rewrites shortcuts.vdf/config.vdf when it exits, so writes must
    wait until it's closed. Requests queue here; the banner polls and runs
    them automatically as soon as Steam is gone."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Banner")
        self.setVisible(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['3'], SP['2'], SP['2'], SP['2'])
        lay.addWidget(icon_label(G['steam'], 16, P['warning']))
        self.text = label("", wrap=True)
        lay.addWidget(self.text, 1)
        self.btn_skip = QPushButton("Skip Steam")
        self.btn_skip.clicked.connect(self._skip)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._cancel)
        lay.addWidget(self.btn_skip)
        lay.addWidget(cancel)
        self.pending: list[tuple] = []       # (what, on_ok, on_skip)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._poll)

    def waiting(self, what: str) -> bool:
        return any(p[0] == what for p in self.pending)

    def run(self, what: str, on_ok, on_skip=None):
        """Call on_ok() now if Steam is closed, else once it is. on_skip, if
        given, offers "Skip Steam" (continue without the Steam part)."""
        if self.waiting(what):
            return
        if not steam_running():
            on_ok()
            return
        self.pending.append((what, on_ok, on_skip))
        self._refresh()
        self.timer.start()

    def _refresh(self):
        if not self.pending:
            self.setVisible(False)
            self.timer.stop()
            return
        whats = ', '.join(p[0] for p in self.pending)
        self.text.setText(f"Steam is running. Quit Steam fully and this continues by "
                          f"itself — waiting to: {whats}.")
        self.btn_skip.setVisible(any(p[2] for p in self.pending))
        self.setVisible(True)

    def _poll(self):
        if steam_running():
            return
        pending, self.pending = self.pending, []
        self._refresh()
        for _what, ok, _skip in pending:
            ok()

    def _skip(self):
        pending, self.pending = self.pending, []
        self._refresh()
        for _what, _ok, skip in pending:
            if skip:
                skip()

    def _cancel(self):
        self.pending = []
        self._refresh()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import anim_ms, MOTION  # noqa: E402
from .config import steam_running  # noqa: E402
from .tray import tray_message  # noqa: E402
