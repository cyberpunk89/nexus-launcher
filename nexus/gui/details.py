"""The game page (details panel), docked beside the grid."""

import html
import os
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QPoint, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QLinearGradient, QPainter,
    QPainterPath, QPixmap)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from nexus import gamedb

from .common import alive, HOME, MAX_NOTE
from .fmt import fmt_ago, fmt_hours, fmt_relative, fmt_size
from .theme import FS, G, P, R, SP


def _hero_art(game: dict) -> str | None:
    return art_source(game, HERO_KINDS)[0]


class HeroBanner(QWidget):
    """Wide art strip at the top of the panel, clipped to the card radius.

    Art and logo are decoded at about the size they're shown at, not full
    size: a 3840x1240 SteamGridDB hero cost ~30 ms on every selection (too
    big for Qt's pixmap cache, so even re-showing a game decoded it again).
    Big files decode on a worker; small ones (most) stay synchronous so the
    banner doesn't flash a placeholder; the last few stay decoded."""
    logo_changed = pyqtSignal(bool)            # a logo is (not) shown now

    DECODE_W = 1600                            # wider than any details panel
    SYNC_BYTES = 1_500_000                     # smaller decodes faster than a frame
    _RECENT: dict[tuple, QPixmap] = {}         # shared by every banner, oldest first
    RECENT_MAX = 8

    def __init__(self):
        super().__init__()
        self.setFixedHeight(176)
        self._pm = None
        self._logo = None
        self._name = ''
        self._waiting = None                   # the hero URL being downloaded for us
        self._keys = {'art': None, 'logo': None}   # (path, mtime, size) shown or on its way
        self._logo_pending = False
        # The art and logo scaled to the current size: smooth-scaling on
        # every repaint made moving through games stutter.
        self._frames: dict[str, tuple] = {}

    def has_logo(self) -> bool:
        """A logo is shown, or still decoding (so the name stays hidden)."""
        return self._logo is not None or self._logo_pending

    def _box(self, which: str) -> tuple[int, int]:
        dpr = self.devicePixelRatioF()
        w = max(self.DECODE_W, round(self.width() * dpr))
        if which == 'art':
            return w, round(self.height() * dpr)
        return round(w * 0.62), round(self.height() * 0.42 * dpr)

    def _want(self, which: str, path: str | None):
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        key = (path, st.st_mtime_ns, st.st_size) if st else None
        if key == self._keys[which]:
            return                              # already shown (or decoding)
        self._keys[which] = key
        if key is None:
            self._set(which, None)
            return
        w, h = self._box(which)
        cache = (which, key, w, h)
        if cache in self._RECENT:
            self._RECENT[cache] = self._RECENT.pop(cache)       # most recent last
            self._set(which, self._RECENT[cache])
        elif key[2] <= self.SYNC_BYTES:
            self._decoded(which, key, cache, fit_image(path, w, h, which == 'art'))
        else:
            self._set(which, None, pending=True)   # placeholder until it's decoded
            in_thread(lambda: fit_image(path, w, h, which == 'art'),
                      lambda img, _e: self._decoded(which, key, cache, img))

    def _decoded(self, which, key, cache, img):
        pm = QPixmap.fromImage(img) if img is not None else None
        if pm is not None:                      # kept even if we've moved on: going
            self._RECENT[cache] = pm            # back is then instant
            while len(self._RECENT) > self.RECENT_MAX:
                self._RECENT.pop(next(iter(self._RECENT)))
        try:
            if key != self._keys[which]:
                return                          # moved on to another game meanwhile
            self._set(which, pm)
        except RuntimeError:                    # the banner is gone
            pass

    def _set(self, which, pm, pending=False):
        if which == 'art':
            self._pm = pm
        else:
            self._logo, self._logo_pending = pm, pending
            self.logo_changed.emit(self.has_logo())
        self._frames.pop(which, None)
        self.update()

    def set_game(self, game: dict):
        self._name = game.get('display_name', '')
        logo, _url = art_source(game, LOGO_KINDS)
        self._want('logo', logo)
        path = _hero_art(game)
        self._waiting = None
        if not path and game.get('hero_url'):
            self._waiting = game['hero_url']
            ART.fetch(self._waiting, lambda p, u=self._waiting: self._arrived(u, p))
        if not path and game.get('cover_url'):
            path = ART.cached(game['cover_url'])
        self._load(path)

    def _arrived(self, url, path):
        try:
            if url == self._waiting:            # not a game we've since moved away from
                self._waiting = None
                self._load(path)
        except RuntimeError:
            pass

    def _load(self, path):
        self._want('art', path)

    def _scaled(self, which: str, pm: QPixmap, w: int, h: int, mode) -> QPixmap:
        hit = self._frames.get(which)
        if hit is None or hit[0] != (w, h):
            hit = ((w, h), pm.scaled(w, h, mode, Qt.TransformationMode.SmoothTransformation))
            self._frames[which] = hit
        return hit[1]

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, R['card'], R['card'])
        pt.setClipPath(clip)
        if self._pm is not None:
            s = self._scaled('art', self._pm, self.width(), self.height(),
                             Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            # Hero art keeps the top (logos / faces); covers look best centred.
            pt.drawPixmap(0, -(s.height() - self.height()) // 3, s)
        else:
            pt.drawPixmap(0, 0, placeholder_pixmap(self._name, self.width(), self.height()))
        g = QLinearGradient(0, r.bottom(), 0, r.top())
        c0 = QColor(P['mantle']); c0.setAlphaF(0.85)
        c1 = QColor(P['mantle']); c1.setAlphaF(0.0)
        g.setColorAt(0.0, c0); g.setColorAt(0.6, c1)
        pt.fillRect(r, QBrush(g))
        if self._logo is not None:
            lg = self._scaled('logo', self._logo, int(self.width() * 0.62),
                              int(self.height() * 0.42), Qt.AspectRatioMode.KeepAspectRatio)
            pt.drawPixmap(SP['4'], self.height() - lg.height() - SP['3'], lg)
        pt.end()


class ElideLabel(QLabel):
    """A one-line label that ends in … instead of clipping."""

    def __init__(self, text='', name=None):
        super().__init__(text)
        if name:
            self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setFont(self.font())
        pt.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                             self.width())
        pt.drawText(self.rect(), int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), text)
        pt.end()


class AchievementRow(QFrame):
    """One achievement: its icon (greyed while locked), title, description
    and when it was unlocked. A locked hidden one keeps its secret."""
    ICON = 36

    def __init__(self, a: dict, scale: float = 1.0):
        super().__init__()
        self.setObjectName("AchRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(int(SP['2_5'] * scale))
        self.size = int(self.ICON * scale)
        self.icon = QLabel()
        self.icon.setFixedSize(self.size, self.size)
        self.icon.setStyleSheet(f"background: {P['surface_2']}; border-radius: {R['sm']}px;")
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        secret = a.get('hidden') and not a.get('unlocked')
        title = plain_label("Hidden achievement" if secret else a.get('title', ''), None, wrap=True)
        title.setStyleSheet(f"color: {P['text'] if a.get('unlocked') else P['subtext']};"
                            f" font-weight: 500;")
        col.addWidget(title)
        desc = "Keep playing to find out." if secret else a.get('desc') or ''
        if desc:
            col.addWidget(label(desc, "Muted", wrap=True))
        lay.addLayout(col, 1)
        if a.get('time'):
            when = datetime.fromtimestamp(a['time'])
            stamp = label(fmt_relative(when.isoformat()) or '', "Muted")
            stamp.setToolTip(when.strftime('%Y-%m-%d %H:%M'))
            lay.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
        url = a.get('icon_url')
        if url and not secret:
            path = ART.cached(url)
            if path:
                self._show(path)
            else:
                ART.fetch(url, self._show)

    def _show(self, path):
        ART.request(path, self.size, self.size, self._set)

    def _set(self, pm):
        if pm is not None:
            self.icon.setPixmap(rounded_pixmap(pm, R['sm']))


class StatTile(QFrame):
    """One number about the game: icon, value, caption. Used in the details
    panel's stats grid."""

    def __init__(self, glyph: str, caption: str, role: str = 'accent'):
        super().__init__()
        self.setObjectName("StatTile")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['2_5'], SP['2'], SP['2_5'], SP['2'])
        lay.setSpacing(SP['2_5'])
        self.icon = icon_label(glyph, 18, P[role])
        self.icon.setFixedWidth(22)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.value = ElideLabel("—", "StatValue")
        col.addWidget(self.value)
        self.caption = ElideLabel(caption, "StatLabel")
        col.addWidget(self.caption)
        lay.addLayout(col, 1)

    def set(self, value: str | None, caption: str | None = None, color: str | None = None,
            tip: str = ''):
        self.value.setText(value or "—")
        self.value.setStyleSheet(f"color: {color};" if color else "")
        if caption is not None:
            self.caption.setText(caption)
        self.setToolTip(tip)


class Disclosure(QWidget):
    """A section that opens and closes under a one-line header, so the
    panel stays compact. Each section remembers whether it was open."""
    OPEN: dict[str, bool] = {}
    toggled = pyqtSignal(bool)

    def __init__(self, key: str, title: str, glyph: str, body: QWidget, summary: str = ''):
        super().__init__()
        self.key = key
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        self.head = ClickRow()
        self.head.setObjectName("DisclosureHead")
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(SP['3'], SP['2'], SP['2_5'], SP['2'])
        hl.setSpacing(SP['2_5'])
        hl.addWidget(icon_label(glyph, 16, P['accent']))
        t = plain_label(title, None)
        t.setStyleSheet(f"color: {P['text']}; font-weight: 500;")
        hl.addWidget(t)
        self.summary = label(summary, "Muted")
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hl.addWidget(self.summary, 1)
        self.chev = icon_label(G['chevron_down'], 16, P['muted'])
        hl.addWidget(self.chev)
        for w in self.head.findChildren(QLabel):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.head.clicked.connect(lambda: self.set_open(not self.is_open))
        lay.addWidget(self.head)
        self.body = body
        lay.addWidget(body)
        self.set_open(self.OPEN.get(key, False), remember=False)

    @property
    def is_open(self) -> bool:
        return not self.body.isHidden()

    def set_open(self, on: bool, remember: bool = True):
        self.body.setVisible(on)
        self.chev.setText(G['chevron_up'] if on else G['chevron_down'])
        if remember:
            Disclosure.OPEN[self.key] = on
        self.toggled.emit(on)

    def set_summary(self, text: str):
        self.summary.setText(text)


class StatChip(QFrame):
    def __init__(self, caption):
        super().__init__()
        self.setObjectName("StatChip")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['2_5'], SP['2'], SP['2_5'], SP['2'])
        lay.setSpacing(0)
        lay.addWidget(label(caption, "StatLabel"))
        self.value = label("—", "StatValue")
        lay.addWidget(self.value)

    def set(self, text):
        self.value.setText(text or "—")


class PerfPanel(QWidget):
    """Performance switches for one game (`nexus-cli --perf`): frame cap,
    power mode, upscaler, overlay, Wine Wayland, hidraw and gamescope.
    `set_state` takes --show-launch's `perf`; `items()` is the CLI argument
    list for what's shown now."""
    FPS = (0, 30, 40, 45, 60, 72, 90, 120, 144, 165, 240)
    RES = ((1280, 720), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160))
    REFRESH = (60, 75, 90, 120, 144, 165, 240)

    def __init__(self):
        super().__init__()
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(SP['2'])
        lay.setVerticalSpacing(SP['1_5'])
        self.cb_fps = NoWheelCombo()
        for n in self.FPS:
            self.cb_fps.addItem("No cap" if not n else f"{n} fps", n)
        self.cb_fps.setToolTip("DXVK_FRAME_RATE / VKD3D_FRAME_RATE: a frame cap inside the "
                               "game's renderer (Direct3D 8–12)")
        self.cb_mode = NoWheelCombo()
        for text, key in (("Off", 'off'), ("game-performance (power profile)", 'game-performance'),
                          ("gamemoderun (Feral GameMode)", 'gamemoderun')):
            self.cb_mode.addItem(text, key)
        self.cb_up = NoWheelCombo()
        for text, key in (("Off", 'off'), ("FSR 4 upgrade", 'fsr4'),
                          ("OptiScaler (DLSS/XeSS → FSR 4)", 'optiscaler')):
            self.cb_up.addItem(text, key)
        self.cb_up.setToolTip("FSR 4 needs an RDNA 4 GPU and a Proton that supports it "
                              "(GE-Proton 10+, proton-cachyos)")
        self.cb_fg = NoWheelCombo()
        for text, key in (("Off", None), ("2× (lsfg-vk)", 2), ("3× (lsfg-vk)", 3),
                          ("4× (lsfg-vk)", 4)):
            self.cb_fg.addItem(text, key)
        self.cb_fg.setToolTip("Lossless Scaling frame generation through lsfg-vk: a profile "
                              "for this game's exe in ~/.config/lsfg-vk/conf.toml")
        self.lsfg = None                 # None: lsfg-vk isn't set up
        for row, (name, w) in enumerate((("Frame cap", self.cb_fps), ("Power mode", self.cb_mode),
                                         ("Upscaler", self.cb_up), ("Frame gen", self.cb_fg))):
            lay.addWidget(label(name, "Muted"), row, 0)
            lay.addWidget(w, row, 1, 1, 3)
        self.ck_mango = QCheckBox("MangoHud overlay")
        self.ck_wayland = QCheckBox("Wine Wayland driver")
        self.ck_wayland.setToolTip("PROTON_ENABLE_WAYLAND=1: no XWayland in between. "
                                   "Newer Protons only; some games misbehave.")
        self.ck_hidraw = QCheckBox("Plain controller input (no hidraw)")
        self.ck_hidraw.setToolTip("PROTON_DISABLE_HIDRAW=1: the game sees an ordinary pad "
                                  "instead of the raw DualSense — try it when buttons or "
                                  "glyphs are wrong")
        lay.addWidget(self.ck_mango, 4, 0, 1, 2)
        lay.addWidget(self.ck_wayland, 4, 2, 1, 2)
        lay.addWidget(self.ck_hidraw, 5, 0, 1, 4)
        self.ck_gs = QCheckBox("Run inside gamescope")
        self.ck_gs.toggled.connect(self._gs_toggled)
        lay.addWidget(self.ck_gs, 6, 0, 1, 4)
        self.cb_res = NoWheelCombo()
        for w_, h_ in self.RES:
            self.cb_res.addItem(f"{w_}×{h_}", (w_, h_))
        self.cb_hz = NoWheelCombo()
        for hz in self.REFRESH:
            self.cb_hz.addItem(f"{hz} Hz", hz)
        self.ck_fsr = QCheckBox("FSR from")
        self.cb_render = NoWheelCombo()
        for w_, h_ in self.RES:
            self.cb_render.addItem(f"{w_}×{h_}", (w_, h_))
        self.ck_full = QCheckBox("Fullscreen")
        self.ck_hdr = QCheckBox("HDR")
        lay.addWidget(self.cb_res, 7, 0, 1, 2)
        lay.addWidget(self.cb_hz, 7, 2, 1, 2)
        lay.addWidget(self.ck_fsr, 8, 0)
        lay.addWidget(self.cb_render, 8, 1, 1, 3)
        lay.addWidget(self.ck_full, 9, 0, 1, 2)
        lay.addWidget(self.ck_hdr, 9, 2, 1, 2)
        self._gs_widgets = (self.cb_res, self.cb_hz, self.ck_fsr, self.cb_render, self.ck_full,
                            self.ck_hdr)
        self.niri = False
        self._gs_toggled(False)

    def _gs_toggled(self, on: bool):
        for w in self._gs_widgets:
            w.setEnabled(on)
        if self.niri:
            self.ck_hdr.setEnabled(False)

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        if i < 0 and value is not None:
            text = (f"{value[0]}×{value[1]}" if isinstance(value, tuple) else
                    f"{value} Hz" if combo.itemText(0).endswith('Hz') else f"{value} fps")
            combo.addItem(text, value)
            i = combo.count() - 1
        combo.setCurrentIndex(max(0, i))

    def set_state(self, perf: dict, tools: dict | None = None, niri: bool = False,
                  lsfg: dict | None = None):
        perf, tools = perf or {}, tools or {}
        self.niri = niri
        self.lsfg = lsfg
        self._select(self.cb_fg, (lsfg or {}).get('multiplier'))
        self.cb_fg.setEnabled(lsfg is not None)
        if lsfg is None:
            self.cb_fg.setToolTip("lsfg-vk isn't set up: install it and point its "
                                  "conf.toml at Lossless Scaling (owned on Steam)")
        self._select(self.cb_fps, perf.get('fps') or 0)
        self._select(self.cb_mode, perf.get('mode') or 'off')
        self._select(self.cb_up, perf.get('upscaler') or 'off')
        model = self.cb_mode.model()
        for i in range(self.cb_mode.count()):
            key = self.cb_mode.itemData(i)
            model.item(i).setEnabled(key == 'off' or tools.get(key, True))
        self.ck_mango.setChecked(bool(perf.get('mangohud')))
        self.ck_mango.setEnabled(tools.get('mangohud', True) or bool(perf.get('mangohud')))
        self.ck_wayland.setChecked(bool(perf.get('wayland')))
        self.ck_hidraw.setChecked(bool(perf.get('no_hidraw')))
        gs = perf.get('gamescope')
        self.ck_gs.setEnabled(tools.get('gamescope', True) or bool(gs))
        self.ck_gs.setChecked(bool(gs))
        gs = gs or {}
        if gs.get('w') and gs.get('h'):
            self._select(self.cb_res, (gs['w'], gs['h']))
        if gs.get('r'):
            self._select(self.cb_hz, gs['r'])
        self.ck_fsr.setChecked(bool(gs.get('fsr')))
        if gs.get('render_w') and gs.get('render_h'):
            self._select(self.cb_render, (gs['render_w'], gs['render_h']))
        self.ck_full.setChecked(bool(gs.get('fullscreen')))
        self.ck_hdr.setChecked(bool(gs.get('hdr')) and not niri)
        self.ck_hdr.setToolTip("niri can't show HDR yet, so gamescope can't pass it on"
                               if niri else "gamescope --hdr-enabled (needs an HDR desktop)")
        self._gs_toggled(self.ck_gs.isChecked())

    def items(self) -> list[str]:
        out = [f"fps={self.cb_fps.currentData() or 0}", f"mode={self.cb_mode.currentData()}",
               f"upscaler={self.cb_up.currentData()}",
               f"mangohud={'on' if self.ck_mango.isChecked() else 'off'}",
               f"wayland={'on' if self.ck_wayland.isChecked() else 'off'}",
               f"hidraw={'off' if self.ck_hidraw.isChecked() else 'on'}"]
        if self.lsfg is not None:
            out.append(f"lsfg={self.cb_fg.currentData() or 'off'}")
        if not self.ck_gs.isChecked():
            return out + ["gamescope=off"]
        w, h = self.cb_res.currentData()
        spec = [f"{w}x{h}@{self.cb_hz.currentData()}"]
        if self.ck_fsr.isChecked():
            rw, rh = self.cb_render.currentData()
            spec += ['fsr', f"render={rw}x{rh}"]
        if self.ck_full.isChecked():
            spec.append('fullscreen')
        if self.ck_hdr.isChecked() and not self.niri:
            spec.append('hdr')
        return out + ["gamescope=" + ','.join(spec)]


class DetailsPanel(QFrame):
    """Everything about one game: Play, stats, integrations, Proton and launch
    options, protongen, maintenance tools and logs. With several tiles selected
    it becomes a batch-actions panel instead.

    Changes are CLI jobs (emitted via `request`) so they land in launch.sh,
    Steam and Heroic exactly as the command line does it."""
    request = pyqtSignal(list, str, bool, str)   # (args, title, needs_steam, kind)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    closed = pyqtSignal()
    toast = pyqtSignal(str, str)
    trailer = pyqtSignal(dict)                   # play a store trailer
    reveal = pyqtSignal(str)                     # show another library game
    notes_changed = pyqtSignal(str, str)         # (slug, text)

    def __init__(self):
        super().__init__()
        self.setObjectName("Details")
        self.setMinimumWidth(340)
        self.setMaximumWidth(440)
        self.game: dict = {}
        self.games: list[dict] = []
        self.info: dict = {}
        self._info_query = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        # ---- single game ----
        page = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack.addWidget(scroll)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        lay.setSpacing(SP['3'])

        hero_wrap = QWidget()
        hw = QGridLayout(hero_wrap)
        hw.setContentsMargins(0, 0, 0, 0)
        self.hero = HeroBanner()
        self.hero.logo_changed.connect(lambda shown: self.name.setVisible(not shown))
        hw.addWidget(self.hero, 0, 0)
        close = icon_button(G['close'], "Close (Esc)", 28, "TileMore")
        close.clicked.connect(self.closed)
        hw.addWidget(close, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        lay.addWidget(hero_wrap)

        self.name = label("", "PageTitle", wrap=True)
        lay.addWidget(self.name)
        # Status that matters right now: source, playing / starting, update.
        self.chips_host = QWidget()
        self.chips = FlowLayout(self.chips_host, margin=0, spacing=SP['1_5'])
        lay.addWidget(self.chips_host)

        prow = QHBoxLayout()
        self.btn_play = QPushButton("Play")
        self.btn_play.setObjectName("PlayBig")
        set_glyph_icon(self.btn_play, G['play'], 18, 'on_accent', hover_role=None)
        self.btn_play.setMinimumHeight(42)
        self.btn_play.clicked.connect(lambda: self.play.emit(self.game))
        prow.addWidget(self.btn_play, 1)
        more = icon_button(G['more'], "Every action for this game", 42, "QuickAction",
                           round_=True)
        more.clicked.connect(self._emit_menu)
        prow.addWidget(more)
        self.btn_more = more
        lay.addLayout(prow)

        # The few things you reach for; everything else is in ⋮ or a section.
        self.quick = QHBoxLayout()
        self.quick.setSpacing(SP['1_5'])
        lay.addLayout(self.quick)

        # The stats grid.
        grid = QGridLayout()
        grid.setHorizontalSpacing(SP['2'])
        grid.setVerticalSpacing(SP['2'])
        self.st_played = StatTile(G['timer'], "Play time")
        self.st_last = StatTile(G['history'], "Last played")
        self.st_size = StatTile(G['storage'], "On disk", 'info')
        self.st_added = StatTile(G['calendar'], "Installed", 'info')
        self.st_proton = StatTile(G['proton'], "Proton", 'warning')
        self.st_rating = StatTile(G['medal'], "ProtonDB", 'warning')
        for i, t in enumerate((self.st_played, self.st_last, self.st_size, self.st_added,
                               self.st_proton, self.st_rating)):
            grid.addWidget(t, i // 2, i % 2)
        self.st_ach = StatTile(G['trophy'], "Achievements", 'success')   # Steam games
        self.st_ach.setVisible(False)
        grid.addWidget(self.st_ach, 3, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        # How far through, when Game-db knows how long the game takes.
        self.progress = QWidget()
        pl_ = QVBoxLayout(self.progress)
        pl_.setContentsMargins(SP['1'], 0, SP['1'], 0)
        pl_.setSpacing(SP['1'])
        self.progress_text = label("", "Muted", wrap=True)
        pl_.addWidget(self.progress_text)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("CardProgress")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setRange(0, 1000)
        pl_.addWidget(self.progress_bar)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        # Inline uninstall confirmation (replaces the old modal message box).
        self.confirm = QFrame()
        self.confirm.setObjectName("DangerBar")
        cl = QVBoxLayout(self.confirm)
        cl.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        self.confirm_text = label("", wrap=True)
        cl.addWidget(self.confirm_text)
        self.confirm_backup = QCheckBox("Back up save-game files first, if any are found")
        self.confirm_backup.setChecked(True)
        cl.addWidget(self.confirm_backup)
        crow = QHBoxLayout()
        crow.addStretch()
        keep = QPushButton("Keep it")
        keep.clicked.connect(lambda: self.confirm.setVisible(False))
        gone = QPushButton("Uninstall")
        gone.setObjectName("DangerSolid")
        gone.clicked.connect(self._do_uninstall)
        crow.addWidget(keep)
        crow.addWidget(gone)
        cl.addLayout(crow)
        self.confirm.setVisible(False)
        lay.addWidget(self.confirm)

        # Compact sections, closed until you need them.
        overview = self._overview_tab()
        lay.addWidget(self.pg_notice)        # protongen drift: always in view
        self.ac_notice = label("", None, wrap=True)
        self.ac_notice.setObjectName("DangerBar")
        self.ac_notice.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        self.ac_notice.setVisible(False)
        lay.addWidget(self.ac_notice)        # online play broken: always in view
        self.sec_launch = Disclosure('launch', "Launch settings", G['tune'], self._launch_tab())
        self.sec_files = Disclosure('files', "Library & files", G['folder'], overview)
        self.sections = QVBoxLayout()
        self.sections.setSpacing(SP['2'])
        self.sections.addWidget(self.sec_launch)
        self.sections.addWidget(self.sec_files)
        lay.addLayout(self.sections)

        # Tuning: ProtonDB, protongen's saved options (for Steam/Heroic games,
        # where Nexus doesn't own the launch options) and recipe suggestions.
        self.tune = QFrame()
        tl = QVBoxLayout(self.tune)
        tl.setContentsMargins(SP['1'], 0, SP['1'], SP['1'])
        tl.setSpacing(SP['1_5'])
        self.tune_pdb = label("", None, wrap=True)
        self.tune_pdb.setOpenExternalLinks(True)
        tl.addWidget(self.tune_pdb)
        self.tune_deal = label("", None, wrap=True)
        self.tune_deal.setOpenExternalLinks(True)
        tl.addWidget(self.tune_deal)
        self.tune_pg_title = label("protongen tuning", "StatLabel")
        tl.addWidget(self.tune_pg_title)
        pgrow = QHBoxLayout()
        self.tune_pg = QLineEdit()
        self.tune_pg.setReadOnly(True)
        pgrow.addWidget(self.tune_pg, 1)
        self.tune_pg_copy = QPushButton("Copy")
        self.tune_pg_copy.setToolTip("Copy, then paste into Steam → Properties → Launch options")
        self.tune_pg_copy.clicked.connect(lambda: self._copy(self.tune_pg.text()))
        pgrow.addWidget(self.tune_pg_copy)
        self.tune_pg_row = QWidget()
        self.tune_pg_row.setLayout(pgrow)
        tl.addWidget(self.tune_pg_row)
        self.tune_sugg_title = label("", "StatLabel")
        tl.addWidget(self.tune_sugg_title)
        self.tune_rows = QVBoxLayout()
        self.tune_rows.setSpacing(SP['1'])
        tl.addLayout(self.tune_rows)
        self.sec_tune = Disclosure('tune', "Tuning", G['wrench'], self.tune)
        self.sec_tune.setVisible(False)
        self.sections.addWidget(self.sec_tune)
        # Compatibility: Steam Deck, anti-cheat and PCGamingWiki's table.
        self.compat_body = QFrame()
        cbl = QVBoxLayout(self.compat_body)
        cbl.setContentsMargins(SP['1'], 0, SP['1'], SP['1'])
        self.compat_text = label("", None, wrap=True)
        self.compat_text.setOpenExternalLinks(True)
        cbl.addWidget(self.compat_text)
        self.sec_compat = Disclosure('compat', "Compatibility", G['shield'], self.compat_body)
        self.sec_compat.setVisible(False)
        self.sections.addWidget(self.sec_compat)
        # About: the store's blurb, reviews, time to beat, trailers, similar games.
        self.about_body = QFrame()
        abl = QVBoxLayout(self.about_body)
        abl.setContentsMargins(SP['1'], 0, SP['1'], SP['1'])
        abl.setSpacing(SP['1_5'])
        self.about_text = label("", None, wrap=True)
        self.about_text.setOpenExternalLinks(True)
        abl.addWidget(self.about_text)
        self.trailer_rows = QVBoxLayout()
        self.trailer_rows.setSpacing(SP['1'])
        abl.addLayout(self.trailer_rows)
        self.similar_title = label("More like this", "StatLabel")
        abl.addWidget(self.similar_title)
        self.similar_rows = QVBoxLayout()
        self.similar_rows.setSpacing(SP['0_5'])
        abl.addLayout(self.similar_rows)
        self.sec_about = Disclosure('about', "About", G['info'], self.about_body)
        self.sec_about.setVisible(False)
        self.sections.addWidget(self.sec_about)
        self._store: dict = {}
        # Notes: yours, saved as you type (a second after you stop).
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Where you left off, settings that worked, "
                                           "a code to remember…")
        self.notes_edit.setFixedHeight(120)
        self._notes_timer = QTimer(self)
        self._notes_timer.setSingleShot(True)
        self._notes_timer.setInterval(1000)
        self._notes_timer.timeout.connect(self._save_notes)
        self.notes_edit.textChanged.connect(self._notes_timer.start)
        self._notes_for = None
        self.sec_notes = Disclosure('notes', "Notes", G['pencil'], self.notes_edit)
        self.sections.addWidget(self.sec_notes)
        self.ach_body = QWidget()
        self.ach_rows = QVBoxLayout(self.ach_body)
        self.ach_rows.setContentsMargins(SP['1'], 0, SP['1'], SP['1'])
        self.ach_rows.setSpacing(SP['1_5'])
        self.sec_ach = Disclosure('achievements', "Achievements", G['trophy'], self.ach_body)
        self.sec_ach.toggled.connect(lambda on: on and self._load_achievements())
        self.sec_ach.setVisible(False)
        self.sections.addWidget(self.sec_ach)
        self._ach_for = None
        self.sec_log = Disclosure('log', "Log", G['log'], self._logs_tab())
        self.sec_log.toggled.connect(lambda on: on and self._load_log())
        self.sections.addWidget(self.sec_log)
        lay.addStretch()
        self._tips_query = None

        # ---- batch (several selected) ----
        batch = QWidget()
        bl = QVBoxLayout(batch)
        bl.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        bl.setSpacing(SP['3'])
        bhead = QHBoxLayout()
        self.batch_title = label("", "PageTitle")
        bhead.addWidget(self.batch_title, 1)
        bclose = icon_button(G['close'], "Clear selection (Esc)", 28, "Ghost")
        bclose.clicked.connect(self.closed)
        bhead.addWidget(bclose)
        bl.addLayout(bhead)
        self.batch_names = label("", "Muted", wrap=True)
        bl.addWidget(self.batch_names)
        for text, args, steam, tip in (
                ("Re-apply settings", ['--apply-launch'], True,
                 "Rewrite launch.sh, Steam and Heroic from each game's settings"),
                ("Add to Steam", ['--steam'], True, "Add a Steam shortcut for each"),
                ("Add to Heroic", ['--heroic'], False, "Add each to Heroic"),
                ("Re-fetch artwork", ['--refetch-art'], False, "Download art again")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, a=args, s=steam, t=text: self._batch(a, t, s))
            bl.addWidget(b)
        bl.addStretch()
        self.stack.addWidget(batch)

    # ---- tabs ----
    def _row(self, lay, title):
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        name = label(title)
        name.setMinimumWidth(78)
        name.setStyleSheet(f"color: {P['muted']};")
        row.addWidget(name, 0, Qt.AlignmentFlag.AlignTop)
        val = label("", wrap=True)
        # Never let a value widen the panel; long ones wrap via soft_wrap().
        val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        val.setMinimumWidth(0)
        row.addWidget(val, 1)
        lay.addLayout(row)
        return row, val

    def _overview_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, SP['3'], 0, 0)
        lay.setSpacing(SP['2_5'])
        r, self.ov_steam = self._row(lay, "Steam")
        self.btn_steam = QPushButton()
        self.btn_steam.clicked.connect(self._toggle_steam)
        r.addWidget(self.btn_steam)
        r, self.ov_heroic = self._row(lay, "Heroic")
        self.btn_heroic = QPushButton()
        self.btn_heroic.clicked.connect(self._toggle_heroic)
        r.addWidget(self.btn_heroic)
        _, self.ov_proton = self._row(lay, "Proton")
        r, self.ov_opts = self._row(lay, "Options")
        self.btn_copy_opts = icon_button(G['copy'], "Copy launch options", 28, "Ghost")
        self.btn_copy_opts.clicked.connect(
            lambda: self._copy(self.game.get('launch_options') or ''))
        r.addWidget(self.btn_copy_opts, 0, Qt.AlignmentFlag.AlignTop)
        self.ov_opts.setStyleSheet(f"font-size: {FS['xs']}px; color: {P['subtext']};")
        r, self.ov_folder = self._row(lay, "Folder")
        self.btn_copy_folder = icon_button(G['copy'], "Copy folder path", 28, "Ghost")
        self.btn_copy_folder.clicked.connect(lambda: self._copy(self._folder))
        r.addWidget(self.btn_copy_folder, 0, Qt.AlignmentFlag.AlignTop)
        self._folder = ''

        # protongen drift notice with the fix right there.
        self.pg_notice = QFrame()
        self.pg_notice.setObjectName("Card")
        pl = QVBoxLayout(self.pg_notice)
        pl.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        self.pg_notice_text = label("", wrap=True)
        pl.addWidget(self.pg_notice_text)
        prow = QHBoxLayout()
        prow.addStretch()
        pull = QPushButton("Pull from protongen")
        pull.clicked.connect(self._pull_protongen)
        prow.addWidget(pull)
        pl.addLayout(prow)
        self.pg_notice.setVisible(False)
        lay.addStretch()
        return w

    def _launch_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, SP['3'], 0, 0)
        lay.setSpacing(SP['2'])
        lay.addWidget(label("Name", "StatLabel"))
        nrow = QHBoxLayout()
        self._dirty: set[str] = set()
        self.ed_name = QLineEdit()
        self.ed_name.textEdited.connect(self._name_edited)
        self.ed_name.returnPressed.connect(self._rename)
        rename = QPushButton("Rename")
        rename.clicked.connect(self._rename)
        nrow.addWidget(self.ed_name, 1)
        nrow.addWidget(rename)
        lay.addLayout(nrow)

        lay.addWidget(label("Executable", "StatLabel"))
        erow = QHBoxLayout()
        self.ed_exe = QLineEdit()
        self.ed_exe.setReadOnly(True)
        change = QPushButton("Change…")
        change.clicked.connect(self._pick_exe)
        erow.addWidget(self.ed_exe, 1)
        erow.addWidget(change)
        lay.addLayout(erow)

        lay.addWidget(label("Proton — used by launch.sh, Steam and Heroic", "StatLabel"))
        prow = QHBoxLayout()
        self.cb_proton = NoWheelCombo()
        self.cb_proton.activated.connect(self._proton_picked)
        apply_p = QPushButton("Apply")
        apply_p.clicked.connect(self._apply_proton)
        prow.addWidget(self.cb_proton, 1)
        prow.addWidget(apply_p)
        lay.addLayout(prow)

        lay.addWidget(label("Launch options", "StatLabel"))
        self.ed_opts = QLineEdit()
        self.ed_opts.setPlaceholderText("PROTON_ENABLE_WAYLAND=1 mangohud %command%")
        self.ed_opts.setToolTip("A Steam launch-options string, or paste a protongen:v1:… "
                                "preset code. Enter applies it.")
        self.ed_opts.textEdited.connect(self._opts_edited)
        self.ed_opts.returnPressed.connect(self._apply_options)
        lay.addWidget(self.ed_opts)
        self.opts_state = label("", "Muted", wrap=True)
        lay.addWidget(self.opts_state)
        orow = QHBoxLayout()
        orow.addStretch()
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: (self._dirty.discard('opts'), self._emit(
            ['--set-launch', self.slug, ''], "Clearing launch options", True)))
        apply_o = QPushButton("Apply options")
        apply_o.setObjectName("Primary")
        apply_o.clicked.connect(self._apply_options)
        orow.addWidget(clear)
        orow.addWidget(apply_o)
        lay.addLayout(orow)

        self.ck_via_steam = QCheckBox("Launch through Steam")
        self.ck_via_steam.setToolTip("Play starts the game's Steam shortcut: Steam Input, the "
                                     "overlay, recording and your Steam status. Nexus's own "
                                     "extras (Discord, stay-awake, session tuning) then don't run.")
        self.ck_via_steam.clicked.connect(lambda on: self._emit(
            ['--launch-via', self.slug, 'steam' if on else 'nexus'],
            "Launching through Steam" if on else "Launching from Nexus", False))
        lay.addWidget(self.ck_via_steam)

        lay.addWidget(label("Performance", "StatLabel"))
        self.perf = PerfPanel()
        lay.addWidget(self.perf)
        prow2 = QHBoxLayout()
        prow2.addStretch()
        apply_perf = QPushButton("Apply performance")
        apply_perf.setToolTip("Writes these into the launch options above "
                              "(launch.sh, Steam and Heroic)")
        apply_perf.clicked.connect(lambda: self._emit(
            ['--perf', self.slug, *self.perf.items()], "Applying performance settings",
            bool(self.game.get('steam'))))
        prow2.addWidget(apply_perf)
        lay.addLayout(prow2)

        lay.addWidget(label("protongen", "StatLabel"))
        self.pg_state = label("", "Muted", wrap=True)
        lay.addWidget(self.pg_state)
        self.pg_opts = QLineEdit()
        self.pg_opts.setReadOnly(True)
        lay.addWidget(self.pg_opts)
        grow = QHBoxLayout()
        grow.addStretch()
        self.btn_pg_open = QPushButton("Open protongen")
        self.btn_pg_open.setToolTip("Copies this game's name, so you can find it there")
        self.btn_pg_open.clicked.connect(self._open_protongen)
        self.btn_pg_pull = QPushButton("Pull tuning")
        self.btn_pg_pull.clicked.connect(self._pull_protongen)
        self.btn_pg_default = QPushButton("Use defaults")
        self.btn_pg_default.clicked.connect(self._protongen_default)
        grow.addWidget(self.btn_pg_open)
        grow.addWidget(self.btn_pg_default)
        grow.addWidget(self.btn_pg_pull)
        lay.addLayout(grow)
        lay.addStretch()
        return w

    def _logs_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, SP['3'], 0, 0)
        row = QHBoxLayout()
        self.cb_log = NoWheelCombo()
        self.cb_log.addItem("Last launch", 'launch_log')
        self.cb_log.addItem("Install log", 'install_log')
        self.cb_log.currentIndexChanged.connect(self._load_log)
        row.addWidget(self.cb_log, 1)
        reload_b = icon_button(G['refresh'], "Reload", 30)
        reload_b.clicked.connect(self._load_log)
        folder = icon_button(G['folder'], "Open logs folder", 30)
        folder.clicked.connect(lambda: self.info.get('install_log') and
                               QDesktopServices.openUrl(QUrl.fromLocalFile(
                                   str(Path(self.info.get('install_log')).parent))))
        row.addWidget(reload_b)
        row.addWidget(folder)
        self.btn_explain = QPushButton("Explain with local AI")
        self.btn_explain.setToolTip("Uses the local model configured in protongen; the log "
                                    "never leaves this machine")
        self.btn_explain.setVisible(False)
        self.btn_explain.clicked.connect(lambda: self._emit(
            ['--explain-log', self.slug], "Explaining the log", False, 'task'))
        row.addWidget(self.btn_explain)
        lay.addLayout(row)
        self.bench_text = label("", "Muted", wrap=True)      # benchmark runs, when any
        self.bench_text.setVisible(False)
        lay.addWidget(self.bench_text)
        self.logview = QPlainTextEdit()
        self.logview.setObjectName("Console")
        self.logview.setReadOnly(True)
        self.logview.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.logview.setMinimumHeight(220)
        lay.addWidget(self.logview, 1)
        return w

    # ---- data ----
    @property
    def slug(self):
        return self.game.get('slug')

    def show_games(self, games: list[dict]):
        self.games = games
        if len(games) > 1:
            self.batch_title.setText(f"{len(games)} games selected")
            self.batch_names.setText(", ".join(g.get('display_name', g['slug'])
                                               for g in games))
            self.stack.setCurrentIndex(1)
            return
        self.stack.setCurrentIndex(0)
        same = games and games[0].get('slug') == self.slug
        self.game = games[0] if games else {}
        if not same:
            self.confirm.setVisible(False)
            self.info = {}
            self._dirty = set()                 # edits belong to the game they were made on
        if not same:
            if self._notes_timer.isActive():        # keep what was typed for the last game
                self._notes_timer.stop()
                self._save_notes()
            self.notes_edit.blockSignals(True)
            self.notes_edit.setPlainText(self.game.get('notes') or '')
            self.notes_edit.blockSignals(False)
            self._notes_for = self.slug
            self.sec_notes.set_summary((self.game.get('notes') or '').strip().split('\n')[0][:40])
        self._fill()
        self._refresh_info()
        if not same:
            self._load_tips()

    def _load_tips(self):
        self.sec_tune.setVisible(False)
        self.sec_compat.setVisible(False)
        self.sec_about.setVisible(False)
        self.ac_notice.setVisible(False)
        self._store, self._tips_hltb = {}, None
        if self.slug:
            slug = self.slug

            def got(info, _err, slug=slug):
                if slug == self.slug:
                    try:
                        self._fill_about(info or {})
                    except RuntimeError:
                        pass
            cli_json(['--store-info', slug, '--json'], got, 60000, ttl=600)
        self.progress.setVisible(False)
        slug = self.slug
        if not slug:
            return

        def done(info, _err, slug=slug):
            if slug != self.slug:
                return
            try:
                self._fill_tips(info or {})
            except RuntimeError:
                pass
        self._tips_query = cli_json(['--game-tips', slug, '--json'], done, 60000, ttl=300)

    def _fill_tips(self, info):
        g = self.game
        external = g.get('source', 'nexus') != 'nexus'
        pdb = info.get('protondb')
        if pdb and pdb.get('tier'):
            color = TIER_COLORS.get(pdb['tier'], P['muted'])
            self.tune_pdb.setText(
                f"ProtonDB: <b style='color:{color}'>{html.escape(str(pdb['tier']).capitalize())}</b>"
                + (f" · trending {html.escape(str(pdb['trending']))}"
                   if pdb.get('trending') not in (None, pdb['tier']) else "")
                + f" · {int(pdb.get('total') or 0)} reports · "
                  f"<a href='{html.escape(pdb['url'])}' style='color:{P['accent']}'>open</a>")
        elif pdb:
            self.tune_pdb.setText("ProtonDB: no reports yet")
        self.tune_pdb.setVisible(bool(pdb))
        deal = self._gamedb_line(info.get('gamedb'))
        self.tune_deal.setText(deal)
        self.tune_deal.setVisible(bool(deal))
        pg = info.get('protongen')
        show_pg = bool(pg) and external
        self.tune_pg_title.setVisible(show_pg)
        self.tune_pg_row.setVisible(show_pg)
        if show_pg:
            self.tune_pg.setText(pg['launch_options'])
            self.tune_pg.setCursorPosition(0)
        clear_layout(self.tune_rows)
        recipes = info.get('recipes', [])
        self.tune_sugg_title.setText(f"Suggestions for {info.get('gpu') or 'your GPU'}"
                                     if recipes else "")
        for i, r in enumerate(recipes):
            row = QHBoxLayout()
            name = plain_label(r['name'])
            name.setToolTip((r.get('symptom') and f"Fixes: {r['symptom']}\n" or '')
                            + r.get('description', '') + "\n\n" + r['launch_options'])
            row.addWidget(name, 1)
            if r.get('kind') == 'fix':
                row.addWidget(label("fix", "Muted"))
            if external:
                b = QPushButton("Copy")
                b.clicked.connect(lambda _=False, t=r['launch_options']: self._copy(t))
            else:
                b = QPushButton("Apply")
                b.setToolTip("Adds this to the game's launch options (Steam, Heroic and "
                             "launch.sh)")
                b.clicked.connect(lambda _=False, n=r['name']: self._emit(
                    ['--apply-recipe', self.slug, n], f"Applying '{n}'",
                    bool(self.game.get('steam'))))
            row.addWidget(b)
            row.setContentsMargins(0, 0, 0, 0)
            w = QWidget()
            w.setLayout(row)
            self.tune_rows.addWidget(w)
            w.setVisible(i < 5)
        if len(recipes) > 5:
            more = QPushButton(f"Show all {len(recipes)} suggestions")
            more.setObjectName("Ghost")

            def expand(_=False, m=more):
                for j in range(self.tune_rows.count()):
                    wid = self.tune_rows.itemAt(j).widget()
                    if wid is not None:
                        wid.setVisible(True)
                m.setVisible(False)
            more.clicked.connect(expand)
            self.tune_rows.addWidget(more)
        self.btn_explain.setVisible(bool(info.get('llm')) and not external)
        lines, more = compat_facts(info)
        self.compat_text.setText(more)
        self.sec_compat.set_summary(lines[0][0] if lines else "")
        self.sec_compat.setVisible(bool(more))
        warn = anticheat_warning(info)
        self.ac_notice.setText(warn or '')
        self.ac_notice.setVisible(bool(warn))
        has_tune = bool(pdb or show_pg or recipes or deal)
        self.sec_tune.setVisible(has_tune)
        self.sec_tune.set_summary(f"{len(recipes)} suggestion{'s' if len(recipes) != 1 else ''}"
                                  if recipes else '')
        if pdb and pdb.get('tier'):
            t = pdb['tier']
            self.st_rating.set(t.capitalize(), f"ProtonDB · {pdb.get('total') or 0} reports",
                               TIER_COLORS.get(t))
        hl = ((info.get('gamedb') or {}).get('hltb') or {}) or (info.get('hltb') or {})
        self._tips_hltb = info.get('hltb') or (hl if hl.get('main') else None)
        if self._store or self._tips_hltb:
            self._fill_about()
        main = hl.get('main')
        played = (self.game.get('playtime_minutes') or 0) / 60
        if main and info.get('gamedb', {}).get('list') != 'wishlist':
            frac = min(1.0, played / main) if main else 0
            self.progress_text.setText(f"{fmt_hours(int(played * 60)) or '0 h'} of about "
                                       f"{main:g} h to beat the main story (HowLongToBeat)")
            self.progress_bar.setValue(int(frac * 1000))
            self.progress.setVisible(True)
        else:
            self.progress.setVisible(False)

    def _save_notes(self):
        if self._notes_for:
            text = self.notes_edit.toPlainText()[:MAX_NOTE]
            if self.game.get('slug') == self._notes_for:
                self.game['notes'] = text
            self.notes_changed.emit(self._notes_for, text)

    def _fill_about(self, info: dict | None = None):
        if info is not None:
            self._store = info
        info = self._store or {}
        st = info.get('store') or {}
        self.about_text.setText(about_html(st, info.get('reviews'), self._tips_hltb))
        clear_layout(self.trailer_rows)
        for t in (st.get('trailers') or [])[:3]:
            b = QPushButton(t.get('name') or "Trailer")
            b.setObjectName("Ghost")
            set_glyph_icon(b, G['play'], 14, 'accent')
            b.setToolTip("Play the trailer here")
            b.setStyleSheet("text-align: left;")
            b.clicked.connect(lambda _=False, t=t: self.trailer.emit(
                {**t, 'game': self.game.get('display_name', '')}))
            self.trailer_rows.addWidget(b)
        clear_layout(self.similar_rows)
        similar = (info.get('similar') or [])[:6]
        self.similar_title.setVisible(bool(similar))
        for s_ in similar:
            if s_.get('owned_slug'):
                b = QPushButton(f"{s_['name']}  ·  in your library")
                b.setObjectName("Ghost")
                b.setStyleSheet("text-align: left;")
                b.clicked.connect(lambda _=False, sl=s_['owned_slug']: self.reveal.emit(sl))
                self.similar_rows.addWidget(b)
            elif str(s_.get('url', '')).startswith('https://store.steampowered.com/'):
                lab = label(f"<a href='{html.escape(s_['url'])}' style='color:{P['text']}'>"
                            f"{html.escape(s_['name'])}</a>", None)
                lab.setOpenExternalLinks(True)
                self.similar_rows.addWidget(lab)
        has = bool(st or info.get('reviews') or similar or self._tips_hltb)
        self.sec_about.setVisible(has)
        rv = info.get('reviews') or {}
        self.sec_about.set_summary(rv.get('desc') or ', '.join((st.get('genres') or [])[:2]))

    @staticmethod
    def _gamedb_line(gd) -> str:
        """Rich-text Game-db line: wishlist price, or HLTB/progress for owned
        games, or the release countdown. Every value is escaped; only https
        links are made clickable."""
        if not gd:
            return ''
        esc = html.escape
        bits = []
        price = gd.get('price') or {}
        if gd.get('list') == 'wishlist':
            if price:
                p = f"<b>{esc(price.get('currentStr') or '')}</b>"
                if price.get('cutPct'):
                    p += f" −{int(price['cutPct'])}%"
                bits.append(f"On your wishlist: {p}")
                if price.get('preorder'):
                    bits.append("pre-order")
                elif price.get('atHistoricalLow'):
                    bits.append(f"<span style='color:{P['success']}'>all-time low</span>")
                elif price.get('historicalLowStr'):
                    bits.append(f"low {esc(price['historicalLowStr'])}")
                url = gamedb.safe_url((price.get('best') or {}).get('url'))
                if url:
                    bits.append(f"<a href='{esc(url)}' style='color:{P['accent']}'>open deal</a>")
            else:
                bits.append("On your wishlist")
            rel = gd.get('release')
            if rel:
                bits.append(esc(gamedb.release_label(rel)))
        else:
            hl = gd.get('hltb') or {}
            if hl.get('main'):
                bits.append(f"HLTB {hl['main']:g} h main")
            c = gd.get('completion') or {}
            if c.get('state') == 'in-progress':
                bits.append(f"{int(c.get('fraction', 0) * 100)}% through")
            elif c.get('label'):
                bits.append(esc(c['label']))
        if not bits:
            return ''
        page = gamedb.safe_url(gd.get('url'), gd.get('url', '').rsplit('/game/', 1)[0])
        head = (f"<a href='{esc(page)}' style='color:{P['accent']}'>Game-db</a>"
                if page else "Game-db")
        ago = fmt_ago(gd.get('fetched_at'))
        return f"{head}: " + " · ".join(bits) + (
            f" <span style='color:{P['muted']}'>({ago})</span>" if ago else '')

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.toast.emit("Copied — paste it into Steam → Properties → Launch options."
                        if self.game.get('source') == 'steam' else "Copied.", 'success')

    def _emit_menu(self):
        if self.game:
            self.menu.emit(self.game, self.btn_more.mapToGlobal(
                QPoint(0, self.btn_more.height())))

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu:                 # X, from anywhere in the panel
            self._emit_menu()
        else:
            super().keyPressEvent(ev)

    def _fill(self):
        g = self.game
        if not g:
            return
        self.hero.set_game(g)
        self.name.setText(g.get('display_name', g['slug']))
        self.name.setVisible(not self.hero.has_logo())     # the logo already names it
        src = g.get('source', 'nexus')
        while self.chips.count():
            it = self.chips.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for text, role, on in (
                (SOURCE_LABEL.get(src, ''), None, True),
                ("Not installed", 'muted', not is_installed(g)),
                ("Playing now", 'success', g.get('playing')),
                ("Starting…", 'accent', g.get('launching') and not g.get('playing')),
                ("Update ready", 'warning', g.get('update_pending')
                 and not (g.get('repack_online') and not g.get('repack_update'))),
                ("Update posted", 'warning', g.get('repack_online') and not g.get('repack_update')),
                ("Also in " + " & ".join(SOURCE_LABEL.get(x, x) for x in g.get('dup_also') or []),
                 None, g.get('dup_also')),
                ("Custom launch options", 'accent', g.get('launch_options'))):
            if on and text:
                self.chips.addWidget(_pill(text, role or 'muted'))
        self.chips_host.setVisible(self.chips.count() > 0)
        external = src != 'nexus'
        for sec in (self.sec_launch, self.sec_files, self.sec_log):
            sec.setVisible(not external)
        if external:
            self.confirm.setVisible(False)
        self._fill_quick(g)
        self.btn_play.setText(play_label(g))
        self.btn_play.setEnabled(play_enabled(g))
        self._fill_stats(g)
        self.sec_launch.set_summary(g.get('proton') or '')
        self.sec_files.set_summary(" · ".join(x for x in (
            "Steam" if g.get('steam') else '', "Heroic" if g.get('heroic') else '') if x))
        self.ov_steam.setText("In your Steam library" if g.get('steam') else "Not added")
        self.btn_steam.setText("Remove" if g.get('steam') else "Add")
        self.ov_heroic.setText("In your Heroic library" if g.get('heroic') else "Not added")
        self.btn_heroic.setText("Remove" if g.get('heroic') else "Add")
        self.ov_proton.setText(g.get('proton') or "default")
        opts = g.get('launch_options') or ''
        self.ov_opts.setText(soft_wrap(opts) or "none (Steam/Heroic keep their own)")
        self.btn_copy_opts.setVisible(bool(opts))
        exe = g.get('exe')
        self._folder = str(Path(exe).parent) if exe else (g.get('wine_prefix') or '')
        self.ov_folder.setText(soft_wrap(self._folder) or '—')
        self.btn_copy_folder.setVisible(bool(self._folder))
        # A refresh (library reload, a game starting…) must not throw away
        # what's being typed: edited fields keep their text until applied.
        if 'name' not in self._dirty and not self.ed_name.hasFocus():
            self.ed_name.setText(g.get('display_name', ''))
        self.ed_exe.setText(exe or '')
        if 'opts' not in self._dirty and not self.ed_opts.hasFocus():
            self.ed_opts.setText(g.get('launch_options') or '')

    def _fill_quick(self, g):
        """Round buttons for the handful of things you'd actually do here.
        Rebuilt only when the set of actions changes: a refresh (or pressing
        the star with a pad) keeps the buttons — and the focus on them."""
        src = g.get('source', 'nexus')
        fav = bool(g.get('favorite'))
        acts = [(G['star'] if fav else G['star_outline'],
                 "Remove from favourites" if fav else "Add to favourites", 'favorite')]
        if not is_installed(g):
            if src == 'steam':
                acts.append((G['steam'], "Open in Steam", 'steam-page'))
        elif src == 'steam':
            acts += [(G['steam'], "Open in Steam", 'steam-page'),
                     (G['settings'], "Steam properties", 'steam-props'),
                     (G['wrench'], "Tune in protongen", 'protongen')]
        elif src == 'nexus':
            acts += [(G['tune'], "Launch settings", '__launch'),
                     (G['log'], "Log", '__log')]
        if is_installed(g):
            acts.append((G['folder'], "Open game folder", 'open-folder'))
        acts.append((G['image'], "Change artwork", 'artwork'))
        keys = tuple(a[2] for a in acts)
        if keys == getattr(self, '_quick_keys', None):
            star = self._quick_btns.get('favorite')
            if star is not None and bool(star.property('on')) != fav:
                star.setText(acts[0][0])
                star.setToolTip(acts[0][1])
                star.setProperty('on', fav)
                star.style().unpolish(star)
                star.style().polish(star)
            return
        clear_layout(self.quick)
        self._quick_keys, self._quick_btns = keys, {}
        for glyph, tip, act in acts:
            b = icon_button(glyph, tip, 38, "QuickAction", round_=True)
            self._quick_btns[act] = b
            if act == 'favorite' and fav:
                b.setProperty('on', True)
            if act == '__launch':
                b.clicked.connect(lambda: self.open_section('launch'))
            elif act == '__log':
                b.clicked.connect(lambda: self.open_section('log'))
            else:
                b.clicked.connect(lambda _=False, a=act: self._act(a))
            self.quick.addWidget(b)
        self.quick.addStretch()

    def _fill_stats(self, g):
        mins = g.get('playtime_minutes') or 0
        self.st_played.set(fmt_hours(mins) or "0 h", "Play time")
        last = g.get('last_played')
        self.st_last.set(fmt_relative(last) or "Never", "Last played",
                         tip=last.replace('T', ' ') if last else '')
        if is_installed(g):
            self.st_size.set(fmt_size(g.get('disk_bytes')) or "—", "On disk")
            added = g.get('added_at')
            self.st_added.set(fmt_relative(added) or "—", "Installed",
                              tip=added.replace('T', ' ') if added else '')
        else:
            self.st_size.set(fmt_size(g.get('download_bytes')) or "—", "Download")
            self.st_added.set(play_label(g) if g.get('installing') else "No", "Installed")
        proton = (self.info.get('proton') if self.info else None) or g.get('proton')
        src = g.get('source', 'nexus')
        self.st_proton.set(proton or ("Steam's choice" if src == 'steam' else "Default"),
                           "Proton")
        ach = g.get('achievements')
        self.st_ach.setVisible(bool(ach))
        self.sec_ach.setVisible(bool(ach))
        if ach:
            done, total = ach
            self.st_ach.set(f"{done} of {total}", f"Achievements · {round(100 * done / total)}%",
                            P['success'] if done == total else None)
            self.sec_ach.set_summary(f"{done} / {total}")
            if self.sec_ach.is_open and self._ach_for != (g['slug'], done):
                self._load_achievements()
        tier = g.get('protondb')
        if tier in TIER_COLORS:
            self.st_rating.set(tier.capitalize(), "ProtonDB", TIER_COLORS[tier])
        else:
            self.st_rating.set(SOURCE_LABEL.get(src, ''), "Library" if src != 'steam'
                               else "ProtonDB")

    def _load_achievements(self):
        """The list, from Steam's stats cache via the CLI — only once the
        section is opened (and again when the count changes)."""
        g = self.game
        if not g.get('achievements') or g.get('source', 'nexus') not in ('steam', 'nexus'):
            return
        self._ach_for = (g['slug'], g['achievements'][0])
        clear_layout(self.ach_rows)
        self.ach_rows.addWidget(label("Loading…", "Muted"))
        slug = g['slug']

        def done(data, err):
            if self.game.get('slug') != slug:
                return
            self._fill_achievements(data or {}, err)
        ident = str(g['appid']) if g.get('source') == 'steam' else g['slug']
        cli_json(['--achievements', ident, '--json'], done, 30000, ttl=120)

    ACH_SHOWN = 60

    def _fill_achievements(self, data, err):
        clear_layout(self.ach_rows)
        items = data.get('items') or []
        if not items:
            self.ach_rows.addWidget(label(err or "Steam hasn't recorded any yet.", "Muted"))
            return
        for a in items[:self.ACH_SHOWN]:
            self.ach_rows.addWidget(AchievementRow(a))
        if len(items) > self.ACH_SHOWN:
            self.ach_rows.addWidget(label(f"…and {len(items) - self.ACH_SHOWN} more", "Muted"))

    def open_section(self, key):
        sec = {'launch': self.sec_launch, 'files': self.sec_files, 'tune': self.sec_tune,
               'log': self.sec_log, 'achievements': self.sec_ach,
               'compat': self.sec_compat, 'about': self.sec_about,
               'notes': self.sec_notes}.get(key)
        if sec is None or sec.isHidden():
            return
        sec.set_open(True)
        QTimer.singleShot(0, lambda: alive(sec.head) and focus_and_reveal(sec.head))

    def _refresh_info(self):
        slug = self.slug
        if not slug or self.game.get('source', 'nexus') != 'nexus':
            return
        # Selection syncs often (every library refresh); one query per game
        # every few seconds is plenty — a finished job refreshes it anyway.
        last = getattr(self, '_info_at', (None, 0.0))
        if last[0] == slug and time.monotonic() - last[1] < 5 and self.info:
            return
        self._info_at = (slug, time.monotonic())

        def done(info, err, slug=slug):
            if slug != self.slug:
                return
            try:
                self._apply_info(info or {}, err)
            except RuntimeError:
                pass
        self._info_query = cli_json(['--show-launch', slug, '--json'], done)

    def _apply_info(self, info, err):
        self.info = info
        pinned = info.get('proton_pinned')
        items = [(f"Default ({info.get('proton') if not pinned else 'global'})", '')] \
            + [(n, n) for n in info.get('protons', [])]
        choice = self.cb_proton.currentData() if 'proton' in self._dirty else None
        if items != [(self.cb_proton.itemText(i), self.cb_proton.itemData(i))
                     for i in range(self.cb_proton.count())]:
            # Rebuilt only when the list changed: a refresh doesn't close an
            # open popup or drop a pick that hasn't been applied yet.
            self.cb_proton.blockSignals(True)
            self.cb_proton.clear()
            for text, data in items:
                self.cb_proton.addItem(text, data)
            self.cb_proton.blockSignals(False)
        want = choice if choice is not None else (pinned or '')
        self.cb_proton.setCurrentIndex(max(0, self.cb_proton.findData(want)))
        if info.get('proton'):
            self.ov_proton.setText(info['proton'] + ("  (pinned for this game)" if pinned else ""))
        if info.get('exe'):
            self.ed_exe.setText(info['exe'])
        prof = info.get('profile') or {}
        self.perf.set_state(info.get('perf') or {}, info.get('perf_tools'), info.get('niri'),
                            info.get('lsfg'))
        self.ck_via_steam.setChecked(info.get('launch_via') == 'steam')
        self.ck_via_steam.setEnabled(bool(info.get('in_steam')) or self.ck_via_steam.isChecked())
        if not info.get('in_steam'):
            self.ck_via_steam.setToolTip("Add the game to Steam first (⋮ → In Steam)")
        self.opts_state.setText(
            f"Managed here · from {prof.get('source', '?')}" if info.get('owns_options') else
            "Not managed yet: Steam and Heroic keep their own options until you apply some here.")
        pg = info.get('protongen')
        pd = info.get('protongen_default')
        self.btn_pg_default.setEnabled(bool(pd) and not pd.get('applied'))
        self.btn_pg_default.setToolTip(
            "No default profile in protongen yet (Settings → Save as default profile)"
            if not pd else "This game already uses your protongen defaults" if pd.get('applied')
            else f"Replace this game's options with protongen's default profile:\n"
                 f"{pd.get('launch_options') or '(no options)'}"
                 + (f"\nProton: {pd['proton']}" if pd.get('proton') else ''))
        self.btn_pg_open.setEnabled(bool(info.get('protongen_installed')))
        if not info.get('protongen_installed'):
            self.pg_state.setText("protongen isn't installed.")
            self.pg_opts.setVisible(False)
            self.btn_pg_pull.setEnabled(False)
            self.pg_notice.setVisible(False)
        elif pg:
            state = "differs from the options here" if pg.get('differs') else "in sync"
            self.pg_state.setText(f"Has tuning for this game (its {pg['source']} entry) — {state}.")
            self.pg_opts.setText(pg.get('launch_options') or '(no options)')
            self.pg_opts.setCursorPosition(0)
            self.pg_opts.setVisible(True)
            self.btn_pg_pull.setEnabled(bool(pg.get('differs')))
            self.pg_notice.setVisible(bool(pg.get('differs')))
            self.pg_notice_text.setText("protongen has different tuning for this game. Pull it "
                                        "in to use it in Steam, Heroic and launch.sh.")
        else:
            self.pg_state.setText(
                "No tuning saved for this game in protongen. Use your protongen defaults, "
                "or tune it there (via its Steam or Heroic entry) and pull it in." if pd else
                "No tuning saved for this game yet — tune it in protongen "
                "(via its Steam or Heroic entry), then pull it in.")
            self.pg_opts.setVisible(False)
            self.btn_pg_pull.setEnabled(False)
            self.pg_notice.setVisible(False)
        if self.sec_log.is_open:
            self._load_log()
        if self.game:
            self._fill_stats(self.game)

    def _load_bench(self):
        slug = self.slug
        if not slug or self.game.get('source', 'nexus') != 'nexus':
            self.bench_text.setVisible(False)
            return

        def done(data, _err, slug=slug):
            if slug != self.slug:
                return
            runs = (data or {}).get('runs') or []
            self.bench_text.setText("<b>Benchmarks</b><br>" + "<br>".join(
                f"{html.escape(fmt_relative(r['at']) or r['at'])}: avg {r['avg_fps']:g} fps · "
                f"1% low {r['low_1']:g} · 0.1% low {r['low_01']:g} · {r['seconds']:g} s"
                for r in runs[:3]))
            self.bench_text.setVisible(bool(runs))
        cli_json(['--bench-results', slug, '--json'], done, 30000, ttl=30)

    def _load_log(self):
        self._load_bench()
        path = self.info.get(self.cb_log.currentData())
        if not path:
            self.logview.setPlainText("Nothing logged yet." if self.info else "Loading…")
            return
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            self.logview.setPlainText(f"Couldn't read {path}: {e}")
            return
        if len(data) > 2_000_000:
            data = b"[... earlier output truncated ...]\n" + data[-2_000_000:]
        self.logview.setPlainText(data.decode('utf-8', 'replace'))
        self.logview.moveCursor(self.logview.textCursor().MoveOperation.End)

    # ---- actions ----
    def _emit(self, args, title, needs_steam, kind='task'):
        name = self.game.get('display_name', self.slug)
        self.request.emit(args, f"{title} — {name}", needs_steam, kind)

    def _act(self, name):
        self.request.emit(['__action__', name], '', False, 'action')

    def _toggle_steam(self):
        on = self.game.get('steam')
        self._emit(['--remove-steam' if on else '--steam', self.slug],
                   "Removing from Steam" if on else "Adding to Steam", True)

    def _toggle_heroic(self):
        on = self.game.get('heroic')
        self._emit(['--remove-heroic' if on else '--heroic', self.slug],
                   "Removing from Heroic" if on else "Adding to Heroic", False)

    def _name_edited(self, _text):
        self._dirty.add('name')

    def _opts_edited(self, _text):
        self._dirty.add('opts')

    def _proton_picked(self, _i):
        self._dirty.add('proton')

    def _rename(self):
        name = self.ed_name.text().strip()
        self._dirty.discard('name')
        if name and name != self.game.get('display_name'):
            self._emit(['--rename', self.slug, name], "Renaming", bool(self.game.get('steam')))

    def _pick_exe(self):
        start = self.ed_exe.text() or self.game.get('wine_prefix') or str(HOME)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the game executable",
            str(Path(start).parent if start.endswith('.exe') else start),
            "Windows executables (*.exe)")
        if path:
            self._emit(['--set-exe', self.slug, path], "Changing executable",
                       bool(self.game.get('steam')))

    def _apply_proton(self):
        data = self.cb_proton.currentData()
        if data is None:
            return
        self._dirty.discard('proton')
        self._emit(['--proton', self.slug, data or 'default'], "Switching Proton",
                   bool(self.game.get('steam')))

    def _apply_options(self):
        text = self.ed_opts.text().strip()
        if not text:
            self.toast.emit("Enter launch options, or use Clear to remove them.", 'warn')
            return
        preview = protongen_code_preview(text)
        if preview is not None:          # someone else's preset: show what it runs first
            ask(self, "Apply this preset?", preview, "Apply",
                lambda: self._send_options(text), danger=False)
            return
        self._send_options(text)

    def _send_options(self, text):
        self._dirty.discard('opts')
        self._emit(['--set-launch', self.slug, text], "Applying launch options",
                   bool(self.game.get('steam')))

    def _pull_protongen(self):
        self._emit(['--from-protongen', self.slug], "Pulling protongen tuning",
                   bool(self.game.get('steam')))

    def _protongen_default(self):
        self._emit(['--protongen-default', self.slug], "Applying protongen defaults",
                   bool(self.game.get('steam')))

    def _open_protongen(self):
        self._act('protongen')

    def ask_uninstall(self):
        name = self.game.get('display_name', self.slug)
        self.confirm_text.setText(
            f"Uninstall {name}? This deletes its install folder and removes it from Steam, "
            f"Heroic and the app menu. Logs are kept.")
        self.confirm.setVisible(True)
        page = self.stack.currentWidget()
        if isinstance(page, QScrollArea):
            QTimer.singleShot(0, lambda: alive(page, self.confirm)
                              and page.ensureWidgetVisible(self.confirm))

    def _do_uninstall(self):
        self.confirm.setVisible(False)
        args = ['--uninstall', self.slug, '--yes']
        if self.confirm_backup.isChecked():
            args.append('--save-backup')
        self._emit(args, "Uninstalling", bool(self.game.get('steam')))

    def _batch(self, flag, title, steam):
        for g in self.games:
            self.request.emit([*flag, g['slug']], f"{title} — {g.get('display_name', g['slug'])}",
                              steam and (flag[0] != '--apply-launch' or bool(g.get('steam'))),
                              'task')


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .actions import about_html  # noqa: E402
from .app import main  # noqa: E402
from .art import (ART, art_source, fit_image, HERO_KINDS, LOGO_KINDS, placeholder_pixmap,
    rounded_pixmap)  # noqa: E402
from .dashboard import ClickRow  # noqa: E402
from .deals import _pill  # noqa: E402
from .gameinfo import (anticheat_warning, compat_facts, is_installed, play_enabled,
    play_label, SOURCE_LABEL, TIER_COLORS)  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .jobs import cli_json, in_thread  # noqa: E402
from .widgets import (ask, clear_layout, FlowLayout, icon_button, icon_label, label,
    NoWheelCombo, plain_label, protongen_code_preview, set_glyph_icon, soft_wrap)  # noqa: E402
