#!/usr/bin/env python3
"""Nexus — game launcher, FitGirl repack installer and Proton manager (PyQt6).

Drives the `nexus-cli` backend via QProcess (every read is a JSON command,
every change a queued job) and reads artwork from disk for display.

Layout: a sidebar (Library / Activity / Storage / Health / Settings) and a
stacked page area. Everything happens inside the one window — tiling and
scrolling compositors (niri) float every dialog, so dialogs are kept to file
pickers and the quit-while-busy question. Jobs report as Activity cards, a
strip under the library and toasts; the raw console is a debug view.

The visual language follows the protongen design system (Rosé Pine dark):
token-driven color/space/radius/type, a two-radial-gradient window background,
translucent cards, one accent with semantic status colors, and pill badges.
Two things in that spec have no Qt equivalent and are approximated here:
`backdrop-filter` blur (Qt cannot blur what is behind a widget — translucent
fills are composited over the gradient instead) and `outline-offset` focus rings
(drawn as a 2px accent border on the element itself).
"""

import codecs
import contextlib
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from string import Template
from typing import NamedTuple

from PyQt6.QtCore import (Qt, QObject, QEvent, QProcess, QProcessEnvironment, QUrl,
                          pyqtSignal, QSize, QSizeF, QRect, QPoint, QPointF, QRectF, QTimer,
                          QPropertyAnimation, QVariantAnimation, QEasingCurve,
                          QFileSystemWatcher, QRunnable, QThreadPool, pyqtProperty)
from PyQt6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QImage, QPixmap, QPainter,
                         QPainterPath, QLinearGradient, QRadialGradient, QBrush,
                         QPen, QDesktopServices, QKeySequence, QShortcut, QKeyEvent, QCursor,
                         QImageReader, QFontMetrics)
from PyQt6.QtNetwork import (QNetworkAccessManager, QNetworkReply, QNetworkRequest,
                             QLocalServer, QLocalSocket)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QScrollArea, QFrame, QPlainTextEdit, QDialog, QLineEdit,
    QCheckBox, QFileDialog, QGroupBox, QMessageBox, QToolButton,
    QLayout, QSplitter,
    QComboBox, QProgressBar, QFormLayout, QGridLayout, QStackedWidget, QMenu,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView, QGraphicsOpacityEffect,
    QSlider, QAbstractButton, QAbstractItemView, QAbstractScrollArea, QScrollBar, QTabBar,
    QTextBrowser, QTextEdit, QButtonGroup, QRadioButton,
    QSizePolicy,
)

# --------------------------------------------------------------------------
# Paths + palette
# --------------------------------------------------------------------------

HOME = Path.home()
# The repo this module lives in (nexus/gui.py), so GUI and CLI are always the
# same version; bin/nexus and bin/nexus-cli are thin stubs that import us.
_REPO = Path(__file__).resolve().parent.parent
from nexus import paths as NX  # noqa: E402
from nexus.icons import ICONS  # noqa: E402
from PyQt6 import sip  # noqa: E402


def alive(*objs) -> bool:
    """Whether these Qt objects still exist. A deferred call (a 0 ms timer, a
    lambda slot) can outlive the widget it touches, and an exception in a
    slot aborts the whole app under PyQt6."""
    return all(o is not None and not sip.isdeleted(o) for o in objs)
from nexus import winfocus  # noqa: E402
from nexus import gamedb  # noqa: E402
from nexus import history  # noqa: E402
from nexus import steamlib  # noqa: E402

CLI_BIN = str(_REPO / 'bin' / 'nexus-cli')
DATA_DIR = NX.DATA_DIR
CONFIG_PATH = NX.CONFIG_PATH
APPLICATIONS_DIR = NX.APPLICATIONS_DIR
ICON_PATH = DATA_DIR / 'nexus-icon.png'
LOGO_SVG = _REPO / 'assets' / 'nexus.svg'           # the app logo; ICON_PATH is rendered from it
DESKTOP_PATH = APPLICATIONS_DIR / f'{NX.APP_ID}.desktop'
LEGACY_DESKTOP = APPLICATIONS_DIR / 'fitgirl-manager.desktop'
INSTALL_BASE_DEFAULT = str((HOME / 'Games' / 'FitGirl'))  # autodetected; config [install] base overrides

# --- Fonts -----------------------------------------------------------------
# The spec asks for Lexend + JetBrains Mono. Neither name is guaranteed to be
# installed, so each role resolves at runtime against a preference list. The
# icon role is separate and deliberate: icons are private-use glyphs from the
# bundled Lucide font (nexus/icons.py), so it must never fall back to the UI face.
SANS_STACK = ['Lexend', 'Inter', 'Inter Display', 'Cantarell', 'Noto Sans',
              'DejaVu Sans']
MONO_STACK = ['JetBrainsMono Nerd Font', 'JetBrains Mono', 'Fira Code',
              'Noto Sans Mono', 'DejaVu Sans Mono']
ICON_TTF = _REPO / 'assets' / 'fonts' / 'lucide.ttf'
# Lucide lacks brand logos and filled shapes; those few icons keep their
# Material Design glyph, which Qt's fallback finds in a Nerd Font. The
# symbols-only one first: its metrics are made for icons, not for text.
ICON_FALLBACK_STACK = ['Symbols Nerd Font', 'Symbols Nerd Font Mono',
                       'JetBrainsMono Nerd Font', 'MesloLGL Nerd Font',
                       'FantasqueSansM Nerd Font']

UI_FONT = 'Noto Sans'          # replaced by resolve_fonts() at startup
MONO_FONT = 'Noto Sans Mono'
ICON_FONT = 'lucide'


def _first_installed(candidates: list[str], fallback: str) -> str:
    have = set(QFontDatabase.families())
    for name in candidates:
        if name in have:
            return name
    return fallback


def resolve_fonts() -> None:
    """Pick the best installed face for each role. Must run after QApplication
    exists (QFontDatabase needs a live app)."""
    global UI_FONT, MONO_FONT, ICON_FONT
    UI_FONT = _first_installed(SANS_STACK, QFont().defaultFamily())
    MONO_FONT = _first_installed(MONO_STACK, 'monospace')
    fid = QFontDatabase.addApplicationFont(str(ICON_TTF))
    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    ICON_FONT = fams[0] if fams else 'lucide'
    # Glyphs the icon face lacks come from here, not whatever fontconfig picks.
    fallback = _first_installed(ICON_FALLBACK_STACK, '')
    if fallback:
        QFont.insertSubstitution(ICON_FONT, fallback)


# --- Design tokens (Rosé Pine) ---------------------------------------------
# Primitives. Nothing below this line should hardcode a hex value.
PAL = {
    'base': '#191724', 'surface': '#1f1d2e', 'overlay': '#26233a',
    'overlay_hi': '#393552',
    'text': '#e0def4', 'subtle': '#c9c7e0', 'muted': '#908caa',
    'iris': '#c4a7e7', 'foam': '#9ccfd8', 'love': '#eb6f92',
    'gold': '#f6c177', 'rose': '#ebbcba', 'pine': '#31748f',
    'white': '#ffffff', 'black': '#000000',
}

# Semantic layer — every widget references these, never PAL directly.
P = {
    'bg':            PAL['base'],
    'mantle':        PAL['surface'],
    # Qt has no backdrop blur, so the spec's 28%-alpha card is precomposited
    # over the gradient background instead of being made translucent.
    'surface':       '#221f31',
    'surface_solid': PAL['surface'],
    'surface_2':     PAL['overlay'],
    'overlay':       PAL['overlay_hi'],
    'border':        PAL['overlay'],
    'border_card':   '#242136',
    'text':          PAL['text'],
    'subtext':       PAL['subtle'],
    'muted':         PAL['muted'],
    'accent':        PAL['iris'],
    'on_accent':     PAL['base'],
    'success':       PAL['foam'],
    'danger':        PAL['love'],
    'warning':       PAL['gold'],
    'attention':     PAL['rose'],
    'info':          PAL['pine'],
    'on_art':        PAL['white'],
    # Accent tints (§2.8) — Qt understands rgba() in stylesheets.
    'accent_09':     'rgba(196,167,231,0.09)',
    'accent_16':     'rgba(196,167,231,0.16)',
    'accent_22':     'rgba(196,167,231,0.22)',
    'accent_35':     'rgba(196,167,231,0.35)',
    'success_18':    'rgba(156,207,216,0.18)',
    'danger_18':     'rgba(235,111,146,0.18)',
    'warning_18':    'rgba(246,193,119,0.18)',
    'scrim_badge':   'rgba(0,0,0,0.45)',
    'tile_scrim':    'rgba(25,23,36,0.84)',
}

# Gradient pairs (start, end) — used sparingly: primary actions, the active
# tab, card accents and the hero. Two colours from the same palette so they
# read as light, not as a rainbow.
GRAD = {
    'dusk':   (PAL['iris'], PAL['love']),
    'aurora': (PAL['iris'], PAL['foam']),
    'tide':   (PAL['foam'], PAL['pine']),
    'dawn':   (PAL['gold'], PAL['rose']),
    'ember':  (PAL['love'], PAL['gold']),
}
# One hue per library source, for small accents (dots, card edges).
SRC_HUE = {'nexus': PAL['iris'], 'steam': PAL['foam'], 'heroic': PAL['gold']}
# Elevation: each step a little lighter than the base, for layered surfaces.
P.update({
    'surface_1': '#211e30',
    'surface_3': '#2c2942',
    'glass':     'rgba(38,35,58,0.72)',
    'glass_hi':  'rgba(57,53,82,0.82)',
})

# Radius (§2.6) / spacing (§2.4), in px. Semantic names first: every
# rounded thing picks one of these (or pill() for capsules), never a literal.
R = {'sm': 6, 'control': 10, 'lg': 10, 'xl': 12, 'tile': 12, 'popover': 12,
     'card': 16, 'panel': 16, 'full': 9999}


R_BASE = dict(R)

# --- Appearance: base themes, accents, and the function that applies them ---
# Each theme is the dark ladder the UI is built from (base → surface →
# overlay → overlay_hi) plus its text tones. Status colours (success,
# danger, warning…) stay Rosé Pine's in every theme.
THEMES = {
    'rose-pine': ("Rosé Pine", {'base': '#191724', 'surface': '#1f1d2e', 'overlay': '#26233a',
                                'overlay_hi': '#393552', 'text': '#e0def4', 'subtle': '#c9c7e0',
                                'muted': '#908caa'}),
    'moon':      ("Moon", {'base': '#232136', 'surface': '#2a273f', 'overlay': '#393552',
                           'overlay_hi': '#44415a', 'text': '#e0def4', 'subtle': '#c9c7e0',
                           'muted': '#908caa'}),
    'mocha':     ("Mocha", {'base': '#181825', 'surface': '#1e1e2e', 'overlay': '#313244',
                            'overlay_hi': '#45475a', 'text': '#cdd6f4', 'subtle': '#bac2de',
                            'muted': '#9399b2'}),
    'slate':     ("Slate", {'base': '#11161d', 'surface': '#18202a', 'overlay': '#222c38',
                            'overlay_hi': '#2f3b4a', 'text': '#e5e9f0', 'subtle': '#c7cfdb',
                            'muted': '#8792a2'}),
    'midnight':  ("Midnight", {'base': '#0b0b10', 'surface': '#13131a', 'overlay': '#1c1c26',
                               'overlay_hi': '#2a2a38', 'text': '#ececf1', 'subtle': '#c6c6d2',
                               'muted': '#8b8b9e'}),
}
ACCENTS = {'iris': '#c4a7e7', 'rose': '#ebbcba', 'love': '#eb6f92', 'gold': '#f6c177',
           'foam': '#9ccfd8', 'sky': '#89b4fa', 'mint': '#a6e3a1', 'pine': '#31748f'}
APPEARANCE_DEFAULT = {'theme': 'rose-pine', 'accent': 'iris', 'partner': 'love',
                      'glow': 'subtle', 'corners': 'default', 'art_tint': True}
GLOW = {'off': 0.0, 'subtle': 1.0, 'vivid': 1.9}
CORNERS = {'sharp': 0.45, 'default': 1.0, 'round': 1.45}
LOOK = {'glow': 1.0, 'art_tint': True}         # read by the painters


def _hex_ok(v) -> bool:
    return isinstance(v, str) and bool(re.fullmatch(r'#[0-9a-fA-F]{6}', v))


def normalize_appearance(ap) -> dict:
    ap = dict(ap) if isinstance(ap, dict) else {}
    out = dict(APPEARANCE_DEFAULT)
    if ap.get('theme') in THEMES:
        out['theme'] = ap['theme']
    for k in ('accent', 'partner'):
        v = ap.get(k)
        if v in ACCENTS or _hex_ok(v) or (k == 'partner' and v == 'none'):
            out[k] = v
    if ap.get('glow') in GLOW:
        out['glow'] = ap['glow']
    if ap.get('corners') in CORNERS:
        out['corners'] = ap['corners']
    if isinstance(ap.get('art_tint'), bool):
        out['art_tint'] = ap['art_tint']
    return out


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() + (cb.red() - ca.red()) * t),
                  int(ca.green() + (cb.green() - ca.green()) * t),
                  int(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


def _rgba(hex_color: str, a: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{a})"


def accent_hex(v: str) -> str:
    return ACCENTS.get(v, v if _hex_ok(v) else ACCENTS['iris'])


def apply_appearance(ap: dict) -> dict:
    """Rebuild the palette (PAL/P/GRAD/R) from an appearance in place, so every
    painter picks it up on its next paint; the caller re-applies the
    stylesheet. Returns the normalised appearance."""
    ap = normalize_appearance(ap)
    PAL.update(THEMES[ap['theme']][1])
    accent = accent_hex(ap['accent'])
    partner = accent if ap['partner'] == 'none' else accent_hex(ap['partner'])
    light = QColor(accent).lightnessF() > 0.55
    P.update({
        'bg': PAL['base'], 'mantle': PAL['surface'],
        'surface': _mix(PAL['base'], PAL['overlay'], 0.35),
        'surface_solid': PAL['surface'], 'surface_2': PAL['overlay'],
        'overlay': PAL['overlay_hi'], 'border': PAL['overlay'],
        'border_card': _mix(PAL['base'], PAL['overlay'], 0.45),
        'text': PAL['text'], 'subtext': PAL['subtle'], 'muted': PAL['muted'],
        'accent': accent, 'on_accent': PAL['base'] if light else '#ffffff',
        'accent_09': _rgba(accent, 0.09), 'accent_16': _rgba(accent, 0.16),
        'accent_22': _rgba(accent, 0.22), 'accent_35': _rgba(accent, 0.35),
        'tile_scrim': _rgba(PAL['base'], 0.84),
        'surface_1': _mix(PAL['base'], PAL['overlay'], 0.30),
        'surface_3': _mix(PAL['overlay'], PAL['overlay_hi'], 0.45),
        'glass': _rgba(PAL['overlay'], 0.72), 'glass_hi': _rgba(PAL['overlay_hi'], 0.82),
    })
    GRAD['dusk'] = (accent, partner)
    GRAD['aurora'] = (accent, PAL['foam'])
    SRC_HUE['nexus'] = accent
    k = CORNERS[ap['corners']]
    R.update({key: max(2, int(round(v * k))) for key, v in R_BASE.items() if key != 'full'})
    LOOK['glow'] = GLOW[ap['glow']]
    LOOK['art_tint'] = ap['art_tint']
    _ICON_CACHE.clear()
    _TINTS.clear()
    return ap


def pill(h: int) -> int:
    """Radius that turns a box of height h into a capsule (Qt draws radii
    larger than half the height badly, so never just use 'full')."""
    return h // 2


# Heights of the fixed-height capsules, so their radius can be derived.
PILL_H = 18      # status pills / badges
CHIP_H = 28      # filter chips
GLYPH_H = 22     # controller hint glyphs


def pill_qss(bg: str, fg: str, h: int = PILL_H, pad: int = 7) -> str:
    return (f"background: {bg}; color: {fg}; border-radius: {pill(h)}px;"
            f" padding: 0 {pad}px; font-size: {FS['2xs']}px; font-weight: 500;")


def qss_grad(name: str, x2: float = 1.0, y2: float = 0.0, lighten: float = 1.0) -> str:
    """A qlineargradient() for QSS from one of the GRAD pairs."""
    a, b = (QColor(c) for c in GRAD[name])
    if lighten != 1.0:
        a, b = a.lighter(int(lighten * 100)), b.lighter(int(lighten * 100))
    return (f"qlineargradient(x1:0, y1:0, x2:{x2}, y2:{y2},"
            f" stop:0 {a.name()}, stop:1 {b.name()})")


SP = {'0_5': 2, '1': 4, '1_5': 6, '2': 8, '2_5': 10, '3': 12, '4': 16,
      '5': 20, '6': 24, '8': 32}

# Type scale (§2.3). Qt stylesheets take px sizes happily.
FS = {'2xs': 10, 'xs_plus': 11, 'xs': 12, 'command': 13, 'sm': 14,
      'base': 16, 'lg': 18, 'display': 20}

TILE_W = 200
TILE_LIFT = 4                    # hover translateY(-4px); reserved, not layout churn


# --------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------

def build_stylesheet(scale: float = 1.0) -> str:
    """The design system rendered as QSS. Every value comes from a token.
    `scale` enlarges type and spacing (Theatre mode, read from the couch)."""
    tpl = Template("""
    QWidget { background: transparent; color: $text; outline: none;
              font-family: "$ui"; font-size: ${fs_sm}px; }
    QMainWindow, QDialog { background: $bg; }
    QToolTip {
        background: $surface_solid; color: $text;
        border: 1px solid $border; border-radius: ${r_sm}px;
        padding: ${sp_1}px ${sp_2}px; font-size: ${fs_xs}px;
    }

    /* ---- Header ------------------------------------------------------- */
    #Header { background: $mantle; border-bottom: 1px solid $border; }
    #Title  { color: $text; font-size: ${fs_display}px; font-weight: 500; }
    #Subtitle { color: $muted; font-size: ${fs_xs}px; }

    /* ---- Buttons (§3.1 / §3.2) ---------------------------------------- */
    QPushButton {
        background: $surface_2; color: $subtext; border: 1px solid $border;
        border-radius: ${r_control}px; padding: ${sp_1_5}px ${sp_3}px;
        font-size: ${fs_xs}px; font-weight: 500;
    }
    QPushButton:hover   { background: $overlay; color: $text; }
    QPushButton:pressed { background: $surface_2; }
    QPushButton:disabled { color: $muted; background: $mantle; border-color: $mantle; }

    QPushButton#Primary {
        background: $grad_dusk; color: $on_accent; border: none;
        padding: ${sp_1_5}px ${sp_3}px;
    }
    QPushButton#Primary:hover    { background: $grad_dusk_hi; }
    QPushButton#Primary:disabled { background: $surface_2; color: $muted;
                                   border-color: $border; }
    QPushButton#Danger:hover { background: $danger; color: $on_accent;
                               border-color: $danger; }

    /* Ghost / icon buttons — used on the tile overlay and for close actions. */
    QToolButton {
        background: $surface_2; color: $text; border: 1px solid $border;
        border-radius: ${r_control}px; padding: ${sp_1_5}px;
        font-family: "$icon"; font-size: ${fs_sm}px;
    }
    QToolButton:hover   { background: $accent; color: $on_accent;
                          border-color: $accent; }
    QToolButton:pressed { background: $accent; color: $on_accent; }
    QToolButton:disabled { color: $muted; background: $mantle;
                           border-color: $mantle; }
    QToolButton#Danger:hover { background: $danger; color: $on_accent;
                               border-color: $danger; }

    /* ---- Tile (§3.12) -------------------------------------------------- */
    /* The tile paints itself; this only styles the hover action overlay. */

    /* ---- Console ------------------------------------------------------- */
    QPlainTextEdit#Console {
        background: $mantle; color: $subtext;
        border: 1px solid $border; border-radius: ${r_control}px;
        font-family: "$mono"; font-size: ${fs_command}px;
        padding: ${sp_3}px; selection-background-color: $accent_35;
    }

    /* ---- Inputs (§3.8 / §3.9) ------------------------------------------ */
    QLineEdit, QComboBox {
        background: $surface_2; border: 1px solid $border;
        border-radius: ${r_control}px; padding: ${sp_1_5}px ${sp_2}px;
        color: $text; font-size: ${fs_xs}px;
        selection-background-color: $accent_35; selection-color: $text;
    }
    QLineEdit:focus, QComboBox:focus { border: 1px solid $accent; }
    QLineEdit::placeholder { color: $muted; }
    /* combobox-popup: 0 = a plain list popup; Fusion's menu-style one paints
       its own square panel behind the rounded list. */
    QComboBox { padding-right: ${sp_6}px; combobox-popup: 0; }
    QComboBox::drop-down { border: none; width: ${sp_6}px;
                           subcontrol-origin: padding; subcontrol-position: center right; }
    QComboBox::down-arrow { image: url("$chevron"); width: 12px; height: 12px; }
    QComboBox::down-arrow:hover, QComboBox::down-arrow:focus,
    QComboBox::down-arrow:on { image: url("$chevron_hi"); }
    QComboBoxPrivateContainer { background: transparent; border: none; }
    QComboBox QAbstractItemView {
        background: $surface_solid; border: 1px solid $border;
        border-radius: ${r_popover}px; color: $text; outline: 0;
        selection-background-color: $accent_22; selection-color: $accent;
        padding: ${sp_1}px;
    }
    QComboBox QAbstractItemView::item { min-height: 26px; padding: 0 ${sp_2}px;
                                        border-radius: ${r_sm}px; }

    /* ---- Group box ----------------------------------------------------- */
    QGroupBox {
        border: 1px solid $border_card; border-radius: ${r_card}px;
        margin-top: ${sp_3}px; background: $surface;
    }
    QGroupBox::title {
        subcontrol-origin: margin; left: ${sp_3}px; padding: 0 ${sp_1}px;
        color: $muted; font-size: ${fs_2xs}px; font-weight: 500;
    }

    /* ---- Checkbox (§3.6 — switch semantics, checkbox affordance) -------- */
    QCheckBox { spacing: ${sp_2}px; color: $subtext; font-size: ${fs_sm}px; }
    QCheckBox:hover { color: $text; }
    QCheckBox::indicator {
        width: 18px; height: 18px; border-radius: ${r_sm}px;
        border: 1px solid $overlay; background: $surface_2;
    }
    QCheckBox::indicator:hover { border-color: $accent; }
    QCheckBox::indicator:checked {
        background: $accent; border-color: $accent;
        image: url("$check_icon");
    }
    QRadioButton { spacing: ${sp_2}px; color: $subtext; font-size: ${fs_sm}px; }
    QRadioButton:hover { color: $text; }
    QRadioButton::indicator {
        width: 18px; height: 18px; border-radius: ${r_control}px;   /* 20 px: a circle */
        border: 1px solid $overlay; background: $surface_2;
    }
    QRadioButton::indicator:hover { border-color: $accent; }
    QRadioButton::indicator:checked {
        border-color: $accent;
        background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
                                    stop: 0 $accent, stop: 0.45 $accent,
                                    stop: 0.55 $surface_2, stop: 1 $surface_2);
    }

    /* ---- Scroll ------------------------------------------------------- */
    QScrollArea { border: none; background: transparent; }
    QScrollBar:vertical { background: transparent; width: ${sp_2_5}px; margin: 0; }
    QScrollBar::handle:vertical {
        background: $overlay; border-radius: ${r_scroll}px; min-height: 32px;
    }
    QScrollBar::handle:vertical:hover { background: $muted; }
    QScrollBar:horizontal { background: transparent; height: ${sp_2_5}px; }
    QScrollBar::handle:horizontal { background: $overlay; border-radius: ${r_scroll}px;
                                    min-width: 32px; }
    QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
    QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
    QSplitter::handle { background: transparent; height: ${sp_2}px; }

    /* ---- Progress -------------------------------------------------------- */

    /* ---- Shell: sidebar, pages, cards ---------------------------------- */
    #TopBar { background: $glass; }
    QToolButton#TopIcon { background: transparent; border: none; color: $subtext;
                          font-size: 20px; padding: 0; }
    QToolButton#TopIcon:hover { background: $surface_2; color: $text; }
    QToolButton#TopIcon:checked { background: $accent_16; color: $accent; }
    QLineEdit#TopSearch { background: $surface_1; border: 1px solid $border;
                          border-radius: ${r_search}px; padding: 0 ${sp_3}px; font-size: ${fs_xs}px; }
    QLineEdit#TopSearch:focus { border: 1px solid $accent; background: $surface_2; }
    #PageTitle { color: $text; font-size: ${fs_lg}px; font-weight: 500; }
    #Muted { color: $muted; font-size: ${fs_xs}px; }
    #Card { background: $surface; border: 1px solid $border_card; border-radius: ${r_card}px; }
    #Details { background: $mantle; border-left: 1px solid $border; }
    #StatChip { background: $surface_2; border-radius: ${r_control}px; }
    #StatLabel { color: $muted; font-size: ${fs_2xs}px; font-weight: 500; }
    #StatValue { color: $text; font-size: ${fs_sm}px; font-weight: 500; }
    #Banner { background: $warning_18; border: 1px solid $warning; border-radius: ${r_control}px; }
    #DangerBar { background: $danger_18; border: 1px solid $danger; border-radius: ${r_control}px; }
    #Toast { background: $surface_solid; border: 1px solid $overlay; border-radius: ${r_xl}px; }
    QPushButton#PlayBig { background: $grad_dusk; color: $on_accent; border: none;
                          border-radius: ${r_control}px; padding: ${sp_2}px; font-size: ${fs_sm}px;
                          font-weight: 600; }
    QPushButton#PlayBig:hover { background: $grad_dusk_hi; }
    QPushButton#PlayBig:disabled { background: $surface_2; color: $muted; border-color: $border; }
    QPushButton#DangerSolid { background: $danger; color: $on_accent; border: 1px solid $danger; }
    QPushButton#Ghost, QToolButton#Ghost { background: transparent; border: none; color: $subtext; }
    QPushButton#Ghost:hover, QToolButton#Ghost:hover { background: $surface_2; color: $text; }
    QPushButton#ToolRow { background: $surface; border: 1px solid $border_card;
                          border-radius: ${r_control}px; text-align: left; padding: ${sp_2}px ${sp_3}px; }
    QPushButton#ToolRow:hover { background: $surface_2; border-color: $overlay; }
    QToolButton#PlayRound { background: $grad_dusk_diag; color: $on_accent; border: none;
                            font-size: 20px; padding-left: 4px; }
    QToolButton#PlayRound:hover { background: $grad_dusk_hi; }
    QToolButton#TileMore { background: $scrim_badge; color: $on_art; border: none;
                           border-radius: ${r_more}px; font-size: ${fs_sm}px; padding: 0; }
    QToolButton#TileMore:hover { background: $accent; color: $on_accent; }
    QProgressBar#CardProgress { background: $surface_2; border: none; border-radius: ${r_bar}px; }
    QProgressBar#CardProgress::chunk { background: $grad_dusk; border-radius: ${r_bar}px; }

    /* ---- Library toolbar ---------------------------------------------- */
    QPushButton#Chip { background: $surface_2; color: $subtext; border: 1px solid $border;
                       border-radius: ${r_chip}px; padding: 0 ${sp_3}px; font-size: ${fs_xs}px;
                       min-height: ${chip_inner}px; max-height: ${chip_inner}px; }
    QPushButton#Chip:hover { color: $text; border-color: $overlay; }
    QPushButton#Chip:checked { background: $accent_22; color: $accent; border-color: $accent_35; }
    /* Segmented control: the track and sliding thumb are painted; the items
       are transparent buttons on top so focus and tooltips still work. */
    QToolButton#SegItem, QPushButton#SegItem {
        background: transparent; border: none; color: $muted;
        border-radius: ${r_control}px; padding: 0 ${sp_2_5}px; font-weight: 500;
    }
    QPushButton#SegItem { font-size: ${fs_xs}px; }
    QToolButton#SegItem:hover, QPushButton#SegItem:hover { background: transparent; color: $text; }
    QToolButton#SegItem:checked, QPushButton#SegItem:checked { color: $on_accent; }
    QPushButton#TabItem { background: transparent; border: none; color: $muted;
                          border-radius: ${r_control}px; padding: 0 ${sp_3}px;
                          font-size: ${fs_sm}px; font-weight: 500; }
    QPushButton#TabItem:hover { background: $accent_09; color: $text; }
    QPushButton#TabItem:checked { color: $text; }
    #ShelfTitle { color: $text; font-size: ${fs_base}px; font-weight: 600; }
    #ShelfCount { color: $muted; font-size: ${fs_xs}px; padding-bottom: 2px; }
    QToolButton#ShelfArrow, QToolButton#GlassIcon {
        background: $glass_hi; color: $on_art; border: 1px solid $glass;
        font-size: ${fs_sm}px; padding: 0;
    }
    QToolButton#ShelfArrow:hover, QToolButton#GlassIcon:hover {
        background: $grad_dusk; color: $on_accent; border-color: transparent;
    }
    QPushButton#Glass { background: $glass_hi; color: $on_art; border: 1px solid $glass;
                        border-radius: ${r_control}px; padding: 0 ${sp_5}px;
                        font-size: ${fs_sm}px; font-weight: 500; }
    QPushButton#Glass:hover { background: $overlay; }
    QLabel#HeroKicker { color: $on_art; font-size: ${fs_xs}px; font-weight: 600; }
    QLabel#HeroKicker[live="true"] { color: $success; }
    QLabel#HeroTitle { color: $on_art; font-weight: 600; }
    QLabel#TheatreClock { color: $subtext; font-size: ${fs_lg}px; font-weight: 500; }
    #StatTile { background: $surface_1; border: 1px solid $border_card;
                border-radius: ${r_control}px; }
    QLabel#StatValue { color: $text; font-size: ${fs_sm}px; font-weight: 600; }
    QToolButton#QuickAction { background: $surface_2; color: $subtext; border: none;
                              font-size: 17px; padding: 0; }
    QToolButton#QuickAction:hover { background: $accent_22; color: $accent; }
    QToolButton#QuickAction[on="true"] { color: $warning; }
    #DisclosureHead { background: $surface_1; border: 1px solid $border_card;
                      border-radius: ${r_control}px; }
    #DisclosureHead:hover { background: $surface_2; }
    #DashCard { background: $surface; border: 1px solid $border_card; border-radius: ${r_card}px; }
    #DashCard[editing="true"], #DashShelf[editing="true"] {
        border: 1px dashed $accent_35; border-radius: ${r_card}px; }
    #DashEditBar { background: $accent_09; border-radius: ${r_control}px; }
    #DashAdd { background: $surface_1; border: 1px dashed $border; border-radius: ${r_card}px; }
    QLabel#DashTitle { color: $text; font-size: ${fs_sm}px; font-weight: 600; }
    QLabel#DashBig { color: $text; font-size: 28px; font-weight: 600; }
    #DashRow { background: transparent; border: 1px solid transparent;
               border-radius: ${r_sm}px; }
    #DashRow:hover { background: $accent_09; }
    #DashRow:focus { background: $accent_09; }
    #SysTile { background: $surface_1; border: 1px solid $border_card;
               border-radius: ${r_control}px; }
    #SysTile:hover, #SysTile:focus { background: $accent_09; border-color: $accent_35; }
    #NewsTile, #DealTile { background: $surface; border: 1px solid $border_card;
                           border-radius: ${r_card}px; }
    #DealTile:hover { background: $surface_1; border-color: $accent_35; }
    #NewsTile:hover { background: $surface_1; border-color: $accent_35; }
    #NewsTile[reading="true"] { border-color: $accent; }
    QLabel#NewsTitle { color: $text; font-size: ${fs_sm}px; font-weight: 500; }
    QLabel#NewsHeadline { color: $on_art; font-size: 26px; font-weight: 600; }
    QLabel#NewsLede { color: $on_art; font-size: ${fs_sm}px; }
    QLabel#NewsReaderTitle { color: $text; font-size: ${fs_lg}px; font-weight: 600; }
    #NewsReader { background: $mantle; border-left: 1px solid $border; }
    QSlider::groove:horizontal { height: 4px; background: $overlay; border-radius: ${r_groove}px; }
    QSlider::sub-page:horizontal { background: $grad_dusk; border-radius: ${r_groove}px; }
    QSlider::handle:horizontal { background: $text; width: 12px; height: 12px;
                                 margin: -4px 0; border-radius: ${r_knob}px; }

    /* ---- Controller hints + focus rings -------------------------------- */
    #HintBar { background: $mantle; border-top: 1px solid $border; }
    QLabel#HintGlyph { background: $surface_2; color: $text; border: 1px solid $overlay;
                       border-radius: ${r_glyph}px; padding: 0 6px; font-size: ${fs_xs}px;
                       font-weight: 500; min-height: ${glyph_inner}px; max-height: ${glyph_inner}px; }
    QCheckBox:focus { color: $text; }

    /* ---- Theatre game page cards ---------------------------------------- */
    #TheatreCard { background: $glass; border: 1px solid $glass_hi; border-radius: ${r_card}px; }
    #TheatreCard:hover { background: $glass_hi; }
    QLabel#TheatreCardTitle { color: $subtext; font-size: ${fs_2xs}px; font-weight: 600; }
    QLabel#TheatreCardBig { color: $on_art; font-size: ${fs_lg}px; font-weight: 600; }
    QLabel#TheatreCardLine { color: $on_art; font-size: ${fs_xs}px; }
    QTextBrowser#TheatreReader { background: $glass; color: $on_art; border: 1px solid $glass_hi;
                                 border-radius: ${r_card}px; padding: ${sp_4}px;
                                 font-size: ${fs_sm}px; }

    /* ---- Theatre search: on-screen keyboard ------------------------------ */
    QLabel#SearchQuery { color: $on_art; font-size: ${fs_display}px; font-weight: 600; }
    QPushButton#Key { background: $glass; color: $on_art; border: 1px solid $glass_hi;
                      border-radius: ${r_control}px; font-size: ${fs_base}px; font-weight: 500;
                      padding: 0; }
    QPushButton#Key:hover, QPushButton#Key:focus { background: $accent_22; }

    /* ---- Controller sheet: the game menu when a pad is driving ---------- */
    #Sheet { background: $surface_solid; border-left: 1px solid $border; }
    QLabel#SheetTitle { color: $text; font-size: ${fs_lg}px; font-weight: 600; }
    QLabel#SheetSection { color: $muted; font-size: ${fs_2xs}px; font-weight: 600; }
    QPushButton#SheetRow { background: transparent; color: $text; border: 1px solid transparent;
                           border-radius: ${r_control}px; text-align: left;
                           padding: ${sp_2}px ${sp_3}px; font-size: ${fs_sm}px; }
    QPushButton#SheetRow:hover, QPushButton#SheetRow:focus { background: $accent_16; }
    QPushButton#SheetRow:disabled { color: $muted; }
    QPushButton#SheetRow[danger="true"] { color: $danger; }

    /* ---- Menus, tabs, tables ------------------------------------------- */
    QMenu { background: $surface_solid; border: 1px solid $border;
            border-radius: ${r_popover}px; padding: ${sp_1}px; }
    QMenu::item { padding: ${sp_1_5}px ${sp_6}px ${sp_1_5}px ${sp_3}px; border-radius: ${r_sm}px;
                  color: $text; }
    QMenu::item:selected { background: $accent_22; color: $accent; }
    QMenu::item:disabled { color: $muted; }
    QMenu::separator { height: 1px; background: $border; margin: ${sp_1}px ${sp_2}px; }
    QMenu::indicator { width: 14px; height: 14px; left: ${sp_1}px; }
    QTabWidget::pane { border: none; }
    QTabBar::tab { background: transparent; color: $muted; border: none;
                   border-bottom: 2px solid transparent; padding: ${sp_1_5}px ${sp_2_5}px; }
    QTabBar::tab:hover { color: $text; }
    QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
    QTableWidget { background: $surface; border: 1px solid $border_card;
                   border-radius: ${r_card}px; selection-background-color: $accent_22;
                   selection-color: $text; alternate-background-color: $surface_2; }
    QTableWidget::item { padding: ${sp_1}px ${sp_2}px; }
    QTableWidget#LibraryList { padding-right: ${sp_4}px; }
    QHeaderView { background: transparent; }
    QHeaderView::section { background: $mantle; color: $muted; border: none;
                           padding: ${sp_1_5}px ${sp_2}px; font-size: ${fs_xs}px; }
    QHeaderView::section:first { border-top-left-radius: ${r_card_inner}px; }
    QHeaderView::section:last { border-top-right-radius: ${r_card_inner}px; }
    QTableCornerButton::section { background: transparent; border: none; }
    QSplitter::handle:horizontal { width: 1px; background: $border; }

    /* ---- Misc ---------------------------------------------------------- */
    QLabel#Empty { color: $muted; font-size: ${fs_base}px; }
    QLabel#EmptyTitle { color: $text; font-size: ${fs_base}px; font-weight: 500; }
    QLabel#Hint  { color: $muted; font-size: ${fs_xs}px; }
    QStatusBar { color: $muted; font-size: ${fs_xs}px; background: $mantle;
                 border-top: 1px solid $border; }
    QMessageBox { background: $surface_solid; }
    """)
    return tpl.substitute(
        ui=UI_FONT, mono=MONO_FONT, icon=ICON_FONT,
        accent_hover=_shade(P['accent'], 0.88),
        check_icon=_check_icon_path(),
        grad_dusk=qss_grad('dusk'), grad_dusk_hi=qss_grad('dusk', lighten=1.12),
        grad_dusk_diag=qss_grad('dusk', 1.0, 1.0),
        r_scroll=pill(SP['2_5']), r_bar=pill(6), r_more=pill(28),
        r_groove=pill(4), r_knob=pill(12), r_search=pill(36),
        r_chip=pill(int(CHIP_H * scale)), chip_inner=int(CHIP_H * scale) - 2,
        r_glyph=pill(int(GLYPH_H * scale)), glyph_inner=int(GLYPH_H * scale) - 2,
        r_card_inner=R['card'] - 1,
        chevron=_chevron_icon_path('muted'),
        chevron_hi=_chevron_icon_path('accent'),
        **{k: v for k, v in P.items()},
        **{f'r_{k}': v for k, v in R.items()},
        **{f'sp_{k}': int(round(v * scale)) for k, v in SP.items()},
        **{f'fs_{k}': int(round(v * scale)) for k, v in FS.items()},
    )


class PopupRounder(QObject):
    """Menus and combo drop-downs are top-level windows; their QSS
    radius only shows if the window itself is translucent (otherwise the
    square window background paints the corners). Set that as each one is
    polished, before its native surface exists."""
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
        return False


def _shade(hex_color: str, factor: float) -> str:
    """Multiply a hex color's channels — used for the accent hover state, which
    the spec expresses as `opacity: 90%` over a dark ground."""
    c = QColor(hex_color)
    return QColor(int(c.red() * factor), int(c.green() * factor),
                  int(c.blue() * factor)).name()


_ICON_CACHE: dict[str, str] = {}


def _stroke_icon(name: str, color: str, points, size=(18, 18), width=2.4) -> str:
    """QSS can't draw glyphs for indicators/arrows, so render a stroked
    polyline to a PNG once and reference it by path."""
    cached = _ICON_CACHE.get(name)
    if cached and Path(cached).exists():
        return cached
    path = DATA_DIR / f'ui-{name}.png'
    pm = QPixmap(*size)
    pm.fill(Qt.GlobalColor.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pt.setPen(pen)
    pt.drawPolyline(*(QPointF(x, y) for x, y in points))
    pt.end()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pm.save(str(path), 'PNG')
    except OSError:
        return ''
    _ICON_CACHE[name] = str(path)
    return str(path)


def _check_icon_path() -> str:
    """Drawn in `on-accent` so it reads against the accent-filled indicator."""
    return _stroke_icon(f"check-{P['on_accent'][1:]}", P['on_accent'],
                        ((4, 9), (8, 13), (14, 5)))


def _chevron_icon_path(color_key: str) -> str:
    """Combo-box arrow — Fusion draws none once ::drop-down is styled."""
    return _stroke_icon(f"chevron-{P[color_key][1:]}", P[color_key],
                        ((3, 4.5), (6, 7.5), (9, 4.5)), (12, 12), 1.6)


# --------------------------------------------------------------------------
# Background + pixmap helpers
# --------------------------------------------------------------------------

# NEXUS_NO_MOTION=1 turns animations off (deterministic screenshots, or for
# anyone who prefers a still UI).
MOTION = os.environ.get('NEXUS_NO_MOTION') != '1'


def anim_ms(ms: int) -> int:
    return ms if MOTION else 0


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


_TINTS: dict[str, QColor] = {}


def dominant_color(path: str | None) -> QColor | None:
    """Average colour of an image, lifted into a usable glow: saturated and
    bright enough to read on the dark base. Cached per path."""
    if not path:
        return None
    try:
        key = f"{path}:{Path(path).stat().st_mtime}"
    except OSError:
        return None
    if key in _TINTS:
        return _TINTS[key]
    reader = QImageReader(path)
    reader.setScaledSize(QSize(48, 48))     # a JPEG decodes at a fraction of the cost
    img = reader.read()
    if img.isNull():
        return None
    c = img.scaled(24, 24, Qt.AspectRatioMode.IgnoreAspectRatio,
                   Qt.TransformationMode.SmoothTransformation).scaled(
        1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
    hsv_h, hsv_s, hsv_v, _ = c.getHsvF()
    if hsv_h < 0:                           # greyscale art: keep the brand glow
        out = QColor(P['accent'])
    else:
        out = QColor.fromHsvF(hsv_h, max(0.45, min(0.75, hsv_s * 1.4)),
                              max(0.75, hsv_v))
    _TINTS[key] = out
    return out


def cover_pixmap(path: str, w: int, h: int) -> QPixmap | None:
    """Load and center-crop cover art to fill w x h (see cover_image)."""
    img = cover_image(path, w, h)
    return QPixmap.fromImage(img) if img is not None else None


def cover_image(path: str, w: int, h: int, grey: bool = False) -> QImage | None:
    """Load and center-crop cover art to fill w x h. Corners stay square — the
    tile clips to its own radius when painting. Decoded straight at (about)
    the size it's shown at: a JPEG then skips most of its pixels, which is
    several times faster than decoding a 600x900 cover and scaling it down.
    QImage only, so it's safe on a worker thread."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > w and size.height() > h:
        k = max(w / size.width(), h / size.height())
        reader.setScaledSize(QSize(max(w, round(size.width() * k)),
                                   max(h, round(size.height() * k))))
    img = reader.read()
    if img.isNull():
        img = QImage(path)                   # formats the reader can't size
        if img.isNull():
            return None
    if img.width() != w and img.height() != h or img.width() < w or img.height() < h:
        img = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                         Qt.TransformationMode.SmoothTransformation)
    x = max(0, (img.width() - w) // 2)
    y = max(0, (img.height() - h) // 2)
    img = img.copy(x, y, w, h)
    if grey:
        img = img.convertToFormat(QImage.Format.Format_Grayscale8)
    return img


THUMB_DIR = NX.CACHE_DIR / 'thumbs'
THUMB_MIN_BYTES = 150_000          # smaller sources decode about as fast as a thumbnail


def thumb_image(path: str, mtime: float, w: int, h: int, grey: bool = False,
                thumbs: Path | None = None) -> QImage | None:
    """cover_image() through a disk cache of the scaled result — for big
    sources (SteamGridDB PNGs run to megabytes and can't be decoded at a
    smaller size), so the next start skips the full decode. Worker-safe."""
    thumbs = thumbs or THUMB_DIR
    try:
        big = os.stat(path).st_size >= THUMB_MIN_BYTES
    except OSError:
        return None
    if not big:
        return cover_image(path, w, h, grey)
    name = hashlib.sha1(f"{path}|{mtime}|{w}x{h}|{int(grey)}".encode()).hexdigest()
    cached = thumbs / f"{name}.png"
    img = QImage(str(cached))
    if not img.isNull():
        return img
    img = cover_image(path, w, h, grey)
    if img is not None:
        try:
            thumbs.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(f'.{os.getpid()}.part')
            if img.save(str(tmp), 'PNG', 80):
                os.replace(tmp, cached)
        except OSError:
            pass
    return img


def rounded_pixmap(src: QPixmap, radius: float) -> QPixmap:
    """`src` with transparent rounded corners — for images shown in widgets
    that can't clip (QLabel pixmaps, item-view icons): a QSS radius never
    clips the image itself."""
    if radius <= 0 or src.isNull():
        return src
    out = QPixmap(src.size())
    out.setDevicePixelRatio(src.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    pt = QPainter(out)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, src.width(), src.height()), radius, radius)
    pt.setClipPath(clip)
    pt.drawPixmap(0, 0, src)
    pt.end()
    return out


_PLACEHOLDERS: dict[tuple, QPixmap] = {}


def placeholder_pixmap(name: str, w: int, h: int) -> QPixmap:
    """Stand-in art: the spec's `{color.surface-2}` tile fill with a faint accent
    wash and the game's initials in muted text. Memoised (it's painted on
    every hero repaint while a game has no art)."""
    key = (name, w, h, P['surface_2'], P['accent'])
    pm = _PLACEHOLDERS.get(key)
    if pm is None:
        if len(_PLACEHOLDERS) > 400:
            _PLACEHOLDERS.clear()
        pm = _PLACEHOLDERS[key] = _paint_placeholder(name, w, h)
    return pm


def _paint_placeholder(name: str, w: int, h: int) -> QPixmap:
    out = QPixmap(w, h)
    out.fill(QColor(P['surface_2']))
    pt = QPainter(out)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 0, h)
    top = QColor(P['accent']); top.setAlphaF(0.12)
    g.setColorAt(0.0, top)
    g.setColorAt(1.0, QColor(P['surface_2']))
    pt.fillRect(0, 0, w, h, QBrush(g))

    initials = ''.join(word[0] for word in name.split()[:3] if word).upper()[:3]
    pt.setPen(QColor(P['muted']))
    pt.setFont(QFont(UI_FONT, 26, QFont.Weight.Medium))
    pt.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, initials or '?')
    pt.end()
    return out


# --------------------------------------------------------------------------
# FlowLayout (reflowing grid) — adapted from the Qt example
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

# Nerd Font (Font Awesome range) glyphs used across the UI.
G = dict(ICONS)


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
    lab.setStyleSheet(f'font-family: "{ICON_FONT}"; font-size: {px}px;'
                      + (f" color: {c};" if c else ""))


def glyph_pixmap(glyph: str, color: str, px: int, dpr: float = 2.0) -> QPixmap:
    pm = QPixmap(int(px * dpr), int(px * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    pt.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    f = QFont(ICON_FONT)
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


def fmt_hours(minutes) -> str | None:
    if not minutes:
        return None
    if minutes < 60:
        return f"{int(minutes)} min"
    return f"{minutes / 60:.1f} h"


def fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def start_steam() -> None:
    QProcess.startDetached('steam', [])


def notify_desktop(summary: str, body: str = '') -> None:
    """Desktop notification (only used while the window isn't focused).
    notify-send reaches whatever notification daemon the session runs; on a
    bare compositor with none, it silently does nothing."""
    if shutil.which('notify-send'):
        QProcess.startDetached('notify-send', [
            '-a', 'Nexus', '-i', str(ICON_PATH) if ICON_PATH.exists()
            else 'applications-games', '-h', f'string:desktop-entry:{NX.APP_ID}',
            '--', summary, body])


# --------------------------------------------------------------------------
# Jobs — every CLI action is a Job; the manager queues them
# --------------------------------------------------------------------------

# Install progress. The CLI prints a step marker at the start of a line
# ("[1/4] Verifying files...", "[3/4] Running setup.exe ...") and, during
# --verify, indented per-file markers ("  [12/40] data.bin: OK").
_STEP_RE = re.compile(r'^\[(\d+)/(\d+)\]\s*(.*)$')
_FILE_RE = re.compile(r'^\s+\[(\d+)/(\d+)\]')
_INSTALL_STEPS = {1: "Checking files", 2: "Setting up the prefix",
                  3: "Installer running — finish it in its window",
                  4: "Adding to your launchers"}
_SILENT_STEP = "Installing — no setup window needed"
# Machine-readable lines from the CLI: "@nexus <event> <json>".
_EVENT_RE = re.compile(r'^@nexus ([\w-]+) (\{.*\})\s*$')


def _cli_env() -> 'QProcessEnvironment':
    env = QProcessEnvironment.systemEnvironment()
    env.insert('PYTHONUNBUFFERED', '1')      # stream status lines, don't block-buffer
    return env


class Job(QObject):
    """One CLI invocation: its state, a headline step, a detail line (last
    output), progress, and its own log. `kind` groups behaviour: 'install',
    'launch' (quick, runs alongside others), 'open' (a steam:// or heroic://
    hand-off that isn't a launch), 'tool', or 'task'."""
    changed = pyqtSignal()
    output = pyqtSignal(str)
    done = pyqtSignal(int)

    PARALLEL = ('launch', 'open', 'tool')

    def __init__(self, argv, title, slug=None, kind='task', name=None):
        super().__init__()
        self.argv, self.title, self.slug, self.kind = argv, title, slug, kind
        self.name = name or title
        self.state = 'queued'            # queued | running | done | failed | cancelled
        self.step = 'Waiting for the current job…'
        self.detail = ''
        self.progress: tuple[int, int] | None = None   # None = indeterminate
        self.log: list[str] = []
        self.code: int | None = None
        self.started = self.ended = None
        self.proc = None
        self._decoder = None
        self._buf = ''
        self.events: dict[str, dict] = {}     # last @nexus event of each kind
        self._rate: tuple[float, int] | None = None
        self._change_due = False

    # Output can arrive hundreds of times a second (an installer's log); the
    # step/detail/progress it moves is announced at most ~8 times a second.
    CHANGE_MS = 120

    def _changed_soon(self):
        if not self._change_due:
            self._change_due = True
            if not hasattr(self, '_change_timer'):   # a child: it dies with the job
                self._change_timer = QTimer(self)
                self._change_timer.setSingleShot(True)
                self._change_timer.timeout.connect(self._flush_change)
            self._change_timer.start(self.CHANGE_MS)

    def _flush_change(self):
        if self._change_due:
            self._change_due = False
            self.changed.emit()

    # ---- lifecycle ----
    def start(self):
        self.state, self.started = 'running', time.monotonic()
        self.step = {'install': "Starting…", 'launch': "Launching…"}.get(self.kind, "Working…")
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(_cli_env())
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        # Incremental: a multi-byte UTF-8 character split across two reads
        # would otherwise decode as two replacement characters.
        self._decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.proc.readyReadStandardOutput.connect(self._drain)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self._emit_text("$ " + " ".join(shlex.quote(a) for a in self.argv) + "\n")
        self.proc.start(self.argv[0], self.argv[1:])
        # EOF on stdin: any unexpected prompt takes its safe default, not a hang.
        self.proc.closeWriteChannel()
        self.changed.emit()

    def cancel(self):
        if self.state == 'queued':
            self._end('cancelled', -1)
        elif self.state == 'running' and self.proc is not None:
            # SIGTERM: the CLI stops whatever it's waiting on (umu-run/wine)
            # too; SIGKILL follows if it hasn't exited in 5 s.
            self.state = 'cancelling'
            self.step = "Cancelling…"
            self.changed.emit()
            proc = self.proc
            proc.terminate()
            # A child timer dies with the process object: a lambda on a bare
            # singleShot outlived it and aborted the app (deleted QProcess).
            # kill() on a process that has already exited does nothing.
            kill = QTimer(proc)
            kill.setSingleShot(True)
            kill.timeout.connect(proc.kill)
            kill.start(5000)

    @property
    def active(self) -> bool:
        return self.state in ('queued', 'running', 'cancelling')

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return (self.ended or time.monotonic()) - self.started

    # ---- process plumbing ----
    def _drain(self):
        if self.proc is None or self._decoder is None:
            return
        text = self._decoder.decode(bytes(self.proc.readAllStandardOutput()))
        if text:
            self._emit_text(text)

    def _emit_text(self, text: str):
        self.log.append(text)
        if len(self.log) > 4000:
            del self.log[:1000]
        self.output.emit(text)
        self._buf += text.replace('\r', '\n')
        *lines, self._buf = self._buf.split('\n')
        before = (self.step, self.detail, self.progress)
        for line in lines:
            self._parse(line)
        if (self.step, self.detail, self.progress) != before:
            self._changed_soon()

    def _parse(self, line: str):
        m = _EVENT_RE.match(line)
        if m:
            try:
                data = json.loads(m.group(2))
            except ValueError:
                return
            self.events[m.group(1)] = data
            if m.group(1) == 'progress':
                self._on_progress(int(data.get('written') or 0))
            return
        m = _STEP_RE.match(line)
        if m:
            n, total = int(m.group(1)), int(m.group(2))
            silent = self.kind == 'install' and n == 3 and '--interactive' not in self.argv
            self.step = (_SILENT_STEP if silent else
                         _INSTALL_STEPS.get(n) if self.kind == 'install' else None) \
                or m.group(3).strip().rstrip('.')
            self.progress = None if (self.kind == 'install' and n == 3) else (n, total)
            self.detail = ''
            return
        m = _FILE_RE.match(line)
        if m:
            self.progress = (int(m.group(1)), int(m.group(2)))
            self.detail = line.strip()
            return
        text = line.strip()
        if text and not text.startswith(('$ ', '=')):
            self.detail = text[:160]

    def _on_progress(self, written: int):
        """Silent installs report bytes written; show that and the speed."""
        now = time.monotonic()
        text = f"{fmt_bytes(written)} written"
        if self._rate and now > self._rate[0] and written >= self._rate[1]:
            speed = (written - self._rate[1]) / (now - self._rate[0])
            if speed > 0:
                text += f" · {fmt_bytes(speed)}/s"
        self._rate = (now, written)
        self.detail = text

    def _finished(self, code, _status):
        self._drain()
        if self._buf.strip():
            self._parse(self._buf)
        state = ('cancelled' if self.state == 'cancelling' else
                 'done' if code == 0 else 'failed')
        self._end(state, code)

    def _error(self, err):
        # FailedToStart never emits finished(); without this the job would
        # stay "running" forever.
        if err == QProcess.ProcessError.FailedToStart:
            self._emit_text(f"[!] Could not start {self.argv[0]}: {self.proc.errorString()}\n")
            self._end('failed', -1)

    def _end(self, state, code):
        if not self.active:
            return
        self.state, self.code, self.ended = state, code, time.monotonic()
        self.step = {'done': "Done", 'failed': f"Failed (exit {code})",
                     'cancelled': "Cancelled"}[state]
        if state == 'failed' and self.detail:
            self.step = f"Failed — {self.detail}"
        self.progress = None
        self._change_due = False               # the final state goes out now
        self.changed.emit()
        self.done.emit(code)


class JobManager(QObject):
    """Runs jobs one at a time (installs and registry edits must not overlap),
    except quick 'launch'/'tool' jobs, which start immediately."""
    added = pyqtSignal(object)
    finished = pyqtSignal(object)
    changed = pyqtSignal()
    output = pyqtSignal(object, str)
    pruned = pyqtSignal(list)                  # finished jobs dropped from the list

    KEEP_FINISHED = 40                         # a long session doesn't grow forever

    def __init__(self):
        super().__init__()
        self.jobs: list[Job] = []

    def submit(self, job: Job) -> Job:
        self.jobs.append(job)
        job.changed.connect(self.changed)
        job.output.connect(lambda text, j=job: self.output.emit(j, text))
        job.done.connect(lambda _c, j=job: self._on_done(j))
        self.added.emit(job)
        if job.kind in Job.PARALLEL:
            job.start()
        else:
            self._pump()
        self.changed.emit()
        return job

    def _on_done(self, job):
        self.finished.emit(job)
        self._pump()
        done = [j for j in self.jobs if not j.active]
        if len(done) > self.KEEP_FINISHED:
            old = done[:len(done) - self.KEEP_FINISHED]
            gone = set(map(id, old))
            self.jobs = [j for j in self.jobs if id(j) not in gone]
            self.pruned.emit(old)
        self.changed.emit()

    def _pump(self):
        if any(j.state in ('running', 'cancelling') and j.kind not in Job.PARALLEL
               for j in self.jobs):
            return
        nxt = next((j for j in self.jobs if j.state == 'queued'), None)
        if nxt:
            nxt.start()

    def active(self) -> list[Job]:
        return [j for j in self.jobs if j.active and j.kind not in Job.PARALLEL]

    def for_slug(self, slug) -> Job | None:
        """The job to show on a game's tile: its queued/running task, or a
        launch that is still starting (a quick spinner while the game boots)."""
        return next((j for j in self.jobs if j.active and j.slug == slug
                     and j.kind not in ('tool', 'open')), None)

    def find(self, argv) -> Job | None:
        """An active job running exactly this command — a double click, or the
        same action asked for from two places."""
        argv = list(argv)
        return next((j for j in self.jobs if j.active and j.argv == argv), None)

    def busy(self, slug) -> bool:
        """Whether a queued or running job is changing this game's files or
        registration (an install, update, uninstall, restore…)."""
        return any(j.active and j.slug == slug and j.kind in ('install', 'task')
                   for j in self.jobs)

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.active]
        self.changed.emit()


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
        self.icon.setStyleSheet(f'font-family: "{ICON_FONT}"; font-size: 34px;'
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
            if self._anim.state() != QVariantAnimation.State.Running:
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
            b = QPushButton(text)
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


# --------------------------------------------------------------------------
# Activity: one card per job; the raw console is only a debug view
# --------------------------------------------------------------------------

class ActivityCard(QFrame):
    """Title, step, progress, elapsed time, Cancel, and (full size) a
    per-job log you can expand. `compact` is the one-line library strip.
    A finished install whose exe was a guess among several lists them, so a
    wrong pick is one click to fix."""
    open_activity = pyqtSignal(object)
    exe_chosen = pyqtSignal(str, str)          # (slug, exe path)

    def __init__(self, job: Job, compact=False):
        super().__init__()
        self.job, self.compact = job, compact
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        outer.setSpacing(SP['1_5'])
        row = QHBoxLayout()
        row.setSpacing(SP['2_5'])
        self.icon = icon_label(G['download'] if job.kind in ('install', 'install-proton')
                               else G['wrench'], 16)
        row.addWidget(self.icon)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = plain_label(job.title)
        self.title.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        self.step = label("", "Muted")
        texts.addWidget(self.title)
        texts.addWidget(self.step)
        row.addLayout(texts, 1)
        self.elapsed = label("", "Muted")
        row.addWidget(self.elapsed)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel)
        row.addWidget(self.btn_cancel)
        self.btn_log = icon_button(G['terminal'], "Show this job's log", 30)
        self.btn_log.clicked.connect(self._toggle_log)
        row.addWidget(self.btn_log)
        outer.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setObjectName("CardProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        outer.addWidget(self.bar)
        self.detail = label("", "Muted")
        outer.addWidget(self.detail)
        self.detail.setVisible(not compact)
        self.exe_box = None
        self.logview = None
        if compact:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        job.changed.connect(self.refresh)
        job.output.connect(self._append)
        self.refresh()

    def mousePressEvent(self, ev):
        if self.compact:
            self.open_activity.emit(self.job)
        super().mousePressEvent(ev)

    def _toggle_log(self):
        if self.compact:
            self.open_activity.emit(self.job)
            return
        if self.logview is None:
            self.logview = QPlainTextEdit()
            self.logview.setObjectName("Console")
            self.logview.setReadOnly(True)
            self.logview.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            self.logview.setMinimumHeight(220)
            self.logview.setMaximumBlockCount(8000)
            self.logview.setPlainText(''.join(self.job.log))
            self.layout().addWidget(self.logview)
            self.logview.moveCursor(self.logview.textCursor().MoveOperation.End)
        else:
            self.logview.setVisible(not self.logview.isVisible())

    def show_log(self):
        if not self.compact and (self.logview is None or not self.logview.isVisible()):
            self._toggle_log()

    def _cancel(self):
        j = self.job
        if j.kind == 'install' and j.state == 'running':
            # Minutes of copying in: one stray click shouldn't end it.
            ask(self, f"Stop installing {j.name}?",
                "What's been copied so far stays; Resume in the library carries on from it "
                "(or Discard throws it away).", "Stop install", j.cancel)
        else:
            j.cancel()

    def _append(self, text):
        if self.logview is not None:
            self.logview.moveCursor(self.logview.textCursor().MoveOperation.End)
            self.logview.insertPlainText(text)

    def refresh(self):
        j = self.job
        self.title.setText(j.title)
        self.step.setText(j.step)
        color = {'failed': P['danger'], 'done': P['success'], 'cancelled': P['muted']}.get(
            j.state, P['accent'])
        if color != getattr(self, '_color', None):     # a restyle is a repolish: only on change
            self._color = color
            self.icon.setStyleSheet(f"color: {color};")
            self.step.setStyleSheet(
                f"color: {color if j.state in ('failed', 'done') else P['muted']};")
        self.detail.setText(j.detail if j.active else '')
        self.detail.setVisible(not self.compact and bool(j.detail) and j.active)
        self.btn_cancel.setVisible(j.active and j.state != 'cancelling')
        self.bar.setVisible(j.active)
        if j.state == 'queued':
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
        elif j.progress:
            self.bar.setRange(0, j.progress[1])
            self.bar.setValue(min(*j.progress))
        else:
            self.bar.setRange(0, 0)            # busy
        if j.state == 'done' and self.exe_box is None and not self.compact:
            self._exe_choices()
        self.tick()

    def _exe_choices(self):
        ev = self.job.events.get('exe-candidates') or {}
        cands, slug = ev.get('candidates') or [], ev.get('slug')
        if len(cands) < 2 or not slug:
            return
        self.exe_box = QFrame()
        lay = QVBoxLayout(self.exe_box)
        lay.setContentsMargins(0, SP['1'], 0, 0)
        lay.setSpacing(SP['1'])
        lay.addWidget(label("Which file starts the game?", "StatLabel"))
        group = QButtonGroup(self.exe_box)
        for c in cands:
            path = Path(c['path'])
            b = QRadioButton(f"{path.name}  ·  {fmt_bytes(c.get('size'))}  ·  {path.parent.name}")
            b.setToolTip(c['path'])
            b.setChecked(c['path'] == ev.get('chosen'))
            group.addButton(b)
            b.toggled.connect(lambda on, p=c['path']: on and self.exe_chosen.emit(slug, p))
            lay.addWidget(b)
        self.layout().insertWidget(self.layout().indexOf(self.detail) + 1, self.exe_box)

    def tick(self):
        j = self.job
        self.elapsed.setText(fmt_elapsed(j.elapsed()) if j.started else '')


class ActivityPage(QWidget):
    """Every job this session, newest first, plus the raw debug log."""

    def __init__(self, jobs: JobManager):
        super().__init__()
        self.jobs = jobs
        self.cards: dict[int, ActivityCard] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        self.page_title = label("Activity", "PageTitle")   # hidden inside SystemPage
        self.content_lay = lay
        head.addWidget(self.page_title)
        head.addStretch()
        clear = QPushButton("Clear finished")
        clear.clicked.connect(self._clear)
        head.addWidget(clear)
        self.btn_debug = QPushButton("Debug log ▾")
        self.btn_debug.setToolTip("Raw output of every job (Ctrl+`)")
        self.btn_debug.clicked.connect(lambda: self.show_debug(not self.debug.isVisible()))
        head.addWidget(self.btn_debug)
        lay.addLayout(head)

        self.split = QSplitter(Qt.Orientation.Vertical)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        self.list = QVBoxLayout(host)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(SP['2'])
        self.empty = EmptyState(G['activity'], "Nothing running",
                                "Installs, Steam and Heroic changes, cleanups and checks "
                                "show up here while they run.")
        self.list.addWidget(self.empty)
        self.list.addStretch()
        scroll.setWidget(host)
        self.split.addWidget(scroll)
        self.debug = QPlainTextEdit()
        self.debug.setObjectName("Console")
        self.debug.setReadOnly(True)
        self.debug.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.debug.setMaximumBlockCount(8000)
        self.debug.setVisible(False)
        # Installer output can arrive hundreds of times a second: appended in
        # batches, and only kept (bounded) while the console is hidden.
        self._debug_buf: list[str] = []
        self._debug_len = 0
        self._debug_timer = QTimer(self)
        self._debug_timer.setSingleShot(True)
        self._debug_timer.setInterval(100)
        self._debug_timer.timeout.connect(self._flush_debug)
        self.split.addWidget(self.debug)
        self.split.setChildrenCollapsible(False)
        lay.addWidget(self.split, 1)

        jobs.added.connect(self._add)
        jobs.pruned.connect(self._pruned)
        jobs.output.connect(self._debug_append)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)                # elapsed time, only while jobs run
        self.timer.timeout.connect(self._tick)
        jobs.changed.connect(lambda: self.timer.start() if jobs.active() else self.timer.stop())

    exe_chosen = pyqtSignal(str, str)

    def _pruned(self, jobs):
        for job in jobs:
            card = self.cards.pop(id(job), None)
            if card is not None:
                card.hide()
                card.deleteLater()
            # The Job itself goes when nothing (a toast's "Show log") holds it.

    def _add(self, job):
        card = ActivityCard(job)
        card.exe_chosen.connect(self.exe_chosen)
        self.cards[id(job)] = card
        self.list.insertWidget(0, card)
        self.empty.setVisible(False)

    def _tick(self):
        for c in self.cards.values():
            if c.job.state in ('running', 'cancelling'):
                c.tick()

    def _clear(self):
        self.jobs.clear_finished()
        for key, card in list(self.cards.items()):
            if not card.job.active:
                card.hide()
                card.deleteLater()
                del self.cards[key]
        self.empty.setVisible(not self.cards)

    DEBUG_KEEP = 2 << 20                      # chars held back while the console is hidden

    def _debug_append(self, job, text):
        self._debug_buf.append(text)
        self._debug_len += len(text)
        while self._debug_len > self.DEBUG_KEEP and len(self._debug_buf) > 1:
            self._debug_len -= len(self._debug_buf.pop(0))
        if self.debug.isVisible() and not self._debug_timer.isActive():
            self._debug_timer.start()

    def _flush_debug(self):
        if not self._debug_buf:
            return
        text, self._debug_buf, self._debug_len = ''.join(self._debug_buf), [], 0
        self.debug.moveCursor(self.debug.textCursor().MoveOperation.End)
        self.debug.insertPlainText(text)

    def show_debug(self, on=True):
        self.debug.setVisible(on)
        if on:
            self._flush_debug()
        self.btn_debug.setText("Debug log ▴" if on else "Debug log ▾")
        if on:
            h = max(self.split.height(), 400)
            self.split.setSizes([int(h * 0.55), int(h * 0.45)])

    def focus_job(self, job):
        card = self.cards.get(id(job))
        if card:
            card.show_log()


class ActivityStrip(QWidget):
    """Compact card for the running job at the bottom of the library."""
    open_activity = pyqtSignal(object)

    def __init__(self, jobs: JobManager):
        super().__init__()
        self.jobs = jobs
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(SP['5'], 0, SP['5'], SP['3'])
        self.lay.setSpacing(SP['2'])
        self.card = None
        self.more = label("", "Muted")
        self.setVisible(False)
        jobs.changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(lambda: self.card and self.card.tick())

    def refresh(self):
        active = self.jobs.active()
        running = next((j for j in active if j.state in ('running', 'cancelling')),
                       active[0] if active else None)
        if running is None:
            self.setVisible(False)
            self.timer.stop()
            return
        self.timer.start()
        if self.card is None or self.card.job is not running:
            if self.card is not None:
                self.card.hide()
                self.card.deleteLater()
            self.card = ActivityCard(running, compact=True)
            self.card.open_activity.connect(self.open_activity)
            self.lay.insertWidget(0, self.card, 1)
            self.lay.addWidget(self.more)
        queued = len(active) - 1
        self.more.setText(f"+{queued} queued" if queued > 0 else "")
        self.setVisible(True)


class CliQuery(QObject):
    """Run a short read-only CLI command (e.g. --list --json) without blocking
    the GUI thread; `callback(stdout_text or None, error_text)` runs on
    completion. Kept alive by a class-level set until it finishes."""
    _live: set = set()

    def __init__(self, args, callback, timeout_ms=60000):
        super().__init__()
        self._cb = callback
        self._done = False
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(_cli_env())
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        CliQuery._live.add(self)
        QTimer.singleShot(timeout_ms, self._timeout)
        self.proc.start(CLI_BIN, args)

    def _complete(self, out, err):
        if self._done:
            return
        self._done = True
        CliQuery._live.discard(self)
        try:
            self._cb(out, err)
        finally:
            self.deleteLater()

    def _finished(self, code, _status):
        out = bytes(self.proc.readAllStandardOutput()).decode('utf-8', 'replace')
        err = bytes(self.proc.readAllStandardError()).decode('utf-8', 'replace').strip()
        if code != 0 and 'Traceback (most recent call last)' in err:
            err = err.splitlines()[-1]         # the one-line summary, not the stack
        self._complete(out if code == 0 else None, err or f"exit {code}")

    def _error(self, e):
        if e == QProcess.ProcessError.FailedToStart:
            self._complete(None, f"could not run {CLI_BIN}: {self.proc.errorString()}")

    def _timeout(self):
        if not self._done:
            self.proc.kill()
            self._complete(None, "timed out")


class _Dispatch(QObject):
    """Carries pool results back to the GUI thread. One, created on the GUI
    thread and never deleted, so a worker never owns (or frees) a GUI-thread
    object; callbacks stay on the GUI thread, keyed by a token."""
    done = pyqtSignal(int, object, object)

    def __init__(self):
        super().__init__()
        self.callbacks: dict[int, object] = {}
        self.next = 0
        self.done.connect(self._fire)

    def _fire(self, token, result, error):
        callback = self.callbacks.pop(token, None)
        if callback is None:
            return
        try:
            callback(result, error)
        except RuntimeError:              # its window was closed meanwhile
            pass


class _Task(QRunnable):
    def __init__(self, fn, token):
        super().__init__()
        self.fn, self.token = fn, token

    def run(self):
        try:
            result, error = self.fn(), None
        except Exception as e:            # handed to the callback, never lost
            result, error = None, e
        self.fn = None                    # nothing of the caller's is freed here later
        _DISPATCH.done.emit(self.token, result, error)


_DISPATCH: '_Dispatch | None' = None


def in_thread(fn, callback):
    """Run fn() on Qt's thread pool and callback(result, error) on the GUI
    thread — for in-process reads (/proc, Steam manifests) that shouldn't
    cost a subprocess or block the window. fn must not touch widgets."""
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = _Dispatch()
    _DISPATCH.next += 1
    _DISPATCH.callbacks[_DISPATCH.next] = callback
    QThreadPool.globalInstance().start(_Task(fn, _DISPATCH.next))


def match_running(games: list[dict], r: dict) -> set:
    """Slugs of the library games in steamlib.running()'s snapshot: Steam's
    reaper AppId (Steam games and repack shortcuts), Nexus's own session
    trackers, a repack's exe name, or a Heroic game's install folder."""
    appids, slugs, exes = set(r['appids']), set(r['slugs']), set(r['exes'])
    out = set()
    for g in games:
        src = g.get('source', 'nexus')
        if src == 'nexus':
            exe = (g.get('exe') or '').replace('\\', '/').rsplit('/', 1)[-1].lower()
            if g['slug'] in slugs or int(g.get('steam_appid') or 0) in appids \
                    or (exe and exe in exes):
                out.add(g['slug'])
        elif src == 'steam':
            if g.get('appid') in appids:
                out.add(g['slug'])
        elif src == 'heroic':
            d = g.get('install_dir')
            if d and steamlib.in_dir_cmdline(d, r['paths']):
                out.add(g['slug'])
    return out


_CLI_MEMO: dict[tuple, tuple[float, dict]] = {}     # args -> (when, answer)
_CLI_WAITING: dict[tuple, list] = {}                 # args -> callbacks of a query in flight


def cli_cache_clear():
    """Forget remembered answers — after any job, which may have changed them."""
    _CLI_MEMO.clear()


def cli_json(args, callback, timeout_ms=60000, ttl: float = 0):
    """CliQuery that parses stdout as JSON: callback(dict_or_None, error).
    With `ttl` (seconds), a recent answer is handed back at once and an
    identical query already running is shared, so moving between pages or
    games doesn't start the same subprocess again."""
    key = tuple(args)
    if ttl:
        hit = _CLI_MEMO.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            callback(hit[1], '')
            return None
        if key in _CLI_WAITING:
            _CLI_WAITING[key].append(callback)
            return None
        _CLI_WAITING[key] = [callback]

    def deliver(data, err):
        cbs = _CLI_WAITING.pop(key, [callback]) if ttl else [callback]
        if ttl and data is not None:
            _CLI_MEMO[key] = (time.monotonic(), data)
        for cb in cbs:
            try:
                cb(data, err)
            except RuntimeError:               # its widget is gone
                pass

    def done(out, err):
        if out is None:
            deliver(None, err)
            return
        try:
            data = json.loads(out)
        except ValueError as e:
            deliver(None, f"bad JSON from CLI: {e}")
            return
        deliver(data, '')
    return CliQuery(args, done, timeout_ms)


# --------------------------------------------------------------------------
# Artwork lookup — one place decides which image a game shows for each role
# --------------------------------------------------------------------------

def art_lookup(game: dict, kinds) -> tuple[str | None, str | None, str | None]:
    """First available image for the given kinds, in order: a local file the
    library knows about, a cached remote URL, or SteamGridDB art in the
    game's artwork dir. Returns (path, url_to_fetch, kind): when nothing is
    on disk yet, the first remote URL is handed back so the caller can fetch
    it."""
    pending = None
    adir = game.get('artwork_dir')
    for kind in kinds:
        p = game.get(kind)
        if p and Path(p).exists():
            return p, None, kind
        url = game.get(f'{kind}_url')
        if url:
            cached = ART.cached(url)
            if cached:
                return cached, None, kind
            pending = pending or url
        if adir and Path(adir).is_dir():
            hits = sorted(Path(adir).glob(f'{kind}.*'))
            if hits:
                return str(hits[0]), None, kind
    return None, pending, None


def art_source(game: dict, kinds) -> tuple[str | None, str | None]:
    path, url, _kind = art_lookup(game, kinds)
    return path, url


HERO_KINDS = ('hero', 'wide', 'cover')
WIDE_KINDS = ('wide', 'hero', 'cover')
COVER_KINDS = ('cover',)
LOGO_KINDS = ('logo',)


# --------------------------------------------------------------------------
# Game tile (§3.12) — just the art; title on hover; Play and ⋮ on hover
# --------------------------------------------------------------------------

class GameTile(QFrame):
    """A game's art, nothing else at rest — no badges or tags (all of that
    lives on the game page). On hover/focus the title fades in with one big
    Play button and a ⋮ menu button. A running job replaces Play with a
    progress ring; a playing game gets a small green dot.

    kind='cover' is the portrait 2/3 tile; kind='wide' is a 16:9 card using
    the game's wide/hero art, which keeps its title visible (hero art rarely
    carries the game's name).

    The widget reserves TILE_LIFT px at the top and paints its body offset into
    it, so the hover lift never re-runs the layout."""
    selected = pyqtSignal(dict, object)     # (game, keyboard modifiers)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)            # Enter / controller A (Settings → Controls)
    double = pyqtSignal(dict)               # a double click (Settings → Controls)
    _loading: dict[tuple, QPixmap] = {}     # shared "art on its way" fills, per size

    def __init__(self, game: dict, pending: bool = False, width: int = TILE_W,
                 kind: str = 'cover', theatre: bool = False):
        super().__init__()
        self.game, self.pending, self.kind = game, pending, kind
        self.theatre = theatre                  # bigger focus glow, read from the couch
        self.tw = width
        self.th = int(width * 9 / 16) if kind == 'wide' else int(width * 3 / 2)
        self.setObjectName("Tile")
        self.setFixedSize(self.tw, self.th + TILE_LIFT)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = 0.0
        self._selected = False
        self.job: Job | None = None
        self.mark_new = False                   # "New" pill: installed, never played
        self._spin = 0

        self._art = None
        art_path, url, self._art_kind = art_lookup(game, WIDE_KINDS if kind == 'wide'
                                                   else COVER_KINDS)
        self._art_sig = self._sig_of(game, art_path, url)
        if not art_path and url:
            ART.fetch(url, self._art_arrived)
        self._set_art(art_path)

        self._anim = QPropertyAnimation(self, b'hoverT', self)
        self._anim.setDuration(150)              # motion.fast
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._spinner = QTimer(self)
        self._spinner.setInterval(40)
        self._spinner.timeout.connect(self._advance_spin)

        self._pb = 56 if min(self.tw, self.th) >= 170 else 44
        self.btn_play = QToolButton(self)
        self.btn_play.setObjectName("PlayRound")
        self.btn_play.setText(G['play'])
        self.btn_play.setToolTip("Play")
        self.btn_play.setFixedSize(self._pb, self._pb)
        self.btn_play.setStyleSheet(f"border-radius: {pill(self._pb)}px;")
        self.btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play.clicked.connect(lambda: self.play.emit(self.game))
        self.btn_more = QToolButton(self)
        self.btn_more.setObjectName("TileMore")
        self.btn_more.setText(G['more'])
        self.btn_more.setToolTip("More actions")
        self.btn_more.setFixedSize(28, 28)
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_more.clicked.connect(lambda: self.menu.emit(
            self.game, self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height()))))
        # Mouse affordances only: keyboard/controller use Enter (details) and
        # the Menu key / X (menu), so these never steal focus from the tile.
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.hide()
        self.btn_more.hide()
        self._place_buttons()
        self.setToolTip(self._tooltip())

    def _set_art(self, art_path):
        # Not installed: the art in grey, so the library reads at a glance.
        grey = not is_installed(self.game)
        pm = ART.peek(art_path, self.tw, self.th, grey) if art_path else None
        if pm is None and art_path:
            # Decoded in the background: a plain card until it's there.
            self._art_for = art_path
            key = (self.tw, self.th, P['surface_2'])
            if key not in GameTile._loading:
                fill = GameTile._loading[key] = QPixmap(self.tw, self.th)
                fill.fill(QColor(P['surface_2']))
            self._art = GameTile._loading[key]
            ART.request(art_path, self.tw, self.th,
                        lambda pm, p=art_path: self._art_decoded(p, pm), grey)
            return
        self._art_for = None
        self._apply_art(pm)

    def _art_decoded(self, path, pm):
        if getattr(self, '_art_for', None) == path:
            self._apply_art(pm)
            self.update()

    def _apply_art(self, pm):
        if pm is None and self.pending:
            pm = QPixmap(self.tw, self.th)       # no initials under the progress ring
            pm.fill(QColor(P['surface_2']))
        self._art = pm if pm is not None else placeholder_pixmap(
            self.game.get('display_name', '?'), self.tw, self.th)

    ART_KEYS = ('cover', 'cover_url', 'wide', 'wide_url', 'hero', 'hero_url', 'artwork_dir',
                'installed', 'display_name')

    def set_game(self, game: dict):
        """Re-point a kept tile at a fresh copy of its game (a library
        reload); the art is only reloaded when it changed."""
        self.game = game
        art_path, url, kind = art_lookup(game, WIDE_KINDS if self.kind == 'wide'
                                         else COVER_KINDS)
        # A new pick is written over the old file (same path), and an install
        # turns grey art to colour: the file's mtime and the install state
        # count as much as the path.
        sig = self._sig_of(game, art_path, url)
        if sig != self._art_sig:
            self._art_sig, self._art_kind = sig, kind
            if not art_path and url:
                ART.fetch(url, self._art_arrived)
            self._set_art(art_path)
        self.setToolTip(self._tooltip())
        self._sync_buttons()
        self.update()

    @staticmethod
    def _sig_of(game, art_path, url):
        try:
            mtime = os.stat(art_path).st_mtime_ns if art_path else 0
        except OSError:
            mtime = 0
        return art_path, mtime, url, is_installed(game)

    def _art_arrived(self, path):
        try:
            self._art_kind = art_lookup(self.game, WIDE_KINDS if self.kind == 'wide'
                                        else COVER_KINDS)[2]
            self._set_art(path)
            self.update()
        except RuntimeError:                     # tile already rebuilt
            pass

    # ---- state ----
    def set_selected(self, on: bool):
        self._selected = on
        self.update()

    def set_job(self, job: 'Job | None'):
        self.job = job
        if job and job.active:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._sync_buttons()
        self.update()

    def _advance_spin(self):
        self._spin = (self._spin + 12) % 360
        if not (self.job and self.job.active):
            self._spinner.stop()
            self._sync_buttons()
        self.update()

    # ---- hover animation driver ----
    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, v: float):
        self._hover = v
        self._place_buttons()
        self.update()

    hoverT = pyqtProperty(float, _get_hover, _set_hover)

    def _place_buttons(self):
        top = TILE_LIFT - TILE_LIFT * self._hover
        cy = self.th * (0.42 if self.kind == 'cover' else 0.40)
        self.btn_play.move((self.tw - self._pb) // 2, int(top + cy - self._pb / 2))
        self.btn_more.move(self.tw - 28 - SP['2'], int(top + SP['2']))

    def _sync_buttons(self):
        # Mouse only: with a controller or keys, A/Enter opens the game page
        # (Play is right there) and X/Menu opens the menu, so the overlay
        # buttons would just cover the art.
        # A click (or A) opens the game's page, where Play is; the card itself
        # only offers ⋮ on mouse hover.
        self.btn_play.setVisible(False)
        self.btn_more.setVisible(self.underMouse() and not self.pending and not self.theatre
                                 and input_mode() == 'mouse')

    def _animate(self, to: float):
        self._anim.stop()
        if not self.isVisible():
            # Hidden (its window closing, a page switched away): no glide —
            # an animation started now would step a tile that may be gone.
            self._hover = to
            return
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(to)
        self._anim.start()

    def _hovered(self, on: bool):
        self._animate(1.0 if on else 0.0)
        self._sync_buttons()

    def enterEvent(self, ev):
        self._hovered(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self.hasFocus():
            self._sync_buttons()           # keep the lift, drop the buttons
        else:
            self._hovered(False)
        super().leaveEvent(ev)

    def focusInEvent(self, ev):
        self._hovered(True)
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        if not self.underMouse():
            self._hovered(False)
        super().focusOutEvent(ev)

    # ---- input ----
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.game, ev.modifiers())
        elif ev.button() == Qt.MouseButton.MiddleButton and not self.pending:
            self.menu.emit(self.game, ev.globalPosition().toPoint())   # the ⋮ menu
            return
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if not self.pending and ev.button() == Qt.MouseButton.LeftButton:
            self.double.emit(self.game)          # play, or open (Settings → Controls)
        super().mouseDoubleClickEvent(ev)

    def contextMenuEvent(self, ev):
        if not self.pending:
            if not self.theatre:          # in Theatre, selecting opens the game page
                self.selected.emit(self.game, Qt.KeyboardModifier.NoModifier)
            self.menu.emit(self.game, ev.globalPos())

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.pending:
                self.selected.emit(self.game, Qt.KeyboardModifier.NoModifier)
            else:
                self.activated.emit(self.game)
        elif ev.key() == Qt.Key.Key_Menu:
            self.menu.emit(self.game, self.mapToGlobal(QPoint(self.tw // 2, self.th // 2)))
        else:
            super().keyPressEvent(ev)

    def _tooltip(self) -> str:
        g = self.game
        lines = [f"<b>{html.escape(g.get('display_name', ''))}</b>"]
        stats = [s for s in (fmt_relative(g.get('last_played')),
                             fmt_hours(g.get('playtime_minutes')),
                             fmt_size(g.get('disk_bytes'))) if s]
        if stats:
            lines.append(" · ".join(stats))
        if g.get('proton'):
            lines.append(f"Proton: {html.escape(g['proton'])}")
        lines.append("<i>Click to open · right-click for actions</i>")
        return "<br>".join(lines)

    # ---- painting ----
    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        top = TILE_LIFT - TILE_LIFT * self._hover
        body = QRectF(0, top, self.tw, self.th)
        rad = R['tile']
        clip = QPainterPath()
        clip.addRoundedRect(body, rad, rad)
        pt.save()
        pt.setClipPath(clip)

        scale = 1.0 + 0.04 * self._hover
        aw, ah = self.tw * scale, self.th * scale
        pt.drawPixmap(QRectF(body.center().x() - aw / 2, body.center().y() - ah / 2, aw, ah),
                      self._art, QRectF(self._art.rect()))
        owned = not is_installed(self.game)
        if self.pending or self._hover > 0.01 or (self.job and self.job.active) or owned:
            # A light scrim so the Play button / progress ring reads on any art.
            dim = QColor(0, 0, 0)
            dim.setAlphaF(0.55 if (self.pending or self.job) else
                          max(0.35 - 0.2 * self._hover, 0.28 * self._hover) if owned else
                          0.28 * self._hover)
            pt.fillRect(body, dim)

        # The caption (and its gradient) fades in with hover; pending tiles and
        # wide cards always show it.
        cap = 1.0 if (self.pending or self.kind == 'wide') else self._hover
        if cap > 0.01:
            cap_h = self.th * (0.42 if self.kind == 'cover' else 0.55)
            g = QLinearGradient(0, body.bottom(), 0, body.bottom() - cap_h)
            c0 = QColor(0, 0, 0); c0.setAlphaF(0.88 * cap)
            c1 = QColor(0, 0, 0); c1.setAlphaF(0.45 * cap)
            c2 = QColor(0, 0, 0); c2.setAlphaF(0.0)
            g.setColorAt(0.0, c0); g.setColorAt(0.45, c1); g.setColorAt(1.0, c2)
            pt.fillRect(QRectF(0, body.bottom() - cap_h, self.tw, cap_h), QBrush(g))
            pt.setOpacity(cap)
            self._paint_caption(pt, body)
            pt.setOpacity(1.0)
        pt.restore()

        if self.game.get('playing') or self.game.get('launching'):
            self._paint_playing(pt, body)
        if self.job and self.job.active:
            self._paint_ring(pt, body)
        elif owned and self.game.get('installing'):
            f = install_fraction(self.game)
            self._paint_progress(pt, body, f, f"{int(f * 100)}%" if f is not None else "")
        elif owned:
            self._paint_download_badge(pt, body)
        elif self.mark_new and not self.game.get('last_played'):
            self._paint_new_badge(pt, body)

        pt.setBrush(Qt.BrushStyle.NoBrush)
        if self._selected:
            g = QLinearGradient(body.topLeft(), body.bottomRight())
            a, b = (QColor(c) for c in GRAD['dusk'])
            g.setColorAt(0.0, a); g.setColorAt(1.0, b)
            pt.setPen(QPen(QBrush(g), 2.5))
            pt.drawRoundedRect(body.adjusted(1.25, 1.25, -1.25, -1.25), rad - 1, rad - 1)
        elif self._hover > 0.01 and not self.hasFocus():   # focus: the FocusRing
            ring = QColor(P['accent']); ring.setAlphaF(min(0.6, self._hover))
            pt.setPen(QPen(ring, 1.5))
            pt.drawRoundedRect(body.adjusted(1, 1, -1, -1), rad - 1, rad - 1)
        else:
            hair = QColor(P['on_art']); hair.setAlphaF(0.06)
            pt.setPen(QPen(hair, 1))
            pt.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        pt.end()

    def _paint_playing(self, pt: QPainter, body: QRectF):
        """A small green dot with a soft halo, top-right — the only status a
        tile shows at rest."""
        c = QPointF(body.right() - SP['3'], body.top() + SP['3'])
        role = 'success' if self.game.get('playing') else 'accent'   # starting: accent
        halo = QColor(P[role]); halo.setAlphaF(0.30)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(halo)
        pt.drawEllipse(c, 7.0, 7.0)
        pt.setBrush(QColor(P[role]))
        pt.drawEllipse(c, 4.0, 4.0)

    def _paint_ring(self, pt: QPainter, body: QRectF):
        prog = self.job.progress if self.job.state == 'running' else None
        if prog and prog[1]:
            frac = min(prog[0], prog[1]) / prog[1]
            self._paint_progress(pt, body, frac, f"{int(frac * 100)}%")
        else:
            self._paint_progress(pt, body, None, "…" if self.job.state == 'queued' else "")

    def _paint_download_badge(self, pt: QPainter, body: QRectF):
        """Owned, not installed: a small download mark, top-left."""
        d = 24
        r = QRectF(body.left() + SP['2'], body.top() + SP['2'], d, d)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['scrim_badge']))
        pt.drawEllipse(r)
        f = QFont(ICON_FONT)
        f.setPixelSize(15)
        pt.setFont(f)
        pt.setPen(QColor(P['on_art']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, G['download'])

    def _paint_new_badge(self, pt: QPainter, body: QRectF):
        """Installed but never played: a small "New" pill, top-left."""
        f = QFont(UI_FONT, -1, QFont.Weight.DemiBold)
        f.setPixelSize(FS['xs'])
        pt.setFont(f)
        w = pt.fontMetrics().horizontalAdvance("New") + 2 * SP['2']
        r = QRectF(body.left() + SP['2'], body.top() + SP['2'], w, 20)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['accent']))
        pt.drawRoundedRect(r, 10, 10)
        pt.setPen(QColor(P['on_accent']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, "New")

    def _paint_progress(self, pt: QPainter, body: QRectF, frac: float | None, text: str):
        d = 60
        cy = self.th * (0.42 if self.kind == 'cover' else 0.40)
        rect = QRectF(body.center().x() - d / 2, body.top() + cy - d / 2, d, d)
        track = QColor(P['on_art']); track.setAlphaF(0.18)
        pt.setPen(QPen(track, 5))
        pt.drawEllipse(rect)
        pen = QPen(QColor(P['accent']), 5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pt.setPen(pen)
        if frac is not None:
            pt.drawArc(rect, 90 * 16, -int(frac * 360 * 16))
        else:
            pt.drawArc(rect, -self._spin * 16, 100 * 16)
        if text:
            f = QFont(UI_FONT, -1, QFont.Weight.Medium)
            f.setPixelSize(FS['xs'])
            pt.setFont(f)
            pt.setPen(QColor(P['on_art']))
            pt.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_caption(self, pt: QPainter, body: QRectF):
        """Title (2-line clamp) plus a muted stats line, over the caption gradient."""
        pad = SP['2_5']
        avail = self.tw - pad * 2
        sub_f = QFont(UI_FONT, -1)
        sub_f.setPixelSize(FS['2xs'] + 1)
        if self.pending:
            sub = self.job.step if self.job else "Queued"
        elif not is_installed(self.game):
            sub = play_label(self.game) if self.game.get('installing') else " · ".join(
                s for s in ("Not installed", fmt_hours(self.game.get('playtime_minutes'))) if s)
        else:
            sub = " · ".join(s for s in (fmt_relative(self.game.get('last_played')),
                                         fmt_hours(self.game.get('playtime_minutes'))) if s) \
                or "Not played yet"
        pt.setFont(sub_f)
        sfm = pt.fontMetrics()
        sub = sfm.elidedText(sub, Qt.TextElideMode.ElideRight, avail)
        sub_y = body.bottom() - SP['2'] - sfm.descent()
        muted = QColor(P['on_art']); muted.setAlphaF(0.72)
        pt.setPen(muted)
        pt.drawText(QPoint(int(pad), int(sub_y)), sub)

        f = QFont(UI_FONT, -1, QFont.Weight.Medium)
        f.setPixelSize(FS['xs'])
        pt.setFont(f)
        fm = pt.fontMetrics()
        words = self.game.get('display_name', '').split()
        lines, cur = [], ''
        for w in words:
            trial = f"{cur} {w}".strip()
            if fm.horizontalAdvance(trial) <= avail or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
                if len(lines) == 2:
                    break
        if cur and len(lines) < 2:
            lines.append(cur)
        consumed = len(' '.join(lines).split())
        if lines and consumed < len(words):
            lines[-1] = fm.elidedText(' '.join(words[len(' '.join(lines[:-1]).split()):]),
                                      Qt.TextElideMode.ElideRight, avail)
        if self.kind == 'wide' and getattr(self, '_art_kind', None) == 'wide' \
                and not self.pending:
            return                       # the banner art already names the game
        lh = int(fm.height() * 1.25)
        y = sub_y - sfm.height() - SP['0_5'] - lh * (len(lines) - 1)
        pt.setPen(QColor(P['on_art']))
        for i, line in enumerate(lines):
            pt.drawText(QPoint(int(pad), int(y + lh * i)), line)


class ArtCache(QObject):
    """Remote cover/hero art (Steam CDN, Epic/GOG URLs) cached on disk under
    ~/.cache/nexus/art, fetched asynchronously so a tile never waits on the
    network. Decoded pixmaps are memoised per (path, size)."""

    def __init__(self):
        super().__init__()
        self.dir = NX.CACHE_DIR / 'art'
        self.net = None
        self.waiting: dict[str, list] = {}
        # url -> when it failed: not asked again for RETRY_AFTER (offline,
        # every tile would wait out a timeout), but not given up on for good.
        self.failed: dict[str, float] = {}
        self.pix: dict[tuple, QPixmap] = {}    # insertion order = recency (LRU)
        # Decodes asked for by tiles: on the thread pool, so a grid of a few
        # hundred games appears at once and fills in without stalling.
        self.queue: dict[tuple, list] = {}
        self.thumbs = THUMB_DIR

    MAX_PIX = 900                              # decoded pixmaps kept (~a few hundred MB max)
    RETRY_AFTER = 600                          # seconds before a failed download is retried

    def forget_failures(self):
        """A manual refresh tries every failed image again."""
        self.failed.clear()

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha1(url.encode()).hexdigest() + '.img')

    def cached(self, url: str) -> str | None:
        p = self._path(url)
        return str(p) if p.exists() and p.stat().st_size > 0 else None

    def fetch(self, url: str, callback):
        if not url.startswith('https://') or \
                time.monotonic() - self.failed.get(url, -1e9) < self.RETRY_AFTER:
            return
        if url in self.waiting:
            self.waiting[url].append(callback)
            return
        self.waiting[url] = [callback]
        if self.net is None:
            self.net = QNetworkAccessManager(self)
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, 'nexus')
        req.setTransferTimeout(30000)
        reply = self.net.get(req)
        # Stop an oversized "image" as it arrives, not after it's all in memory.
        reply.downloadProgress.connect(
            lambda got, _total, r=reply: r.abort() if got >= self.MAX_BYTES else None)
        reply.finished.connect(lambda r=reply, u=url: self._done(r, u))

    MAX_BYTES = 16 << 20

    def _done(self, reply, url):
        data = bytes(reply.readAll())
        ok = (reply.error() == QNetworkReply.NetworkError.NoError
              and 0 < len(data) < self.MAX_BYTES)
        reply.deleteLater()
        if not ok:
            self.waiting.pop(url, None)
            self.failed[url] = time.monotonic()
            return
        path = self._path(url)

        def store():                       # worker: check it's an image, write it
            if QImage.fromData(data).isNull():
                return None
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f'.{threading.get_ident()}.part')
            tmp.write_bytes(data)
            os.replace(tmp, path)
            return str(path)
        in_thread(store, lambda saved, _err, u=url: self._stored(u, saved))

    def _stored(self, url, saved):
        cbs = self.waiting.pop(url, [])
        if not saved:
            self.failed[url] = time.monotonic()
            return
        for cb in cbs:
            try:
                cb(saved)
            except RuntimeError:                 # the tile is gone
                pass

    @staticmethod
    def _key(path, w, h, radius, grey):
        try:
            return (path, Path(path).stat().st_mtime, w, h, radius, grey)
        except (OSError, TypeError):
            return None

    def peek(self, path: str, w: int, h: int, grey: bool = False) -> QPixmap | None:
        """Already decoded? (No decoding here.)"""
        key = self._key(path, w, h, 0, grey)
        return self.pix.get(key) if key else None

    def request(self, path: str, w: int, h: int, callback, grey: bool = False):
        """Decode on the thread pool, then callback(pixmap or None) here."""
        key = self._key(path, w, h, 0, grey)
        if key is None:
            callback(None)
            return
        first = key not in self.queue
        self.queue.setdefault(key, []).append(callback)
        if first:
            thumbs = self.thumbs
            in_thread(lambda k=key: thumb_image(k[0], k[1], k[2], k[3], k[5], thumbs),
                      lambda img, _err, k=key: self._decoded(k, img))

    def _decoded(self, key, img):
        cbs = self.queue.pop(key, [])
        pm = QPixmap.fromImage(img) if img is not None else None
        if pm is not None:
            self.pix.pop(key, None)
            while len(self.pix) >= self.MAX_PIX:
                self.pix.pop(next(iter(self.pix)))
            self.pix[key] = pm
        for cb in cbs:
            try:
                cb(pm)
            except RuntimeError:                 # the tile is gone
                pass

    def pixmap(self, path: str, w: int, h: int, radius: float = 0,
               grey: bool = False) -> QPixmap | None:
        """Centre-cropped w x h art, optionally with rounded corners or in
        greyscale. Keyed by mtime too, so a newly picked image replaces the
        old one."""
        try:
            mtime = Path(path).stat().st_mtime if path else None
        except OSError:
            mtime = None
        if mtime is None:
            return None
        key = (path, mtime, w, h, radius, grey)
        pm = self.pix.pop(key, None)
        if pm is None:
            pm = cover_pixmap(path, w, h)
            if pm is None:
                return None
            if grey:
                pm = QPixmap.fromImage(pm.toImage().convertToFormat(
                    QImage.Format.Format_Grayscale8))
            pm = rounded_pixmap(pm, radius)
            while len(self.pix) >= self.MAX_PIX:
                self.pix.pop(next(iter(self.pix)))
        self.pix[key] = pm
        return pm

    def pixmap_async(self, path: str, w: int, h: int, radius: float, callback):
        """pixmap(), decoded on the thread pool: callback(pixmap) here, now if
        it's already decoded, else once it is (never with None)."""
        try:
            key = (path, Path(path).stat().st_mtime, w, h, radius, False)
        except (OSError, TypeError):
            return
        if key in self.pix:
            callback(self.pixmap(path, w, h, radius))
            return

        def done(pm, key=key):
            if pm is None:
                return
            pm = rounded_pixmap(pm, radius)
            while len(self.pix) >= self.MAX_PIX:
                self.pix.pop(next(iter(self.pix)))
            self.pix[key] = pm
            callback(pm)
        self.request(path, w, h, done)


ART = ArtCache()


# --------------------------------------------------------------------------
# Dashboard layout model — pure functions, so they're easy to test
# --------------------------------------------------------------------------

DASH_SPAN = {'small': 1, 'wide': 2, 'full': 99}
# (id, sizes it allows, default size, hidden by default)
DASH_KINDS = [                       # default order packs evenly in 3 columns
    ('jump',       ('full',),                  'full',  False),
    ('surprise',   ('full',),                  'full',  False),
    ('system',     ('small', 'wide'),          'wide',  False),
    ('news',       ('small', 'wide', 'full'),  'small', False),
    ('installed',  ('full',),                  'full',  False),
    ('deals',      ('small', 'wide', 'full'),  'wide',  False),
    ('upcoming',   ('small', 'wide', 'full'),  'small', False),
    ('favourites', ('full',),                  'full',  False),
    ('collections', ('full',),                 'full',  False),
    ('updates',    ('full',),                  'full',  False),
    ('playtime',   ('small', 'wide'),          'small', True),
    ('achievements', ('small', 'wide'),        'small', True),
    ('owned',      ('full',),                  'full',  True),
]
DASH_SIZES = {k: sizes for k, sizes, _d, _h in DASH_KINDS}
DASH_DEFAULT_SIZE = {k: d for k, _s, d, _h in DASH_KINDS}
# Widgets folded into another: a saved layout's first one becomes the new
# widget in its place (so it doesn't jump to the end).
DASH_MERGED = {'activity': 'system', 'storage': 'system', 'health': 'system',
               'proton': 'system'}
# Widgets added later that belong next to a related one rather than last.
DASH_PLACE_AFTER = {'upcoming': 'deals', 'surprise': 'jump', 'collections': 'favourites'}


def dash_default() -> list[dict]:
    return [{'id': k, 'size': d, 'hidden': h} for k, _s, d, h in DASH_KINDS]


def dash_normalize(items) -> list[dict]:
    """A saved layout made safe: unknown widgets dropped, bad sizes fixed,
    merged widgets (DASH_MERGED) replaced in place by the one they became,
    and widgets added since it was saved put in (next to their DASH_PLACE_AFTER
    anchor, else at the end) with their defaults."""
    items = [it for it in (items if isinstance(items, list) else []) if isinstance(it, dict)]
    legacy: dict[str, list[dict]] = {}
    for it in items:
        if it.get('id') in DASH_MERGED:
            legacy.setdefault(DASH_MERGED[it['id']], []).append(it)
    out, seen = [], set()
    for it in items:
        wid = it.get('id')
        if wid in DASH_MERGED:
            wid = DASH_MERGED[wid]
            if wid in seen:
                continue
            olds = legacy[wid]
            it = {'id': wid, 'size': None, 'hidden': all(o.get('hidden') for o in olds)}
        if wid not in DASH_SIZES or wid in seen:
            continue
        seen.add(wid)
        size = it.get('size')
        out.append({'id': wid,
                    'size': size if size in DASH_SIZES[wid] else DASH_DEFAULT_SIZE[wid],
                    'hidden': bool(it.get('hidden'))})
    for d in dash_default():
        if d['id'] in seen:
            continue
        anchor = DASH_PLACE_AFTER.get(d['id'])
        at = next((i for i, o in enumerate(out) if o['id'] == anchor), None)
        if at is None:
            out.append(d)
        else:
            out.insert(at + 1, d)
        seen.add(d['id'])
    return out


def system_badge(active: int, problems: int, fails: int) -> tuple[str, str]:
    """The System icon's badge: running jobs first (accent), else health
    problems (danger when anything failed, else warning), else none."""
    if active:
        return str(active), 'accent'
    if problems:
        return str(problems), 'danger' if fails else 'warning'
    return '', 'accent'


def system_status(jobs: list[dict], disks: list[dict] | None, checks: list[dict] | None,
                  proton: dict | None) -> dict:
    """The Home System widget's four tiles and the one thing (if any) that
    needs attention, from plain data. `jobs` are {title, state, step,
    progress (pct|None), active}; `disks` as from --library; `checks` from
    --doctor; `proton` the Proton page's info ({} while loading, None if the
    check never ran)."""
    tiles, attention = [], None
    running = [j for j in jobs if j.get('active')]
    failed = [j for j in jobs if j.get('state') == 'failed']
    if running:
        tiles.append(('activity', 'activity', f"{len(running)} running",
                      running[0].get('title') or '', 'accent'))
    elif failed:
        tiles.append(('activity', 'activity', f"{len(failed)} failed",
                      failed[-1].get('title') or '', 'danger'))
    else:
        tiles.append(('activity', 'activity', "Idle", "Nothing running", 'muted'))
    real = [d for d in disks or [] if d.get('total')]
    fullest = max(real, key=lambda d: d['used'] / d['total']) if real else None
    if fullest:
        used = fullest['used'] / fullest['total']
        name = Path(fullest.get('path') or '/').name or fullest.get('path') or '/'
        tiles.append(('storage', 'storage', f"{fmt_size(fullest['free'])} free",
                      f"{name} · {int(used * 100)}% used",
                      'danger' if used > 0.9 else 'warning' if used > 0.8 else 'text'))
    else:
        tiles.append(('storage', 'storage', "Measuring…", "Disk space", 'muted'))
    if not checks:
        tiles.append(('health', 'health', "Checking…", "Health", 'muted'))
        bad = []
    else:
        bad = sorted((c for c in checks if c.get('status') in ('fail', 'warn')),
                     key=lambda c: c['status'] != 'fail')
        if bad:
            fails = any(c['status'] == 'fail' for c in bad)
            tiles.append(('health', 'health', f"{len(bad)} issue{'s' if len(bad) != 1 else ''}",
                          bad[0].get('title') or '', 'danger' if fails else 'warning'))
        else:
            tiles.append(('health', 'health', "All good", f"{len(checks)} checks passed",
                          'success'))
    ge = ((proton or {}).get('families') or {}).get('ge') or {}
    if not proton:
        tiles.append(('proton', 'proton', "Checking…", "Proton", 'muted'))
    elif ge.get('update'):
        tiles.append(('proton', 'proton', "Update", f"{ge.get('latest')} is out", 'warning'))
    else:
        tiles.append(('proton', 'proton', "Up to date", proton.get('default') or 'Proton',
                      'success'))
    if running:
        j = running[0]
        pct = j.get('progress')
        attention = {'key': 'activity', 'glyph': 'spinner', 'role': 'accent',
                     'text': j.get('title') or '',
                     'sub': " · ".join(x for x in (f"{pct}%" if pct is not None else '',
                                                   j.get('step') or '') if x)}
    elif fullest and fullest['used'] / fullest['total'] > 0.9:
        attention = {'key': 'storage', 'glyph': 'storage', 'role': 'danger',
                     'text': f"{tiles[1][3].split(' · ')[0]} is almost full",
                     'sub': f"{fmt_size(fullest['free'])} left — see what can go"}
    elif bad and bad[0]['status'] == 'fail':
        attention = {'key': 'health', 'glyph': 'fail', 'role': 'danger',
                     'text': bad[0].get('title') or '', 'sub': bad[0].get('fix') or ''}
    elif ge.get('update'):
        attention = {'key': 'proton', 'glyph': 'download', 'role': 'warning',
                     'text': f"{ge.get('latest')} is out",
                     'sub': "Installs next to your current builds", 'action': 'install-ge',
                     'latest': ge.get('latest')}
    return {'tiles': [dict(zip(('key', 'glyph', 'value', 'caption', 'role'), t))
                      for t in tiles],
            'attention': attention}


def dash_cols(width: int) -> int:
    # Three at most: cards stay readable and the default layout packs evenly.
    return 3 if width >= 1100 else 2 if width >= 700 else 1


def dash_pack(items, cols: int, skip=()) -> list[tuple[str, int, int, int]]:
    """Row-wise flow of the visible widgets: (id, row, col, span). A widget
    that doesn't fit what's left of a row starts the next one, so the order
    you set is the order you read."""
    out, row, col = [], 0, 0
    for it in items:
        if it.get('hidden') or it['id'] in skip:
            continue
        span = min(DASH_SPAN.get(it['size'], 1), cols)
        if col and col + span > cols:
            row, col = row + 1, 0
        out.append((it['id'], row, col, span))
        col += span
        if col >= cols:
            row, col = row + 1, 0
    return out


def dash_move(items, wid: str, direction: str, cols: int, skip=()) -> list[dict]:
    """Move one widget: left/right swap it with its visible neighbour;
    up/down move it to the row above/below, under the same column."""
    items = [dict(it) for it in items]
    vis = [i for i, it in enumerate(items) if not it.get('hidden') and it['id'] not in skip]
    pos = next((i for i in vis if items[i]['id'] == wid), None)
    if pos is None:
        return items
    k = vis.index(pos)
    if direction in ('left', 'right'):
        j = k - 1 if direction == 'left' else k + 1
        if 0 <= j < len(vis):
            a, b = vis[k], vis[j]
            items[a], items[b] = items[b], items[a]
        return items
    packed = dash_pack(items, cols, skip)
    where = {i: (r, c, s) for i, r, c, s in packed}
    r, c, _s = where[wid]
    target_row = r - 1 if direction == 'up' else r + 1
    row_items = [(i, cc, ss) for i, rr, cc, ss in packed if rr == target_row]
    if not row_items:
        return items
    # The widget in that row under our column (or the nearest one).
    tid = min(row_items, key=lambda t: 0 if t[1] <= c < t[1] + t[2] else
              min(abs(t[1] - c), abs(t[1] + t[2] - 1 - c)) + 1)[0]
    me = items.pop(pos)
    t = next(i for i, it in enumerate(items) if it['id'] == tid)
    items.insert(t if direction == 'up' else t + 1, me)
    return items


# The user's collections, in their order: set by MainWindow whenever they
# change, read by game_actions() (which only gets a game) and Theatre's rows.
COLLECTION_NAMES: list[str] = []
MAX_COLLECTION_NAME = 40
MAX_TITLE = 120
ATTRACT_MINUTES = (0, 3, 5, 10, 20)
MAX_NOTE = 20000


def clean_collection_name(name) -> str:
    return ' '.join(str(name or '').split())[:MAX_COLLECTION_NAME]


class LibraryPrefs:
    """Favourites, hidden games and collections — GUI-only preferences, JSON
    in ~/.config/nexus/library.json."""
    PATH = NX.CONFIG_DIR / 'library.json'

    def __init__(self):
        try:
            data = json.loads(self.PATH.read_text())
        except (OSError, ValueError):
            data = {}
        self._data = data if isinstance(data, dict) else {}
        self.favorites = set(self._data.get('favorites', []))
        self.hidden = set(self._data.get('hidden', []))
        # Named groups of games, in the order they were made: name -> slugs.
        self.collections: dict[str, list[str]] = {}
        cols = self._data.get('collections')
        for name, slugs in (cols.items() if isinstance(cols, dict) else ()):
            name = clean_collection_name(name)
            if name and isinstance(slugs, list) and not self.find_collection(name):
                self.collections[name] = list(dict.fromkeys(
                    s for s in slugs if isinstance(s, str)))
        self.view = self._data.get('view', 'home')
        self.tile = int(self._data.get('tile', TILE_W))
        dash = self._data.get('dashboard') or {}
        self.dashboard = dash_normalize(dash.get('items') if isinstance(dash, dict) else None)
        self.appearance = normalize_appearance(self._data.get('appearance'))
        # Games you own but haven't installed: shown at all (Settings), and
        # which of installed / not installed / all the Library lists.
        self.show_owned = bool(self._data.get('show_owned', True))
        own = self._data.get('owned_view', 'installed')
        self.owned_view = own if own in ('installed', 'owned', 'all') else 'installed'
        # Repack downloads already announced (path|fingerprint); None until the
        # first scan, which only records what's there without announcing it.
        seen = self._data.get('seen_repacks')
        self.seen_repacks = set(seen) if isinstance(seen, list) else None
        # How a tile is activated: a double click and A/Enter each play the
        # game or open its page (Settings → Controls).
        ctl = self._data.get('controls') if isinstance(self._data.get('controls'), dict) else {}
        self.controls = {
            'double_click': ctl.get('double_click') if ctl.get('double_click') in
            ('play', 'details') else 'play',
            'pad_a': ctl.get('pad_a') if ctl.get('pad_a') in ('play', 'details') else 'details',
            # One tile per game when it's in several stores (Settings → Controls).
            'dedupe': ctl.get('dedupe') is not False,
            # Theatre's screensaver after this many idle minutes (0 = never).
            'attract': ctl.get('attract') if ctl.get('attract') in ATTRACT_MINUTES else 5,
        }
        # Your notes on a game (the details panel's Notes section).
        notes = self._data.get('notes')
        self.notes = {str(k): str(v)[:MAX_NOTE] for k, v in
                      (notes.items() if isinstance(notes, dict) else ()) if str(v).strip()}
        # Your own name for a game, any source: {slug: {'name': str}}.
        ov = self._data.get('overrides')
        self.overrides = {k: {'name': str(v['name'])[:MAX_TITLE]}
                          for k, v in (ov.items() if isinstance(ov, dict) else ())
                          if isinstance(v, dict) and str(v.get('name') or '').strip()}
        # The Library toolbar as it was left: sort, status filter, source chip,
        # and how wide the details panel was dragged.
        lib = self._data.get('library_state')
        self.library_state = lib if isinstance(lib, dict) else {}
        # The Deals page as it was left: section, filter, sort, hide owned.
        ds = self._data.get('deals_state')
        self.deals_state = ds if isinstance(ds, dict) else {}

    def save(self):
        self.PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.PATH.with_suffix('.tmp')
        # Keys this version doesn't know about are kept, not dropped.
        out = dict(self._data)
        out.update({'favorites': sorted(self.favorites), 'hidden': sorted(self.hidden),
                    'view': self.view, 'tile': self.tile,
                    'dashboard': {'version': 1, 'items': self.dashboard},
                    'appearance': self.appearance, 'show_owned': self.show_owned,
                    'owned_view': self.owned_view, 'controls': self.controls,
                    'library_state': self.library_state, 'collections': self.collections,
                    'deals_state': self.deals_state,
                    'overrides': self.overrides, 'notes': self.notes})
        if self.seen_repacks is not None:
            out['seen_repacks'] = sorted(self.seen_repacks)[-300:]
        try:
            tmp.write_text(json.dumps(out, indent=2))
            os.replace(tmp, self.PATH)
        except OSError as e:                 # a full disk: keep running, say so
            sys.stderr.write(f"nexus: couldn't save {self.PATH}: {e}\n")

    def toggle(self, which: str, slug: str) -> bool:
        s = self.favorites if which == 'favorite' else self.hidden
        s.symmetric_difference_update({slug})
        self.save()
        return slug in s

    # ---- collections ----
    def find_collection(self, name: str) -> str | None:
        """The existing collection with this name, ignoring case and spacing."""
        key = clean_collection_name(name).casefold()
        return next((n for n in self.collections if n.casefold() == key), None)

    def collections_of(self, slug: str) -> list[str]:
        return [n for n, slugs in self.collections.items() if slug in slugs]

    def add_collection(self, name: str) -> str | None:
        """Make a collection (or find the one already called that)."""
        name = clean_collection_name(name)
        if not name:
            return None
        hit = self.find_collection(name)
        if hit is None:
            self.collections[name] = []
            self.save()
        return hit or name

    def toggle_member(self, name: str, slug: str) -> bool:
        slugs = self.collections.get(name)
        if slugs is None:
            return False
        on = slug not in slugs
        if on:
            slugs.append(slug)
        else:
            slugs.remove(slug)
        self.save()
        return on

    def rename_collection(self, old: str, new: str) -> str | None:
        """None if `new` is empty or another collection already has it."""
        new = clean_collection_name(new)
        hit = self.find_collection(new) if new else None
        if not new or old not in self.collections or (hit is not None and hit != old):
            return None
        self.collections = {new if n == old else n: v for n, v in self.collections.items()}
        self.save()
        return new

    def delete_collection(self, name: str):
        if self.collections.pop(name, None) is not None:
            self.save()


SOURCE_LABEL = {'nexus': 'Repack', 'steam': 'Steam', 'heroic': 'Heroic'}
TIER_COLORS = {'platinum': '#b4c7dc', 'gold': '#cfb53b', 'silver': '#a6a6a6',
               'bronze': '#cd7f32', 'borked': '#eb6f92', 'native': '#9ccfd8'}
PCGW_COLORS = {'true': 'success', 'always on': 'success', 'limited': 'warning',
               'hackable': 'warning', 'false': 'danger', 'n/a': 'muted'}
PCGW_WORDS = {'true': 'yes', 'false': 'no', 'hackable': 'with a fix', 'always on': 'always on',
              'limited': 'limited', 'n/a': 'n/a'}


def anticheat_warning(tips: dict) -> str | None:
    """One line when Are We Anti-Cheat Yet says online play won't work."""
    ac = (tips or {}).get('anticheat') or {}
    if ac.get('status') not in ('Broken', 'Denied'):
        return None
    what = ', '.join(ac.get('anticheats') or []) or 'its anti-cheat'
    verb = "is blocked by the developer" if ac['status'] == 'Denied' else "doesn't run"
    return f"Online play: {what} {verb} on Linux — multiplayer likely won't work."


def compat_facts(tips: dict) -> tuple[list[tuple[str, str | None, bool]], str]:
    """(lines, rich-text detail) for Steam Deck, anti-cheat and
    PCGamingWiki facts. Lines are (text, colour, bold) like the Theatre
    cards; the detail escapes everything and links only https URLs."""
    tips = tips or {}
    lines, more = [], []
    dk = tips.get('deck') or {}
    if dk.get('deck'):
        lines.append((f"Steam Deck {dk['deck']}", {'Verified': P['success'], 'Playable':
                      P['warning'], 'Unsupported': P['danger']}.get(dk['deck']), False))
        more.append(f"<p><b>Steam Deck</b>: {html.escape(dk['deck'])}"
                    + (f" · SteamOS {html.escape(dk['steamos'])}" if dk.get('steamos') else '')
                    + "</p>")
    elif dk.get('steamos'):
        lines.append((f"SteamOS {dk['steamos']}", None, False))
        more.append(f"<p><b>SteamOS</b>: {html.escape(dk['steamos'])}</p>")
    ac = tips.get('anticheat') or {}
    if ac.get('status'):
        bad = ac['status'] in ('Broken', 'Denied')
        lines.append((f"Anti-cheat {ac['status'].lower()}", P['danger'] if bad else None, bad))
        link = (f" <a href='{html.escape(ac['url'])}' style='color:{P['accent']}'>details</a>"
                if str(ac.get('url', '')).startswith('https://') else '')
        more.append(f"<p><b>Anti-cheat</b> ({html.escape(', '.join(ac.get('anticheats') or []))})"
                    f": {html.escape(ac['status'])}"
                    + (f" — {html.escape(ac['note'])}" if ac.get('note') else '') + link + "</p>")
    pw = tips.get('pcgw') or {}
    if pw:
        sup = pw.get('support') or {}
        good = [k for k, v in sup.items() if v in ('true', 'always on')]
        if good:
            lines.append((', '.join(good[:3]), P['subtext'], False))
        chips = " · ".join(f"<span style='color:{P[PCGW_COLORS.get(v, 'muted')]}'>"
                           f"{html.escape(k)}: {html.escape(PCGW_WORDS.get(v, v))}</span>"
                           for k, v in sup.items())
        fixes = pw.get('fixes') or []
        link = (f"<a href='{html.escape(pw['url'])}' style='color:{P['accent']}'>"
                "Open on PCGamingWiki</a>" if str(pw.get('url', '')).startswith('https://')
                else '')
        more.append("<p>" + (chips or "No support table yet") + "</p>"
                    + (f"<p>{html.escape(', '.join(pw.get('apis') or []))}</p>"
                       if pw.get('apis') else '')
                    + ("<p><b>Essential fixes</b>: " + html.escape('; '.join(fixes)) + "</p>"
                       if fixes else '')
                    + (f"<p>{link}</p>" if link else ''))
        if fixes:
            lines.append((f"{len(fixes)} essential fix{'es' if len(fixes) != 1 else ''}",
                          P['warning'], False))
    return lines, "".join(more)


# --------------------------------------------------------------------------
# Game menu — the one place every per-game action lives (⋮ and right-click)
# --------------------------------------------------------------------------

def is_installed(g: dict) -> bool:
    return g.get('installed', True) is not False


def install_fraction(g: dict) -> float | None:
    """How far Steam is with a download (None: unknown or not downloading)."""
    i = g.get('installing') or {}
    return min(1.0, i['done'] / i['total']) if i.get('total') else None


def play_label(g: dict) -> str:
    """What the Play button says: the truth about the game right now."""
    if g.get('incomplete'):
        return "Resume install"
    if not is_installed(g):
        if g.get('installing'):
            if g['installing'].get('paused'):
                return "Download paused"
            f = install_fraction(g)
            return f"Installing {int(f * 100)}%" if f is not None else "Installing…"
        if g.get('install_requested'):
            return "Waiting for " + ("Steam" if g.get('source') == 'steam' else "Heroic")
        return "Install"
    if g.get('playing'):
        return "Playing"
    if g.get('launching'):
        return "Starting…"
    if g.get('busy'):
        return "Working…"
    return "Play"


def play_enabled(g: dict) -> bool:
    if g.get('incomplete'):
        return not g.get('resuming')
    if not is_installed(g):
        return bool(g.get('install_url')) and not g.get('installing') \
            and not g.get('install_requested')
    return bool(g.get('launcher')) and not g.get('playing') and not g.get('launching') \
        and not g.get('busy') and not g.get('stale')


def can_quick_play(g: dict) -> bool:
    """Whether a double click / A may start the game without its page:
    installed, finished and free to start (never an install or a resume)."""
    return is_installed(g) and not g.get('incomplete') and play_enabled(g)


class GameAction(NamedTuple):
    """One per-game action, rendered as a QMenu item (mouse/keyboard) or an
    ActionSheet row (controller, Theatre). `key` is what on_game_action runs."""
    key: str
    label: str
    section: str = ''
    checked: bool | None = None        # a toggle: shown ticked / unticked
    enabled: bool = True
    danger: bool = False
    desk: bool = False                 # opens on the desktop: leaves Theatre
    sub: str = ''                      # a submenu in the QMenu (Wine tools)


# The sheet's row icons (the QMenu stays text-only, like any desktop menu).
ACTION_GLYPHS = {'launch': 'play', 'open-folder': 'folder', 'settings': 'settings',
                 'refetch-art': 'image', 'restore-saves': 'save', 'check-integrity': 'shield',
                 'tool:kill': 'stop', 'view-log': 'log', 'steam-page': 'steam',
                 'steam-props': 'settings', 'steam-verify': 'shield', 'protongen': 'tune',
                 'pg-default': 'tune', 'discard-incomplete': 'trash',
                 'install-update': 'download', 'repack-page': 'update',
                 'pause-toggle': 'timer', 'benchmark': 'system',
                 'rename-local': 'pencil', 'rename-reset': 'reset',
                 'artwork': 'image', 'hide': 'eye_off', 'uninstall': 'trash',
                 'steam-uninstall': 'trash', 'col-new': 'plus'}


def action_glyph(a: 'GameAction') -> str:
    if a.key == 'favorite':
        return G['star' if a.checked else 'star_outline']
    if a.key == 'launch' and a.label.startswith(("Install", "Waiting", "Download", "Resume")):
        return G['download']
    if a.checked is not None and a.key != 'hide':  # In Steam / In Heroic
        return G['check_circle' if a.checked else 'plus']
    if a.key.startswith('tool:') and a.key != 'tool:kill':
        return G['wine']
    return G[ACTION_GLYPHS.get(a.key, 'more')]


# Desktop-only things nobody wants from the couch: file managers, Wine's
# own dialogs, another app. Not offered in Theatre at all.
COUCH_SKIP = ('open-folder', 'protongen', 'tool:')


# Actions that change a game's files or registration: not while a job is
# already doing that (busy), and the ones that rewrite what a running game is
# using not while it plays.
NOT_WHILE_BUSY = ('install-update', 'discard-incomplete', 'add-steam', 'remove-steam',
                  'add-heroic', 'remove-heroic', 'pg-default', 'restore-saves',
                  'check-integrity', 'tool:winecfg', 'tool:regedit', 'tool:winetricks',
                  'uninstall')
NOT_WHILE_PLAYING = ('install-update', 'restore-saves', 'uninstall', 'steam-verify',
                     'steam-uninstall', 'tool:winetricks', 'benchmark')


def _gate_actions(game: dict, out: list['GameAction']) -> list['GameAction']:
    playing = game.get('playing') or game.get('launching')
    busy = game.get('busy')
    res = []
    for a in out:
        why = ("not while it's running" if playing and a.key in NOT_WHILE_PLAYING else
               "busy — a job is running" if busy and a.key in NOT_WHILE_BUSY else '')
        res.append(a._replace(enabled=False, label=f"{a.label}  ·  {why}") if why else a)
    return res


def game_actions(game: dict, theatre: bool = False) -> list[GameAction]:
    """Every action for one game. What's offered depends on who owns it:
    Nexus installs get everything; Steam/Heroic games get launcher shortcuts
    (their launcher manages files and updates)."""
    src = game.get('source', 'nexus')
    A = GameAction
    if game.get('incomplete'):          # a cancelled or failed install: finish or drop it
        out = [A('launch', play_label(game), 'play', enabled=play_enabled(game)),
               A('open-folder', "Open game folder", 'play'),
               A('discard-incomplete', "Discard unfinished install", 'danger', danger=True)]
        return _gate_actions(game, [a for a in out
                                    if not (theatre and a.key.startswith(COUCH_SKIP))])
    out = [A('launch', play_label(game), 'play', enabled=play_enabled(game)),
           A('open-folder', "Open game folder", 'play')]
    if src == 'nexus' and game.get('repack_update'):
        out.insert(1, A('install-update', "Install update", 'play'))
    elif src == 'nexus' and (game.get('repack_online') or {}).get('url'):
        out.insert(1, A('repack-page', "See the update on FitGirl", 'play'))
    if src == 'nexus':
        out += [A('settings', "Game settings…", 'play', desk=True),
                A('remove-steam' if game.get('steam') else 'add-steam', "In Steam",
                  'stores', checked=bool(game.get('steam'))),
                A('remove-heroic' if game.get('heroic') else 'add-heroic', "In Heroic",
                  'stores', checked=bool(game.get('heroic'))),
                A('pg-default', "Use protongen defaults", 'tools'),
                A('refetch-art', "Re-fetch artwork", 'tools'),
                A('restore-saves', "Restore save backup", 'tools'),
                A('check-integrity', "Check file integrity", 'tools'),
                A('benchmark', "Benchmark run (MangoHud log)", 'tools', desk=True),
                A('tool:winecfg', "winecfg — Wine settings", 'tools', sub="Wine tools"),
                A('tool:regedit', "regedit — registry editor", 'tools', sub="Wine tools"),
                A('tool:winetricks', "winetricks", 'tools', sub="Wine tools"),
                A('tool:kill', "Stop everything in the prefix", 'kill', sub="Wine tools"),
                A('view-log', "View log", 'tools', desk=True)]
    if src == 'nexus' and game.get('playing'):
        out.insert(1, A('pause-toggle', "Pause or resume", 'play'))
    if 'sunshine' in game and is_installed(game):       # streaming, next to the stores
        at = next((i + 1 for i, a in enumerate(out) if a.section == 'stores'), len(out))
        out.insert(at + (1 if src == 'nexus' else 0),
                   A('remove-sunshine' if game['sunshine'] else 'add-sunshine', "In Sunshine",
                     'stores', checked=bool(game['sunshine'])))
    if src == 'steam':
        out += [A('steam-page', "Open in Steam", 'tools'),
                A('steam-props', "Steam properties…", 'tools'),
                A('steam-verify', "Verify game files", 'tools'),
                A('protongen', "Tune in protongen", 'tools')]
    if src != 'nexus':                    # a repack is renamed for real, in Game settings
        out.append(A('rename-local', "Rename in library…", 'tools'))
        if game.get('store_name'):
            out.append(A('rename-reset', f"Use the store's name ({game['store_name']})", 'tools'))
    out += [A('artwork', "Change artwork…", 'tools', desk=True),
            A('favorite', "Favourite", 'prefs', checked=bool(game.get('favorite'))),
            A('hide', "Hidden from library", 'prefs', checked=bool(game.get('hidden')))]
    mine = game.get('collections') or ()
    out += [A(f'col:{n}', n, 'prefs', checked=n in mine, sub="Collections")
            for n in COLLECTION_NAMES]
    out.append(A('col-new', "New collection…", 'prefs', sub="Collections"))
    if src == 'nexus':
        out.append(A('uninstall', "Uninstall…", 'danger', danger=True, desk=True))
    elif src == 'steam':
        out.append(A('steam-uninstall', "Uninstall in Steam…", 'danger', danger=True))
    if not is_installed(game):           # nothing on disk yet: install, or tidy up
        out = [a for a in out if a.key in ('launch', 'steam-page', 'artwork', 'favorite',
                                            'hide', 'col-new') or a.key.startswith('col:')]
    if theatre:
        out = [a for a in out if not a.key.startswith(COUCH_SKIP)]
    return _gate_actions(game, out)


def build_game_menu(parent: QWidget, game: dict, act) -> QMenu:
    """`act(name, game)` runs an action. The mouse/keyboard rendering of
    game_actions(): the tile's ⋮ button, right-click, the list view and the
    details panel (a controller gets ActionSheet instead)."""
    m = QMenu(parent)
    subs: dict[str, QMenu] = {}
    last = {}                                  # menu -> last section seen in it
    for a in game_actions(game):
        target = m
        if a.sub:
            if a.sub not in subs:
                subs[a.sub] = m.addMenu(a.sub)
            target = subs[a.sub]
        prev = last.get(id(target))
        if prev is not None and prev != a.section:
            target.addSeparator()
        last[id(target)] = a.section
        qa = target.addAction(a.label)
        qa.setEnabled(a.enabled)
        if a.checked is not None:
            qa.setCheckable(True)
            qa.setChecked(a.checked)
        if a.key == 'launch':
            f = qa.font()
            f.setWeight(QFont.Weight.DemiBold)
            qa.setFont(f)
        qa.triggered.connect(lambda _=False, k=a.key: act(k, game))
    return m


class ActionSheet(QFrame):
    """The game menu for a controller: a panel of big rows sliding over a
    dimmed page. D-pad moves, A runs, B or X closes. Used wherever a pad is
    driving, and always in Theatre — never the desktop right-click menu."""
    chosen = pyqtSignal(str, dict)
    closed = pyqtSignal()

    def __init__(self, parent: QWidget, scale: float = 1.0):
        super().__init__(parent)
        self.setObjectName("SheetScrim")
        self.scale = scale
        self.game: dict = {}
        self._back_to = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch()
        self.panel = QFrame()
        self.panel.setObjectName("Sheet")
        self.panel.setFixedWidth(int(400 * scale))
        outer.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        m = int(SP['5'] * scale)
        lay.setContentsMargins(m, m, m, m)
        lay.setSpacing(int(SP['1'] * scale))
        self.title = label("", "SheetTitle", wrap=True)
        lay.addWidget(self.title)
        self.sub = label("", "Muted")
        lay.addWidget(self.sub)
        lay.addSpacing(int(SP['3'] * scale))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.rows = QVBoxLayout(host)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(int(SP['0_5'] * scale))
        self.scroll.setWidget(host)
        lay.addWidget(self.scroll, 1)
        parent.installEventFilter(self)          # follow the parent's size
        self.hide()

    def open(self, game: dict, actions: list[GameAction], theatre: bool = False):
        self.game = game
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        clear_layout(self.rows)
        self.title.setText(game.get('display_name', ''))
        self.sub.setText(SOURCE_LABEL.get(game.get('source', 'nexus'), ''))
        last, last_sub, first = None, '', None
        px = int(18 * self.scale)
        for a in actions:
            if a.sub and a.sub != last_sub:
                self.rows.addSpacing(int(SP['2'] * self.scale))
                self.rows.addWidget(label(a.sub.upper(), "SheetSection"))
            elif last is not None and a.section != last and not (a.sub and last_sub):
                self.rows.addSpacing(int(SP['2'] * self.scale))
            last, last_sub = a.section, a.sub
            text = a.label
            if a.desk and theatre:
                text += "  ·  leaves Theatre"
            b = QPushButton(text)
            b.setObjectName("SheetRow")
            b.setProperty('danger', a.danger)
            set_glyph_icon(b, action_glyph(a), px,
                           'danger' if a.danger else 'accent' if a.checked else 'subtext',
                           hover_role=None)
            b.setMinimumHeight(int(44 * self.scale))
            b.setEnabled(a.enabled)
            b.clicked.connect(lambda _=False, k=a.key: self._run(k))
            self.rows.addWidget(b)
            if first is None and a.enabled:
                first = b
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        if first is not None:
            first.setFocus(Qt.FocusReason.TabFocusReason)

    def ask(self, title: str, text: str, yes: str, on_yes, danger: bool = True):
        """The sheet as a yes/no question. Cancel has the focus: A pressed
        out of habit never confirms anything."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self.game = {}
        clear_layout(self.rows)
        self.title.setText(title)
        self.sub.setText("")
        self.rows.addWidget(label(text, "Muted", wrap=True))
        self.rows.addSpacing(int(SP['3'] * self.scale))
        px = int(18 * self.scale)
        ok = QPushButton(yes)
        ok.setObjectName("SheetRow")
        ok.setProperty('danger', danger)
        set_glyph_icon(ok, G['check'], px, 'danger' if danger else 'accent', hover_role=None)
        ok.setMinimumHeight(int(44 * self.scale))
        ok.clicked.connect(lambda: (self.close_sheet(), on_yes()))
        no = QPushButton("Cancel")
        no.setObjectName("SheetRow")
        set_glyph_icon(no, G['close'], px, 'subtext', hover_role=None)
        no.setMinimumHeight(int(44 * self.scale))
        no.clicked.connect(lambda: self.close_sheet())
        self.rows.addWidget(ok)
        self.rows.addWidget(no)
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        no.setFocus(Qt.FocusReason.TabFocusReason)

    def choose(self, title: str, items: list, sub: str = ''):
        """The sheet as a plain list of choices — anything that isn't a game
        action (a deal's links, a game's collections). `items` are
        (text, glyph, fn) or (text, glyph, fn, checked); fn runs once the
        sheet has closed."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self.game = {}
        clear_layout(self.rows)
        self.title.setText(title)
        self.sub.setText(sub)
        px = int(18 * self.scale)
        first = None
        for text, glyph, fn, *rest in items:
            b = QPushButton(text)
            b.setObjectName("SheetRow")
            set_glyph_icon(b, glyph, px, 'accent' if rest and rest[0] else 'subtext',
                           hover_role=None)
            b.setMinimumHeight(int(44 * self.scale))
            b.clicked.connect(lambda _=False, fn=fn: (self.close_sheet(), fn()))
            self.rows.addWidget(b)
            first = first or b
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        if first is not None:
            first.setFocus(Qt.FocusReason.TabFocusReason)

    def prompt(self, title: str, initial: str, ok: str, on_ok, text: str = '',
               max_len: int = MAX_COLLECTION_NAME):
        """The sheet asking for a name (keyboard): Return or `ok` saves,
        Cancel / Escape drops it. An empty name saves nothing."""
        f = QApplication.focusWidget()
        self._back_to = f if f is not None and not self.isAncestorOf(f) else self._back_to
        self.game = {}
        clear_layout(self.rows)
        self.title.setText(title)
        self.sub.setText("")
        if text:
            self.rows.addWidget(label(text, "Muted", wrap=True))
        ed = QLineEdit(initial)
        ed.setMaxLength(max_len)
        ed.setMinimumHeight(int(40 * self.scale))
        self.rows.addWidget(ed)
        self.rows.addSpacing(int(SP['2'] * self.scale))
        px = int(18 * self.scale)
        save = QPushButton(ok)
        save.setObjectName("SheetRow")
        set_glyph_icon(save, G['check'], px, 'accent', hover_role=None)
        save.setMinimumHeight(int(44 * self.scale))
        no = QPushButton("Cancel")
        no.setObjectName("SheetRow")
        set_glyph_icon(no, G['close'], px, 'subtext', hover_role=None)
        no.setMinimumHeight(int(44 * self.scale))

        def done():
            name = ed.text().strip()
            if name:
                self.close_sheet()
                on_ok(name)
        save.clicked.connect(done)
        ed.returnPressed.connect(done)
        no.clicked.connect(lambda: self.close_sheet())
        self.rows.addWidget(save)
        self.rows.addWidget(no)
        self.rows.addStretch()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        ed.selectAll()
        ed.setFocus(Qt.FocusReason.TabFocusReason)

    def default_focus(self):
        return next((w for w in self.findChildren(QPushButton)
                     if w.isEnabled() and not w.isHidden()), None)

    def _run(self, key):
        game = self.game
        self.close_sheet(restore=key not in ('artwork', 'settings', 'view-log', 'uninstall'))
        self.chosen.emit(key, game)

    def close_sheet(self, restore: bool = True):
        if self.isHidden():
            return
        self.hide()
        back, self._back_to = self._back_to, None
        try:
            if restore and back is not None and back.isVisible():
                focus_and_reveal(back)
        except RuntimeError:                     # rebuilt while the sheet was open
            pass
        self.closed.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Menu, Qt.Key.Key_Back):
            self.close_sheet()
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        if not self.panel.geometry().contains(ev.position().toPoint()):
            self.close_sheet()
        super().mousePressEvent(ev)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, 115))
        pt.end()


class TrailerOverlay(QFrame):
    """A Steam trailer playing over the window (an overlay, not a dialog:
    niri floats dialogs). Esc / B / the close button end it. QtMultimedia
    is imported on first use; the FFmpeg backend plays Steam's HLS streams."""
    closed = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("SheetScrim")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.player = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['6'], SP['5'], SP['6'], SP['6'])
        lay.setSpacing(SP['2'])
        top = QHBoxLayout()
        self.title = label("", "SheetTitle")
        top.addWidget(self.title, 1)
        self.btn_close = icon_button(G['close'], "Close (Esc)", 36)
        self.btn_close.clicked.connect(self.stop)
        top.addWidget(self.btn_close)
        lay.addLayout(top)
        self.host = QVBoxLayout()
        lay.addLayout(self.host, 1)
        self.status = label("", "Muted")
        lay.addWidget(self.status)
        parent.installEventFilter(self)
        self.hide()

    def _ensure(self) -> bool:
        if self.player is not None:
            return True
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PyQt6.QtMultimediaWidgets import QVideoWidget
        except ImportError:
            return False
        self.video = QVideoWidget()
        self.host.addWidget(self.video, 1)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.6)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(self._error)
        self.player.mediaStatusChanged.connect(self._media_status)
        return True

    def play(self, trailer: dict, game_name: str = ''):
        url = str(trailer.get('url') or '')
        if not url.startswith('https://'):
            return
        self.setGeometry(self.parentWidget().rect())
        self.title.setText(" · ".join(x for x in (game_name, trailer.get('name')) if x))
        self.show()
        self.raise_()
        self.setFocus()
        if not self._ensure():
            self.status.setText("Video playback needs Qt Multimedia (qt6-multimedia-ffmpeg).")
            return
        self.status.setText("Loading…")
        self.player.setSource(QUrl(url))
        self.player.play()

    def _media_status(self, st):
        from PyQt6.QtMultimedia import QMediaPlayer
        if st in (QMediaPlayer.MediaStatus.BufferedMedia, QMediaPlayer.MediaStatus.LoadedMedia):
            self.status.setText("")
        elif st == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop()

    def _error(self, _err, text=''):
        self.status.setText(f"Couldn't play the trailer: {text or 'unknown error'}")

    def stop(self):
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Back, Qt.Key.Key_Backspace):
            self.stop()
        elif ev.key() == Qt.Key.Key_Space and self.player is not None:
            from PyQt6.QtMultimedia import QMediaPlayer
            (self.player.pause if self.player.playbackState()
             == QMediaPlayer.PlaybackState.PlayingState else self.player.play)()
        else:
            super().keyPressEvent(ev)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(0, 0, 0, 225))
        pt.end()


def about_html(store: dict | None, reviews: dict | None, hltb: dict | None) -> str:
    """Rich text for the details panel's About section; everything escaped,
    links only to the sites they came from."""
    st, rv, out = store or {}, reviews or {}, []
    if st.get('blurb'):
        out.append(f"<p>{html.escape(st['blurb'])}</p>")
    facts = [x for x in (', '.join(st.get('genres') or []), st.get('release'),
                         ', '.join(st.get('developers') or [])) if x]
    if facts:
        out.append(f"<p style='color:{P['subtext']}'>{html.escape(' · '.join(facts))}</p>")
    scores = []
    if rv.get('desc'):
        good = (rv.get('percent') or 0) >= 70
        scores.append(f"<span style='color:{P['success'] if good else P['warning']}'>"
                      f"{html.escape(rv['desc'])}</span>"
                      + (f" ({rv['percent']}% of {rv['total']:,})" if rv.get('percent')
                         is not None else ''))
    mc = st.get('metacritic') or {}
    if mc.get('score'):
        scores.append(f"Metacritic {int(mc['score'])}")
    if scores:
        out.append("<p><b>Reviews</b>: " + " · ".join(scores) + "</p>")
    if hltb and hltb.get('main'):
        parts = [f"main {hltb['main']:g} h"]
        if hltb.get('plus'):
            parts.append(f"+extras {hltb['plus']:g} h")
        if hltb.get('complete'):
            parts.append(f"100% {hltb['complete']:g} h")
        link = (f" <a href='{html.escape(hltb['url'])}' style='color:{P['accent']}'>HLTB</a>"
                if str(hltb.get('url', '')).startswith('https://howlongtobeat.com/') else '')
        out.append("<p><b>How long to beat</b>: " + " · ".join(parts) + link + "</p>")
    if str(st.get('url', '')).startswith('https://store.steampowered.com/'):
        out.append(f"<p><a href='{html.escape(st['url'])}' style='color:{P['accent']}'>"
                   "Steam store page</a></p>")
    return "".join(out)


class _TileSignals:
    """Mixin for the three library views: they all emit the same signals and
    leave selection bookkeeping to the main window."""

    def _wire(self, tile: 'GameTile'):
        tile.selected.connect(self.clicked)
        tile.play.connect(self.play)
        tile.menu.connect(self.menu)
        tile.activated.connect(self.activated)
        tile.double.connect(self.double)


class LibraryView(QScrollArea, _TileSignals):
    """Reflowing grid of tiles with a centered empty state.

    The empty message is a sibling of the flow rather than an item inside it —
    a FlowLayout sizes each child to its own hint, so a label placed in it would
    sit top-left however it's aligned."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setMinimumWidth(180 + SP['5'] * 2 + 12)
        self.container = QWidget()
        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.flow_host = QWidget()
        self.flow = FlowLayout(self.flow_host, margin=SP['5'], spacing=SP['4'], justify=True)
        outer.addWidget(self.flow_host)
        self._empty = EmptyState(G['library'], "")
        self._empty.setVisible(False)
        outer.addWidget(self._empty, 1)
        outer.addStretch()
        self.setWidget(self.container)
        self.tiles: dict[str, list[GameTile]] = {}
        self.tile_width = TILE_W

    def set_games(self, games, empty_text=None, pending_jobs=()):
        # Tiles are kept and re-pointed at the new game dicts where they can
        # be (same game, same size): a search, a favourite or a library
        # reload then costs a re-layout, not a few hundred new widgets.
        spare: dict[str, list[GameTile]] = {}
        while self.flow.count():
            item = self.flow.takeAt(0)
            w = item.widget()
            if isinstance(w, GameTile) and not w.pending and w.tw == self.tile_width:
                spare.setdefault(w.game.get('slug'), []).append(w)
            elif w is not None:
                w.hide()                       # gone now, not at the next event loop
                w.deleteLater()
        self.tiles = {}
        for job in pending_jobs:
            t = GameTile({'slug': f'__pending_{id(job)}', 'display_name': job.name},
                         pending=True, width=self.tile_width)
            t.set_job(job)
            t.selected.connect(lambda _g, _m, j=job: self.pending_clicked.emit(j))
            self.flow.addWidget(t)
        for g in games:
            pool = spare.get(g['slug'])
            if pool:
                tile = pool.pop()
                tile.set_game(g)
            else:
                tile = GameTile(g, width=self.tile_width)
                self._wire(tile)
            self.flow.addWidget(tile)
            self.tiles.setdefault(g['slug'], []).append(tile)
        for pool in spare.values():
            for w in pool:
                w.hide()
                w.deleteLater()
        shown = bool(games) or bool(pending_jobs)
        self._empty.set(*empty_state_text(empty_text))
        self._empty.setVisible(not shown)
        self.flow_host.setVisible(shown)

    def mark_selected(self, slugs: set):
        for s, ts in self.tiles.items():
            for t in ts:
                t.set_selected(s in slugs)

    def reveal(self, slug):
        for t in self.tiles.get(slug, [])[:1]:
            self.ensureWidgetVisible(t)

    def set_jobs(self, jobs: 'JobManager'):
        for slug, ts in self.tiles.items():
            for t in ts:
                t.set_job(jobs.for_slug(slug))


class KeepPlace:
    """`with KeepPlace(view): rebuild…` — a rebuild of `view`'s shelves
    (a library reload, an install finishing) keeps the keyboard/controller
    user where they were: the same game's tile gets the focus back, and each
    shelf goes back to its sideways offset."""

    def __init__(self, scope: QWidget):
        self.scope = scope

    def _rows(self):
        return [r for r in self.scope.findChildren(ShelfRow) if not r.isHidden()]

    def __enter__(self):
        f = QApplication.focusWidget()
        self.focus = None
        if isinstance(f, GameTile) and self.scope.isAncestorOf(f):
            row = f.parentWidget()
            while row is not None and not isinstance(row, ShelfRow):
                row = row.parentWidget()
            self.focus = (f.game.get('slug'), row.title if row is not None else None)
        self.offsets = {r.title: r.bar.value() for r in self._rows() if r.bar.value()}
        return self

    def __exit__(self, *exc):
        if self.focus is not None or self.offsets:
            # New rows are shown by the layout on the next event loop pass;
            # a hidden tile can't take the focus, so put things back then.
            # A child timer: it dies with the view. (A lambda, not the bound
            # method: PyQt holds a plain object's method only weakly, and
            # this KeepPlace is gone once the `with` block ends.)
            t = QTimer(self.scope)
            t.setSingleShot(True)
            t.timeout.connect(lambda: self._restore())
            t.timeout.connect(t.deleteLater)
            t.start(0)
        return False

    def _restore(self):
        rows = self._rows()
        for r in rows:
            if r.title in self.offsets:
                r.restore_offset(self.offsets[r.title])
        if self.focus is None:
            return
        slug, title = self.focus
        cur = QApplication.focusWidget()
        # Hiding the old tile moved Qt's focus to some neighbour (or nowhere).
        if not (isinstance(cur, GameTile) and not cur.isHidden()
                and cur.game.get('slug') == slug):
            same = [t for r in rows if r.title == title for t in r.tiles
                    if t.game.get('slug') == slug]
            anywhere = [t for t in self.scope.findChildren(GameTile)
                        if t.game.get('slug') == slug and not t.isHidden()]
            target = (same or anywhere or [None])[0]
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)


def shelf_page_target(value: int, maximum: int, viewport_w: int, pitch: int,
                      direction: int) -> int:
    """Where a shelf scrolls for one arrow press: a viewport's worth of whole
    tiles in `direction` (-1 / +1), snapped to the tile pitch, clamped."""
    if maximum <= 0:
        return 0
    if pitch <= 0:
        step = max(1, viewport_w)
        return max(0, min(maximum, value + direction * step))
    step = max(1, viewport_w // pitch) * pitch
    target = round((value + direction * step) / pitch) * pitch
    return max(0, min(maximum, target))


class _EdgeFade(QWidget):
    """Soft fade over a shelf edge, hinting that there's more that way."""

    def __init__(self, parent, side: str):
        super().__init__(parent)
        self.side = side
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, ev):
        pt = QPainter(self)
        g = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        solid = QColor(P['bg']); solid.setAlphaF(0.85)
        clear = QColor(P['bg']); clear.setAlphaF(0.0)
        g.setColorAt(0.0, solid if self.side == 'left' else clear)
        g.setColorAt(1.0, clear if self.side == 'left' else solid)
        pt.fillRect(self.rect(), QBrush(g))
        pt.end()


class _ShelfScroll(QScrollArea):
    """A plain vertical wheel belongs to the page (scrolling a shelf sideways
    under the cursor is a common annoyance); horizontal wheel / Shift+wheel
    scroll the shelf."""

    def wheelEvent(self, ev):
        d = ev.angleDelta()
        if d.x() == 0 and not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            ev.ignore()
            return
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - (d.x() or d.y()))
        ev.accept()


class ShelfRow(QWidget):
    """A titled row of tiles that pages sideways with ‹ › buttons instead of
    a scrollbar. Keyboard/controller focus moving along the row scrolls it
    smoothly so the focused tile is always in view."""
    see_all = pyqtSignal()

    def __init__(self, title: str, tile_w: int, kind: str = 'cover', count: int | None = None,
                 see_all: bool = False):
        super().__init__()
        self.title = title
        self._want: int | None = None             # an offset to come back to (KeepPlace)
        self.tile_w, self.kind = tile_w, kind
        tile_h = int(tile_w * 9 / 16) if kind == 'wide' else int(tile_w * 3 / 2)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        head.addWidget(label(title, "ShelfTitle"))
        if count:
            c = label(str(count), "ShelfCount")
            head.addWidget(c, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch()
        if see_all:
            b = QPushButton("See all")
            b.setObjectName("Ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(self.see_all)
            head.addWidget(b)
        lay.addLayout(head)

        self.body = QWidget()
        self.body.setFixedHeight(tile_h + TILE_LIFT)
        self.scroll = _ShelfScroll(self.body)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self.row = QHBoxLayout(host)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(SP['3'])
        self.scroll.setWidget(host)
        self.fades = {side: _EdgeFade(self.body, side) for side in ('left', 'right')}
        self.prev = icon_button(G['chevron_left'], "Previous", 40, "ShelfArrow")
        self.next = icon_button(G['chevron_right'], "Next", 40, "ShelfArrow")
        for btn, d in ((self.prev, -1), (self.next, 1)):
            btn.setParent(self.body)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # D-pad moves along tiles
            btn.clicked.connect(lambda _=False, d=d: self.page(d))
        self.bar = self.scroll.horizontalScrollBar()
        self.bar.valueChanged.connect(self._sync_arrows)
        self.bar.rangeChanged.connect(self._range_changed)
        self._anim = QPropertyAnimation(self.bar, b"value", self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.body.installEventFilter(self)
        lay.addWidget(self.body)
        self.tiles: list[GameTile] = []

    @property
    def pitch(self) -> int:
        return self.tile_w + self.row.spacing()

    def add(self, tile: 'GameTile'):
        self.row.addWidget(tile)
        tile.installEventFilter(self)
        self.tiles.append(tile)

    def finish(self):
        self.row.addStretch()

    def page(self, direction: int):
        self.scroll_to(shelf_page_target(self.bar.value(), self.bar.maximum(),
                                         self.scroll.viewport().width(), self.pitch, direction))

    def scroll_to(self, value: int):
        self._anim.stop()
        self._anim.setDuration(anim_ms(320))
        if self._anim.duration() == 0:
            self.bar.setValue(value)
            return
        self._anim.setStartValue(self.bar.value())
        self._anim.setEndValue(value)
        self._anim.start()

    def _range_changed(self, _lo, hi):
        if self._want is not None:                # laid out now: go back there
            self.bar.setValue(min(self._want, hi))
            if hi >= self._want:
                self._want = None
        self._sync_arrows()

    def restore_offset(self, value: int):
        self._want = value or None
        self.bar.setValue(value)

    def _sync_arrows(self):
        v, mx = self.bar.value(), self.bar.maximum()
        for side, btn, on in (('left', self.prev, v > 0), ('right', self.next, v < mx)):
            btn.setVisible(on)
            self.fades[side].setVisible(on)

    def eventFilter(self, obj, ev):
        if obj is self.body and ev.type() == QEvent.Type.Resize:
            w, h = self.body.width(), self.body.height()
            self.scroll.setGeometry(0, 0, w, h)
            fw = 56
            self.fades['left'].setGeometry(0, 0, fw, h)
            self.fades['right'].setGeometry(w - fw, 0, fw, h)
            y = (h - self.prev.height()) // 2
            self.prev.move(SP['2'], y)
            self.next.move(w - self.next.width() - SP['2'], y)
            for wdg in (*self.fades.values(), self.prev, self.next):
                wdg.raise_()
            self._sync_arrows()
        elif ev.type() == QEvent.Type.FocusIn and obj in self.tiles:
            self._reveal(obj)
        return False

    def _reveal(self, tile):
        vw, v, margin = self.scroll.viewport().width(), self.bar.value(), SP['6']
        x0, x1 = tile.x(), tile.x() + tile.width()
        if x0 < v + margin:
            self.scroll_to(max(0, x0 - margin))
        elif x1 > v + vw - margin:
            self.scroll_to(min(self.bar.maximum(), x1 - vw + margin))


class HeroCarousel(QWidget):
    """The cinematic banner on Home: up to five featured games, full-bleed
    art with the game's logo, a few facts, and Play. It cross-fades to the
    next game every few seconds — never while you're pointing at it, while
    it has focus, while it's hidden or while the window is in the
    background. Portrait-only art gets a blurred backdrop with the cover
    inset, so even repacks without a hero image look intentional."""
    play = pyqtSignal(dict)
    open = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    tint = pyqtSignal(object)            # QColor of the current art

    INTERVAL = 8000

    def __init__(self, scale: float = 1.0):
        super().__init__()
        self.scale = scale
        self.games: list[dict] = []
        self.i = 0
        self._frames: dict[tuple, QPixmap] = {}
        self._sources: dict[tuple, QImage] = {}   # decoded art, a little over banner size
        self._logos: dict[tuple, QPixmap] = {}
        # While the window is being resized the old frame is stretched; the
        # art is rebuilt once, when the size settles.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(120)
        self._settle.timeout.connect(self._settled)
        self._cur = self._prev = None          # (path, is_portrait)
        self._fade = 1.0
        self.setMinimumHeight(int(300 * scale))
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.valueChanged.connect(self._faded)
        self.timer = QTimer(self)
        self.timer.setInterval(self.INTERVAL)
        self.timer.timeout.connect(self._auto)

        lay = QVBoxLayout(self)
        m = int(SP['8'] * 1.5 * scale)
        lay.setContentsMargins(m, m, m, int(SP['6'] * scale))
        lay.setSpacing(int(SP['2'] * scale))
        lay.addStretch()
        self.kicker = label("", "HeroKicker")
        lay.addWidget(self.kicker)
        self.logo = QLabel()
        self.logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.logo)
        self.title = label("", "HeroTitle", wrap=True)
        self.title.setStyleSheet(f"font-size: {int(36 * scale)}px;")
        self.title.setMaximumWidth(int(640 * scale))
        lay.addWidget(self.title)
        self.facts = QHBoxLayout()
        self.facts.setSpacing(SP['1_5'])
        lay.addLayout(self.facts)
        lay.addSpacing(int(SP['2'] * scale))
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        self.btn = QPushButton("Play")
        self.btn.setObjectName("PlayBig")
        set_glyph_icon(self.btn, G['play'], int(20 * scale), 'on_accent', hover_role=None)
        self.btn.setMinimumSize(int(150 * scale), int(44 * scale))
        self.btn.clicked.connect(lambda: self.play.emit(self.game))
        self.btn_details = QPushButton("Details")
        self.btn_details.setObjectName("Glass")
        self.btn_details.setMinimumHeight(int(44 * scale))
        self.btn_details.clicked.connect(lambda: self.open.emit(self.game))
        self.btn_more = icon_button(G['more'], "More actions", int(44 * scale), "GlassIcon")
        self.btn_more.clicked.connect(lambda: self.menu.emit(
            self.game, self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height()))))
        row.addWidget(self.btn)
        row.addWidget(self.btn_details)
        row.addWidget(self.btn_more)
        row.addStretch()
        self.btn_prev = icon_button(G['chevron_left'], "Previous", int(36 * scale), "GlassIcon")
        self.btn_next = icon_button(G['chevron_right'], "Next", int(36 * scale), "GlassIcon")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.dots = QWidget()
        self.dots.setFixedSize(1, int(36 * scale))
        self.dots.paintEvent = self._paint_dots
        self.dots.mousePressEvent = self._dot_clicked
        row.addWidget(self.btn_prev)
        row.addWidget(self.dots)
        row.addWidget(self.btn_next)
        lay.addLayout(row)

    @property
    def game(self) -> dict:
        return self.games[self.i] if self.games else {}

    # ---- data ----
    def set_games(self, games: list[dict]):
        keep = self.game.get('slug')
        self.games = games[:5]
        slugs = [g['slug'] for g in self.games]
        self.i = slugs.index(keep) if keep in slugs else 0
        many = len(self.games) > 1
        for w in (self.btn_prev, self.btn_next, self.dots):
            w.setVisible(many)
        self.dots.setFixedWidth(max(1, len(self.games) * 16))
        if self.games:
            self._show(animate=False)
        if many and MOTION:
            self.timer.start()
        else:
            self.timer.stop()

    def step(self, d: int):
        if len(self.games) > 1:
            self.i = (self.i + d) % len(self.games)
            self._show(animate=True)
            self.timer.start()              # a manual step restarts the clock

    def wheelEvent(self, ev):
        # A sideways swipe (touchpad, tilt wheel) flips through the games; an
        # up/down wheel keeps scrolling the page.
        dx, dy = ev.angleDelta().x(), ev.angleDelta().y()
        if abs(dx) > abs(dy) and dx:
            now = time.monotonic()
            if now - getattr(self, '_swiped', 0.0) > 0.45:    # one step per gesture
                self._swiped = now
                self.step(-1 if dx > 0 else 1)
            ev.accept()
        else:
            ev.ignore()

    def _auto(self):
        f = QApplication.focusWidget()
        if not self.isVisible() or (self.underMouse() and input_mode() == 'mouse') \
                or (f and self.isAncestorOf(f)) \
                or not self.window().isActiveWindow():
            return
        self.step(1)

    def _show(self, animate: bool):
        g = self.game
        playing = bool(g.get('playing'))
        last = fmt_relative(g.get('last_played'))
        self.kicker.setText(("Now playing" if playing else
                             "Starting…" if g.get('launching') else
                             "Continue playing" if g.get('last_played') else
                             "Ready to play").upper())
        self.kicker.setProperty('live', playing)
        self.kicker.style().unpolish(self.kicker)
        self.kicker.style().polish(self.kicker)
        self.btn.setText(play_label(g))
        self.btn.setEnabled(play_enabled(g))
        logo, url = art_source(g, LOGO_KINDS)
        pm = self._logo(logo) if logo else QPixmap()
        if not pm.isNull():
            self.logo.setPixmap(pm)
        elif url:
            ART.fetch(url, lambda _p, slug=g.get('slug'): self._arrived(slug))
        self.logo.setVisible(not pm.isNull())
        self.title.setText(g.get('display_name', ''))
        self.title.setVisible(pm.isNull())
        clear_layout(self.facts)
        tier = g.get('protondb')
        facts = [(SOURCE_LABEL.get(g.get('source', 'nexus'), ''), None),
                 (f"Played {last}" if last else '', None),
                 (fmt_hours(g.get('playtime_minutes')) or '', None),
                 (tier.capitalize() if tier in TIER_COLORS else '', TIER_COLORS.get(tier)),
                 (fmt_size(g.get('disk_bytes')) or '', None),
                 ("Update ready" if g.get('update_pending') else '', P['warning'])]
        for text, color in facts:
            if text:
                h = int(24 * self.scale)
                lab = label(text, None)
                lab.setStyleSheet(pill_qss(P['glass'], color or P['on_art'], h,
                                           int(10 * self.scale))
                                  + f" font-size: {int(FS['xs'] * self.scale)}px;")
                lab.setFixedHeight(h)
                self.facts.addWidget(lab)
        self.facts.addStretch()
        path, url = art_source(g, HERO_KINDS)
        if not path and url:
            ART.fetch(url, lambda _p, slug=g.get('slug'): self._arrived(slug))
        nxt = (path, self._is_portrait(path)) if path else None
        if animate and self._cur is not None and MOTION:
            self._prev, self._cur, self._fade = self._cur, nxt, 0.0
            self._anim.stop()
            self._anim.setDuration(450)
            self._anim.start()
        else:
            self._prev, self._cur, self._fade = None, nxt, 1.0
        self.tint.emit(dominant_color(path))
        self.dots.update()
        self.update()

    def _arrived(self, slug):
        try:
            if self.game.get('slug') == slug:
                self._show(animate=False)
        except RuntimeError:
            pass

    def _faded(self, v):
        self._fade = float(v)
        if self._fade >= 1.0:
            self._prev = None
        self.update()

    @staticmethod
    def _is_portrait(path) -> bool:
        size = QImageReader(path).size()
        return size.isValid() and size.height() > size.width()

    def _logo(self, path) -> QPixmap:
        """The game's logo at banner size, scaled once per file."""
        try:
            key = (path, Path(path).stat().st_mtime, self.scale)
        except OSError:
            return QPixmap()
        if key not in self._logos:
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(int(420 * self.scale), int(140 * self.scale),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            if len(self._logos) > 8:
                self._logos.clear()
            self._logos[key] = pm
        return self._logos[key]

    def _source(self, path, mtime, w, h) -> QImage | None:
        """The art decoded to cover w x h (with headroom, so a slightly larger
        window doesn't decode again) — a 4K hero image is decoded once, not
        at every size."""
        key = (path, mtime)
        img = self._sources.get(key)
        if img is not None and img.width() >= w and img.height() >= h:
            return img
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            k = min(1.0, max(w / size.width(), h / size.height()) * 1.5)
            if k < 1.0:
                reader.setScaledSize(QSize(max(1, round(size.width() * k)),
                                           max(1, round(size.height() * k))))
        img = reader.read()
        if img.isNull():
            return None
        if len(self._sources) >= 6:
            self._sources.pop(next(iter(self._sources)))
        self._sources[key] = img
        return img

    # ---- paint ----
    def _frame(self, src) -> QPixmap | None:
        """The art scaled to fill this banner (cached per size)."""
        if src is None:
            return None
        path, portrait = src
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return None
        w, h = self.width(), self.height()
        key = (path, mtime, w, h)
        if key in self._frames:
            return self._frames[key]
        if self._settle.isActive():          # mid-resize: stretch the last frame
            old = next((f for k, f in reversed(self._frames.items()) if k[:2] == key[:2]),
                       None)
            if old is not None:
                return old.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                  Qt.TransformationMode.FastTransformation)
        img = self._source(path, mtime, w, h)
        if img is None:
            return None
        pm = QPixmap.fromImage(img)
        out = QPixmap(w, h)
        out.fill(QColor(P['bg']))
        pt = QPainter(out)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        if portrait:
            # Blurred, darkened backdrop from the cover itself, cover inset right.
            tiny = pm.scaled(24, 36, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            blur = tiny.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
            pt.drawPixmap((w - blur.width()) // 2, (h - blur.height()) // 2, blur)
            pt.fillRect(out.rect(), QColor(0, 0, 0, 90))
            ch = int(h * 0.82)
            cw = int(ch * 2 / 3)
            cover = rounded_pixmap(cover_pixmap(path, cw, ch), R['tile'])
            pt.drawPixmap(int(w - cw - w * 0.08), (h - ch) // 2, cover)
        else:
            s = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
            # Hero art keeps the top (logos / faces) — crop from the upper third.
            pt.drawPixmap((w - s.width()) // 2, -(s.height() - h) // 3, s)
        pt.end()
        if len(self._frames) > 12:
            self._frames.clear()
        self._frames[key] = out
        return out

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._settle.start()

    def _settled(self):
        w, h = self.width(), self.height()
        self._frames = {k: f for k, f in self._frames.items() if k[2:] == (w, h)}
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, R['panel'], R['panel'])
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_1']))
        prev, cur = self._frame(self._prev), self._frame(self._cur)
        if prev is not None and self._fade < 1.0:
            pt.drawPixmap(0, 0, prev)
        if cur is not None:
            pt.setOpacity(self._fade if prev is not None else 1.0)
            pt.drawPixmap(0, 0, cur)
            pt.setOpacity(1.0)
        elif prev is None:
            pt.drawPixmap(0, 0, placeholder_pixmap(self.game.get('display_name', ''),
                                                   self.width(), self.height()))
        base = QColor(P['bg'])
        # Left scrim for the text, bottom scrim melting into the page.
        g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
        c = QColor(base); c.setAlphaF(0.86); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.45); g.setColorAt(0.38, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(0.72, c)
        pt.fillRect(r, QBrush(g))
        g = QLinearGradient(0.0, r.bottom(), 0.0, r.top())
        c = QColor(base); c.setAlphaF(0.92); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(0.5, c)
        pt.fillRect(r, QBrush(g))
        # A whisper of the dusk gradient along the top edge.
        g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
        a, b = (QColor(x) for x in GRAD['dusk'])
        a.setAlphaF(0.0); b.setAlphaF(0.35)
        g.setColorAt(0.0, a); g.setColorAt(1.0, b)
        pt.fillRect(QRectF(0, 0, r.width(), 2), QBrush(g))
        pt.end()

    def _paint_dots(self, ev):
        pt = QPainter(self.dots)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(Qt.PenStyle.NoPen)
        cy = self.dots.height() / 2
        for i in range(len(self.games)):
            on = i == self.i
            c = QColor(P['on_art']); c.setAlphaF(0.95 if on else 0.35)
            pt.setBrush(c)
            w = 12.0 if on else 6.0
            pt.drawRoundedRect(QRectF(i * 16 + (16 - w) / 2, cy - 3, w, 6.0), 3.0, 3.0)
        pt.end()

    def _dot_clicked(self, ev):
        i = int(ev.position().x() // 16)
        if 0 <= i < len(self.games) and i != self.i:
            self.i = i
            self._show(animate=True)
            self.timer.start()


def surprise_pick(games: list[dict], rng, recent=(), now: datetime | None = None):
    """"Surprise me": one installed, visible game that isn't running, picked
    at random — weighted toward games never played (×3) or not played for a
    month (×2) and favourites (×1.5), and never one of the `recent` picks
    while there's anything else. None when there's nothing to pick."""
    now = now or datetime.now()
    cands = [g for g in games if is_installed(g) and not g.get('hidden')
             and not g.get('incomplete') and not g.get('playing') and not g.get('launching')]
    fresh = [g for g in cands if g.get('slug') not in recent] or cands
    if not fresh:
        return None

    def weight(g):
        w, lp = 1.0, g.get('last_played')
        if not lp:
            w *= 3
        else:
            try:
                if (now - datetime.fromisoformat(str(lp)[:19])).days >= 30:
                    w *= 2
            except ValueError:
                pass
        return w * (1.5 if g.get('favorite') else 1.0)
    return rng.choices(fresh, weights=[weight(g) for g in fresh])[0]


def featured_games(games: list[dict], n: int = 5) -> list[dict]:
    """What the hero shows: the game being played, then the most recently
    played, then a favourite, an update or something new — no repeats."""
    out: list[dict] = []

    def add(g):
        if g is not None and g not in out and len(out) < n:
            out.append(g)
    for g in games:
        if g.get('playing'):
            add(g)
    played = sorted((g for g in games if g.get('last_played')),
                    key=lambda g: g['last_played'], reverse=True)
    for g in played[:3]:
        add(g)
    add(next((g for g in games if g.get('favorite') and g not in out), None))
    add(next((g for g in games if g.get('update_pending') and g not in out), None))
    fresh = sorted((g for g in games if g.get('added_at') and g not in out),
                   key=lambda g: g['added_at'], reverse=True)
    for g in fresh:
        add(g)
    for g in played[3:]:
        add(g)
    return out


class HomeView(QScrollArea, _TileSignals):
    """Rows of games that page sideways.

    mode='home'    — the Home page: the hero carousel, then the dashboard
                     widgets (shelves like Jump back in, and cards).
    mode='shelves' — the Library's shelf view: one row per source."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)
    see_all = pyqtSignal(str, str)         # (chip, sort) for the Library
    tint = pyqtSignal(object)

    SHELF_TILE = 150
    WIDE_TILE = 300

    def __init__(self, mode: str = 'home'):
        super().__init__()
        self.mode = mode
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.lay = QVBoxLayout(host)
        self.lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['6'])
        self.lay.setSpacing(SP['5'])
        self.hero = HeroCarousel()
        self.hero.play.connect(self.play)
        self.hero.open.connect(self.activated)
        self.hero.menu.connect(self.menu)
        self.hero.tint.connect(self.tint)
        self.hero.setVisible(False)
        self.lay.addWidget(self.hero)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['5'])
        self.lay.addLayout(self.rows)
        self.extra = QVBoxLayout()            # Home: the dashboard goes here
        self.lay.addLayout(self.extra)
        self.lay.addStretch()
        self.setWidget(host)
        self.tiles: dict[str, list[GameTile]] = {}

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        h = self.viewport().height()
        self.hero.setFixedHeight(int(max(320, min(560, h * 0.52))))

    def set_games(self, games, empty_text=None, pending_jobs=()):
        clear_layout(self.rows)
        self.tiles = {}             # the dashboard re-adds its shelves' tiles after this
        if self.mode == 'home':
            feat = featured_games(games)
            self.hero.setVisible(bool(feat))
            self.hero.set_games(feat)
            # Everything else on Home is a dashboard widget (MainWindow fills
            # it); only an install in progress gets a row of its own.
            shelves = [("Installing", [], pending_jobs, 'cover', None)]
        else:
            self.hero.setVisible(False)
            shelves = [("Installing", [], pending_jobs, 'cover', None)]
            for src, name in (('nexus', "Repacks"), ('steam', "Steam"), ('heroic', "Heroic")):
                items = sorted((g for g in games if g.get('source', 'nexus') == src),
                               key=lambda g: (g.get('display_name') or '').lower())
                shelves.append((name, items, (), 'cover', (src, 'name')))
        for title, items, jobs, kind, link in shelves:
            if items or jobs:
                self._shelf(title, items, jobs, kind, link)
        if not games and not pending_jobs:
            self.rows.addWidget(EmptyState(G['library'], *empty_state_text(empty_text)))

    def make_shelf(self, title, games, kind='cover', link=None) -> 'ShelfRow':
        """A ShelfRow whose tiles are wired to this view (for the dashboard)."""
        return self._shelf(title, games, (), kind, link, add=False)

    def _shelf(self, title, games, jobs, kind, link, add=True):
        w = self.WIDE_TILE if kind == 'wide' else self.SHELF_TILE
        row = ShelfRow(title, w, kind, count=len(games) if link else None,
                       see_all=bool(link))
        if link:
            row.see_all.connect(lambda c=link[0], s=link[1]: self.see_all.emit(c, s))
        for job in jobs:
            t = GameTile({'slug': f'__pending_{id(job)}', 'display_name': job.name},
                         pending=True, width=self.SHELF_TILE)
            t.set_job(job)
            t.selected.connect(lambda _g, _m, j=job: self.pending_clicked.emit(j))
            row.add(t)
        for g in games:
            t = GameTile(g, width=w, kind=kind)
            self._wire(t)
            row.add(t)
            self.tiles.setdefault(g['slug'], []).append(t)
        row.finish()
        if add:
            self.rows.addWidget(row)
        return row

    def _each_tile(self):
        for slug, ts in self.tiles.items():
            for t in list(ts):
                try:
                    t.objectName()
                except RuntimeError:           # deleted with its row
                    ts.remove(t)
                    continue
                yield slug, t

    def mark_selected(self, slugs):
        for s, t in self._each_tile():
            t.set_selected(s in slugs)

    def reveal(self, slug):
        for t in self.tiles.get(slug, [])[:1]:
            focus_and_reveal(t)

    def set_jobs(self, jobs):
        for slug, t in self._each_tile():
            t.set_job(jobs.for_slug(slug))


# --------------------------------------------------------------------------
# Dashboard — glanceable widgets under the Home hero, laid out by the user
# --------------------------------------------------------------------------

class ClickRow(QFrame):
    """A focusable, clickable row with a real layout (a QPushButton ignores
    its child layout's height, so wrapped text in it collapses)."""
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("DashRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(ev)


class GlyphBadge(QWidget):
    """A widget's icon on a small gradient tile."""

    def __init__(self, glyph: str, grad: str, size: int = 28):
        super().__init__()
        self.glyph, self.grad = glyph, grad
        self.setFixedSize(size, size)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        a, b = (QColor(c) for c in GRAD[self.grad])
        g.setColorAt(0.0, a)
        g.setColorAt(1.0, b)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QBrush(g))
        pt.drawRoundedRect(r, R['sm'] + 2, R['sm'] + 2)
        f = QFont(ICON_FONT)
        f.setPixelSize(int(self.height() * 0.5))
        pt.setFont(f)
        pt.setPen(QColor(P['on_accent']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, self.glyph)
        pt.end()


class DashWidget(QFrame):
    """One dashboard card. Subclasses set the class attributes and fill
    `self.body` in fill(hub); `hub` is the main window (games, jobs, pages).
    Shelf widgets are unframed rows of tiles instead of cards."""
    KIND, TITLE, GLYPH, GRAD, LINK = '', '', '', 'dusk', None
    FRAMED = True
    go = pyqtSignal(str)                  # open a page
    edit = pyqtSignal(str, str)           # (kind, action) from the edit bar

    def __init__(self):
        super().__init__()
        self.setObjectName("DashCard" if self.FRAMED else "DashShelf")
        self.size_ = 'small'
        lay = QVBoxLayout(self)
        pad = SP['4'] if self.FRAMED else 0
        lay.setContentsMargins(pad, pad - 2 if pad else 0, pad, pad)
        lay.setSpacing(SP['2_5'])
        self.editbar = self._edit_bar()
        self.editbar.setVisible(False)
        lay.addWidget(self.editbar)
        if self.FRAMED:
            head = QHBoxLayout()
            head.setSpacing(SP['2'])
            head.addWidget(GlyphBadge(self.GLYPH, self.GRAD))
            t = label(self.TITLE, "DashTitle")
            head.addWidget(t)
            head.addStretch()
            if self.LINK:
                more = QPushButton("Open")
                more.setObjectName("Ghost")
                more.setCursor(Qt.CursorShape.PointingHandCursor)
                more.clicked.connect(lambda: self.go.emit(self.LINK))
                head.addWidget(more)
            lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(SP['1_5'])
        lay.addLayout(self.body)
        if self.FRAMED:
            lay.addStretch()

    def _edit_bar(self):
        bar = QFrame()
        bar.setObjectName("DashEditBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(SP['2'], SP['1'], SP['1'], SP['1'])
        bl.setSpacing(SP['1'])
        self.edit_name = label(self.TITLE, "StatLabel")
        bl.addWidget(self.edit_name)
        bl.addStretch()
        for glyph, act, tip in ((G['arrow_left'], 'left', "Move left"), (G['arrow_up'], 'up', "Move up"),
                                (G['arrow_down'], 'down', "Move down"),
                                (G['arrow_right'], 'right', "Move right")):
            b = icon_button(glyph, tip, 28, "Ghost", round_=True)
            b.clicked.connect(lambda _=False, a=act: self.edit.emit(self.KIND, a))
            bl.addWidget(b)
        self.btn_size = QPushButton()
        self.btn_size.setObjectName("Ghost")
        self.btn_size.setToolTip("Change size")
        self.btn_size.clicked.connect(lambda: self.edit.emit(self.KIND, 'size'))
        bl.addWidget(self.btn_size)
        hide = icon_button(G['eye_off'], "Hide this widget", 28, "Ghost", round_=True)
        hide.clicked.connect(lambda: self.edit.emit(self.KIND, 'hide'))
        bl.addWidget(hide)
        return bar

    def set_editing(self, on: bool, size: str):
        self.size_ = size
        self.editbar.setVisible(on)
        sizes = DASH_SIZES.get(self.KIND, ('small',))
        self.btn_size.setVisible(len(sizes) > 1)
        self.btn_size.setText({'small': "Small", 'wide': "Wide", 'full': "Full width"}[size])
        self.setProperty('editing', on)
        self.style().unpolish(self)
        self.style().polish(self)

    def has_content(self, hub) -> bool:
        """Shelf widgets with nothing to show stay out of the layout."""
        return True

    def fill(self, hub):
        pass

    def clear(self):
        clear_layout(self.body)

    def _error(self, message: str, retry) -> bool:
        """A failed load: say why, with Retry (instead of "Loading…" that
        never ends). False when there's no error to show."""
        if not message:
            return False
        self.body.addWidget(label(f"Couldn't load — {message}", "Muted", wrap=True))
        b = QPushButton("Retry")
        b.clicked.connect(retry)
        self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        return True

    # small row helpers shared by the card widgets
    def row(self, glyph: str, text: str, sub: str = '', color: str | None = None,
            on_click=None) -> QWidget:
        w = ClickRow() if on_click else QWidget()
        if on_click:
            w.clicked.connect(on_click)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2'])
        ic = icon_label(glyph, 13, color or P['muted'])
        ic.setFixedWidth(18)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = label(text, None, wrap=True)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        t.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        col.addWidget(t)
        if sub:
            sl = label(sub, "Muted", wrap=True)
            sl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            col.addWidget(sl)
        lay.addLayout(col, 1)
        return w

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self.FRAMED:
            return
        # A soft glow of the widget's gradient in its top-left corner.
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), R['card'], R['card'])
        pt.setClipPath(clip)
        g = QRadialGradient(0.0, 0.0, float(max(160, self.width() * 0.6)))
        c = QColor(GRAD[self.GRAD][0]); c.setAlphaF(0.10)
        e = QColor(c); e.setAlphaF(0.0)
        g.setColorAt(0.0, c)
        g.setColorAt(1.0, e)
        pt.fillRect(self.rect(), QBrush(g))
        pt.end()


class ShelfWidget(DashWidget):
    """A row of game tiles (Jump back in, Recently installed, …)."""
    FRAMED = False
    KIND_TILE = 'cover'
    LINK_CHIP = None                      # (chip, sort) for "See all"

    def games(self, hub) -> list[dict]:
        return []

    def has_content(self, hub):
        return bool(self.games(hub))

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):           # forget the old row's tiles
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        games = self.games(hub)
        if games:
            row = view.make_shelf(self.TITLE, games, self.KIND_TILE, self.LINK_CHIP)
            self._tiles = row.tiles
            self.body.addWidget(row)


def _visible_games(hub):
    """What Home and its widgets are about: installed, not hidden, and one
    copy of a game you have in several stores."""
    return [g for g in hub._games if not g.get('hidden') and is_installed(g)
            and not g.get('dup_of')]


DUP_ORDER = ('nexus', 'steam', 'heroic')


def _game_store_id(g: dict) -> int | None:
    if g.get('source') == 'steam' and str(g.get('appid', '')).isdigit():
        return int(g['appid'])
    v = str(g.get('umu_gameid') or '').removeprefix('umu-')
    return int(v) if v.isdigit() and 0 < int(v) < 0x80000000 else None


def mark_duplicates(games: list[dict], enabled: bool = True) -> None:
    """The same game from more than one source (a repack and a Steam copy,
    a Heroic and a Steam one…) keeps one tile: installed beats owned, then
    repack > Steam > Heroic, then more play time. The others get `dup_of`
    (the kept slug); the kept one lists the others' sources in `dup_also`.
    Matched by Steam store id, else by name."""
    for g in games:
        g.pop('dup_of', None)
        g.pop('dup_also', None)
    if not enabled:
        return
    groups: dict = {}
    for g in games:
        sid = _game_store_id(g)
        name = _search_norm(g.get('store_name') or g.get('display_name') or '')
        key = ('id', sid) if sid else ('name', name) if name else None
        if key:
            groups.setdefault(key, []).append(g)
    # A store id and a name can describe the same game: fold name-only
    # groups into the id group with that name.
    by_name = {}
    for key, members in list(groups.items()):
        if key[0] == 'id':
            for m in members:
                by_name.setdefault(_search_norm(m.get('store_name') or m.get('display_name')
                                                or ''), key)
    for key in [k for k in groups if k[0] == 'name']:
        if key[1] in by_name and by_name[key[1]] in groups:
            groups[by_name[key[1]]] += groups.pop(key)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda g: (not is_installed(g),
                                    DUP_ORDER.index(g.get('source', 'nexus'))
                                    if g.get('source', 'nexus') in DUP_ORDER else 9,
                                    -(g.get('playtime_minutes') or 0)))
        keep = members[0]
        keep['dup_also'] = sorted({m.get('source', 'nexus') for m in members[1:]}
                                  - {keep.get('source', 'nexus')})
        for m in members[1:]:
            m['dup_of'] = keep['slug']


class JumpWidget(ShelfWidget):
    KIND, TITLE, KIND_TILE = 'jump', "Jump back in", 'wide'

    def games(self, hub):
        return sorted((g for g in _visible_games(hub) if g.get('last_played')),
                      key=lambda g: g['last_played'], reverse=True)[:10]


class InstalledWidget(ShelfWidget):
    KIND, TITLE, LINK_CHIP = 'installed', "Recently installed", ('all', 'added')

    def games(self, hub):
        return sorted((g for g in _visible_games(hub) if g.get('added_at')),
                      key=lambda g: g['added_at'], reverse=True)[:12]

    def fill(self, hub):
        super().fill(hub)
        for t in self._tiles:            # never played yet: what "Not played yet" was for
            t.mark_new = not t.game.get('last_played')
            t.update()


class FavouritesWidget(ShelfWidget):
    KIND, TITLE, LINK_CHIP = 'favourites', "Favourites", ('favorite', 'name')

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('favorite')]


class CollectionsWidget(ShelfWidget):
    """One row per collection that has an installed game in it."""
    KIND, TITLE = 'collections', "Collections"

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('collections')]

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        games = _visible_games(hub)
        for name in COLLECTION_NAMES:
            items = [g for g in games if name in (g.get('collections') or ())]
            if items:
                row = view.make_shelf(name, items, self.KIND_TILE, (f'col:{name}', 'name'))
                self._tiles += row.tiles
                self.body.addWidget(row)


class SurpriseWidget(DashWidget):
    """"Can't decide?" — one button that picks a game for you."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'surprise', "Surprise me", G['dice'], 'dawn', None

    def has_content(self, hub):
        return bool(_visible_games(hub))

    def fill(self, hub):
        self.clear()
        row = QHBoxLayout()
        row.setSpacing(SP['3'])
        t = label("Can't decide? Nexus picks one of your games — ones you haven't played "
                  "in a while and favourites come up more often.", "Muted", wrap=True)
        row.addWidget(t, 1)
        b = QPushButton("Pick a game")
        b.setObjectName("Primary")
        set_glyph_icon(b, G['dice'], 16, 'on_accent', hover_role=None)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda: hub.surprise())
        row.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(row)


class UpdatesWidget(ShelfWidget):
    KIND, TITLE = 'updates', "Updates ready"

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('update_pending')]


class OwnedWidget(ShelfWidget):
    """Games you own on Steam / Epic / GOG but haven't installed: the ones
    downloading first, then the ones you've played most."""
    KIND, TITLE, LINK_CHIP = 'owned', "In your library", ('owned', 'most')

    def games(self, hub):
        owned = [g for g in hub._games if not g.get('hidden') and not is_installed(g)]
        owned.sort(key=lambda g: (not g.get('installing'), -(g.get('playtime_minutes') or 0)))
        return owned[:24]


def job_facts(jobs) -> list[dict]:
    """JobManager jobs as the plain dicts system_status() reads."""
    out = []
    for j in jobs:
        prog = getattr(j, 'progress', None)
        pct = (int(100 * min(prog[0], prog[1]) / prog[1])
               if j.state == 'running' and prog and prog[1] else None)
        out.append({'title': j.title, 'state': j.state, 'step': j.step, 'progress': pct,
                    'active': j.active, 'job': j})
    return out


class SysTile(ClickRow):
    """One clickable status tile in the System widget: icon, value, caption."""

    def __init__(self, t: dict):
        super().__init__()
        self.setObjectName("SysTile")
        self.key = t['key']
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['2_5'], SP['2'], SP['2_5'], SP['2'])
        lay.setSpacing(SP['2_5'])
        role = t['role']
        color = P['muted'] if role in ('muted', 'text') else P[role]
        ic = icon_label(G[t['glyph']], 18, color)
        ic.setFixedWidth(22)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.value = ElideLabel(t['value'], "StatValue")
        if role not in ('muted', 'text'):
            self.value.setStyleSheet(f"color: {P[role]};")
        col.addWidget(self.value)
        self.caption = ElideLabel(t['caption'], "StatLabel")
        col.addWidget(self.caption)
        lay.addLayout(col, 1)
        for w in (ic, self.value, self.caption):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip(f"{t['value']} — {t['caption']}")


class SystemWidget(DashWidget):
    """Jobs, disk space, health and Proton at a glance — four tiles, each
    opening its part of the System page (Proton: the Proton page). Wide adds
    the one thing that needs you, if anything does."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'system', "System", G['system'], 'tide', 'system'

    def status(self, hub) -> dict:
        proton = getattr(hub.protonpage, 'info', None)
        return system_status(job_facts(hub.jobs.jobs), hub._disks,
                             getattr(hub.health, 'checks', []), proton)

    def fill(self, hub):
        st = self.status(hub)
        sig = (self.size_, repr([(t['value'], t['caption'], t['role']) for t in st['tiles']]),
               repr({k: v for k, v in (st['attention'] or {}).items() if k != 'job'}))
        if sig == getattr(self, '_sig', None):
            return                  # unchanged: keep the widgets (and pad focus) as they are
        self._sig = sig
        self.clear()
        grid = QGridLayout()
        grid.setSpacing(SP['2'])
        cols = 4 if self.size_ == 'wide' else 2
        self.tiles = []
        for i, t in enumerate(st['tiles']):
            tile = SysTile(t)
            tile.clicked.connect(lambda k=t['key']: hub.go(k))
            self.tiles.append(tile)
            grid.addWidget(tile, i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        self.body.addLayout(grid)
        att = st['attention']
        if att and self.size_ == 'wide':
            if att['key'] == 'activity':
                job = next((j['job'] for j in job_facts(hub.jobs.jobs) if j['active']), None)
                go = (lambda _=False, j=job: hub._open_job(j)) if job else None
            else:
                go = lambda _=False, k=att['key']: hub.go(k)
            line = QHBoxLayout()
            line.addWidget(self.row(G[att['glyph']], att['text'], att['sub'], P[att['role']],
                                    go), 1)
            if att.get('action') == 'install-ge':
                b = QPushButton(f"Install {att['latest']}")
                b.setObjectName("Primary")
                b.clicked.connect(lambda: hub.protonpage._install('latest'))
                line.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
            self.body.addLayout(line)


class NewsWidget(DashWidget):
    KIND, TITLE, GLYPH, GRAD, LINK = 'news', "News", G['news'], 'aurora', 'news'

    def fill(self, hub):
        self.clear()
        if not hub.config.get('news', True):
            self.body.addWidget(label("The News page is off (Settings).", "Muted", wrap=True))
            return
        items = (getattr(hub.newspage, 'data', {}) or {}).get('items', [])
        if not items:
            if not self._error(hub.newspage.error, lambda: hub.newspage.refresh(force=True)):
                self.body.addWidget(label("Loading headlines…", "Muted"))
                hub.newspage.refresh()            # once; cached by the CLI
            return
        n = {'small': 3, 'wide': 4, 'full': 6}[self.size_]
        for it in items[:n]:
            self.body.addWidget(self._story_row(it, hub))

    def _story_row(self, it, hub):
        """Thumbnail, headline, source · time; opens the story in the News
        page's reader."""
        w = ClickRow()
        w.clicked.connect(lambda: (hub.go(hub.NEWS), hub.newspage.open_item(it)))
        lay = QHBoxLayout(w)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2_5'])
        art = NewsArt(it, 64, 36, R['sm'] - 2, hub.newspage._image_for)
        art.load()                       # a handful of rows: fine to load now
        lay.addWidget(art, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = plain_label(it.get('title', ''), None, wrap=True)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        col.addWidget(t)
        role = news_style(it)[0]
        sub = label(" · ".join(x for x in (it.get('source'), fmt_relative(it.get('date')))
                               if x), None)
        sub.setStyleSheet(f"color: {P[role]}; font-size: {FS['2xs']}px;")
        col.addWidget(sub)
        lay.addLayout(col, 1)
        for x in (t, sub):
            x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        return w


class DealsWidget(DashWidget):
    KIND, TITLE, GLYPH, GRAD, LINK = 'deals', "Wishlist deals", G['deals'], 'dawn', 'deals'

    def fill(self, hub):
        self.clear()
        if not hub.config.get('gamedb_enabled', False):
            self.body.addWidget(label("Connect Game-db in Settings to see deals on your "
                                      "Steam wishlist.", "Muted", wrap=True))
            b = QPushButton("Open Settings")
            b.clicked.connect(lambda: hub.go(hub.SETTINGS))
            self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
            return
        d = hub.dealspage._data()
        items = [g for g in d.get('wishlist') or [] if (g.get('price') or {}).get('onSale')]
        if not d:
            if not self._error(hub.dealspage.error, hub.dealspage.retry):
                self.body.addWidget(label("Loading deals…", "Muted"))
                hub.dealspage.refresh()
            return
        if not items:
            self.body.addWidget(label("Nothing on your wishlist is on sale right now.",
                                      "Muted", wrap=True))
            return
        items.sort(key=lambda g: -((g.get('price') or {}).get('cutPct') or 0))
        n = {'small': 3, 'wide': 4, 'full': 6}[self.size_]
        for g in items[:n]:
            price = g.get('price') or {}
            cur = label(price.get('currentStr') or '', None)
            cur.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px; font-weight: 600;")
            right = [cur] + ([_pill(f"-{price['cutPct']}%", 'accent')]
                             if price.get('cutPct') else [])
            self.body.addWidget(game_row(g, right, lambda: hub.go(hub.DEALS)))
        radar = d.get('radar') or {}
        if radar.get('onSaleCount'):
            self.body.addWidget(label(f"{radar['onSaleCount']} on sale · "
                                      f"save ~{radar.get('totalSavings', 0):.0f} "
                                      f"{d.get('currency') or ''}", "Muted"))


def game_row(g: dict, right: list, on_click, sub: str = '') -> ClickRow:
    """A dashboard row for a store game: header thumbnail (fetched once,
    cached), title (and an optional muted line), then `right` widgets."""
    w = ClickRow()
    w.clicked.connect(on_click)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
    lay.setSpacing(SP['2_5'])
    thumb = QLabel()
    thumb.setFixedSize(64, 30)
    thumb.setStyleSheet(f"background: {P['surface_2']}; border-radius: {R['sm'] - 2}px;")
    url = gamedb.safe_url(g.get('headerImage'))

    def show(pm, thumb=thumb):
        try:
            thumb.setPixmap(pm)
            thumb.setStyleSheet("")
        except RuntimeError:                     # the row is gone
            pass

    def put(path):
        ART.pixmap_async(path, 64, 30, R['sm'] - 2, show)
    if url:
        cached = ART.cached(url)
        if cached:
            put(cached)
        else:
            ART.fetch(url, put)
    lay.addWidget(thumb)
    col = QVBoxLayout()
    col.setSpacing(0)
    t = plain_label(g.get('title') or '', None)
    t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
    t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    col.addWidget(t)
    parts = [thumb, t]
    if sub:
        sl = label(sub, "Muted")
        sl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(sl)
        parts.append(sl)
    lay.addLayout(col, 1)
    for x in right:
        lay.addWidget(x)
        parts.append(x)
    for x in parts:
        x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return w


class UpcomingWidget(DashWidget):
    """Unreleased games on your wishlist, soonest first, with the cheapest
    current price (keyshops included) — from the Game-db summary the Deals
    page already holds."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'upcoming', "Upcoming", G['calendar'], 'aurora', 'upcoming'

    def has_content(self, hub):
        # Off without Game-db: the Deals widget already says how to connect it.
        if not hub.config.get('gamedb_enabled', False):
            return False
        return not hub.dealspage._loaded or bool(gamedb.upcoming(hub.dealspage._data()))

    def fill(self, hub):
        self.clear()
        d = hub.dealspage._data()
        if not d:
            if not self._error(hub.dealspage.error, hub.dealspage.retry):
                self.body.addWidget(label("Loading releases…", "Muted"))
                hub.dealspage.refresh()
            return
        items = gamedb.upcoming(d)
        if not items:
            self.body.addWidget(label("Nothing unreleased on your wishlist.", "Muted",
                                      wrap=True))
            return
        n = {'small': 3, 'wide': 4, 'full': 6}[self.size_]
        for g in items[:n]:
            text, preorder = gamedb.price_line(g)
            when = label(gamedb.release_label(g), None)
            when.setStyleSheet(f"color: {P['accent']}; font-size: {FS['xs']}px; "
                               f"font-weight: 600;")
            right = [_pill("Pre-order", 'warning')] if preorder else []
            base = hub.dealspage._base()
            url = g.get('steamUrl') or (gamedb.detail_url(base, g['appid'])
                                        if base and g.get('appid') else '')
            row = game_row(g, [when] + right, lambda _=False, u=url: hub.dealspage._open(u),
                           sub=text)
            row.setToolTip(" · ".join(x for x in (g.get('releaseDateText'), text) if x))
            self.body.addWidget(row)
        if len(items) > n:
            self.body.addWidget(label(f"+{len(items) - n} more on the Deals page", "Muted"))


class PlaytimeWidget(DashWidget):
    KIND, TITLE, GLYPH, GRAD, LINK = 'playtime', "Play time", G['clock'], 'dusk', None

    def fill(self, hub):
        self.clear()
        games = [g for g in hub._games if g.get('playtime_minutes')]
        total = sum(g['playtime_minutes'] for g in games)
        if not total:
            self.body.addWidget(label("No play time recorded yet.", "Muted"))
            return
        big = label(fmt_hours(total) or '0 h', "DashBig")
        self.body.addWidget(big)
        self.body.addWidget(label(f"across {len(games)} game{'s' if len(games) != 1 else ''}",
                                  "Muted"))
        top = sorted(games, key=lambda g: g['playtime_minutes'], reverse=True)
        top = top[:3 if self.size_ == 'small' else 5]
        most = top[0]['playtime_minutes']
        for g in top:
            row = QHBoxLayout()
            n = label(g.get('display_name', ''), None)
            n.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['xs']}px;")
            n.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(n, 1)
            row.addWidget(label(fmt_hours(g['playtime_minutes']) or '', "Muted"))
            self.body.addLayout(row)
            bar = QProgressBar()
            bar.setObjectName("CardProgress")
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setRange(0, 1000)
            bar.setValue(int(1000 * g['playtime_minutes'] / most))
            self.body.addWidget(bar)


class AchievementsWidget(DashWidget):
    """Steam achievements: how many you've unlocked, the newest unlocks and
    the games closest to 100% — from Steam's own stats cache."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'achievements', "Achievements", G['trophy'], 'dusk', None

    def __init__(self):
        super().__init__()
        self._recent: list | None = None
        self._err = ''
        self._asked = 0.0

    def fill(self, hub):
        self.clear()
        games = [g for g in hub._games if g.get('source') == 'steam' and g.get('achievements')]
        if not games:
            self.body.addWidget(label("Steam games you've played show their achievements "
                                      "here.", "Muted", wrap=True))
            return
        unlocked = sum(g['achievements'][0] for g in games)
        self.body.addWidget(label(f"{unlocked:,}", "DashBig"))
        done = sum(1 for g in games if g['achievements'][0] == g['achievements'][1])
        self.body.addWidget(label(f"unlocked across {len(games)} games"
                                  + (f" · {done} complete" if done else ""), "Muted"))
        n = 3 if self.size_ == 'small' else 5
        if self._recent is None or time.monotonic() - self._asked > 300:
            self._asked = time.monotonic()
            cli_json(['--achievements', 'recent', '--json'],
                     lambda d, e, hub=hub: self._arrived(hub, d, e), 30000, ttl=300)
        if self._err:
            self.body.addWidget(label(f"Recent unlocks unavailable: {self._err}", "Muted",
                                      wrap=True))
        names = {g['appid']: g.get('display_name', '') for g in hub._games if g.get('appid')}
        for a in (self._recent or [])[:n]:
            row = QHBoxLayout()
            t = plain_label(a['title'], None)
            t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
            t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(t, 1)
            when = fmt_relative(datetime.fromtimestamp(a['time']).isoformat()) or ''
            row.addWidget(label(f"{names.get(a['appid'], '')} · {when}".strip(' ·'), "Muted"))
            self.body.addLayout(row)
        close = sorted((g for g in games if 0 < g['achievements'][0] < g['achievements'][1]),
                       key=lambda g: g['achievements'][0] / g['achievements'][1], reverse=True)
        if close:
            self.body.addWidget(label("Closest to 100%", "StatLabel"))
        for g in close[:2 if self.size_ == 'small' else 4]:
            a, b = g['achievements']
            row = QHBoxLayout()
            nm = label(g.get('display_name', ''), None)
            nm.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['xs']}px;")
            nm.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(nm, 1)
            row.addWidget(label(f"{a}/{b}", "Muted"))
            self.body.addLayout(row)
            bar = QProgressBar()
            bar.setObjectName("CardProgress")
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setRange(0, b)
            bar.setValue(a)
            self.body.addWidget(bar)

    def _arrived(self, hub, data, err=''):
        if data is None:
            if not self._err:
                self._err = err or 'error'
                self._recent = self._recent or []
                self.fill(hub)
            return
        self._err = ''
        first = self._recent is None
        self._recent = data.get('recent') or []
        if first or self._recent:
            self.fill(hub)


DASH_WIDGETS = {cls.KIND: cls for cls in (
    JumpWidget, SurpriseWidget, SystemWidget, NewsWidget, InstalledWidget, DealsWidget,
    UpcomingWidget, FavouritesWidget, CollectionsWidget, UpdatesWidget, PlaytimeWidget,
    AchievementsWidget, OwnedWidget)}


class Dashboard(QWidget):
    """The widget grid under the hero. Columns follow the window width;
    "Customise" turns on per-widget edit bars (move ←↑↓→, size, hide) and an
    "Add widgets" row for hidden ones — all buttons, so a controller can do
    it too. The layout is saved in library.json."""

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.editing = False
        self.widgets: dict[str, DashWidget] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        self.title = label("At a glance", "ShelfTitle")
        head.addWidget(self.title)
        head.addStretch()
        self.btn_reset = QPushButton("Reset layout")
        self.btn_reset.setObjectName("Ghost")
        self.btn_reset.clicked.connect(self._reset)
        self.btn_reset.setVisible(False)
        head.addWidget(self.btn_reset)
        self.btn_edit = QPushButton("Customise")
        self.btn_edit.setObjectName("Ghost")
        self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn_edit, G['customise'], 16)
        self.btn_edit.clicked.connect(lambda: self.set_editing(not self.editing))
        head.addWidget(self.btn_edit)
        lay.addLayout(head)
        self.add_row_host = QFrame()
        self.add_row_host.setObjectName("DashAdd")
        self.add_row = FlowLayout(self.add_row_host, margin=SP['3'], spacing=SP['1_5'])
        self.add_row_host.setVisible(False)
        lay.addWidget(self.add_row_host)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(SP['4'])
        self.grid.setVerticalSpacing(SP['5'])
        lay.addLayout(self.grid)
        self._cols = 0
        for kind, cls in DASH_WIDGETS.items():
            w = cls()
            w.setParent(self)                 # never a window of its own, even unplaced
            w.hide()
            w.go.connect(hub.go)
            w.edit.connect(self._edit)
            self.widgets[kind] = w

    @property
    def items(self):
        return self.hub.prefs.dashboard

    def _skip(self):
        """Shelf widgets with nothing in them stay out of the layout (except
        while editing, so they can still be arranged)."""
        if self.editing:
            return set()
        return {k for k, w in self.widgets.items() if not w.has_content(self.hub)}

    # ---- data ----
    def refresh(self, kinds=None):
        for k, w in self.widgets.items():
            if kinds is None or k in kinds:
                it = next((i for i in self.items if i['id'] == k), None)
                w.size_ = it['size'] if it else 'small'
                if it and not it['hidden']:
                    w.fill(self.hub)
        if kinds is None or {'jump', 'installed', 'favourites', 'collections', 'updates',
                             'upcoming', 'owned'} & set(kinds or ()):
            self.relayout(force=True)

    # ---- layout ----
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if dash_cols(self.width()) != self._cols:
            self.relayout()

    def relayout(self, force=False):
        cols = dash_cols(max(self.width(), 1))
        while self.grid.count():
            self.grid.takeAt(0)
        for c in range(6):
            self.grid.setColumnStretch(c, 0)
        self._cols = cols
        placed = dash_pack(self.items, cols, self._skip())
        on = {k for k, *_ in placed}
        for k, r, c, span in placed:
            it = next(i for i in self.items if i['id'] == k)
            self.widgets[k].set_editing(self.editing, it['size'])
            self.grid.addWidget(self.widgets[k], r, c, 1, span)
        for k, w in self.widgets.items():
            w.setVisible(k in on)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        self._fill_add_row()

    def _fill_add_row(self):
        while self.add_row.count():
            it = self.add_row.takeAt(0)
            if it.widget():
                it.widget().hide()             # gone now, not at the next event loop
                it.widget().deleteLater()
        hidden = [i for i in self.items if i['hidden']]
        self.add_row_host.setVisible(self.editing)
        if not self.editing:
            return
        if not hidden:
            self.add_row.addWidget(label("Every widget is on the dashboard. Hide one with its"
                                         " eye button to remove it.", "Muted"))
        for it in hidden:
            b = QPushButton(f"＋  {DASH_WIDGETS[it['id']].TITLE}")
            b.setObjectName("Chip")
            b.clicked.connect(lambda _=False, k=it['id']: self._edit(k, 'show'))
            self.add_row.addWidget(b)

    # ---- editing ----
    def set_editing(self, on: bool):
        self.editing = on
        self.btn_edit.setText("Done" if on else "Customise")
        self.btn_edit.setObjectName("Primary" if on else "Ghost")
        set_glyph_icon(self.btn_edit, G['check'] if on else G['customise'], 16,
                       'on_accent' if on else 'subtext', hover_role=None if on else 'text')
        self.btn_edit.style().unpolish(self.btn_edit)
        self.btn_edit.style().polish(self.btn_edit)
        self.btn_reset.setVisible(on)
        self.relayout(force=True)
        if not on:
            self.hub.prefs.save()

    def _edit(self, kind, action):
        items = self.items
        if action in ('left', 'right', 'up', 'down'):
            new = dash_move(items, kind, action, self._cols, self._skip())
        else:
            new = [dict(i) for i in items]
            it = next(i for i in new if i['id'] == kind)
            if action == 'size':
                sizes = DASH_SIZES[kind]
                it['size'] = sizes[(sizes.index(it['size']) + 1) % len(sizes)]
            elif action == 'hide':
                it['hidden'] = True
            elif action == 'show':
                it['hidden'] = False
        self.hub.prefs.dashboard = new
        self.refresh([kind]) if action in ('size', 'show') else None
        self.relayout(force=True)
        self.hub.prefs.save()
        # Keep the controller's focus on the widget that moved.
        w = self.widgets.get(kind)
        if w is not None and w.isVisible() and action in ('left', 'right', 'up', 'down'):
            btn = next((b for b in w.editbar.findChildren(QToolButton)
                        if b.toolTip().lower().endswith(action)), None)
            QTimer.singleShot(0, lambda: btn and alive(btn) and focus_and_reveal(btn))

    def _reset(self):
        self.hub.prefs.dashboard = dash_default()
        self.refresh()
        self.hub.prefs.save()


class ListView(QTableWidget):
    """Dense list: thumbnail, name, source, size, played, last played, Proton."""
    clicked = pyqtSignal(dict, object)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)
    double = pyqtSignal(dict)
    pending_clicked = pyqtSignal(object)

    COLS = ["", "Game", "Source", "Size", "Played", "Last played", "Proton"]
    NUMERIC = (3, 4)

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(52)
        self.setIconSize(QSize(30, 45))
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in self.AUTO_COLS:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        # Headers line up with their cells: text left, numbers right.
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for i in self.NUMERIC:
            self.horizontalHeaderItem(i).setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hh.setMinimumSectionSize(64)
        # No sort column until the user picks one: keep the toolbar's order
        # (otherwise Qt sorts by the empty thumbnail column).
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.setSortingEnabled(True)
        # Keep the last column off the window edge (QTableView owns its
        # viewport margins, so pad via QSS instead).
        self.setObjectName("LibraryList")
        self.games: dict[str, dict] = {}
        self._rows: dict[str, QTableWidgetItem] = {}
        self._syncing = False
        self.cellClicked.connect(self._click)
        self.cellDoubleClicked.connect(
            lambda r, _c: self._game(r) and self.double.emit(self._game(r)))
        self.customContextMenuRequested.connect(self._context)
        self.setContentsMargins(SP['5'], 0, SP['5'], 0)
        # Arrow keys move the details panel along with the highlighted row
        # (a click selects on its own; holding a key down settles first).
        self._cur_timer = QTimer(self)
        self._cur_timer.setSingleShot(True)
        self._cur_timer.setInterval(150)
        self._cur_timer.timeout.connect(self._emit_current)
        self.currentCellChanged.connect(self._current_changed)

    def _current_changed(self, row, _col, prev, _pcol):
        if self._syncing or row < 0 or row == prev \
                or QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return
        self._cur_timer.start()

    def _emit_current(self):
        row = self.currentRow()
        if row >= 0 and self._game(row):
            self.clicked.emit(self._game(row), Qt.KeyboardModifier.NoModifier)

    def reset_sort(self):
        """The toolbar's Sort was changed: it wins over a header click."""
        self.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

    def keyPressEvent(self, ev):
        row = self.currentRow()
        if row >= 0 and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self._game(row))
        elif row >= 0 and ev.key() == Qt.Key.Key_Menu:
            rect = self.visualItemRect(self.item(row, 1))
            self.menu.emit(self._game(row), self.viewport().mapToGlobal(rect.center()))
        else:
            super().keyPressEvent(ev)

    def _game(self, row):
        it = self.item(row, 1)
        return self.games.get(it.data(Qt.ItemDataRole.UserRole)) if it else {}

    def _click(self, row, _col):
        mods = QApplication.keyboardModifiers()
        self.clicked.emit(self._game(row), mods)

    def _context(self, pos):
        row = self.rowAt(pos.y())
        if row >= 0:
            self.menu.emit(self._game(row), self.viewport().mapToGlobal(pos))

    AUTO_COLS = (0, 2, 3, 4, 5, 6)

    def set_games(self, games, empty_text=None, pending_jobs=()):
        self._syncing = True
        self.setSortingEnabled(False)
        # Columns sized to their contents re-measure every row on each
        # setItem — a few hundred games took most of a second. Fixed while
        # filling, measured once at the end.
        hh = self.horizontalHeader()
        for i in self.AUTO_COLS:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self.setUpdatesEnabled(False)
        self.games = {g['slug']: g for g in games}
        self._rows: dict[str, QTableWidgetItem] = {}      # slug -> its icon cell
        jobs = list(pending_jobs)
        self.setRowCount(len(jobs) + len(games))
        for r, job in enumerate(jobs):             # installs in progress, on top
            self.setItem(r, 0, QTableWidgetItem())
            it = SortItem(f"Installing — {job.name}", '')
            it.setForeground(QColor(P['subtext']))
            self.setItem(r, 1, it)
            for c in range(2, len(self.COLS)):
                self.setItem(r, c, SortItem('', ''))
        for r, g in enumerate(games, start=len(jobs)):
            icon_item = QTableWidgetItem()
            path = g.get('cover') or (ART.cached(g['cover_url']) if g.get('cover_url') else None)
            if path:                       # decoded off the GUI thread, set when ready
                pm = ART.peek(path, 30, 45)
                if pm is not None:
                    icon_item.setIcon(QIcon(rounded_pixmap(pm, R['sm'] / 1.5)))
                else:                      # by slug: the row may have moved or gone
                    ART.request(path, 30, 45, lambda pm, slug=g['slug']: self._cover(slug, pm))
            self.setItem(r, 0, icon_item)
            self._rows[g['slug']] = icon_item
            cells = [
                (g.get('display_name', g['slug']), (g.get('display_name') or '').lower()),
                (SOURCE_LABEL.get(g.get('source', 'nexus'), ''), g.get('source', '')),
                (fmt_size(g.get('disk_bytes')) or '—', g.get('disk_bytes') or 0),
                (fmt_hours(g.get('playtime_minutes')) or '—', g.get('playtime_minutes') or 0),
                (fmt_relative(g.get('last_played')) or 'never', g.get('last_played') or ''),
                (g.get('proton') or '—', g.get('proton') or ''),
            ]
            for c, (text, key) in enumerate(cells, start=1):
                it = SortItem(text, key)
                if c in self.NUMERIC:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if c == 1:
                    it.setData(Qt.ItemDataRole.UserRole, g['slug'])
                    if g.get('favorite'):
                        it.setText(f"★ {text}")
                self.setItem(r, c, it)
        self.setSortingEnabled(True)
        for i in self.AUTO_COLS:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.setUpdatesEnabled(True)
        self._syncing = False

    def _cover(self, slug, pm):
        # By slug through the row's own item (it moves with sorting): a
        # covers-arriving burst used to scan every row for each one.
        it = self._rows.get(slug) if pm is not None else None
        if it is not None:
            try:
                it.setIcon(QIcon(rounded_pixmap(pm, R['sm'] / 1.5)))
            except RuntimeError:                   # the table was refilled since
                pass

    def mark_selected(self, slugs):
        self._syncing = True
        self.clearSelection()
        for r in range(self.rowCount()):
            it = self.item(r, 1)
            if it and it.data(Qt.ItemDataRole.UserRole) in slugs:
                self.selectRow(r)
        self._syncing = False

    def reveal(self, slug):
        for r in range(self.rowCount()):
            it = self.item(r, 1)
            if it and it.data(Qt.ItemDataRole.UserRole) == slug:
                self.scrollToItem(it)

    def set_jobs(self, jobs):
        pass


# --------------------------------------------------------------------------
# Details panel — the game page, docked beside the grid (no dialogs)
# --------------------------------------------------------------------------

def _hero_art(game: dict) -> str | None:
    return art_source(game, HERO_KINDS)[0]


class HeroBanner(QWidget):
    """Wide art strip at the top of the panel, clipped to the card radius."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(176)
        self._pm = None
        self._logo = None
        self._name = ''
        # The art and logo scaled to the current size: smooth-scaling a
        # full-size hero on every repaint made moving through games stutter.
        self._frames: dict[str, tuple] = {}

    def set_game(self, game: dict):
        self._name = game.get('display_name', '')
        logo, _url = art_source(game, LOGO_KINDS)
        self._logo = QPixmap(logo) if logo else None
        if self._logo is not None and self._logo.isNull():
            self._logo = None
        self._frames.pop('logo', None)
        path = _hero_art(game)
        if not path and game.get('hero_url'):
            ART.fetch(game['hero_url'], self._arrived)
        if not path and game.get('cover_url'):
            path = ART.cached(game['cover_url'])
        self._load(path)

    def _arrived(self, path):
        try:
            self._load(path)
        except RuntimeError:
            pass

    def _load(self, path):
        self._pm = QPixmap(path) if path and Path(path).exists() else None
        if self._pm is not None and self._pm.isNull():
            self._pm = None
        self._frames.pop('art', None)
        self.update()

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
        self.name.setVisible(self.hero._logo is None)      # the logo already names it
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


# --------------------------------------------------------------------------
# Storage page — where the space goes, and what can be reclaimed
# --------------------------------------------------------------------------

# Segment colours for the per-game storage bar (Rosé Pine accents, then tints).
_SEG_COLORS = [PAL['iris'], PAL['foam'], PAL['rose'], PAL['gold'], PAL['pine'],
               PAL['love'], '#a58fcf', '#7fb4bd', '#c99f9d', '#d1a566']


class StorageBar(QWidget):
    """One stacked bar: a segment per game, then other files, then free."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(22)
        self.segments: list[tuple[str, int, str]] = []   # (label, bytes, color)
        self.other = self.free = self.total = 0
        self.setMouseTracking(True)

    def set_data(self, segments, other, free, total):
        self.segments, self.other, self.free, self.total = segments, other, free, total
        self.update()

    def _rects(self):
        if not self.total:
            return []
        x, w = 0.0, float(self.width())
        out = []
        for name, size, color in [*self.segments,
                                  ("Other files", self.other, P['overlay']),
                                  ("Free", self.free, None)]:
            sw = w * size / self.total
            out.append((QRectF(x, 0, sw, self.height()), name, size, color))
            x += sw
        return out

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, R['sm'], R['sm'])
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        for rect, _name, _size, color in self._rects():
            if color:
                pt.fillRect(rect.adjusted(0, 0, -1, 0), QColor(color))
        pt.end()

    def mouseMoveEvent(self, ev):
        for rect, name, size, _c in self._rects():
            if rect.contains(QPointF(ev.position())):
                self.setToolTip(f"{name}: {fmt_size(size) or '0 GB'}")
                return
        self.setToolTip('')


class StoragePage(QWidget):
    """Where the space goes (one bar per drive, a sortable table) and what
    can be reclaimed: a cleanup checklist (to the Trash by default) and the
    quarantine of stray Steam prefixes (restorable)."""
    request = pyqtSignal(list, str, bool, str)
    select_game = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.games: list[dict] = []
        self.disks: list[dict] = []
        self.cands: list[dict] = []
        self.checks: dict[str, QCheckBox] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        host = QWidget()
        scroll.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        self.page_title = label("Storage", "PageTitle")   # hidden inside SystemPage
        self.content_lay = lay
        head.addWidget(self.page_title)
        head.addStretch()
        measure = QPushButton("Measure sizes")
        measure.setToolTip("Recompute every repack's size on disk (reads file sizes only)")
        measure.clicked.connect(lambda: self.request.emit(
            ['--list', '--sizes'], "Measuring game sizes", False, 'task'))
        head.addWidget(measure)
        lay.addLayout(head)

        self.disk_box = QVBoxLayout()
        self.disk_box.setSpacing(SP['2'])
        lay.addLayout(self.disk_box)

        # ---- cleanup ----
        cc = QFrame()
        cc.setObjectName("Card")
        cl = QVBoxLayout(cc)
        cl.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        cl.setSpacing(SP['2'])
        top = QHBoxLayout()
        t = label("Clean up", None)
        t.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        top.addWidget(t)
        top.addStretch()
        self.btn_rescan = QPushButton("Re-scan")
        self.btn_rescan.clicked.connect(lambda: self.load_orphans(fresh=True))
        top.addWidget(self.btn_rescan)
        cl.addLayout(top)
        cl.addWidget(label("Nothing is removed until you choose. Selected items go to the Trash, "
                           "so you can still get them back; stray Steam prefixes go to "
                           "quarantine first. Anything that may hold saves is never "
                           "pre-selected.", "Muted", wrap=True))
        self.clean_rows = QVBoxLayout()
        self.clean_rows.setSpacing(SP['1'])
        cl.addLayout(self.clean_rows)
        foot = QHBoxLayout()
        self.sel_label = label("", None)
        self.sel_label.setStyleSheet(f"color: {P['text']};")
        foot.addWidget(self.sel_label, 1)
        self.btn_forever = QPushButton("Delete forever…")
        self.btn_forever.setObjectName("Danger")
        self.btn_forever.clicked.connect(self._ask_forever)
        self.btn_trash = QPushButton("Move to Trash")
        self.btn_trash.setObjectName("Primary")
        self.btn_trash.clicked.connect(lambda: self._clean(False))
        foot.addWidget(self.btn_forever)
        foot.addWidget(self.btn_trash)
        cl.addLayout(foot)
        self.forever_bar = QFrame()
        self.forever_bar.setObjectName("DangerBar")
        fb = QHBoxLayout(self.forever_bar)
        fb.setContentsMargins(SP['3'], SP['2'], SP['2'], SP['2'])
        self.forever_text = label("", None, wrap=True)
        fb.addWidget(self.forever_text, 1)
        no = QPushButton("Cancel")
        no.clicked.connect(lambda: self.forever_bar.setVisible(False))
        yes = QPushButton("Delete forever")
        self._forever = None
        yes.setObjectName("DangerSolid")
        yes.clicked.connect(lambda: self._clean(True))
        fb.addWidget(no)
        fb.addWidget(yes)
        self.forever_bar.setVisible(False)
        cl.addWidget(self.forever_bar)
        self.trash_note = label("", "Muted", wrap=True)
        trow = QHBoxLayout()
        trow.addWidget(self.trash_note, 1)
        open_trash = QPushButton("Open Trash")
        open_trash.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("trash:///")))
        trow.addWidget(open_trash)
        cl.addLayout(trow)
        lay.addWidget(cc)

        # ---- quarantine ----
        self.q_card = QFrame()
        self.q_card.setObjectName("Card")
        ql = QVBoxLayout(self.q_card)
        ql.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        ql.setSpacing(SP['2'])
        self.q_title = label("", None)
        self.q_title.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        ql.addWidget(self.q_title)
        ql.addWidget(label("Stray prefixes you moved out of Steam's way. Steam no longer sees "
                           "them, but they still take space until you trash them. Restore "
                           "puts one back exactly where it was.", "Muted", wrap=True))
        self.q_rows = QVBoxLayout()
        ql.addLayout(self.q_rows)
        self.q_card.setVisible(False)
        lay.addWidget(self.q_card)

        # ---- per-game table ----
        lay.addWidget(label("Games by size", "ShelfTitle"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Game", "Source", "Size", "Played",
                                              "Last played", "Added"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(320)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 6):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(
            lambda r, _c: self.select_game.emit(self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.table)
        lay.addStretch()

    # ---- drives + table ----
    def set_library(self, games, disks):
        self.games = games
        self.disks = disks if isinstance(disks, list) else ([disks] if disks else [])
        clear_layout(self.disk_box)
        for disk in self.disks:
            self.disk_box.addWidget(self._disk_card(disk))
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(games))
        for r, g in enumerate(games):
            cells = [
                (g.get('display_name', g['slug']), (g.get('display_name') or '').lower()),
                (SOURCE_LABEL.get(g.get('source', 'nexus'), ''), g.get('source', '')),
                (fmt_size(g.get('disk_bytes')) or '—', g.get('disk_bytes') or 0),
                (fmt_hours(g.get('playtime_minutes')) or '—', g.get('playtime_minutes') or 0),
                (fmt_relative(g.get('last_played')) or 'never', g.get('last_played') or ''),
                (fmt_relative(g.get('added_at')) or '—', g.get('added_at') or ''),
            ]
            for c, (text, key) in enumerate(cells):
                it = SortItem(text, key)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, g['slug'])
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.SortOrder.DescendingOrder)

    def _disk_card(self, disk):
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        cl.setSpacing(SP['2'])
        top = QHBoxLayout()
        t = label(disk['path'], None)
        t.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        top.addWidget(t)
        top.addStretch()
        top.addWidget(label(f"{fmt_size(disk['free'])} free of {fmt_size(disk['total'])}",
                            "Muted"))
        cl.addLayout(top)
        on = sorted((g for g in self.games if g.get('disk_bytes')
                     and (g.get('dev') == disk.get('dev') or 'dev' not in disk)),
                    key=lambda g: g['disk_bytes'], reverse=True)
        games_total = sum(g['disk_bytes'] for g in on)
        segs = [(g.get('display_name', g['slug']), g['disk_bytes'],
                 _SEG_COLORS[i % len(_SEG_COLORS)]) for i, g in enumerate(on)]
        bar = StorageBar()
        bar.set_data(segs, max(0, disk['used'] - games_total), disk['free'], disk['total'])
        cl.addWidget(bar)
        host = QWidget()
        legend = FlowLayout(host, margin=0, spacing=SP['3'])
        for i, g in enumerate(on[:12]):
            legend.addWidget(self._legend(_SEG_COLORS[i % len(_SEG_COLORS)],
                                          f"{g.get('display_name', g['slug'])} · "
                                          f"{fmt_size(g['disk_bytes'])}"))
        if len(on) > 12:
            legend.addWidget(label(f"+{len(on) - 12} more", "Muted"))
        legend.addWidget(self._legend(P['overlay'], "Other files · "
                                      f"{fmt_size(max(0, disk['used'] - games_total))}"))
        cl.addWidget(host)
        return card

    def _legend(self, color, text):
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(SP['1_5'])
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background: {color}; border-radius: {pill(10)}px;")
        l.addWidget(dot)
        l.addWidget(label(text, "Muted"))
        return w

    # ---- cleanup ----
    def load_orphans(self, fresh: bool = False):
        """(Re)scan cleanup candidates and the quarantine in the background.
        A scan from the last two minutes is reused unless `fresh` (Rescan);
        any job clears it."""
        self.btn_rescan.setEnabled(False)
        clear_layout(self.clean_rows)
        self.clean_rows.addWidget(label("Scanning…", "Muted"))

        def done(data, err):
            try:
                self.btn_rescan.setEnabled(True)
                self._fill_cleanup(data or {}, err)
            except RuntimeError:
                pass
        if fresh:
            _CLI_MEMO.pop(('--cleanup-scan', '--json'), None)
        cli_json(['--cleanup-scan', '--json'], done, 180000, ttl=120)

    def _fill_cleanup(self, data, err):
        clear_layout(self.clean_rows)
        self.checks = {}
        self.forever_bar.setVisible(False)
        self.cands = data.get('candidates', [])
        cats = data.get('categories', {})
        if not data:
            self.clean_rows.addWidget(label(f"Couldn't scan: {err}", "Muted", wrap=True))
        elif not self.cands:
            self.clean_rows.addWidget(label("Nothing to clean up.", "Muted"))
        order = list(cats) or sorted({c['category'] for c in self.cands})
        for cat in order:
            items = [c for c in self.cands if c['category'] == cat]
            if not items:
                continue
            meta = cats.get(cat, {'title': cat, 'about': ''})
            head = QHBoxLayout()
            box = QCheckBox(f"{meta['title']}  ·  {fmt_bytes(sum(c['size'] for c in items))}")
            box.setStyleSheet(f"color: {P['text']}; font-weight: 500;")
            head.addWidget(box, 1)
            body = QWidget()
            body_lay = QVBoxLayout(body)
            body_lay.setContentsMargins(0, 0, 0, 0)
            body_lay.setSpacing(SP['1'])
            if len(items) > 3:
                more = QPushButton(f"Show all {len(items)} ▾")
                more.setObjectName("Ghost")
                more.clicked.connect(lambda _=False, b=body, m=more, n=len(items): (
                    b.setVisible(not b.isVisible()),
                    m.setText(f"Hide ▴" if b.isVisible() else f"Show all {n} ▾")))
                head.addWidget(more)
                body.setVisible(False)
            self.clean_rows.addSpacing(SP['2'])
            self.clean_rows.addLayout(head)
            if meta.get('about'):
                self.clean_rows.addWidget(label(meta['about'], "Muted", wrap=True))
            self.clean_rows.addWidget(body)
            children = []
            for c in items:
                row = QHBoxLayout()
                row.setContentsMargins(SP['5'], 0, 0, 0)
                cb = QCheckBox(c['title'])
                cb.setChecked(c['preselect'])
                cb.setToolTip(c['path'])
                cb.toggled.connect(self._update_selected)
                self.checks[c['id']] = cb
                children.append(cb)
                row.addWidget(cb, 1)
                why = label(c['reason'], "Muted")
                row.addWidget(why)
                if c.get('saves'):
                    w = label("may hold saves", None)
                    w.setStyleSheet(f"color: {P['warning']};")
                    row.addWidget(w)
                size = label(fmt_bytes(c['size']), "Muted")
                size.setMinimumWidth(70)
                size.setAlignment(Qt.AlignmentFlag.AlignRight)
                row.addWidget(size)
                body_lay.addLayout(row)
            box.setChecked(all(ch.isChecked() for ch in children))
            box.clicked.connect(lambda on, ch=children: [c.setChecked(on) for c in ch])
        tb = data.get('trash_bytes') or 0
        self.trash_note.setText(
            f"The Trash holds {fmt_size(tb) or 'under 0.1 GB'} — that space is only freed "
            f"once you empty it." if tb else "The Trash is empty.")
        self._fill_quarantine(data.get('quarantine', []))
        self._update_selected()

    def _selected(self):
        return [c for c in self.cands if self.checks.get(c['id']) and
                self.checks[c['id']].isChecked()]

    def _update_selected(self):
        sel = self._selected()
        total = sum(c['size'] for c in sel)
        self.sel_label.setText(f"{len(sel)} selected · {fmt_bytes(total)}"
                               if sel else "Nothing selected")
        self.btn_trash.setEnabled(bool(sel))
        self.btn_forever.setEnabled(bool(sel))
        # The question named a selection; a changed one needs asking again.
        self.forever_bar.setVisible(False)

    def _ask_forever(self):
        sel = self._forever = self._selected()
        saves = [c for c in sel if c.get('saves')]
        self.forever_text.setText(
            f"Permanently delete {len(sel)} item(s), {fmt_size(sum(c['size'] for c in sel))}? "
            f"This can't be undone."
            + (f" {len(saves)} of them may hold saves." if saves else ""))
        self.forever_bar.setVisible(True)

    def _clean(self, forever):
        # Delete forever deletes exactly what the confirmation listed.
        sel = getattr(self, '_forever', None) if forever else self._selected()
        self._forever = None
        if not sel:
            return
        self.forever_bar.setVisible(False)
        args = ['--cleanup', *[c['id'] for c in sel], '--yes']
        if forever:
            args.append('--forever')
        total = fmt_bytes(sum(c['size'] for c in sel))
        self.request.emit(args, f"{'Deleting' if forever else 'Trashing'} {len(sel)} "
                          f"item(s) · {total}", False, 'task')

    def _fill_quarantine(self, items):
        clear_layout(self.q_rows)
        self.q_card.setVisible(bool(items))
        self.q_title.setText(f"Quarantine · {len(items)} item(s) · "
                             f"{fmt_size(sum(q['size'] for q in items)) or '< 0.1 GB'}")
        for q in items:
            row = QHBoxLayout()
            texts = QVBoxLayout()
            texts.setSpacing(0)
            m = re.match(r'(\d+)', q['name'])
            appid = int(m.group(1)) if m else None
            size = fmt_size(q['size']) or '< 0.1 GB'
            if appid is None:
                title, sub = q['name'], ''
            elif appid >= 0x80000000:
                title, sub = "Non-Steam shortcut prefix", f"id {q['name']}"
            else:
                title = f"Steam app {appid}"
                sub = (f"<a href='https://store.steampowered.com/app/{appid}' "
                       f"style='color:{P['accent']}'>store page</a>")
            texts.addWidget(label(f"{title} · {size}"))
            hint = q.get('save_hint') or []
            if hint:
                sub = (sub + " · " if sub else "") + "saves: " + html.escape(", ".join(hint))
            if sub:
                sub_l = label(sub, "Muted")
                sub_l.setOpenExternalLinks(True)
                texts.addWidget(sub_l)
            row.addLayout(texts, 1)
            if q.get('has_savedata'):
                w = _pill("may hold saves", 'warning')
                row.addWidget(w)
            restore = QPushButton("Restore")
            restore.setToolTip(f"Back to {q['origin']}")
            restore.clicked.connect(lambda _=False, n=q['name']: self.request.emit(
                ['--quarantine-restore', n], f"Restoring {n}", True, 'task'))
            trash = QPushButton("Move to Trash")
            trash.clicked.connect(lambda _=False, p=q['path']: self.request.emit(
                ['--cleanup', f"quarantine:{p}", '--yes'], f"Trashing {Path(p).name}",
                False, 'task'))
            row.addWidget(restore)
            row.addWidget(trash)
            self.q_rows.addLayout(row)


class SortItem(QTableWidgetItem):
    """Table cell that shows `text` but sorts by `key`."""

    def __init__(self, text, key):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        try:
            return self.key < other.key
        except TypeError:
            return str(self.key) < str(other.key)


# --------------------------------------------------------------------------
# Proton page — installed builds, who uses them, GE-Proton updates
# --------------------------------------------------------------------------

class ProtonPage(QWidget):
    """Like ProtonPlus, inside the launcher: install/update GE-Proton (verified
    download into Steam's compatibilitytools.d), see which games use each
    build, move games between builds, and remove builds nothing needs."""
    request = pyqtSignal(list, str, bool, str)
    update_available = pyqtSignal(bool)
    toast = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.info: dict = {}
        self.releases: list[dict] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        host = QWidget()
        scroll.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        head.addWidget(label("Proton", "PageTitle"))
        head.addStretch()
        self.btn_check = QPushButton("Check for updates")
        self.btn_check.clicked.connect(lambda: self.refresh(force=True))
        head.addWidget(self.btn_check)
        lay.addLayout(head)

        # Update banner
        self.banner = QFrame()
        self.banner.setObjectName("Card")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(SP['4'], SP['3'], SP['3'], SP['3'])
        bl.addWidget(icon_label(G['download'], 18, P['accent']))
        self.banner_text = label("", None, wrap=True)
        self.banner_text.setStyleSheet(f"color: {P['text']};")
        bl.addWidget(self.banner_text, 1)
        self.btn_install_latest = QPushButton("Install")
        self.btn_install_latest.setObjectName("Primary")
        self.btn_install_latest.clicked.connect(lambda: self._install('latest'))
        bl.addWidget(self.btn_install_latest)
        self.banner.setVisible(False)
        lay.addWidget(self.banner)

        lay.addWidget(label("Installed", "ShelfTitle"))
        self.installed = QVBoxLayout()
        self.installed.setSpacing(SP['2'])
        lay.addLayout(self.installed)

        lay.addWidget(label("GE-Proton releases", "ShelfTitle"))
        self.rel_list = QVBoxLayout()
        self.rel_list.setSpacing(SP['1_5'])
        lay.addLayout(self.rel_list)
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("Console")
        self.notes.setReadOnly(True)
        self.notes.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.notes.setMaximumHeight(220)
        self.notes.setVisible(False)
        lay.addWidget(self.notes)
        lay.addStretch()

    def refresh(self, force=False):
        self.btn_check.setEnabled(False)
        args = ['--protons', '--json'] + (['--refresh'] if force else [])

        def done(data, err):
            try:
                self.btn_check.setEnabled(True)
            except RuntimeError:
                return
            if data is None:
                self.toast.emit(f"Couldn't read Proton builds: {err}", 'error')
                return
            self.info = data
            self._fill_installed()
            self._fill_banner()
        cli_json(args, done, 60000, ttl=0 if force else 120)

        def rel_done(data, err):
            try:
                self._fill_releases((data or {}).get('releases', []), err)
            except RuntimeError:
                pass
        cli_json(['--proton-releases', '--json'] + (['--refresh'] if force else []),
                 rel_done, 60000, ttl=0 if force else 600)

    def _fill_banner(self):
        ge = self.info.get('families', {}).get('ge', {})
        upd = bool(ge.get('update'))
        self.update_available.emit(upd)
        dismissed = ge.get('latest') in self.info.get('protongen_dismissed', [])
        self.banner.setVisible(upd)
        if upd:
            self.banner_text.setText(
                f"{ge['latest']} is available."
                + ("  (You dismissed it in protongen.)" if dismissed else "")
                + "  Installing adds it next to your current builds; nothing is replaced.")
        cy = self.info.get('families', {}).get('cachyos', {})
        self._cachy = cy

    def _fill_installed(self):
        clear_layout(self.installed)
        rows = self.info.get('installed', [])
        others = [r['name'] for r in rows]
        for r in rows:
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
            cl.setSpacing(SP['1'])
            top = QHBoxLayout()
            name = plain_label(r['name'], None)
            name.setStyleSheet(f"font-weight: 500; color: {P['text']};")
            top.addWidget(name)
            tag = {'user': "installed by you", 'system': "system package"}.get(r['source'], '')
            top.addWidget(label(tag, "Muted"))
            if r['name'] == self.info.get('default'):
                top.addWidget(_pill("default", 'accent'))
            top.addStretch()
            users = [u for u in r['used_by'] if u != 'Default for new games']
            if r['source'] == 'system' and r.get('family') == 'cachyos':
                cy = self.info.get('families', {}).get('cachyos', {})
                top.addWidget(label(f"pacman {cy.get('installed') or ''}", "Muted"))
                copy = QPushButton("Copy update command")
                copy.setToolTip("System package — update it with pacman")
                copy.clicked.connect(lambda: (QApplication.clipboard().setText(
                    "sudo pacman -Syu proton-cachyos"),
                    self.toast.emit("Copied: sudo pacman -Syu proton-cachyos", 'info')))
                top.addWidget(copy)
            if len(others) > 1 and users:
                mv = QPushButton("Move games to…")
                menu = QMenu(mv)
                for o in others:
                    if o != r['name']:
                        menu.addAction(o).triggered.connect(
                            lambda _=False, old=r['name'], new=o, n=len(users):
                            self._ask_migrate(old, new, n))
                mv.setMenu(menu)
                top.addWidget(mv)
            if r.get('removable'):
                rm = QPushButton("Remove")
                rm.setObjectName("Danger")
                rm.setToolTip("Moves it to the Trash" if not users else
                              "Still in use — move its games to another build first")
                rm.setEnabled(not users)
                rm.clicked.connect(lambda _=False, n=r['name']: self.request.emit(
                    ['--proton-remove', n], f"Removing {n}", False, 'task'))
                top.addWidget(rm)
            cl.addLayout(top)
            self._add_usage(cl, r['used_by'])
            self.installed.addWidget(card)
        for v in self.info.get('valve', []):
            card = QFrame()
            card.setObjectName("Card")
            cl = QHBoxLayout(card)
            cl.setContentsMargins(SP['4'], SP['2_5'], SP['4'], SP['2_5'])
            n = plain_label(v['name'])
            cl.addWidget(n)
            cl.addWidget(label("managed by Steam", "Muted"))
            cl.addStretch()
            self.installed.addWidget(card)

    def _ask_migrate(self, old, new, n):
        ask(self, f"Move {n} game{'s' if n != 1 else ''} to {new}?",
            f"Every game that uses {old} switches to {new}. You can move them back the "
            f"same way.", "Move games",
            lambda: self.request.emit(['--proton-migrate', old, new],
                                      f"Moving games from {old} to {new}", True, 'task'),
            danger=False)

    def _add_usage(self, lay, used_by):
        """"Default · used by 11 games (4 Nexus, 5 Steam, 2 Heroic)", with the
        names one click away instead of a three-line run-on paragraph."""
        default = 'Default for new games' in used_by
        games = [u for u in used_by if u != 'Default for new games']
        groups: dict[str, list[str]] = {}
        for u in games:
            src, _, name = u.partition(': ')
            groups.setdefault(src if name else '', []).append(name or u)
        bits = ["Default for new games"] if default else []
        if games:
            n = len(games)
            by_src = ", ".join(f"{len(v)} {k}" for k, v in groups.items() if k)
            bits.append(f"used by {n} game{'s' if n != 1 else ''}"
                        + (f" ({by_src})" if by_src else ""))
        text = " · ".join(bits) if bits else "Not used by any game"
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        summary = label(text[0].upper() + text[1:], "Muted", wrap=True)
        row.addWidget(summary, 1)
        if games:
            names = label("\n".join(f"{k}: {', '.join(v)}" if k else ', '.join(v)
                                    for k, v in groups.items()), "Muted", wrap=True)
            names.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['xs']}px;")
            names.setVisible(False)
            more = QPushButton("Show games")
            more.setObjectName("Ghost")
            more.clicked.connect(lambda _=False, w=names, b=more: (
                w.setVisible(not w.isVisible()),
                b.setText("Hide games" if w.isVisible() else "Show games")))
            row.addWidget(more, 0, Qt.AlignmentFlag.AlignTop)
            lay.addLayout(row)
            lay.addWidget(names)
        else:
            lay.addLayout(row)

    def _fill_releases(self, rels, err):
        clear_layout(self.rel_list)
        self.releases = rels
        if not rels:
            self.rel_list.addWidget(label(f"Couldn't load releases: {err}" if err else
                                          "No releases found.", "Muted", wrap=True))
            return
        for r in rels[:8]:
            row = QHBoxLayout()
            t = QPushButton(f"{r['tag']}   ·   {(r.get('published_at') or '')[:10]}   ·   "
                            f"{fmt_bytes(r.get('tar_size'))}")
            t.setObjectName("ToolRow")
            t.setStyleSheet("text-align: left;")
            t.setToolTip("Show release notes")
            t.clicked.connect(lambda _=False, r=r: self._show_notes(r))
            row.addWidget(t, 1)
            # Same-width trailing slot on every row so the rows line up.
            slot = QWidget()
            slot.setFixedWidth(84)
            sl = QHBoxLayout(slot)
            sl.setContentsMargins(0, 0, 0, 0)
            if r.get('installed'):
                sl.addWidget(_pill("Installed", 'success'), 0, Qt.AlignmentFlag.AlignCenter)
            else:
                b = QPushButton("Install")
                b.setEnabled(bool(r.get('tar_url') and r.get('sha_url')))
                b.clicked.connect(lambda _=False, tag=r['tag']: self._install(tag))
                sl.addWidget(b)
            row.addWidget(slot)
            self.rel_list.addLayout(row)

    def _show_notes(self, r):
        body = re.sub(r'<[^>]+>', '', r.get('body') or '').strip()
        self.notes.setPlainText(f"{r['name']}\n\n{body or 'No release notes.'}")
        self.notes.setVisible(True)

    def _install(self, tag):
        self.request.emit(['--proton-install', tag],
                          f"Installing {tag if tag != 'latest' else 'the latest GE-Proton'}",
                          False, 'install-proton')


# --------------------------------------------------------------------------
# News page (experimental)
# --------------------------------------------------------------------------

NEWS_STYLE = {'steam': ('accent', 'tide', 'steam'), 'proton': ('success', 'aurora', 'proton')}
NEWS_FEED_STYLE = ('info', 'dusk', 'news')


def news_style(item: dict) -> tuple[str, str, str]:
    """(text role, gradient, glyph) for a story's kind."""
    return NEWS_STYLE.get(item.get('kind'), NEWS_FEED_STYLE)


def news_group(item: dict) -> str:
    """The chip a story belongs to."""
    kind = item.get('kind')
    return 'Your games' if kind == 'steam' else 'Proton' if kind == 'proton' else \
        item.get('source') or 'Other'


def news_image(item: dict, games_by_appid: dict) -> tuple[str | None, str | None]:
    """(local path, url still to fetch) for a story's picture: the feed's own
    image, else — for news about a game you have — that game's banner art
    (already on disk, so no network)."""
    url = item.get('image')
    if url:
        return ART.cached(url), (None if ART.cached(url) else url)
    game = games_by_appid.get(item.get('appid')) if item.get('kind') == 'steam' else None
    if game:
        path, url, _k = art_lookup(game, ('wide', 'hero'))
        return path, url
    return None, None


def elide_lines(text: str, fm: QFontMetrics, width: int, lines: int) -> str:
    """Word-wrap `text` into at most `lines` lines of `width` px, the last
    one elided — so a fixed-size card never clips mid-line."""
    words, out, cur = (text or '').split(), [], ''
    i = 0
    while i < len(words) and len(out) < lines:
        trial = f"{cur} {words[i]}".strip()
        if fm.horizontalAdvance(trial) <= width or not cur:
            cur = trial
            i += 1
        else:
            out.append(cur)
            cur = ''
    if cur and len(out) < lines:
        out.append(cur)
    if i < len(words) and out:
        rest = ' '.join([out[-1]] + words[i:])
        out[-1] = fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return '\n'.join(out)


class NewsArt(QWidget):
    """A story's picture, painted: the image cover-cropped when it arrives,
    else a gradient in the story's colour with its glyph. Loading is lazy —
    the page calls load() only once the card is near the screen."""

    def __init__(self, item: dict, w: int, h: int, radius: float, images,
                 top_only: bool = False):
        super().__init__()
        self.item, self.radius, self._images = item, radius, images
        self.top_only = top_only               # a card's top: square bottom corners
        self.setFixedSize(w, h)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._pm: QPixmap | None = None
        self.requested = False

    def load(self):
        if self.requested:
            return
        self.requested = True
        path, url = self._images(self.item)
        if path:
            self._decode(path)
        elif url:
            ART.fetch(url, self._arrived)

    def _arrived(self, path):
        try:
            self._decode(path)
        except RuntimeError:                  # the card is gone
            pass

    def _decode(self, path):
        self._path = path
        w, h = self.width(), self.height()
        pm = ART.peek(path, w, h)
        if pm is not None:
            self._set(pm)
        else:
            ART.request(path, w, h, lambda pm, size=(w, h): self._set(pm, size))

    def _set(self, pm, size=None):
        if pm is not None and (size is None or size == (self.width(), self.height())):
            self._pm = pm
            self.update()

    def resize_to(self, w: int, h: int):
        """New card width: keep showing the old picture (scaled) until the
        one decoded at the new size arrives."""
        if (w, h) == (self.width(), self.height()):
            return
        self.setFixedSize(w, h)
        if getattr(self, '_path', None):
            self._decode(self._path)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        if self.radius:
            clip.addRoundedRect(r.adjusted(0, 0, 0, self.radius if self.top_only else 0),
                                self.radius, self.radius)
        else:
            clip.addRect(r)
        pt.setClipPath(clip)
        if self._pm is not None:
            if self._pm.size() == self.size():
                pt.drawPixmap(0, 0, self._pm)
            else:
                pt.drawPixmap(self.rect(), self._pm)
        else:
            _role, grad, glyph = news_style(self.item)
            a, b = (QColor(c) for c in GRAD[grad])
            a.setAlphaF(0.55)
            b.setAlphaF(0.30)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.fillRect(r, QColor(P['surface_2']))
            pt.fillRect(r, QBrush(g))
            f = QFont(ICON_FONT)
            f.setPixelSize(int(min(r.height() * 0.38, 64)))
            pt.setFont(f)
            c = QColor(P['on_art']); c.setAlphaF(0.75)
            pt.setPen(c)
            pt.drawText(r, Qt.AlignmentFlag.AlignCenter, G[glyph])
        pt.end()


class NewsTile(QFrame):
    """One story in the grid: picture on top, source · time, a 3-line title.
    The page sets its width so the columns fill the row (set_width)."""
    W = 296                                    # starting width; MIN_W..: see NewsPage
    opened = pyqtSignal(int)

    def __init__(self, index: int, item: dict, images, width: int = W):
        super().__init__()
        self.index, self.item = index, item
        self.W = width
        self.IMG_H = int((width - 2) * 9 / 16)
        self.setObjectName("NewsTile")
        self.setFixedWidth(self.W)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, SP['3'])
        lay.setSpacing(SP['1_5'])
        self.art = NewsArt(item, self.W - 2, self.IMG_H, R['card'] - 1, images, top_only=True)
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        role = news_style(item)[0]
        meta = QHBoxLayout()
        meta.setContentsMargins(SP['3'], SP['1'], SP['3'], 0)
        meta.setSpacing(SP['1_5'])
        src = ElideLabel(item.get('source') or '', None)
        src.setStyleSheet(f"color: {P[role]}; font-size: {FS['xs']}px; font-weight: 600;")
        meta.addWidget(src, 1)
        when = label(fmt_relative(item.get('date')) or '', "Muted")
        meta.addWidget(when)
        lay.addLayout(meta)
        self.title = QLabel()
        self.title.setObjectName("NewsTitle")       # size/weight from the QSS
        f = QFont(UI_FONT, -1, QFont.Weight.Medium)
        f.setPixelSize(FS['sm'])
        self._fm = QFontMetrics(f)
        self._set_title()
        self.title.setFixedHeight(self._fm.lineSpacing() * 3 + 2)
        self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.title.setContentsMargins(SP['3'], 0, SP['3'], 0)
        lay.addWidget(self.title)
        for w in (src, when, self.title):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip(item.get('title') or '')

    def _set_title(self):
        # Less the frame and a little slack, so the last glyph never clips.
        self.title.setText(elide_lines(self.item.get('title') or '', self._fm,
                                       self.W - 2 * SP['3'] - 8, 3))

    def set_width(self, w: int):
        if w == self.W:
            return
        self.W = w
        self.setFixedWidth(w)
        self.art.resize_to(w - 2, int((w - 2) * 9 / 16))
        self._set_title()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.opened.emit(self.index)
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.index)
        else:
            super().keyPressEvent(ev)


class NewsFeature(QFrame):
    """The lead story: a wide picture with the headline over a scrim."""
    H = 280
    opened = pyqtSignal(int)

    def __init__(self, images):
        super().__init__()
        self.setObjectName("NewsFeature")
        self._images = images
        self.index, self.item = -1, {}
        self._pm: QPixmap | None = None
        self._path = None
        self.setFixedHeight(self.H)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        col = QVBoxLayout()
        col.setSpacing(SP['1_5'])
        col.addStretch()
        self.kicker = label("", "HeroKicker")
        col.addWidget(self.kicker)
        self.headline = QLabel()
        self.headline.setObjectName("NewsHeadline")
        self.headline.setWordWrap(True)
        col.addWidget(self.headline)
        self.summary = QLabel()
        self.summary.setObjectName("NewsLede")
        self.summary.setWordWrap(True)
        col.addWidget(self.summary)
        lay.addLayout(col, 6)
        lay.addStretch(4)
        for w in (self.kicker, self.headline, self.summary):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_item(self, index: int, item: dict):
        self.index, self.item = index, item
        self._pm, self._path = None, None
        when = fmt_relative(item.get('date'))
        self.kicker.setText("  ·  ".join(x for x in ("FEATURED", item.get('source'), when) if x))
        self._relabel()
        path, url = self._images(item)
        if path:
            self._path = path
            self._decode()
        elif url:
            ART.fetch(url, self._arrived)
        self.update()

    def _arrived(self, path):
        try:
            self._path = path
            self._decode()
        except RuntimeError:
            pass

    def _size(self):
        # Decoded in 64 px steps, so a window resize doesn't decode every frame.
        return max(64, (self.width() + 63) // 64 * 64), self.H

    def _decode(self):
        if not self._path:
            return
        w, h = self._size()
        pm = ART.peek(self._path, w, h)
        if pm is not None:
            self._pm = pm
            self.update()
        else:
            want = self._path
            ART.request(self._path, w, h, lambda pm, p=want: self._decoded(p, pm))

    def _decoded(self, path, pm):
        if path == self._path and pm is not None:
            self._pm = pm
            self.update()

    def _relabel(self):
        w = max(200, int(self.width() * 0.58) - SP['5'])
        hf = QFont(UI_FONT, -1, QFont.Weight.DemiBold)      # as #NewsHeadline / #NewsLede
        hf.setPixelSize(26)
        self.headline.setText(elide_lines(self.item.get('title') or '', QFontMetrics(hf), w, 2))
        sf = QFont(UI_FONT)
        sf.setPixelSize(FS['sm'])
        self.summary.setText(elide_lines(self.item.get('summary') or '', QFontMetrics(sf), w, 3))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.item:
            self._relabel()
            if self._pm is not None and self._pm.width() != self._size()[0]:
                self._decode()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), R['card'], R['card'])
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        if self._pm is not None:
            x = (self.width() - self._pm.width()) // 2
            pt.drawPixmap(x, 0, self._pm)
        else:
            _role, grad, glyph = news_style(self.item)
            a, b = (QColor(c) for c in GRAD[grad])
            a.setAlphaF(0.6)
            b.setAlphaF(0.25)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.fillRect(r, QBrush(g))
            f = QFont(ICON_FONT)
            f.setPixelSize(140)
            pt.setFont(f)
            c = QColor(P['on_art']); c.setAlphaF(0.18)
            pt.setPen(c)
            pt.drawText(r.adjusted(r.width() * 0.55, 0, 0, 0), Qt.AlignmentFlag.AlignCenter,
                        G[glyph])
        # Scrim: dark on the left where the text sits, clear on the right.
        s = QLinearGradient(r.topLeft(), r.topRight())
        base = QColor(PAL['base'])
        for at, alpha in ((0.0, 0.92), (0.45, 0.72), (0.8, 0.10), (1.0, 0.0)):
            c = QColor(base); c.setAlphaF(alpha)
            s.setColorAt(at, c)
        pt.fillRect(r, QBrush(s))
        pt.end()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.index >= 0:
            self.opened.emit(self.index)
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self.index >= 0:
            self.opened.emit(self.index)
        else:
            super().keyPressEvent(ev)


class NewsReader(QFrame):
    """A story opened beside the grid: picture, headline, the summary, and
    the way out to the full article. Prev / Next walk the current filter."""
    W = 420
    closed = pyqtSignal()
    step = pyqtSignal(int)                    # -1 / +1
    show_game = pyqtSignal(dict)

    def __init__(self, images):
        super().__init__()
        self.setObjectName("NewsReader")
        self._images = images
        self.item: dict = {}
        self.setFixedWidth(self.W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['4'])
        lay.setSpacing(SP['2_5'])
        bar = QHBoxLayout()
        self.btn_prev = icon_button(G['chevron_up'], "Previous story", 30, "Ghost", round_=True)
        self.btn_prev.clicked.connect(lambda: self.step.emit(-1))
        self.btn_next = icon_button(G['chevron_down'], "Next story", 30, "Ghost", round_=True)
        self.btn_next.clicked.connect(lambda: self.step.emit(1))
        self.pos = label("", "Muted")
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.pos)
        bar.addStretch()
        close = icon_button(G['close'], "Close (Esc)", 30, "Ghost", round_=True)
        close.clicked.connect(self.closed)
        bar.addWidget(close)
        lay.addLayout(bar)
        self.art_host = QVBoxLayout()
        lay.addLayout(self.art_host)
        self.art = None
        self.meta = label("", None)
        lay.addWidget(self.meta)
        self.title = label("", "NewsReaderTitle", wrap=True)
        lay.addWidget(self.title)
        self.body = label("", None, wrap=True)
        self.body.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['sm']}px;")
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.body)
        lay.addStretch()
        acts = QHBoxLayout()
        acts.setSpacing(SP['2'])
        self.btn_open = QPushButton("Read the article")
        self.btn_open.setObjectName("Primary")
        set_glyph_icon(self.btn_open, G['external'], role='on_accent')
        self.btn_open.clicked.connect(self._open)
        self.btn_copy = icon_button(G['copy'], "Copy link", 34, None, round_=True)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_game = QPushButton("Show game")
        set_glyph_icon(self.btn_game, G['library'])
        self.btn_game.clicked.connect(lambda: self.show_game.emit(self.item))
        acts.addWidget(self.btn_open)
        acts.addWidget(self.btn_game)
        acts.addStretch()
        acts.addWidget(self.btn_copy)
        lay.addLayout(acts)

    def show_item(self, item: dict, pos: int, total: int, has_game: bool):
        self.item = item
        if self.art is not None:
            self.art.setParent(None)
            self.art.deleteLater()
        self.art = NewsArt(item, self.W - 2 * SP['4'], int((self.W - 2 * SP['4']) * 9 / 16),
                           R['control'], self._images)
        self.art_host.addWidget(self.art)
        self.art.load()
        role = news_style(item)[0]
        self.meta.setText(" · ".join(x for x in (item.get('source'),
                                                  fmt_relative(item.get('date'))) if x))
        self.meta.setStyleSheet(f"color: {P[role]}; font-size: {FS['xs']}px; font-weight: 600;")
        self.title.setText(soft_wrap(item.get('title') or ''))
        self.body.setText(item.get('summary') or "No summary — read it on the site.")
        self.pos.setText(f"{pos + 1} of {total}")
        self.btn_prev.setEnabled(pos > 0)
        self.btn_next.setEnabled(pos < total - 1)
        link = item.get('link')
        self.btn_open.setEnabled(bool(link))
        self.btn_copy.setEnabled(bool(link))
        self.btn_open.setToolTip(link or "This story has no link")
        self.btn_game.setVisible(has_game)

    def _open(self):
        if self.item.get('link'):
            QDesktopServices.openUrl(QUrl(self.item['link']))    # the browser renders it

    def _copy(self):
        if self.item.get('link'):
            QApplication.clipboard().setText(self.item['link'])


class NewsPage(QWidget):
    """Linux gaming news, Proton releases and Steam news for your installed
    games, as a magazine: a featured story, then a grid of picture cards,
    and a reader beside it. Built for a few hundred stories: cards are made
    once per load (36 at a time), filters only show and hide them, and a
    picture is fetched and decoded only when its card nears the screen."""
    toast = pyqtSignal(str, str)
    loaded = pyqtSignal()
    show_game = pyqtSignal(str)               # slug
    PAGE = 36
    MAX = 120
    MIN_W = 270                               # cards grow from here to fill their row
    GAP = SP['3']

    def __init__(self):
        super().__init__()
        self.data: dict = {}
        self.items: list[dict] = []
        self.games_by_appid: dict = {}
        self._src = 'all'
        self._view: list[int] = []            # item indexes in the current filter
        self._limit = self.PAGE
        self.tiles: dict[int, NewsTile] = {}
        self._reading = -1
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        main = QWidget()
        outer = QVBoxLayout(main)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        outer.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.addWidget(label("News", "PageTitle"))
        head.addWidget(_pill("experimental", 'warning'))
        head.addStretch()
        self.status = label("", "Muted")
        head.addWidget(self.status)
        self.btn_refresh = icon_button(G['refresh'], "Refresh (bypasses the 1-hour cache)", 32)
        self.btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        head.addWidget(self.btn_refresh)
        outer.addLayout(head)
        self.chips_host = QWidget()
        self.chips = QHBoxLayout(self.chips_host)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(SP['1_5'])
        outer.addWidget(self.chips_host)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.host = host
        col = QVBoxLayout(host)
        col.setContentsMargins(0, SP['1'], 0, SP['4'])
        col.setSpacing(SP['4'])
        self.feature = NewsFeature(self._image_for)
        self.feature.opened.connect(self.open_index)
        self.feature.setVisible(False)
        col.addWidget(self.feature)
        self.grid_host = QWidget()
        self.grid = FlowLayout(self.grid_host, spacing=self.GAP)
        col.addWidget(self.grid_host)
        self.empty_host = QVBoxLayout()
        col.addLayout(self.empty_host)
        self.btn_more = QPushButton("Show more")
        self.btn_more.clicked.connect(self._more)
        col.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignHCenter)
        self.btn_more.setVisible(False)
        col.addWidget(self._feeds_box())
        col.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)
        root.addWidget(main, 1)
        self.reader = NewsReader(self._image_for)
        self.reader.closed.connect(self.close_reader)
        self.reader.step.connect(self._step)
        self.reader.show_game.connect(self._show_game)
        self.reader.setVisible(False)
        root.addWidget(self.reader)
        # Pictures load for what's on (or near) the screen, a beat after it
        # stops moving.
        self._art_timer = QTimer(self)
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(60)
        self._art_timer.timeout.connect(self._load_visible)
        # A bound method, not a lambda: PyQt drops it when the page is
        # destroyed (a lambda here segfaulted on teardown, mid-resize).
        self.scroll.verticalScrollBar().valueChanged.connect(self._scrolled)
        self._loaded = False
        self.error = ''                          # the last load's failure, if any

    def _feeds_box(self):
        body = QWidget()
        feeds = QHBoxLayout(body)
        feeds.setContentsMargins(0, 0, 0, 0)
        self.feed_url = QLineEdit()
        self.feed_url.setPlaceholderText("Add a feed: https://example.com/rss")
        add = QPushButton("Add feed")
        add.clicked.connect(self._add_feed)
        self.feed_url.returnPressed.connect(self._add_feed)
        self.user_feeds = NoWheelCombo()
        self.user_feeds.setToolTip("Feeds you added")
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove_feed)
        self.btn_remove_feed = rm
        feeds.addWidget(self.feed_url, 1)
        feeds.addWidget(add)
        feeds.addWidget(self.user_feeds)
        feeds.addWidget(rm)
        self.feeds_row = feeds
        self.feeds_box = Disclosure('news-feeds', "Manage feeds", G['news'], body)
        return self.feeds_box

    # ---- data ----
    def set_games(self, games: list[dict]):
        """Library games, so news about one can show its own art."""
        self.games_by_appid = {g['appid']: g for g in games
                               if g.get('source') == 'steam' and g.get('appid')}

    def _image_for(self, item):
        return news_image(item, self.games_by_appid)

    def refresh(self, force=False):
        # One fetch at a time: the dashboard's News card and opening the page
        # both ask until the first answer lands.
        if (self._loaded and not force) or getattr(self, '_busy', False):
            return
        self._busy = True
        self.error = ''
        self.btn_refresh.setEnabled(False)
        self.status.setText("Loading…")

        def done(data, err):
            self._busy = False
            try:
                self.btn_refresh.setEnabled(True)
            except RuntimeError:
                return
            if data is None:
                self.error = err or 'unknown error'
                self.status.setText(f"Couldn't load news: {self.error}")
                self.loaded.emit()               # the Home card shows it, with Retry
                return
            self.error = ''
            self._loaded = True
            self.set_data(data)
            self.loaded.emit()
        cli_json(['--news', '--json'] + (['--refresh'] if force else []), done, 120000)

    def set_data(self, data: dict):
        self.data = data
        self.items = (data.get('items') or [])[:self.MAX]
        bad = [s['name'] for s in data.get('sources', []) if s.get('error')]
        self.status.setText(f"{len(data.get('items', []))} stories"
                            + (f" · unavailable: {', '.join(bad)}" if bad else ""))
        for t in self.tiles.values():
            t.setParent(None)
            t.deleteLater()
        self.tiles = {}
        while self.grid.count():
            self.grid.takeAt(0)
        if self._src != 'all' and self._src not in {news_group(i) for i in self.items}:
            self._src = 'all'
        self.close_reader()
        self._fill_chips()
        self._fill()
        self.user_feeds.clear()
        for f in data.get('feeds', []):
            if f.get('user'):
                self.user_feeds.addItem(f['name'], f['url'])
        self.user_feeds.setVisible(self.user_feeds.count() > 0)
        self.btn_remove_feed.setVisible(self.user_feeds.count() > 0)

    # ---- filter ----
    def _fill_chips(self):
        clear_layout(self.chips)
        counts: dict[str, int] = {}
        for i in self.items:
            k = news_group(i)
            counts[k] = counts.get(k, 0) + 1
        order = sorted(counts, key=lambda k: (k != 'Your games', k != 'Proton', -counts[k]))
        for key in ['all', *order]:
            text = (f"All ({len(self.items)})" if key == 'all' else f"{key} ({counts[key]})")
            c = QPushButton(text)
            c.setObjectName("Chip")
            c.setCheckable(True)
            c.setChecked(key == self._src)
            c.clicked.connect(lambda _=False, k=key: self._pick(k))
            self.chips.addWidget(c)
        self.chips.addStretch()

    def _pick(self, key):
        self._src = key
        for i in range(self.chips.count()):
            w = self.chips.itemAt(i).widget()
            if isinstance(w, QPushButton):
                w.setChecked(w.text().startswith("All (") if key == 'all'
                             else w.text().startswith(f"{key} ("))
        self._limit = self.PAGE
        self.close_reader()
        self._fill()
        self.scroll.verticalScrollBar().setValue(0)

    def _fill(self):
        """Show the current filter: reuse cards, make only missing ones,
        and lay them out in story order."""
        view = [n for n, i in enumerate(self.items)
                if self._src == 'all' or news_group(i) == self._src]
        self._view = view
        lead = next((n for n in view[:12] if self.items[n].get('image')
                     or (self.items[n].get('kind') == 'steam'
                         and self.items[n].get('appid') in self.games_by_appid)),
                    view[0] if view else None)
        if lead is not None:
            self.feature.set_item(lead, self.items[lead])
        self.feature.setVisible(lead is not None)
        rest = [n for n in view if n != lead]
        shown = rest[:self._limit]
        keep = set(shown)
        while self.grid.count():
            self.grid.takeAt(0)
        for n, t in self.tiles.items():
            if n not in keep:
                t.setVisible(False)
        for n in shown:
            t = self.tiles.get(n)
            if t is None:
                t = self.tiles[n] = NewsTile(n, self.items[n], self._image_for,
                                             self._tile_w())
                t.opened.connect(self.open_index)
            self.grid.addWidget(t)
            t.setVisible(True)
        self.grid.invalidate()
        self.btn_more.setVisible(len(rest) > len(shown))
        self.btn_more.setText(f"Show more ({len(rest) - len(shown)})")
        clear_layout(self.empty_host)
        if not view:
            self.empty_host.addWidget(EmptyState(G['news'], "No stories",
                                                 "Nothing in these feeds yet — try another "
                                                 "source or refresh."))
        self._art_timer.start()

    def _more(self):
        self._limit += self.PAGE
        self._fill()

    def _tile_w(self) -> int:
        """Card width that fills the grid's row exactly."""
        avail = self.scroll.viewport().width() - 2
        cols = max(1, (avail + self.GAP) // (self.MIN_W + self.GAP))
        return max(self.MIN_W if avail >= self.MIN_W else avail,
                   (avail - (cols - 1) * self.GAP) // cols)

    def _fit_tiles(self) -> bool:
        """Resize the cards to the grid; True if anything changed (positions
        are then stale until the layout runs)."""
        w = self._tile_w()
        changed = [t for t in self.tiles.values() if t.W != w]
        for t in changed:
            t.set_width(w)
        if changed:
            self.grid.invalidate()
        return bool(changed)

    # ---- lazy pictures ----
    def _scrolled(self, _value):
        self._art_timer.start()

    def _load_visible(self):
        if not self.isVisible():
            return
        if self._fit_tiles():
            self._art_timer.start()          # load once the new layout has run
            return
        vh = self.scroll.viewport().height()
        top = self.scroll.verticalScrollBar().value()
        lo, hi = top - vh // 2, top + 2 * vh          # what's on screen, and the next screen
        for t in self.tiles.values():
            if not t.isVisible() or t.art.requested:
                continue
            y = t.mapTo(self.host, QPoint(0, 0)).y()
            if y + t.height() >= lo and y <= hi:
                t.art.load()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._art_timer.start()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._art_timer.start()

    # ---- reader ----
    def open_index(self, n: int):
        if not (0 <= n < len(self.items)):
            return
        self._reading = n
        it = self.items[n]
        pos = self._view.index(n) if n in self._view else 0
        has_game = it.get('kind') == 'steam' and it.get('appid') in self.games_by_appid
        self.reader.show_item(it, pos, max(1, len(self._view)), has_game)
        self.reader.setVisible(True)
        for i, t in self.tiles.items():
            t.setProperty('reading', i == n)
            t.style().unpolish(t)
            t.style().polish(t)
        self._art_timer.start()

    def open_item(self, item: dict):
        """Open a story by content (from the Home widget)."""
        key = item.get('link') or item.get('title')
        n = next((n for n, i in enumerate(self.items)
                  if (i.get('link') or i.get('title')) == key), None)
        if n is not None:
            if self._src != 'all' and n not in self._view:
                self._pick('all')
            self.open_index(n)
            self.reader.btn_open.setFocus()

    def reader_open(self) -> bool:
        return self.reader.isVisible()

    def close_reader(self):
        if not self.reader.isVisible():
            return
        n, self._reading = self._reading, -1
        self.reader.setVisible(False)
        self._art_timer.start()              # the grid got wider
        for t in self.tiles.values():
            if t.property('reading'):
                t.setProperty('reading', False)
                t.style().unpolish(t)
                t.style().polish(t)
        t = self.tiles.get(n)
        if t is not None and t.isVisible():
            t.setFocus()
            self.scroll.ensureWidgetVisible(t)

    def _step(self, d: int):
        if self._reading not in self._view:
            return
        k = self._view.index(self._reading) + d
        if 0 <= k < len(self._view):
            n = self._view[k]
            if n not in self.tiles or not self.tiles[n].isVisible():
                self._more()                 # past the loaded batch: grow the grid
            self.open_index(n)
            t = self.tiles.get(n)
            if t is not None and t.isVisible():
                self.scroll.ensureWidgetVisible(t)

    def _show_game(self, item):
        game = self.games_by_appid.get(item.get('appid'))
        if game:
            self.show_game.emit(game['slug'])

    # ---- feeds ----
    def _add_feed(self):
        url = self.feed_url.text().strip()
        if not url.startswith('https://'):
            self.toast.emit("Enter a feed address starting with https://", 'warn')
            return

        def done(out, err):
            if out is None:
                self.toast.emit(f"Couldn't add the feed: {err}", 'error')
                return
            self.feed_url.clear()
            self.toast.emit("Feed added.", 'success')
            self.refresh(force=True)
        CliQuery(['--news-add-feed', url], done)

    def _remove_feed(self):
        url = self.user_feeds.currentData()
        if not url:
            return
        CliQuery(['--news-remove-feed', url], lambda out, err: self.refresh(force=True))


# --------------------------------------------------------------------------
# Deals page — Game-db: wishlist deals, upcoming releases, backlog picks
# --------------------------------------------------------------------------

def fmt_ago(ts) -> str:
    try:
        mins = int((time.time() - float(ts)) // 60)
    except (TypeError, ValueError):
        return ''
    if mins < 1:
        return 'just now'
    if mins < 60:
        return f'{mins} min ago'
    if mins < 48 * 60:
        return f'{mins // 60} h ago'
    return f'{mins // 1440} days ago'


def _pill(text: str, role: str) -> QLabel:
    lab = label(text, None)
    tint = {'success': P['success_18'], 'warning': P['warning_18'], 'danger': P['danger_18'],
            'accent': P['accent_22']}.get(role, P['surface_2'])
    lab.setStyleSheet(pill_qss(tint, P.get(role, P['subtext'])))
    lab.setFixedHeight(PILL_H)
    return lab


def deal_was(price: dict) -> str | None:
    """The price before the discount, in the same format as currentStr
    ("€10.03" at -44% -> "€17.91")."""
    cur, cut = price.get('current'), price.get('cutPct')
    text = price.get('currentStr') or ''
    if not cur or not cut or not 0 < cut < 100 or not re.search(r'\d', text):
        return None
    was = cur / (1 - cut / 100)
    sep = ',' if re.search(r'\d,\d{2}(?!\d)', text) else '.'
    return re.sub(r'\d[\d.,]*', f"{was:.2f}".replace('.', sep), text, count=1)


def deal_sort_key(sort: str):
    """A sort key for wishlist items. 'best' = on sale, at the all-time low,
    the biggest cut, then the best reviews."""
    def p(g):
        return g.get('price') or {}

    def rev(g):
        return (g.get('reviews') or {}).get('steamPercent') or 0
    if sort == 'discount':
        return lambda g: (-(p(g).get('cutPct') or 0), -rev(g))
    if sort == 'price':
        return lambda g: (p(g).get('current') is None, p(g).get('current') or 0)
    if sort == 'reviews':
        return lambda g: (-rev(g), -((g.get('reviews') or {}).get('metacritic') or 0))
    if sort == 'added':
        return lambda g: [-ord(c) for c in (g.get('dateAdded') or '')]
    if sort == 'name':
        return lambda g: (g.get('title') or '').casefold()
    return lambda g: (not p(g).get('onSale'), not p(g).get('atHistoricalLow'),
                      -(p(g).get('cutPct') or 0), -rev(g))


def deal_meta(item: dict) -> list[tuple[str, str | None]]:
    """(text, colour) bits for a deal's facts line: Proton tier, reviews,
    time to beat."""
    out = []
    tier = item.get('protonTier')
    if tier:
        out.append((tier.capitalize(), TIER_COLORS.get(tier, P['muted'])))
    rv = item.get('reviews') or {}
    if rv.get('steamPercent'):
        out.append((f"★ {rv['steamPercent']}%", None))
    elif rv.get('metacritic'):
        out.append((f"MC {rv['metacritic']}", None))
    hl = item.get('hltb') or {}
    if hl.get('main'):
        out.append((f"{hl['main']:g} h", None))
    return out


def meta_html(bits) -> str:
    dot = f" <span style='color:{P['overlay']}'>·</span> "
    return dot.join(f"<span style='color:{c}'>{html.escape(t)}</span>" if c else html.escape(t)
                    for t, c in bits)


def deal_links(item: dict, kind: str, base: str | None, installed: set):
    """(primary url, primary label, [(label, url)]) for a Game-db item.
    Only https links (or the configured local Game-db) are kept."""
    appid = item.get('appid')
    price = item.get('price') or {}
    best = price.get('best') or {}
    steam_page = gamedb.safe_url(item.get('steamUrl'))
    links: list[tuple[str, str]] = []
    if kind == 'tonight' and isinstance(appid, int):      # an id, never a path
        if appid in installed:
            primary, primary_label = f"steam://rungameid/{appid}", "Play"
        else:
            primary, primary_label = f"steam://install/{appid}", "Install in Steam"
        links.append((primary_label, primary))
    else:
        deal = gamedb.safe_url(best.get('url'))
        if deal:
            links.append((f"Open best deal ({best.get('shop') or 'keyshop'})", deal))
        gg = gamedb.safe_url(price.get('ggUrl'))
        if gg and gg != deal:
            links.append(("Compare on GG.deals", gg))
        primary = deal or steam_page
        primary_label = "Open deal" if deal else "Open on Steam"
    if steam_page:
        links.append(("Open on Steam", steam_page))
    if base and appid:
        gdb = gamedb.safe_url(gamedb.detail_url(base, appid), base)
        if gdb:
            links.append(("Open in Game-db", gdb))
    return primary, primary_label, links


def steam_hero_url(appid) -> str | None:
    return (f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{int(appid)}"
            "/library_hero.jpg") if str(appid or '').isdigit() else None


class DealArt(QWidget):
    """A deal's Steam header art with its badges painted on: the discount
    top-left, FitGirl / owned top-right, a countdown bottom-left."""

    def __init__(self, w: int, h: int, radius: int):
        super().__init__()
        self.setFixedSize(w, h)
        self.radius = radius
        self._pm: QPixmap | None = None
        self._path = None
        self.top_left: list[tuple[str, str]] = []      # (text, role)
        self.top_right: list[tuple[str, str]] = []
        self.bottom_left: list[tuple[str, str]] = []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def badges(self) -> list[str]:
        return [t for t, _r in self.top_left + self.top_right + self.bottom_left]

    def set_url(self, url: str | None):
        url = gamedb.safe_url(url)
        if not url:
            return
        path = ART.cached(url)
        if path:
            self._got(path)
        else:
            ART.fetch(url, self._got)

    def _got(self, path):
        try:
            self._path = path
            self._decode()
        except RuntimeError:                     # the tile is gone
            pass

    def _decode(self):
        if not self._path:
            return
        w, h = self.width(), self.height()
        pm = ART.peek(self._path, w, h)
        if pm is not None:
            self._pm = pm
            self.update()
            return
        want = (self._path, w, h)
        ART.request(self._path, w, h, lambda pm, k=want: self._decoded(k, pm))

    def _decoded(self, key, pm):
        if key == (self._path, self.width(), self.height()) and pm is not None:
            self._pm = pm
            self.update()

    def resize_to(self, w: int, h: int):
        if (w, h) != (self.width(), self.height()):
            self.setFixedSize(w, h)
            self._pm = None
            self._decode()

    def _badge(self, pt, x, y, text, role, right=False, up=False):
        f = QFont(UI_FONT, -1, QFont.Weight.DemiBold)
        f.setPixelSize(FS['xs'])
        pt.setFont(f)
        w = pt.fontMetrics().horizontalAdvance(text) + 14
        h = 20
        r = QRectF(x - w if right else x, y - h if up else y, w, h)
        if role == 'glass':
            bg = QColor(PAL['base']); bg.setAlphaF(0.78)
            fg = QColor(P['on_art'])
        else:
            bg, fg = QColor(P.get(role, P['accent'])), QColor(P['on_accent'])
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(bg)
        pt.drawRoundedRect(r, h / 2, h / 2)
        pt.setPen(fg)
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        return w

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()                      # rounded on top only
        clip.addRoundedRect(r.adjusted(0, 0, 0, self.radius), self.radius, self.radius)
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        if self._pm is not None:
            pt.drawPixmap(0, 0, self._pm)
        pad = SP['2']
        x = pad
        for text, role in self.top_left:
            x += self._badge(pt, x, pad, text, role) + SP['1']
        x = self.width() - pad
        for text, role in self.top_right:
            x -= self._badge(pt, x, pad, text, role, right=True) + SP['1']
        x = pad
        for text, role in self.bottom_left:
            x += self._badge(pt, x, self.height() - pad, text, role, up=True) + SP['1']
        pt.end()


class DealCard(QFrame):
    """One Game-db game as a store tile: art (with the discount and FitGirl
    badges), title, price and the old price, the all-time-low verdict and a
    facts line. Focusable like a GameTile — Return/Space/click does the
    primary action, the Menu key or right-click opens the link menu. Only
    https links (or the configured local Game-db) are ever opened."""
    W = 280
    activated = pyqtSignal(str)        # url (https:// or steam://)
    repack_action = pyqtSignal(str, dict, dict)   # 'grab'|'page'|'reject', item, match

    @staticmethod
    def art_h(w: int) -> int:
        return int((w - 2) * 215 / 460)           # Steam's header.jpg

    def __init__(self, item: dict, kind: str, base: str | None, installed: set,
                 repack: dict | None = None, owned: bool = False, width: int = W):
        super().__init__()
        self.setObjectName("DealTile")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.item, self.kind, self.base = item, kind, base
        self.repack = repack if kind in ('deal', 'upcoming') else None
        self.W = width
        self.setFixedWidth(width)
        self.primary, self.primary_label, self.links = deal_links(item, kind, base, installed)
        price = item.get('price') or {}
        best = price.get('best') or {}
        self.name = item.get('title') or f"App {item.get('appid')}"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, SP['3'])
        lay.setSpacing(SP['1_5'])
        self.art = DealArt(width - 2, self.art_h(width), R['card'] - 1)
        if price.get('cutPct') and kind != 'tonight':
            self.art.top_left.append((f"-{price['cutPct']}%", 'accent'))
        if self.repack:
            exact = self.repack.get('confidence') == 'exact'
            self.art.top_right.append(("FitGirl" if exact else "FitGirl?",
                                       'success' if exact else 'warning'))
        if owned and kind != 'tonight':
            self.art.top_right.append(("Owned", 'glass'))
        if kind == 'upcoming':
            when = item.get('countdown') or gamedb.release_label(item)
            if when and when != item.get('releaseDateText'):
                self.art.bottom_left.append((when, 'glass'))
        self.art.set_url(item.get('headerImage'))
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)

        body = QVBoxLayout()
        body.setContentsMargins(SP['3'], SP['0_5'], SP['3'], 0)
        body.setSpacing(SP['1'])
        self.title = QLabel()
        self.title.setObjectName("NewsTitle")
        tf = QFont(UI_FONT, -1, QFont.Weight.Medium)
        tf.setPixelSize(FS['sm'])
        self._fm = QFontMetrics(tf)
        self.title.setFixedHeight(self._fm.lineSpacing() * 2 + 2)
        self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._set_title()
        body.addWidget(self.title)

        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        if kind == 'tonight':
            act = label(self.primary_label, None)
            act.setStyleSheet(f"color: {P['accent']}; font-size: {FS['sm']}px; font-weight: 600;")
            row.addWidget(act)
        elif kind == 'upcoming':
            # The countdown is on the art; here, the date itself.
            when = label(item.get('releaseDateText') or gamedb.release_label(item) or "TBA",
                         None)
            when.setStyleSheet(f"color: {P['accent']}; font-size: {FS['base']}px;"
                               f" font-weight: 600;")
            row.addWidget(when)
        elif price:
            cur = label(price.get('currentStr') or '', None)
            cur.setStyleSheet(f"color: {P['text']}; font-size: {FS['base']}px;"
                              f" font-weight: 600;")
            row.addWidget(cur)
            was = deal_was(price)
            if was:
                old = label(f"<s>{html.escape(was)}</s>", "Muted")
                old.setTextFormat(Qt.TextFormat.RichText)
                row.addWidget(old, 0, Qt.AlignmentFlag.AlignBottom)
        else:
            row.addWidget(label("No price yet" if not item.get('comingSoon') else
                                gamedb.release_label(item), "Muted"))
        row.addStretch()
        self.grab = None
        if self.repack:
            self.grab = QPushButton("Grab")
            self.grab.setObjectName("Chip")
            self.grab.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # the card's menu has it too
            set_glyph_icon(self.grab, G['download'], 14, 'accent')
            self.grab.setToolTip(f"Download {self.repack.get('title')} with your torrent client")
            self.grab.clicked.connect(self._grab)
            row.addWidget(self.grab)
        body.addLayout(row)

        verdict = QHBoxLayout()
        verdict.setSpacing(SP['1_5'])
        notes = []
        if kind == 'tonight':
            reasons = item.get('reasons') or []
            if reasons:
                why = label(" · ".join(reasons), "Muted", wrap=True)
                why.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                verdict.addWidget(why, 1)
        elif kind == 'upcoming':
            if item.get('owned'):
                verdict.addWidget(_pill("Owned", 'accent'))
            elif price:
                text, preorder = gamedb.price_line(item)
                if text:
                    notes.append(text)
                if preorder:
                    verdict.addWidget(_pill("Pre-order", 'warning'))
        elif price:
            if price.get('preorder'):
                verdict.addWidget(_pill("Pre-order", 'warning'))
            elif price.get('atHistoricalLow'):
                verdict.addWidget(_pill("All-time low", 'success'))
            elif price.get('onSale'):
                verdict.addWidget(_pill("Near all-time low", 'success'))
            if price.get('historicalLowStr') and not (price.get('atHistoricalLow') or
                                                      price.get('preorder')):
                notes.append(f"low {price['historicalLowStr']}")
            if best.get('priceStr') and best.get('price') is not None and \
                    price.get('current') and best['price'] < price['current'] - 0.005:
                notes.append(f"{best['priceStr']} at "
                             f"{best.get('shop') or ('a keyshop' if best.get('category') == 'keyshop' else 'a shop')}")
        if item.get('inBundle'):
            verdict.addWidget(_pill("In a bundle", 'warning'))
        if notes:
            n = ElideLabel(" · ".join(notes), "Muted")
            n.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            verdict.addWidget(n, 1)
        else:
            verdict.addStretch()
        body.addLayout(verdict)

        bits = deal_meta(item)
        if kind == 'library':
            c = item.get('completion') or {}
            if c.get('state') == 'in-progress':
                bits.append((f"{int(c.get('fraction', 0) * 100)}% through", None))
        self.meta = label(meta_html(bits), "Muted")
        self.meta.setTextFormat(Qt.TextFormat.RichText)
        self.meta.setVisible(bool(bits))
        body.addWidget(self.meta)
        self.dl = label("", None)
        self.dl.setStyleSheet(f"color: {P['accent']}; font-size: {FS['xs']}px; font-weight: 600;")
        self.dl.setVisible(False)
        body.addWidget(self.dl)
        lay.addLayout(body)
        lay.addStretch()
        for w in self.findChildren(QLabel):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        tip = self.primary or ''
        self.setToolTip(f"{self.name}\n{self.primary_label}: {tip}" if tip else self.name)

    def _set_title(self):
        self.title.setText(elide_lines(self.name, self._fm, self.W - 2 * SP['3'] - 8, 2))

    def set_width(self, w: int):
        if w == self.W:
            return
        self.W = w
        self.setFixedWidth(w)
        self.art.resize_to(w - 2, self.art_h(w))
        self._set_title()

    def _grab(self):
        self.repack_action.emit('grab', self.item, self.repack)

    def repack_items(self) -> list[tuple]:
        """The FitGirl entries for this card's menu: (text, glyph, fn)."""
        if not self.repack:
            return []
        emit = self.repack_action.emit
        return [("Download repack", G['download'], self._grab),
                ("Open the repack page", G['external'],
                 lambda: emit('page', self.item, self.repack)),
                ("Not this repack", G['close'],
                 lambda: emit('reject', self.item, self.repack))]

    def set_download(self, row: dict | None):
        """The repack's torrent, while one is going: "Downloading 42%"."""
        text = ''
        if row and not row.get('gone') and not row.get('installed'):
            if row.get('done'):
                text = "Downloaded — installing" if row.get('auto_install') else "Downloaded"
            else:
                text = (f"{'Paused' if row.get('paused') else 'Downloading'} "
                        f"{row.get('percent', 0):.0f}%")
        self.dl.setText(text)
        self.dl.setVisible(bool(text))
        if self.grab is not None:
            self.grab.setEnabled(not text)

    def activate(self):
        if self.primary:
            self.activated.emit(self.primary)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        # On release, inside: the primary action can launch a game through
        # Steam, so a press that turns into a drag (or slides off) does nothing.
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                ev.position().toPoint()):
            self.activate()
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        self._menu(ev.globalPos())

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate()
        elif ev.key() == Qt.Key.Key_Menu:
            self._menu(self.mapToGlobal(QPoint(self.width() // 3, self.height() // 2)))
        else:
            super().keyPressEvent(ev)

    def _menu(self, pos):
        web = next((u for _t, u in self.links if u.startswith(('https://', 'http://'))), None)
        sheet = getattr(self.window(), 'sheet', None)
        if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
            # A controller gets the sheet, never a desktop menu.
            items = self.repack_items() + [
                (text, G['external'], lambda u=url: self.activated.emit(u))
                for text, url in self.links]
            if web:
                items.append(("Copy link", G['copy'],
                              lambda: QApplication.clipboard().setText(web)))
            if items:
                sheet.choose(self.name, items)
            return
        m = QMenu(self)
        repack = self.repack_items()
        for text, _glyph, fn in repack:
            m.addAction(text, fn)
        if repack:
            m.addSeparator()
        for text, url in self.links:
            m.addAction(text, lambda u=url: self.activated.emit(u))
        if web:
            m.addSeparator()
            m.addAction("Copy link", lambda: QApplication.clipboard().setText(web))
        if not m.isEmpty():
            m.exec(pos)
        m.deleteLater()


class DealSpotlight(QFrame):
    """The best deals right now, one at a time: the game's Steam hero art
    with the price over a scrim, Open deal / Grab / Steam buttons, and
    ‹ › to step through the top few."""
    H = 236
    activated = pyqtSignal(str)
    repack_action = pyqtSignal(str, dict, dict)

    def __init__(self):
        super().__init__()
        self.setObjectName("DealSpotlight")
        self.setFixedHeight(self.H)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.items: list[dict] = []
        self.repacks: dict = {}
        self.base = None
        self.index = 0
        self.primary = None
        self._pm: QPixmap | None = None
        self._path = self._want = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['4'], SP['4'])
        col = QVBoxLayout()
        col.setSpacing(SP['1_5'])
        col.addStretch()
        self.kicker = label("", "HeroKicker")
        col.addWidget(self.kicker)
        self.headline = QLabel()
        self.headline.setObjectName("NewsHeadline")
        col.addWidget(self.headline)
        prow = QHBoxLayout()
        prow.setSpacing(SP['2'])
        self.cut = _pill("", 'accent')
        prow.addWidget(self.cut)
        self.price = label("", None)
        self.price.setStyleSheet(f"color: {P['on_art']}; font-size: 22px; font-weight: 600;")
        prow.addWidget(self.price)
        self.was = label("", None)
        self.was.setTextFormat(Qt.TextFormat.RichText)
        self.was.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['sm']}px;")
        prow.addWidget(self.was, 0, Qt.AlignmentFlag.AlignBottom)
        prow.addStretch()
        col.addLayout(prow)
        self.meta = label("", "NewsLede")
        self.meta.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(self.meta)
        brow = QHBoxLayout()
        brow.setSpacing(SP['2'])
        self.btn_open = QPushButton("Open deal")
        self.btn_open.setObjectName("Primary")
        self.btn_open.clicked.connect(self.activate)
        self.btn_grab = QPushButton("Grab from FitGirl")
        self.btn_grab.setObjectName("Glass")
        set_glyph_icon(self.btn_grab, G['download'], 15, 'on_art', hover_role=None)
        self.btn_grab.clicked.connect(self._grab)
        self.btn_steam = QPushButton("Steam page")
        self.btn_steam.setObjectName("Glass")
        self.btn_steam.clicked.connect(self._steam)
        for b in (self.btn_open, self.btn_grab, self.btn_steam):
            b.setFixedHeight(34)
            brow.addWidget(b)
        brow.addStretch()
        col.addSpacing(SP['1'])
        col.addLayout(brow)
        lay.addLayout(col, 6)
        lay.addStretch(3)
        nav = QVBoxLayout()
        nav.addStretch()
        nrow = QHBoxLayout()
        nrow.setSpacing(SP['1'])
        self.btn_prev = icon_button(G['chevron_left'], "Previous deal", 30, "GlassIcon")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.counter = label("", "HeroKicker")
        self.btn_next = icon_button(G['chevron_right'], "Next deal", 30, "GlassIcon")
        self.btn_next.clicked.connect(lambda: self.step(1))
        nrow.addWidget(self.btn_prev)
        nrow.addWidget(self.counter)
        nrow.addWidget(self.btn_next)
        nav.addLayout(nrow)
        lay.addLayout(nav)
        for w in (self.kicker, self.headline, self.price, self.was, self.meta, self.cut,
                  self.counter):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_items(self, items: list[dict], repacks: dict, base):
        keep = self.items[self.index]['appid'] if self.items else None
        self.items, self.repacks, self.base = items, repacks, base
        self.index = next((i for i, g in enumerate(items) if g.get('appid') == keep), 0)
        if items:
            self._show()

    def step(self, d: int):
        if len(self.items) > 1:
            self.index = (self.index + d) % len(self.items)
            self._show()

    def current(self) -> dict:
        return self.items[self.index] if self.items else {}

    def _show(self):
        g = self.current()
        price = g.get('price') or {}
        self.primary, _lab, self.links = deal_links(g, 'deal', self.base, set())
        bits = ["BEST DEAL RIGHT NOW" if self.index == 0 else f"TOP DEAL #{self.index + 1}"]
        if price.get('atHistoricalLow'):
            bits.append("ALL-TIME LOW")
        elif price.get('historicalLowStr'):
            bits.append(f"LOWEST EVER {price['historicalLowStr']}")
        self.kicker.setText("  ·  ".join(bits))
        self.cut.setText(f"-{price['cutPct']}%" if price.get('cutPct') else '')
        self.cut.setVisible(bool(price.get('cutPct')))
        self.price.setText(price.get('currentStr') or '')
        was = deal_was(price)
        self.was.setText(f"<s>{html.escape(was)}</s>" if was else '')
        meta = deal_meta(g)
        rv = g.get('reviews') or {}
        if rv.get('steamDesc'):
            meta.insert(1 if meta and g.get('protonTier') else 0, (rv['steamDesc'], None))
        self.meta.setText(meta_html(meta))
        self.btn_open.setVisible(bool(self.primary))
        self.btn_grab.setVisible(str(g.get('appid')) in self.repacks)
        self.btn_steam.setVisible(bool(gamedb.safe_url(g.get('steamUrl'))))
        many = len(self.items) > 1
        for w in (self.btn_prev, self.btn_next, self.counter):
            w.setVisible(many)
        self.counter.setText(f"{self.index + 1} / {len(self.items)}")
        self._relabel()
        self._pm = self._path = None
        hero, header = steam_hero_url(g.get('appid')), gamedb.safe_url(g.get('headerImage'))
        self._want = g.get('appid')
        for url in (header, hero):              # the header now, the hero once it's here
            if not url:
                continue
            path = ART.cached(url)
            if path:
                self._arrived(path, g.get('appid'), url == hero)
            else:
                ART.fetch(url, lambda p, a=g.get('appid'), h=(url == hero):
                          self._arrived(p, a, h))
        self.update()

    def _arrived(self, path, appid, is_hero):
        try:
            if appid != self._want or (not is_hero and self._path and 'hero' in self._kind):
                return
        except RuntimeError:
            return
        self._path, self._kind = path, ('hero' if is_hero else 'header')
        self._decode()

    _kind = ''

    def _size(self):
        return max(64, (self.width() + 63) // 64 * 64), self.H

    def _decode(self):
        if not self._path:
            return
        w, h = self._size()
        pm = ART.peek(self._path, w, h)
        if pm is not None:
            self._pm = pm
            self.update()
        else:
            want = self._path
            ART.request(self._path, w, h, lambda pm, p=want: self._decoded(p, pm))

    def _decoded(self, path, pm):
        try:
            if path == self._path and pm is not None:
                self._pm = pm
                self.update()
        except RuntimeError:
            pass

    def _relabel(self):
        w = max(200, int(self.width() * 0.62) - SP['5'])
        hf = QFont(UI_FONT, -1, QFont.Weight.DemiBold)      # as #NewsHeadline
        hf.setPixelSize(26)
        self.headline.setText(elide_lines(self.current().get('title') or '',
                                          QFontMetrics(hf), w, 1))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.items:
            self._relabel()
            if self._pm is not None and self._pm.width() != self._size()[0]:
                self._decode()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), R['card'], R['card'])
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        if self._pm is not None:
            pt.drawPixmap((self.width() - self._pm.width()) // 2, 0, self._pm)
        else:
            a, b = (QColor(c) for c in GRAD['dawn'])
            a.setAlphaF(0.5)
            b.setAlphaF(0.2)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.fillRect(r, QBrush(g))
        s = QLinearGradient(r.topLeft(), r.topRight())
        base = QColor(PAL['base'])
        for at, alpha in ((0.0, 0.94), (0.45, 0.75), (0.8, 0.15), (1.0, 0.05)):
            c = QColor(base); c.setAlphaF(alpha)
            s.setColorAt(at, c)
        pt.fillRect(r, QBrush(s))
        pt.end()

    def activate(self):
        if self.primary:
            self.activated.emit(self.primary)

    def _grab(self):
        g = self.current()
        m = self.repacks.get(str(g.get('appid')))
        if m:
            self.repack_action.emit('grab', g, m)

    def _steam(self):
        u = gamedb.safe_url(self.current().get('steamUrl'))
        if u:
            self.activated.emit(u)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                ev.position().toPoint()):
            self.activate()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate()
        elif ev.key() == Qt.Key.Key_BracketRight:
            self.step(1)
        elif ev.key() == Qt.Key.Key_BracketLeft:
            self.step(-1)
        else:
            super().keyPressEvent(ev)


class DealsPage(QWidget):
    """Game-db in Nexus, as a storefront: the best deal in a spotlight,
    the wishlist's numbers, then a grid of store tiles — deals (sortable,
    searchable, FitGirl matches marked), upcoming releases by month, and
    tonight's backlog picks. Everything comes from one cached
    `nexus-cli --gamedb --json` call; nothing is fetched until the page
    (or the Library's background warm-up) asks."""
    toast = pyqtSignal(str, str)
    counts = pyqtSignal(int)
    open_settings = pyqtSignal()
    repack = pyqtSignal(str, dict, dict)      # a FitGirl action on a wishlist game
    SECTIONS = (('deals', "Deals"), ('upcoming', "Upcoming"), ('backlog', "Backlog"))
    DEAL_FILTERS = (('sale', "On sale"), ('atl', "All-time low"), ('fitgirl', "On FitGirl"),
                    ('all', "All wishlist"))
    SORTS = (('best', "Best deal"), ('discount', "Biggest discount"), ('price', "Lowest price"),
             ('reviews', "Best reviewed"), ('added', "Recently added"), ('name', "Name"))
    BUDGETS = (('lt1h', "Under an hour"), ('1to3', "1–3 hours"), ('3plus', "3+ hours"),
               ('long', "Long session"))
    SPOTLIGHT = 5                     # deals the spotlight steps through
    MIN_TILE, GAP = 250, SP['3']

    loaded = pyqtSignal()

    def __init__(self, prefs=None):
        super().__init__()
        self.prefs = prefs
        self.res: dict = {}
        self._downloads: dict = {}
        st = getattr(prefs, 'deals_state', None) or {}
        self._section = st.get('section') if st.get('section') in dict(self.SECTIONS) else 'deals'
        self._filter = st.get('filter') if st.get('filter') in dict(self.DEAL_FILTERS) else 'sale'
        self._sort = st.get('sort') if st.get('sort') in dict(self.SORTS) else 'best'
        self._budget = st.get('budget') if st.get('budget') in dict(self.BUDGETS) else '1to3'
        self._hide_owned = st.get('hide_owned') is True
        self._query = ''
        self._loaded = self._busy = False
        self._cards: list[DealCard] = []
        self._grids: list[QGridLayout] = []
        self._cols = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        outer.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        head.addWidget(label("Deals", "PageTitle"))
        head.addSpacing(SP['3'])
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your wishlist")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(320)
        self.search.textChanged.connect(self._searched)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._fill)
        head.addWidget(self.search, 1)
        head.addStretch()
        self.status = label("", "Muted")
        head.addWidget(self.status)
        self.btn_refresh = icon_button(G['refresh'], "Refresh from Game-db (bypasses the "
                                                     "45-minute cache)", 32)
        self.btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        head.addWidget(self.btn_refresh)
        self.btn_site = QPushButton("Open Game-db")
        self.btn_site.clicked.connect(self._open_site)
        head.addWidget(self.btn_site)
        outer.addLayout(head)
        self.banner = label("", "Banner", wrap=True)
        self.banner.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        self.banner.setVisible(False)
        outer.addWidget(self.banner)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        body = QVBoxLayout(host)
        body.setContentsMargins(0, 0, SP['2'], SP['4'])
        body.setSpacing(SP['3'])
        self.spot = DealSpotlight()
        self.spot.activated.connect(self._open)
        self.spot.repack_action.connect(self.repack)
        body.addWidget(self.spot)
        self.spot.setVisible(False)
        self.stats_host = QWidget()
        self.stats = QHBoxLayout(self.stats_host)
        self.stats.setContentsMargins(0, 0, 0, 0)
        self.stats.setSpacing(SP['2'])
        body.addWidget(self.stats_host)
        self.stats_host.setVisible(False)
        self.bar_host = QWidget()
        bar = QHBoxLayout(self.bar_host)
        bar.setContentsMargins(0, SP['1'], 0, 0)
        bar.setSpacing(SP['2'])
        self.seg = SegmentedControl('underline', 34)
        for key, text in self.SECTIONS:
            self.seg.add(key, text)
        self.seg.set_current(self._section, animate=False)
        self.seg.changed.connect(lambda k: self._pick('_section', k))
        bar.addWidget(self.seg)
        bar.addStretch()
        self.owned_chip = QPushButton("Hide owned")
        self.owned_chip.setObjectName("Chip")
        self.owned_chip.setCheckable(True)
        self.owned_chip.setChecked(self._hide_owned)
        self.owned_chip.setToolTip("Hide wishlist games you already have: in your Steam "
                                   "library, installed, or installed from a repack")
        self.owned_chip.toggled.connect(self._owned_toggled)
        bar.addWidget(self.owned_chip)
        self.sort = QComboBox()
        for key, text in self.SORTS:
            self.sort.addItem(f"Sort: {text}", key)
        self.sort.setCurrentIndex(max(0, self.sort.findData(self._sort)))
        self.sort.activated.connect(lambda _i: self._pick('_sort', self.sort.currentData()))
        bar.addWidget(self.sort)
        body.addWidget(self.bar_host)
        self.sub_host = QWidget()
        self.sub = QHBoxLayout(self.sub_host)
        self.sub.setContentsMargins(0, 0, 0, 0)
        self.sub.setSpacing(SP['1_5'])
        body.addWidget(self.sub_host)
        self.summary = label("", "Muted", wrap=True)
        body.addWidget(self.summary)
        self.list = QVBoxLayout()
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(SP['3'])
        body.addLayout(self.list)
        body.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)
        self._fill_tabs()

    # ---- data ----
    def refresh(self, force=False):
        if self._busy or (self._loaded and not force):
            return
        self._busy = True
        self.btn_refresh.setEnabled(False)
        if not self._loaded:
            self.status.setText("Loading…")

        def done(data, err):
            self._busy = False
            try:
                self.btn_refresh.setEnabled(True)
            except RuntimeError:
                return
            self.res = data if data is not None else {
                'configured': True, 'error': {'kind': 'net', 'message': err}}
            self._loaded = data is not None
            self._render()
            self.loaded.emit()
        cli_json(['--gamedb', '--json'] + (['--refresh'] if force else []), done, 90000)

    @property
    def error(self) -> str:
        """Why there's nothing to show ('' while loading or when there is)."""
        if self._busy or self._data():
            return ''
        e = self.res.get('error')
        return (e.get('message') or e.get('kind') or 'error') if isinstance(e, dict) else ''

    def retry(self):
        self.res = {}
        self.refresh(force=True)

    def _data(self) -> dict:
        return self.res.get('summary') or {}

    def _base(self):
        return self.res.get('url')

    def _owned(self) -> set:
        return {int(a) for a in (self.res.get('owned_appids') or [])
                + (self.res.get('installed_appids') or []) if str(a).isdigit()}

    def _open_site(self):
        base = self._base() or gamedb.settings()['url'] or gamedb.DEFAULT_URL
        self._open(base + ('/' if self.res.get('summary') else '/account'))

    def _open(self, url):
        if url.startswith('steam://'):
            CliQuery(['--open-url', url], lambda out, err: out is None and self.toast.emit(
                f"Couldn't open Steam: {err}", 'error'))
        elif gamedb.safe_url(url, self._base()):
            QDesktopServices.openUrl(QUrl(url))      # browser; nothing rendered here

    def _save_state(self):
        if self.prefs is None:
            return
        self.prefs.deals_state = {'section': self._section, 'filter': self._filter,
                                  'sort': self._sort, 'budget': self._budget,
                                  'hide_owned': self._hide_owned}
        self.prefs.save()

    # ---- rendering ----
    def _render(self):
        r = self.res
        err = r.get('error') or {}
        configured = r.get('configured', False)
        if not configured:
            self.status.setText("")
            self.banner.setVisible(False)
        elif err:
            msg = err.get('message', 'unknown error')
            when = f" — showing data from {fmt_ago(r['fetched_at'])}" if r.get('summary') else ''
            self.banner.setText(html.escape(msg) + when)
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)
        if r.get('summary'):
            d = self._data()
            self.status.setText(f"synced {fmt_ago(r.get('fetched_at'))} · region "
                                f"{d.get('region', '?')}")
            self.counts.emit(int((d.get('radar') or {}).get('onSaleCount') or 0))
        elif configured:
            self.status.setText("")
        self._fill_tabs()
        self._fill()

    def _chip(self, lay, text, checked, fn):
        c = QPushButton(text)
        c.setObjectName("Chip")
        c.setCheckable(True)
        c.setChecked(checked)
        c.clicked.connect(fn)
        lay.addWidget(c)
        return c

    def _filtered(self, key: str) -> list[dict]:
        """The wishlist under one deal filter (before search / hide owned)."""
        items = self._data().get('wishlist') or []
        if key == 'sale':
            return [g for g in items if (g.get('price') or {}).get('onSale')]
        if key == 'atl':
            return [g for g in items if (g.get('price') or {}).get('atHistoricalLow')]
        if key == 'fitgirl':
            return [g for g in items if str(g.get('appid')) in self.repacks()]
        return list(items)

    def _fill_tabs(self):
        d = self._data()
        has = bool(self.res.get('summary'))
        n = {'deals': (d.get('radar') or {}).get('onSaleCount'),
             'upcoming': len(gamedb.upcoming(d)) or None}
        for key, _text in self.SECTIONS:
            self.seg.set_badge(key, str(n[key]) if n.get(key) else '')
        self.seg.set_current(self._section, animate=False)
        clear_layout(self.sub)
        if self._section == 'deals':
            for key, text in self.DEAL_FILTERS:
                count = len(self._filtered(key)) if has else 0
                self._chip(self.sub, f"{text}  {count}" if count else text,
                           key == self._filter,
                           lambda _=False, k=key: self._pick('_filter', k))
        elif self._section == 'backlog':
            for key, text in self.BUDGETS:
                self._chip(self.sub, text, key == self._budget,
                           lambda _=False, k=key: self._pick('_budget', k))
        self.sub.addStretch()
        self.sub_host.setVisible(has and self._section in ('deals', 'backlog'))
        self.bar_host.setVisible(has)
        self.sort.setVisible(self._section == 'deals')
        self.owned_chip.setVisible(self._section in ('deals', 'upcoming'))
        self.search.setVisible(has)

    def _fill_stats(self):
        clear_layout(self.stats)
        d = self._data()
        tiles = []
        if self._section == 'deals':
            radar = d.get('radar') or {}
            cur = d.get('currency') or ''
            wl = d.get('wishlist') or []
            tiles = [(G['deals'], "on sale", str(radar.get('onSaleCount', 0)), 'accent'),
                     (G['trophy'], "at their all-time low",
                      str(len(self._filtered('atl'))), 'success'),
                     (G['star'], "you'd save on all of them",
                      f"{radar['totalSavings']:.2f} {cur}" if radar.get('totalSavings')
                      else None, 'warning'),
                     (G['download'], "on FitGirl", str(len(self.repacks())) if self.repacks()
                      else None, 'success'),
                     (G['library'], "games on your wishlist", str(len(wl)), 'subtext')]
        elif self._section == 'backlog':
            b = d.get('backlog') or {}
            tiles = [(G['trophy'], "beaten", b.get('beaten'), 'success'),
                     (G['play'], "in progress", b.get('inProgress'), 'accent'),
                     (G['clock'], "not started", b.get('notStarted'), 'warning'),
                     (G['timer'], "hours left", b.get('backlogHours'), 'subtext')]
        for glyph, cap, val, role in tiles:
            t = StatTile(glyph, cap, role)
            t.set(str(val) if val is not None else None)
            self.stats.addWidget(t, 1)
        self.stats_host.setVisible(bool(tiles))

    def section(self) -> str:
        return self._section

    def show_section(self, key: str):
        """Switch section without moving focus (for go('upcoming'))."""
        if key in dict(self.SECTIONS) and key != self._section:
            self._section = key
            self._fill_tabs()
            self._fill()

    def _pick(self, attr, key):
        if getattr(self, attr) == key and attr != '_section':
            return
        setattr(self, attr, key)
        self._save_state()
        self._fill_tabs()
        self._fill()
        if attr in ('_filter', '_budget'):
            for i in range(self.sub.count()):
                w = self.sub.itemAt(i).widget()
                if isinstance(w, QPushButton) and w.isChecked():
                    w.setFocus()

    def _owned_toggled(self, on):
        self._hide_owned = on
        self._save_state()
        self._fill()

    def _searched(self, text):
        self._query = text.strip().casefold()
        self._search_timer.start()

    def _empty_state(self):
        box = QFrame()
        box.setObjectName("Card")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(SP['5'], SP['5'], SP['5'], SP['5'])
        lay.setSpacing(SP['2'])
        t = label("Connect Game-db", None)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['sm']}px; font-weight: 500;")
        lay.addWidget(t)
        url = self._base() or gamedb.DEFAULT_URL
        lay.addWidget(label(
            "See deals on your Steam wishlist, upcoming releases and what to play tonight. "
            f"Create a desktop app token on {url}/account, then paste it into "
            "Settings → Game-db.", "Muted", wrap=True))
        row = QHBoxLayout()
        site = QPushButton("Create a token…")
        site.clicked.connect(lambda: self._open(url + '/account'))
        go = QPushButton("Open Settings")
        go.setObjectName("Primary")
        go.clicked.connect(self.open_settings.emit)
        row.addWidget(go)
        row.addWidget(site)
        row.addStretch()
        lay.addLayout(row)
        return box

    def _groups(self, d: dict) -> tuple[list[tuple[str, list]], str, str]:
        """([(heading or '', items)], tile kind, empty-state text) for the
        current section, filter, sort, search and owned setting."""
        owned = self._owned()
        q = self._query

        def keep(g):
            if q and q not in (g.get('title') or '').casefold():
                return False
            return not (self._hide_owned and (g.get('appid') in owned or g.get('owned')))
        if self._section == 'deals':
            items = [g for g in self._filtered(self._filter) if keep(g)]
            items.sort(key=deal_sort_key(self._sort))
            empty = ("Your wishlist is empty." if self._filter == 'all' else
                     "No wishlist game has a FitGirl repack yet." if self._filter == 'fitgirl'
                     else "Nothing on sale right now.")
            return [('', items)], 'deal', ("Nothing matches your search." if q else empty)
        if self._section == 'upcoming':
            groups: dict[str, list] = {}
            for g in gamedb.upcoming(d):
                if keep(g):
                    groups.setdefault(self._month(g), []).append(g)
            return (list(groups.items()), 'upcoming',
                    "Nothing matches your search." if q else
                    "No upcoming releases on your wishlist.")
        items = [g for g in (d.get('tonight') or {}).get(self._budget) or [] if keep(g)]
        return [('', items)], 'tonight', "No picks for this time budget."

    @staticmethod
    def _month(g: dict) -> str:
        """"October 2026" from the release date; "Later" without one."""
        try:
            when = datetime.fromisoformat(str(g.get('releaseDate') or '').replace('Z', '+00:00'))
        except ValueError:
            return "Later"
        now = datetime.now()
        if (when.year, when.month) == (now.year, now.month):
            return "This month"
        return when.strftime("%B %Y")

    def _fill(self):
        self._cards, self._grids = [], []
        self._cols = 0
        clear_layout(self.list)
        self.summary.setText("")
        r = self.res
        d = self._data()
        self._fill_stats() if d else self.stats_host.setVisible(False)
        self._fill_spotlight(d)
        if r and not r.get('configured'):
            self.list.addWidget(self._empty_state())
            return
        if not d:
            if r:
                self.list.addWidget(EmptyState(G['deals'], "Nothing to show yet"))
            return
        installed = set(r.get('installed_appids') or [])
        owned = self._owned()
        base = self._base()
        repacks = self.repacks()
        groups, kind, empty = self._groups(d)
        total = sum(len(items) for _h, items in groups)
        if self._section == 'deals':
            cur = d.get('currency') or ''
            radar = d.get('radar') or {}
            bits = [f"{total} game{'s' if total != 1 else ''}"]
            if self._filter == 'sale' and radar.get('totalNow'):
                bits.append(f"{radar['totalNow']:.2f} {cur} for all of them")
            self.summary.setText(" · ".join(bits))
        elif self._section == 'upcoming':
            self.summary.setText(f"{total} unreleased games, soonest first")
        else:
            self.summary.setText("Tonight's picks from your owned games — the same three "
                                 "all day, chosen by Game-db")
        shown = 0
        for heading, items in groups:
            if shown >= 200:
                break
            if heading:
                h = label(heading, "SectionTitle")
                h.setStyleSheet(f"color: {P['text']}; font-size: {FS['base']}px;"
                                f" font-weight: 600;")
                self.list.addWidget(h)
            host = QWidget()
            grid = QGridLayout(host)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(self.GAP)
            grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            grid._cards = []
            for g in items[:200 - shown]:
                card = DealCard(g, kind, base, installed, repacks.get(str(g.get('appid'))),
                                g.get('appid') in owned, self._tile_w())
                card.activated.connect(self._open)
                card.repack_action.connect(self.repack)
                card.set_download(self._downloads.get(str(g.get('appid'))))
                self._cards.append(card)
                grid._cards.append(card)
            shown += len(grid._cards)
            self._grids.append(grid)
            self.list.addWidget(host)
        self._place()
        if not total:
            self.list.addWidget(EmptyState(G['deals'], empty))

    def _fill_spotlight(self, d: dict):
        on = (self._section == 'deals' and bool(d) and not self._query)
        top = []
        if on:
            sale = [g for g in d.get('wishlist') or [] if (g.get('price') or {}).get('onSale')]
            if self._hide_owned:
                owned = self._owned()
                sale = [g for g in sale if g.get('appid') not in owned]
            top = sorted(sale, key=deal_sort_key('best'))[:self.SPOTLIGHT]
        self.spot.setVisible(bool(top))
        if top:
            self.spot.set_items(top, self.repacks(), self._base())

    # Columns fill the row: as many MIN_TILE-wide tiles as fit, then
    # stretched to share the width evenly.
    def _avail(self) -> int:
        vp = self.scroll.viewport().width() if hasattr(self, 'scroll') else self.width()
        return max(self.MIN_TILE, vp - SP['2'])

    def _columns(self) -> int:
        return max(1, (self._avail() + self.GAP) // (self.MIN_TILE + self.GAP))

    def _tile_w(self) -> int:
        cols = self._columns()
        return (self._avail() - self.GAP * (cols - 1)) // cols

    def _place(self):
        cols, w = self._columns(), self._tile_w()
        for card in self._cards:
            card.set_width(w)
        if cols == self._cols:
            return
        self._cols = cols
        for grid in self._grids:
            for card in grid._cards:
                grid.removeWidget(card)
            for i, card in enumerate(grid._cards):
                grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if getattr(self, '_cards', None):
            try:
                self._place()
            except RuntimeError:
                pass

    def repacks(self) -> dict:
        """{appid (str): FitGirl match} for the wishlist."""
        return self.res.get('repacks') or {}

    def forget_repack(self, appid: str):
        """A match was wrong: drop it here too (the CLI remembers)."""
        if self.repacks().pop(str(appid), None) is not None:
            self._fill_tabs()
            self._fill()

    def set_downloads(self, rows: list[dict]):
        """The torrents Nexus grabbed for wishlist games, onto their cards."""
        by_appid = {str(r['appid']): r for r in rows if r.get('appid') is not None}
        if by_appid == self._downloads:
            return
        self._downloads = by_appid
        for card in getattr(self, '_cards', []):
            try:
                card.set_download(by_appid.get(str(card.item.get('appid'))))
            except RuntimeError:
                pass


# --------------------------------------------------------------------------
# Downloads — the torrent client (Transmission / qBittorrent) from inside
# Nexus: every torrent, with pause / resume / remove
# --------------------------------------------------------------------------

def fmt_eta(seconds) -> str:
    if not seconds:
        return ''
    s = int(seconds)
    if s < 90:
        return f"{s} s left"
    if s < 5400:
        return f"{s // 60} min left"
    if s < 172800:
        return f"{s // 3600} h {s % 3600 // 60:02d} min left"
    return f"{s // 86400} days left"


def torrent_state(r: dict) -> str:
    """One line for a torrent: "42% · 8.1 MB/s · 12 min left"."""
    if r.get('gone'):
        return "No longer in the torrent client"
    if r.get('error'):
        return "Error: " + str(r['error'])
    if r.get('installed'):
        return "Installed"
    state = r.get('state') or ('paused' if r.get('paused') else '')
    if r.get('done'):
        bits = ["Seeding" if state == 'seeding' else "Downloaded"]
        if r.get('up'):
            bits.append(f"↑ {fmt_bytes(r['up'])}/s")
        if r.get('ratio') is not None:
            bits.append(f"ratio {r['ratio']:.2f}")
        return " · ".join(bits)
    if state == 'paused':
        return f"Paused at {r.get('percent', 0):.0f}%"
    if state in ('queued', 'checking'):
        return f"{state.capitalize()} · {r.get('percent', 0):.0f}%"
    bits = [f"{r.get('percent', 0):.0f}%"]
    if r.get('rate'):
        bits.append(f"↓ {fmt_bytes(r['rate'])}/s")
    if r.get('eta'):
        bits.append(fmt_eta(r['eta']))
    return " · ".join(bits)


class DownloadRow(QFrame):
    """One torrent: name, state, progress, a pause/resume button and ⋮."""
    action = pyqtSignal(str, dict)   # pause|resume|remove|delete|folder|install, row

    def __init__(self, r: dict):
        super().__init__()
        self.setObjectName("Card")
        self.r: dict = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['3'], SP['2_5'], SP['2'], SP['2_5'])
        lay.setSpacing(SP['3'])
        col = QVBoxLayout()
        col.setSpacing(SP['1'])
        top = QHBoxLayout()
        top.setSpacing(SP['1_5'])
        self.name = label("", None)
        self.name.setStyleSheet(f"color: {P['text']}; font-size: {FS['sm']}px; font-weight: 500;")
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.name, 1)
        self.tag = _pill("Nexus", 'accent')
        top.addWidget(self.tag)
        col.addLayout(top)
        self.state = label("", "Muted")
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.state)
        self.bar = QProgressBar()
        self.bar.setObjectName("CardProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setRange(0, 1000)
        col.addWidget(self.bar)
        lay.addLayout(col, 1)
        self.size = label("", "Muted")
        lay.addWidget(self.size)
        self.btn_toggle = icon_button(G['pause'], "Pause", 32)
        self.btn_toggle.clicked.connect(self._toggle)
        lay.addWidget(self.btn_toggle)
        self.btn_more = icon_button(G['more'], "More", 32)
        self.btn_more.clicked.connect(self._menu)
        lay.addWidget(self.btn_more)
        self.set(r)

    def set(self, r: dict):
        if r == self.r:
            return
        self.r = r
        self.name.setText(r.get('name') or r['hash'][:12])
        self.name.setToolTip(r.get('path') or r.get('name') or '')
        self.tag.setVisible(bool(r.get('tracked')))
        self.tag.setText("Nexus · installs when done" if r.get('auto_install')
                         and not r.get('installed') and not r.get('done') else "Nexus")
        self.state.setText(torrent_state(r))
        self.state.setStyleSheet(f"color: {P['danger']};" if r.get('error') else '')
        gone = bool(r.get('gone'))
        self.bar.setVisible(not gone and not r.get('done'))
        self.bar.setValue(int(10 * (r.get('percent') or 0)))
        self.size.setText(fmt_bytes(r['size']) if r.get('size') else '')
        paused = r.get('state') == 'paused' or (r.get('paused') and not r.get('state'))
        set_glyph_icon(self.btn_toggle, G['play'] if paused else G['pause'], 16)
        self.btn_toggle.setToolTip("Resume" if paused else "Pause")
        self.btn_toggle.setVisible(not gone)

    def _toggle(self):
        paused = self.r.get('state') == 'paused' or self.r.get('paused')
        self.action.emit('resume' if paused else 'pause', self.r)

    def items(self) -> list[tuple]:
        r, out = self.r, []
        path = r.get('path')
        if r.get('gone'):
            return [("Stop following it", G['close'], lambda: self.action.emit('forget', r))]
        if path and Path(path).exists():
            out.append(("Open the folder", G['folder'], lambda: self.action.emit('folder', r)))
            if r.get('done') and not r.get('installed') and Path(path).is_dir():
                out.append(("Install it now", G['download'],
                            lambda: self.action.emit('install', r)))
        out.append(("Remove from the client", G['close'], lambda: self.action.emit('remove', r)))
        out.append(("Remove and delete the files", G['trash'],
                    lambda: self.action.emit('delete', r)))
        return out

    def _menu(self):
        sheet = getattr(self.window(), 'sheet', None)
        if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
            sheet.choose(self.name.text(), self.items())
            return
        m = QMenu(self)
        for text, _glyph, fn in self.items():
            m.addAction(text, fn)
        m.exec(self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height())))
        m.deleteLater()


class DownloadsPage(QWidget):
    """Every torrent in your client, live: progress, speed, pause / resume /
    remove, and the repacks Nexus installs when they finish. The data comes
    from `nexus-cli --torrents --all --json`, polled by the main window."""
    toast = pyqtSignal(str, str)
    control = pyqtSignal(str, list, bool)     # pause|resume|remove, hashes, delete files
    add = pyqtSignal(str, bool)               # magnet/.torrent, install when done
    row_action = pyqtSignal(str, dict)        # folder|install|forget, row
    open_settings = pyqtSignal()
    use_client = pyqtSignal(dict)             # {'client', 'url'} found by --torrent-detect
    FILTERS = (('all', "All"), ('active', "Downloading"), ('done', "Finished"),
               ('nexus', "From Nexus"))

    def __init__(self):
        super().__init__()
        self.data: dict = {}
        self.rows: dict[str, DownloadRow] = {}
        self._filter = 'all'
        self._detected = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        outer.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.addWidget(label("Downloads", "PageTitle"))
        head.addStretch()
        self.status = label("", "Muted")
        head.addWidget(self.status)
        self.btn_pause_all = QPushButton("Pause all")
        self.btn_pause_all.clicked.connect(lambda: self._all('pause'))
        head.addWidget(self.btn_pause_all)
        self.btn_resume_all = QPushButton("Resume all")
        self.btn_resume_all.clicked.connect(lambda: self._all('resume'))
        head.addWidget(self.btn_resume_all)
        outer.addLayout(head)
        self.addbox = QWidget()
        arow = QHBoxLayout(self.addbox)
        arow.setContentsMargins(0, 0, 0, 0)
        arow.setSpacing(SP['1_5'])
        self.magnet = QLineEdit()
        self.magnet.setPlaceholderText("Paste a magnet link, or pick a .torrent file")
        self.magnet.returnPressed.connect(self._send)
        tfile = QPushButton("File…")
        tfile.clicked.connect(self._pick)
        tgo = QPushButton("Download")
        tgo.setObjectName("Primary")
        tgo.clicked.connect(self._send)
        self.auto_install = QCheckBox("Install when finished")
        self.auto_install.setChecked(True)
        arow.addWidget(self.magnet, 1)
        arow.addWidget(tfile)
        arow.addWidget(tgo)
        arow.addWidget(self.auto_install)
        outer.addWidget(self.addbox)
        self.chips_host = QWidget()
        self.chips = QHBoxLayout(self.chips_host)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(SP['1_5'])
        self._chip_btns = {}
        for key, text in self.FILTERS:
            c = QPushButton(text)
            c.setObjectName("Chip")
            c.setCheckable(True)
            c.setChecked(key == self._filter)
            c.clicked.connect(lambda _=False, k=key: self._pick_filter(k))
            self.chips.addWidget(c)
            self._chip_btns[key] = c
        self.chips.addStretch()
        outer.addWidget(self.chips_host)
        self.empty_host = QWidget()
        elay = QVBoxLayout(self.empty_host)
        elay.setContentsMargins(0, 0, 0, 0)
        self.empty = EmptyState(G['download'], "Loading…")
        self.empty.action.connect(self._empty_action)
        elay.addWidget(self.empty)
        self.empty2 = QPushButton("Open Settings")
        self.empty2.clicked.connect(self.open_settings.emit)
        elay.addWidget(self.empty2, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty2.setVisible(False)
        elay.addStretch()
        outer.addWidget(self.empty_host, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.list = QVBoxLayout(host)
        self.list.setContentsMargins(0, 0, 0, SP['4'])
        self.list.setSpacing(SP['2'])
        self.list.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)
        self.set_data({})

    # ---- input ----
    def _send(self):
        src = self.magnet.text().strip()
        if not src:
            return
        if not (src.lower().startswith('magnet:?') or src.lower().endswith('.torrent')):
            self.toast.emit("That isn't a magnet link or a .torrent file.", 'warn')
            return
        self.add.emit(src, self.auto_install.isChecked())
        self.magnet.clear()

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "Pick a .torrent file", str(Path.home()),
                                              "Torrent files (*.torrent)")
        if path:
            self.magnet.setText(path)
            self._send()

    def _all(self, action):
        hashes = [r['hash'] for r in self.data.get('torrents') or [] if not r.get('gone')
                  and (r.get('state') == 'paused') == (action == 'resume')]
        if hashes:
            self.control.emit(action, hashes, False)

    def _row_action(self, action, r):
        if action in ('pause', 'resume', 'remove'):
            self.control.emit(action, [r['hash']], False)
        elif action == 'delete':
            self.control.emit('remove', [r['hash']], True)
        else:
            self.row_action.emit(action, r)

    def _pick_filter(self, key):
        self._filter = key
        for k, c in self._chip_btns.items():
            c.setChecked(k == key)
        self._fill()

    def _empty_action(self):
        if self._detected:
            self.use_client.emit(self._detected)
        else:
            self.open_settings.emit()

    def set_detected(self, found: dict | None):
        """A client answering on its default port while none is set up."""
        self._detected = found
        self._render_state()

    # ---- data ----
    def set_data(self, data: dict | None, err: str | None = None):
        self.data = data if data is not None else {
            'error': {'kind': 'net', 'message': err or "no answer"}, 'configured': True,
            'torrents': []}
        self._render_state()
        self._fill()

    def configured(self) -> bool:
        return self.data.get('configured', True) is not False

    def _render_state(self):
        d, err = self.data, (self.data.get('error') or {})
        info = d.get('client') or {}
        rows = d.get('torrents') or []
        ok = bool(d) and not err
        self.addbox.setVisible(ok)
        self.chips_host.setVisible(ok and bool(rows))
        self.btn_pause_all.setVisible(ok and any(r.get('state') not in ('paused', None)
                                                 for r in rows if not r.get('gone')))
        self.btn_resume_all.setVisible(ok and any(r.get('state') == 'paused' for r in rows))
        if ok:
            self.status.setText(f"{info.get('client', '')} {info.get('version', '')}".strip()
                                + f" · ↓ {fmt_bytes(info.get('down') or 0)}/s"
                                + f" · ↑ {fmt_bytes(info.get('up') or 0)}/s")
        else:
            self.status.setText("")
        self.empty2.setVisible(False)
        if not d:
            self.empty.set("Loading…", "", G['download'])
            self.empty.button.setVisible(False)
        elif not self.configured():
            if self._detected:
                name = self._detected['client'].capitalize()
                self.empty.set(f"{name} is running", f"Nexus found {name}'s remote access at "
                               f"{self._detected['url']}. Use it to manage your downloads "
                               "here and to grab repacks from your wishlist.", G['download'])
                self.empty.button.setText(f"Use {name}")
                self.empty2.setVisible(True)
            else:
                self.empty.set("Connect your torrent client",
                               "Nexus talks to Transmission or qBittorrent on this machine. "
                               "Turn on its remote access, then pick it in Settings → "
                               "Torrent client.", G['download'])
                self.empty.button.setText("Open Settings")
            self.empty.button.setVisible(True)
        elif err:
            self.empty.set("Can't reach the torrent client",
                           f"{err.get('message', '')}\n\nIn Transmission: Edit → Preferences → "
                           "Remote → Allow remote access. In qBittorrent: Tools → Options → "
                           "Web UI.", G['download'], 'warning')
            self.empty.button.setText("Open Settings")
            self.empty.button.setVisible(True)
        else:
            self.empty.set("No torrents", "Paste a magnet link above, or grab a repack from "
                           "Deals → On FitGirl.", G['download'], 'muted')
            self.empty.button.setVisible(False)

    def _shown(self, r) -> bool:
        if r.get('installed') and r.get('gone'):
            return False
        f = self._filter
        if f == 'active':
            return not r.get('done') and not r.get('gone')
        if f == 'done':
            return bool(r.get('done'))
        if f == 'nexus':
            return bool(r.get('tracked'))
        return True

    def _fill(self):
        rows = [r for r in self.data.get('torrents') or [] if self._shown(r)]
        keep = {r['hash'] for r in rows}
        for h in [h for h in self.rows if h not in keep]:
            w = self.rows.pop(h)
            self.list.removeWidget(w)
            w.deleteLater()
        for i, r in enumerate(rows):
            w = self.rows.get(r['hash'])
            if w is None:
                w = DownloadRow(r)
                w.action.connect(self._row_action)
                self.rows[r['hash']] = w
                self.list.insertWidget(i, w)
            else:
                w.set(r)
                if self.list.indexOf(w) != i:
                    self.list.removeWidget(w)
                    self.list.insertWidget(i, w)
        has = bool(rows)
        self.scroll.setVisible(has)
        self.empty_host.setVisible(not has)
        if not has and self.data.get('torrents') and not self.data.get('error'):
            self.empty.set("Nothing here", "No torrent matches this filter.", G['download'])
            self.empty.button.setVisible(False)

    def active_count(self) -> int:
        return sum(1 for r in self.data.get('torrents') or []
                   if not r.get('done') and not r.get('gone') and r.get('state') != 'paused')


# --------------------------------------------------------------------------
# Artwork picker — choose a game's cover, banner, hero, logo and icon from
# SteamGridDB (a full page: banners need the width)
# --------------------------------------------------------------------------

ART_KIND_LABEL = {'cover': "Cover", 'wide': "Banner", 'hero': "Hero", 'logo': "Logo",
                  'icon': "Icon"}
ART_THUMB = {'cover': (120, 180), 'wide': (230, 108), 'hero': (320, 103),
             'logo': (220, 96), 'icon': (80, 80)}


class ArtThumb(QFrame):
    """One SteamGridDB image to pick. Focusable; Enter/A or a click picks it."""
    picked = pyqtSignal(dict)

    def __init__(self, item: dict, kind: str, current: bool):
        super().__init__()
        self.item, self.kind, self.current = item, kind, current
        w, h = ART_THUMB[kind]
        self.setFixedSize(w, h)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        tip = [f"{item.get('width')}×{item.get('height')}"]
        if item.get('style'):
            tip.append(item['style'])
        if item.get('author'):
            tip.append(f"by {item['author']}")
        self.setToolTip(" · ".join(tip))
        self._pm = None
        cached = ART.cached(item['thumb'])
        if cached:
            self._load(cached)
        else:
            ART.fetch(item['thumb'], self._arrived)

    def _arrived(self, path):
        try:
            self._load(path)
        except RuntimeError:
            pass

    def _load(self, path):
        pm = QPixmap(path)
        if pm.isNull():
            return
        w, h = self.width(), self.height()
        if self.kind in ('logo', 'icon'):
            self._pm = pm.scaled(w - 16, h - 16, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        else:
            self._pm = cover_pixmap(path, w, h)
        self.update()

    def set_current(self, on: bool):
        self.current = on
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.item)
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.picked.emit(self.item)
        else:
            super().keyPressEvent(ev)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(r, R['tile'], R['tile'])
        pt.setClipPath(clip)
        if self.kind in ('logo', 'icon'):
            # A checkerboard, so dark logos are as readable as light ones.
            pt.fillRect(r, QColor('#3a3650'))
            sq, alt = 10, QColor('#2a273d')
            for yy in range(0, self.height(), sq):
                for xx in range((yy // sq) % 2 * sq, self.width(), sq * 2):
                    pt.fillRect(xx, yy, sq, sq, alt)
        else:
            pt.fillRect(r, QColor(P['surface_2']))
        if self._pm is not None:
            x = (self.width() - self._pm.width()) / 2
            y = (self.height() - self._pm.height()) / 2
            pt.drawPixmap(QPointF(x, y), self._pm)
        pt.setClipping(False)
        pt.setBrush(Qt.BrushStyle.NoBrush)
        if self.current or self.underMouse():
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            a, b = (QColor(c) for c in GRAD['dusk'])
            if not self.current:
                a.setAlphaF(0.7); b.setAlphaF(0.7)
            g.setColorAt(0.0, a); g.setColorAt(1.0, b)
            pt.setPen(QPen(QBrush(g), 3.0 if self.current else 2.0))
            pt.drawRoundedRect(r.adjusted(1, 1, -1, -1), R['tile'] - 1, R['tile'] - 1)
        else:
            hair = QColor(P['on_art']); hair.setAlphaF(0.06)
            pt.setPen(QPen(hair, 1.0))
            pt.drawRoundedRect(r, R['tile'], R['tile'])
        if self.current:
            c = QPointF(self.width() - 16.0, 16.0)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(P['accent']))
            pt.drawEllipse(c, 10.0, 10.0)
            f = QFont(ICON_FONT)
            f.setPixelSize(11)
            pt.setFont(f)
            pt.setPen(QColor(P['on_accent']))
            pt.drawText(QRectF(c.x() - 10, c.y() - 10, 20, 20), Qt.AlignmentFlag.AlignCenter,
                        G['check'])
        pt.end()


class ArtPickerPage(QWidget):
    """Every SteamGridDB image for one game and one kind, as a grid to pick
    from. Picking runs `--set-art` as a job; Steam's own files are never
    touched (a repack's Steam shortcut gets the new art too)."""
    request = pyqtSignal(list, str, bool, str)
    back = pyqtSignal()
    open_settings = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.game: dict = {}
        self.kind = 'hero'
        self.page = 0
        self.items: list[dict] = []
        self.current_url = None
        self._query = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        outer.setSpacing(SP['3'])
        head = QHBoxLayout()
        back = QPushButton("Back")
        back.setObjectName("Ghost")
        set_glyph_icon(back, G['arrow_left'], 16)
        back.clicked.connect(self.back)
        head.addWidget(back)
        self.title = label("Artwork", "PageTitle")
        head.addWidget(self.title)
        head.addStretch()
        self.btn_reset = QPushButton("Reset to default")
        self.btn_reset.clicked.connect(self._reset)
        head.addWidget(self.btn_reset)
        self.btn_site = QPushButton("Open on SteamGridDB")
        self.btn_site.setObjectName("Ghost")
        self.btn_site.clicked.connect(self._open_site)
        head.addWidget(self.btn_site)
        self.btn_match = QPushButton("Wrong game?")
        self.btn_match.setObjectName("Ghost")
        self.btn_match.clicked.connect(self._toggle_match)
        head.addWidget(self.btn_match)
        outer.addLayout(head)
        # Choose which SteamGridDB game this one fetches art from.
        self.match_box = QFrame()
        self.match_box.setObjectName("Card")
        ml = QVBoxLayout(self.match_box)
        ml.setContentsMargins(SP['3'], SP['3'], SP['3'], SP['3'])
        ml.setSpacing(SP['2'])
        self.match_note = label("", "Muted", wrap=True)
        ml.addWidget(self.match_note)
        srow = QHBoxLayout()
        self.match_ed = QLineEdit()
        self.match_ed.setPlaceholderText("Search SteamGridDB for the right game")
        self.match_ed.returnPressed.connect(self._match_search)
        srow.addWidget(self.match_ed, 1)
        go = QPushButton("Search")
        go.clicked.connect(self._match_search)
        srow.addWidget(go)
        self.btn_auto = QPushButton("Use automatic match")
        self.btn_auto.setObjectName("Ghost")
        self.btn_auto.clicked.connect(self._match_reset)
        srow.addWidget(self.btn_auto)
        ml.addLayout(srow)
        self.match_rows = QVBoxLayout()
        self.match_rows.setSpacing(SP['1'])
        ml.addLayout(self.match_rows)
        self.match_box.setVisible(False)
        outer.addWidget(self.match_box)
        row = QHBoxLayout()
        self.kinds = SegmentedControl('pill', 34)
        for k in ('cover', 'wide', 'hero', 'logo', 'icon'):
            self.kinds.add(k, ART_KIND_LABEL[k])
        self.kinds.changed.connect(self._pick_kind)
        row.addWidget(self.kinds)
        row.addSpacing(SP['3'])
        self.status = label("", "Muted")
        row.addWidget(self.status)
        row.addStretch()
        outer.addLayout(row)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, SP['1'], 0, SP['4'])
        self.flow_host = QWidget()
        self.flow = FlowLayout(self.flow_host, margin=SP['1'], spacing=SP['3'], justify=True)
        hl.addWidget(self.flow_host)
        self.empty = EmptyState(G['image'], "")
        self.empty.action.connect(self.open_settings)
        self.empty.setVisible(False)
        hl.addWidget(self.empty)
        self.btn_more = QPushButton("Load more")
        self.btn_more.clicked.connect(lambda: self._load(self.page + 1))
        hl.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignHCenter)
        hl.addStretch()
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)

    def open(self, game: dict, kind: str = 'hero'):
        self.game = game
        self.title.setText(f"Artwork · {game.get('display_name', '')}")
        self.btn_site.setVisible(False)
        self.match_box.setVisible(False)
        self._match_q = 0
        self.kinds.set_current(kind, animate=False)
        self._pick_kind(kind)

    def reload(self):
        """Fetch the current kind again (after the SteamGridDB match changed)."""
        self._pick_kind(self.kind)

    def _toggle_match(self):
        show = not self.match_box.isVisible()
        self.match_box.setVisible(show)
        if not show:
            return
        self.match_ed.setText(self.game.get('display_name', ''))
        self.match_note.setText(
            "SteamGridDB matched this game "
            + ("by its Steam id." if self.game.get('source') == 'steam' else "by name.")
            + " Pick the right entry and the artwork is fetched from it instead. "
              "Art you chose for the old match is cleared.")
        self.match_ed.setFocus()
        self._match_search()

    def _match_search(self):
        term = self.match_ed.text().strip()
        if not term:
            return
        self._match_q += 1
        q = self._match_q
        clear_layout(self.match_rows)
        self.match_rows.addWidget(label("Searching…", "Muted"))

        def done(data, err, q=q):
            if q != self._match_q:
                return
            clear_layout(self.match_rows)
            items = (data or {}).get('items') or []
            if not items:
                msg = {'no-key': "Add a SteamGridDB API key in Settings.",
                       'auth': "SteamGridDB rejected the API key.",
                       'rate': "SteamGridDB is rate-limiting; try again in a minute."
                       }.get((data or {}).get('error'), "No games found.")
                self.match_rows.addWidget(label(msg, "Muted"))
                return
            for it in items[:8]:
                text = it['name'] + (f" ({it['year']})" if it.get('year') else "")
                b = QPushButton(text)
                b.setObjectName("Ghost")
                b.setStyleSheet("text-align: left;")
                b.clicked.connect(lambda _=False, it=it: self._match_pick(it))
                self.match_rows.addWidget(b)
        cli_json(['--sgdb-search', term, '--json'], done, 30000)

    def _match_pick(self, item):
        self.match_box.setVisible(False)
        self.request.emit(['--set-sgdb', self.game['slug'], '--id', str(item['id']),
                           '--sgdb-name', item['name']],
                          f"Using “{item['name']}” for {self.game.get('display_name', '')}'s art",
                          False, 'task')

    def _match_reset(self):
        self.match_box.setVisible(False)
        self.request.emit(['--set-sgdb', self.game['slug'], '--reset'],
                          f"Matching {self.game.get('display_name', '')}'s art automatically",
                          False, 'task')

    def _pick_kind(self, kind):
        self.kind = kind
        self.items = []
        self._clear()
        self._load(0)

    def _clear(self):
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()

    def _load(self, page):
        self.page = page
        self._query += 1
        q = self._query
        self.status.setText("Looking on SteamGridDB…")
        self.btn_more.setVisible(False)
        self.empty.setVisible(False)
        slug, kind = self.game.get('slug'), self.kind

        def done(data, err, q=q, kind=kind):
            if q != self._query:
                return                       # a newer request (other kind/page) won
            try:
                self._fill(data or {}, err)
            except RuntimeError:
                pass
        cli_json(['--art-variants', slug, '--kind', kind, '--page', str(page), '--json'],
                 done, 60000)

    def _fill(self, data, err):
        error = data.get('error') or (err and 'net')
        new = data.get('items') or []
        self.current_url = data.get('current')
        gid = data.get('game_id')
        self.btn_site.setVisible(bool(gid))
        self._gid = gid
        for it in new:
            t = ArtThumb(it, self.kind, it['url'] == self.current_url)
            t.picked.connect(self._picked)
            self.flow.addWidget(t)
        self.items += new
        n = len(self.items)
        label_ = ART_KIND_LABEL[self.kind].lower()
        if n:
            self.status.setText(f"{n} {label_} image{'s' if n != 1 else ''} · pick one to use it")
        else:
            self.status.setText("")
            title, hint, action = {
                'no-key': ("Add a SteamGridDB API key",
                           "Settings → SteamGridDB API key (free at steamgriddb.com).",
                           "Open Settings"),
                'auth': ("SteamGridDB rejected the API key", "Check it in Settings.",
                         "Open Settings"),
                'rate': ("SteamGridDB is rate-limiting", "Try again in a minute.", ''),
                'net': ("Couldn't reach SteamGridDB", "Check your connection.", ''),
            }.get(error, (f"No {label_} art for this game yet",
                          "Someone may upload one to SteamGridDB.", ''))
            self.empty.set(title, hint)
            self.empty.button.setText(action)
            self.empty.button.setVisible(bool(action))
            self.empty.setVisible(True)
        # SteamGridDB pages are 50 images.
        self.btn_more.setVisible(len(new) >= 50)
        first = next((t for t in self.flow_host.findChildren(ArtThumb) if not t.isHidden()), None)
        if first is not None and self.isVisible() and QApplication.focusWidget() is None:
            first.setFocus()

    def _picked(self, item):
        for t in self.flow_host.findChildren(ArtThumb):
            t.set_current(t.item is item)
        self.current_url = item['url']
        name = self.game.get('display_name', '')
        self.request.emit(['--set-art', self.game['slug'], '--kind', self.kind,
                           '--url', item['url']],
                          f"Setting the {ART_KIND_LABEL[self.kind].lower()} for {name}",
                          False, 'task')

    def _reset(self):
        for t in self.flow_host.findChildren(ArtThumb):
            t.set_current(False)
        self.current_url = None
        name = self.game.get('display_name', '')
        self.request.emit(['--set-art', self.game['slug'], '--kind', self.kind, '--reset'],
                          f"Resetting the {ART_KIND_LABEL[self.kind].lower()} for {name}",
                          False, 'task')

    def _open_site(self):
        if getattr(self, '_gid', None):
            QDesktopServices.openUrl(QUrl(f"https://www.steamgriddb.com/game/{self._gid}"))


# --------------------------------------------------------------------------
# Health page — doctor as a checklist with fix buttons
# --------------------------------------------------------------------------

class HealthPage(QWidget):
    request = pyqtSignal(list, str, bool, str)
    counts = pyqtSignal(int, int)          # (problems, failures) for the sidebar badge
    toast = pyqtSignal(str, str)

    STATUS = {'ok': (G['check'], 'success'), 'warn': (G['warn'], 'warning'),
              'fail': (G['fail'], 'danger'), 'info': (G['info'], 'muted')}

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        self.page_title = label("Health", "PageTitle")   # hidden inside SystemPage
        self.content_lay = lay
        head.addWidget(self.page_title)
        head.addStretch()
        self.btn_ok_toggle = QCheckBox("Show passing checks")
        self.btn_ok_toggle.toggled.connect(lambda _: self._render())
        head.addWidget(self.btn_ok_toggle)
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        head.addWidget(copy)
        self.btn_recheck = QPushButton("Re-check")
        self.btn_recheck.clicked.connect(self.refresh)
        head.addWidget(self.btn_recheck)
        lay.addLayout(head)
        self.summary = label("Checking…", "Muted")
        lay.addWidget(self.summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.body = QVBoxLayout(host)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(SP['2'])
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        self.checks: list[dict] = []
        self._checked_at = 0.0

    def refresh(self):
        self.btn_recheck.setEnabled(False)
        self.summary.setText("Checking…")

        def done(data, err):
            try:
                self.btn_recheck.setEnabled(True)
            except RuntimeError:
                return
            if data is None:
                self.summary.setText(f"Couldn't run the checks: {err}")
                return
            self.checks = data.get('checks', [])
            self._checked_at = time.time()
            self._render()
        cli_json(['--doctor', '--json'], done, 120000)

    def _render(self):
        clear_layout(self.body)
        problems = [c for c in self.checks if c['status'] in ('warn', 'fail')]
        fails = [c for c in problems if c['status'] == 'fail']
        self.counts.emit(len(problems), len(fails))
        self.summary.setText("" if not problems else
                             f"{len(problems)} thing(s) need attention"
                             + (f", {len(fails)} serious." if fails else "."))
        self.summary.setVisible(bool(problems))
        show_ok = self.btn_ok_toggle.isChecked()
        if not problems and not show_ok:
            groups = list(dict.fromkeys(c['group'] for c in self.checks))
            when = (datetime.fromtimestamp(self._checked_at).strftime('%H:%M')
                    if self._checked_at else '')
            ok = EmptyState(G['check'], f"All {len(self.checks)} checks passed",
                            ", ".join(groups) + (f" · checked at {when}" if when else ""),
                            "Show passing checks", 'success')
            ok.action.connect(lambda: self.btn_ok_toggle.setChecked(True))
            self.body.addWidget(ok)
        group = None
        card = None
        for c in self.checks:
            if c['status'] in ('ok', 'info') and not show_ok and not c['actions']:
                continue
            if c['group'] != group:
                group = c['group']
                self.body.addWidget(label(group, "StatLabel"))
                card = QFrame()
                card.setObjectName("Card")
                QVBoxLayout(card).setContentsMargins(SP['3'], SP['1'], SP['3'], SP['1'])
                self.body.addWidget(card)
            card.layout().addWidget(self._row(c))
        self.body.addStretch()

    def _row(self, c):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, SP['2'], 0, SP['2'])
        lay.setSpacing(SP['2_5'])
        glyph, role = self.STATUS[c['status']]
        ic = icon_label(glyph, 14, P[role])
        ic.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(SP['0_5'])
        texts.addWidget(plain_label(c['title'], wrap=True))
        if c['detail']:
            # Launch strings are long comma runs with no spaces; give the
            # word-wrapper somewhere to break them.
            detail = soft_wrap(c['detail'])
            d = label(detail, "Muted", wrap=True)
            d.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            texts.addWidget(d)
        if c['fix']:
            texts.addWidget(label(c['fix'], "Muted", wrap=True))
        lay.addLayout(texts, 1)
        for a in c['actions']:
            b = QPushButton(a['label'])
            b.clicked.connect(lambda _=False, a=a, b=b: (
                b.setEnabled(False),
                self.request.emit(a['args'], f"{a['label']} — {c['title'].split(':')[0]}",
                                  bool(a.get('steam')), 'task')))
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        return w

    def _copy(self):
        def done(out, err):
            if out is None:
                self.toast.emit(f"Couldn't build the report: {err}", 'error')
                return
            QApplication.clipboard().setText(out)
            self.toast.emit("Health report copied to the clipboard.", 'success')
        CliQuery(['--doctor'], done, 120000)


# --------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------

class WeekBars(QWidget):
    """Hours played per week, oldest left: plain painted bars with the
    week's hours above each and its date below."""

    def __init__(self):
        super().__init__()
        self.weeks: list[dict] = []
        self.setMinimumHeight(170)

    def set_weeks(self, weeks: list[dict]):
        self.weeks = weeks
        self.update()

    def paintEvent(self, ev):
        if not self.weeks:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(UI_FONT)
        f.setPixelSize(FS['2xs'])
        pt.setFont(f)
        n = len(self.weeks)
        top, bottom = 18.0, 20.0
        h = self.height() - top - bottom
        slot = self.width() / n
        most = max((w['minutes'] for w in self.weeks), default=0) or 1
        for i, w in enumerate(self.weeks):
            x = i * slot + slot * 0.18
            bw = slot * 0.64
            bh = max(2.0, h * w['minutes'] / most) if w['minutes'] else 2.0
            r = QRectF(x, top + h - bh, bw, bh)
            pt.setPen(Qt.PenStyle.NoPen)
            col = QColor(P['accent'] if w['minutes'] else P['overlay'])
            if w['minutes'] and i != n - 1:
                col.setAlphaF(0.55)                     # this week stands out
            pt.setBrush(col)
            pt.drawRoundedRect(r, 3.0, 3.0)
            pt.setPen(QColor(P['subtext']))
            if w['minutes']:
                pt.drawText(QRectF(i * slot, r.top() - 16, slot, 14),
                            Qt.AlignmentFlag.AlignCenter, f"{w['minutes'] / 60:.0f} h"
                            if w['minutes'] >= 60 else f"{w['minutes']:.0f} m")
            if i % 2 == (n - 1) % 2:
                try:
                    d = datetime.fromisoformat(w['start'])
                    pt.setPen(QColor(P['muted']))
                    pt.drawText(QRectF(i * slot - slot / 2, self.height() - bottom + 4, slot * 2, 14),
                                Qt.AlignmentFlag.AlignCenter, d.strftime('%d %b'))
                except ValueError:
                    pass
        pt.end()


class PlayHistoryPage(QWidget):
    """Your play sessions: the last twelve weeks and this year in review.
    Filled from `nexus-cli --history --json`."""
    reveal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        self.content_lay = lay
        self.page_title = label("Play history", "PageTitle")
        lay.addWidget(self.page_title)
        self.empty = EmptyState(G['history'], "No sessions yet",
                                "Every time you play — a repack, or a Steam or Heroic game "
                                "while Nexus is open — the session is noted here.")
        lay.addWidget(self.empty)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(SP['3'])
        self.year_title = label("", None)
        self.year_title.setStyleSheet(f"font-size: {FS['lg']}px; font-weight: 600;")
        bl.addWidget(self.year_title)
        self.tiles = QGridLayout()
        self.tiles.setHorizontalSpacing(SP['2'])
        self.tiles.setVerticalSpacing(SP['2'])
        self.t_hours = StatTile(G['timer'], "Played")
        self.t_sessions = StatTile(G['play'], "Sessions")
        self.t_games = StatTile(G['gamepad'], "Games")
        self.t_streak = StatTile(G['calendar'], "Best streak", 'info')
        self.t_long = StatTile(G['trophy'], "Longest session", 'success')
        self.t_day = StatTile(G['history'], "Busiest day", 'info')
        for i, t in enumerate((self.t_hours, self.t_sessions, self.t_games, self.t_streak,
                               self.t_long, self.t_day)):
            self.tiles.addWidget(t, i // 3, i % 3)
        bl.addLayout(self.tiles)
        bl.addWidget(label("Last 12 weeks", "StatLabel"))
        self.bars = WeekBars()
        bl.addWidget(self.bars)
        bl.addWidget(label("Most played this year", "StatLabel"))
        self.top = QVBoxLayout()
        self.top.setSpacing(SP['1'])
        bl.addLayout(self.top)
        bl.addStretch()
        lay.addWidget(self.body, 1)
        self.body.setVisible(False)
        self._asked = 0.0

    def refresh(self, force: bool = False):
        if not force and time.monotonic() - self._asked < 30:
            return
        self._asked = time.monotonic()

        def done(data, _err):
            try:
                self.fill(data or {})
            except RuntimeError:
                pass
        cli_json(['--history', '--json'], done, 30000)

    def fill(self, data: dict):
        y = data.get('year') or {}
        has = bool(y.get('sessions'))
        self.empty.setVisible(not has)
        self.body.setVisible(has)
        if not has:
            return
        self.year_title.setText(f"{y['year']} so far"
                                + (f" · since {data['since']}" if data.get('since') else ''))
        self.t_hours.set(fmt_hours(int(y['minutes'])) or '0 h')
        self.t_sessions.set(str(y['sessions']))
        self.t_games.set(str(y['games']), f"Games · {y['days']} days")
        self.t_streak.set(f"{y['best_streak']} day{'s' if y['best_streak'] != 1 else ''}")
        lg = y.get('longest') or {}
        self.t_long.set(fmt_hours(int(lg.get('minutes') or 0)) or '—',
                        f"Longest · {lg.get('name', '')}" if lg else "Longest session")
        self.t_day.set(y.get('busiest_day') or '—')
        self.bars.set_weeks(data.get('weeks') or [])
        clear_layout(self.top)
        most = max((t['minutes'] for t in y.get('top') or []), default=1) or 1
        for t in y.get('top') or []:
            row = QHBoxLayout()
            b = QPushButton(t['name'])
            b.setObjectName("Ghost")
            b.setStyleSheet("text-align: left;")
            b.clicked.connect(lambda _=False, sl=t['slug']: self.reveal.emit(sl))
            row.addWidget(b, 1)
            row.addWidget(label(fmt_hours(int(t['minutes'])) or '', "Muted"))
            self.top.addLayout(row)
            bar = QProgressBar()
            bar.setObjectName("CardProgress")
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setRange(0, 1000)
            bar.setValue(int(1000 * t['minutes'] / most))
            self.top.addWidget(bar)


class SystemPage(QWidget):
    """Activity, Storage and Health under one top-bar icon: a switcher over
    the three existing pages (their own titles hidden; their toolbars stay)."""
    section_changed = pyqtSignal(str)
    SECTIONS = (('activity', "Activity", 'activity'), ('play', "Play history", 'history'),
                ('storage', "Storage", 'storage'), ('health', "Health", 'health'))

    def __init__(self, pages: dict):
        super().__init__()
        self.pages = pages
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        head.setSpacing(SP['3'])
        head.addWidget(label("System", "PageTitle"))
        self.seg = SegmentedControl('pill', 34)
        for key, text, glyph in self.SECTIONS:
            self.seg.add(key, text, G[glyph])
        self.seg.changed.connect(self.show_section)
        head.addWidget(self.seg)
        head.addStretch()
        lay.addLayout(head)
        self.stack = QStackedWidget()
        for key, _t, _g in self.SECTIONS:
            p = pages[key]
            p.page_title.hide()
            m = p.content_lay.contentsMargins()
            p.content_lay.setContentsMargins(m.left(), SP['2'], m.right(), m.bottom())
            self.stack.addWidget(p)
        lay.addWidget(self.stack, 1)
        self.seg.set_current('activity', animate=False)

    def section(self) -> str:
        return self.seg.current()

    def show_section(self, key: str):
        if key not in self.pages:
            return
        if self.seg.current() != key:
            self.seg.set_current(key, animate=self.isVisible())
        self.stack.setCurrentWidget(self.pages[key])
        self.section_changed.emit(key)

    def set_badge(self, key: str, text: str, role: str = 'accent'):
        self.seg.set_badge(key, text, role)


class Swatch(QAbstractButton):
    """A colour (or theme preview, or gradient) to pick, painted; checkable
    and focusable so the controller can choose too."""

    def __init__(self, kind: str, value, tip: str, size=(30, 30)):
        super().__init__()
        self.kind, self.value = kind, value
        self.setCheckable(True)
        self.setToolTip(tip)
        self.setFixedSize(*size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(f"border-radius: {pill(size[1]) if kind != 'theme' else R['control']}px;")

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        pt.setPen(Qt.PenStyle.NoPen)
        if self.kind == 'theme':
            t = THEMES[self.value][1]
            rad = float(R_BASE['control'])
            pt.setBrush(QColor(t['base']))
            pt.drawRoundedRect(r, rad, rad)
            card = QRectF(r.left() + 8, r.top() + 8, r.width() * 0.55, r.height() - 16)
            pt.setBrush(QColor(_mix(t['base'], t['overlay'], 0.6)))
            pt.drawRoundedRect(card, 4, 4)
            pt.setBrush(QColor(t['text']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.top() + 7, card.width() - 18, 4), 2, 2)
            pt.setBrush(QColor(t['muted']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.top() + 15, card.width() - 28, 3), 1.5, 1.5)
            pt.setBrush(QColor(P['accent']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.bottom() - 11, 26, 6), 3, 3)
            pt.setBrush(QColor(t['overlay_hi']))
            pt.drawEllipse(QPointF(r.right() - 16, r.center().y()), 7, 7)
            ring = r
        else:
            a, b = self.value if isinstance(self.value, tuple) else (self.value, self.value)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, QColor(a))
            g.setColorAt(1.0, QColor(b))
            pt.setBrush(QBrush(g))
            pt.drawEllipse(r.adjusted(3, 3, -3, -3))
            ring = r
        if self.isChecked():
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.setPen(QPen(QColor(P['text']), 2.0))
            if self.kind == 'theme':
                pt.drawRoundedRect(ring, float(R_BASE['control']), float(R_BASE['control']))
            else:
                pt.drawEllipse(ring)
        pt.end()


class AppearanceCard(QFrame):
    """Theme, accent, gradient, glow, corners — applied as you click, saved
    straight away (no Save needed)."""
    changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.setMaximumWidth(720)
        self.ap = dict(APPEARANCE_DEFAULT)
        lay = QFormLayout(self)
        lay.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        lay.setHorizontalSpacing(SP['3'])
        lay.setVerticalSpacing(SP['3'])

        def lab(t):
            w = label(t)
            w.setFixedWidth(SettingsPage.LABEL_W)
            return w

        self.groups: dict[str, list[Swatch]] = {}
        row = QHBoxLayout()
        row.setSpacing(SP['2'])
        for key, (name, _t) in THEMES.items():
            sw = Swatch('theme', key, name, (96, 56))
            sw.clicked.connect(lambda _=False, k=key: self._set('theme', k))
            row.addWidget(sw)
            self.groups.setdefault('theme', []).append(sw)
        row.addStretch()
        lay.addRow(lab("Theme"), row)
        self.theme_name = label("", "Muted")
        lay.addRow(lab(""), self.theme_name)

        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        for key, hexv in ACCENTS.items():
            sw = Swatch('color', hexv, key.capitalize())
            sw.clicked.connect(lambda _=False, k=key: self._set('accent', k))
            row.addWidget(sw)
            self.groups.setdefault('accent', []).append(sw)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("#rrggbb")
        self.custom.setMaxLength(7)
        self.custom.setFixedWidth(96)
        self.custom.setToolTip("Any colour, as a hex code")
        self.custom.editingFinished.connect(self._custom)
        row.addSpacing(SP['2'])
        row.addWidget(self.custom)
        row.addStretch()
        lay.addRow(lab("Accent colour"), row)

        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        self.partner_row = row
        lay.addRow(lab("Gradient"), row)

        self.glow = SegmentedControl('pill', 32)
        for k, t in (('off', "Off"), ('subtle', "Subtle"), ('vivid', "Vivid")):
            self.glow.add(k, t)
        self.glow.changed.connect(lambda k: self._set('glow', k))
        g = QHBoxLayout()
        g.addWidget(self.glow)
        self.art_tint = QCheckBox("Tint it with the featured game's art")
        self.art_tint.toggled.connect(lambda on: self._set('art_tint', on))
        g.addSpacing(SP['3'])
        g.addWidget(self.art_tint)
        g.addStretch()
        lay.addRow(lab("Background glow"), g)

        self.corners = SegmentedControl('pill', 32)
        for k, t in (('sharp', "Sharp"), ('default', "Soft"), ('round', "Round")):
            self.corners.add(k, t)
        self.corners.changed.connect(lambda k: self._set('corners', k))
        c = QHBoxLayout()
        c.addWidget(self.corners)
        c.addStretch()
        reset = QPushButton("Reset appearance")
        reset.setObjectName("Ghost")
        set_glyph_icon(reset, G['reset'], 15)
        reset.clicked.connect(lambda: self.load(dict(APPEARANCE_DEFAULT), emit=True))
        c.addWidget(reset)
        lay.addRow(lab("Corners"), c)

    def _partners(self):
        clear_layout(self.partner_row)
        self.groups['partner'] = []
        accent = accent_hex(self.ap['accent'])
        sw = Swatch('color', (accent, accent), "None — solid accent")
        sw.clicked.connect(lambda _=False: self._set('partner', 'none'))
        self.partner_row.addWidget(sw)
        self.groups['partner'].append(sw)
        sw.key = 'none'
        for key, hexv in ACCENTS.items():
            if hexv == accent:
                continue
            sw = Swatch('color', (accent, hexv), f"{accent} → {key.capitalize()}")
            sw.key = key
            sw.clicked.connect(lambda _=False, k=key: self._set('partner', k))
            self.partner_row.addWidget(sw)
            self.groups['partner'].append(sw)
        self.partner_row.addStretch()

    def load(self, ap: dict, emit=False):
        self.ap = normalize_appearance(ap)
        self._sync()
        if emit:
            self.changed.emit(dict(self.ap))

    def _sync(self):
        self._partners()
        for sw in self.groups['theme']:
            sw.setChecked(sw.value == self.ap['theme'])
        for sw, key in zip(self.groups['accent'], ACCENTS):
            sw.setChecked(key == self.ap['accent'])
        for sw in self.groups['partner']:
            sw.setChecked(sw.key == self.ap['partner'])
        self.custom.setText(self.ap['accent'] if _hex_ok(self.ap['accent']) else "")
        self.theme_name.setText(THEMES[self.ap['theme']][0])
        self.glow.set_current(self.ap['glow'], animate=False)
        self.corners.set_current(self.ap['corners'], animate=False)
        self.art_tint.blockSignals(True)
        self.art_tint.setChecked(self.ap['art_tint'])
        self.art_tint.blockSignals(False)
        self.art_tint.setEnabled(self.ap['glow'] != 'off')
        for grp in self.groups.values():
            for sw in grp:
                sw.update()

    def _set(self, key, value):
        if self.ap.get(key) == value:
            self._sync()
            return
        self.ap[key] = value
        if key == 'accent' and self.ap['partner'] not in ('none',) and \
                accent_hex(self.ap['partner']) == accent_hex(value):
            self.ap['partner'] = 'none'
        self._sync()
        self.changed.emit(dict(self.ap))

    def _custom(self):
        v = self.custom.text().strip()
        if not v.startswith('#'):
            v = '#' + v
        if _hex_ok(v):
            self._set('accent', v.lower())


class SettingsPage(QWidget):
    """Edits the config.toml keys the GUI owns; everything else in the file
    (comments, unknown keys) is preserved by save_config()."""
    request = pyqtSignal(list, str, bool, str)
    saved = pyqtSignal(bool)            # proton_changed
    controls_changed = pyqtSignal(dict)  # a library preference, applied at once

    def __init__(self):
        super().__init__()
        # Scrolls: three sections don't fit a half-height tiled window.
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        scroll.setWidget(host)
        page.addWidget(scroll)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        outer.setSpacing(SP['3'])
        outer.addWidget(label("Settings", "PageTitle"))
        self.error = label("", "Muted", wrap=True)
        outer.addWidget(self.error)
        head = label("Appearance", "ShelfTitle")
        head.setMaximumWidth(720)
        outer.addWidget(head)
        self.appearance = AppearanceCard()
        outer.addWidget(self.appearance)
        form = self._section(outer, "General")
        key_row = QHBoxLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self.key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row.addWidget(self.key)
        key_row.addWidget(show)
        fill = QPushButton("Fetch extra artwork")
        fill.setToolTip("Download missing hero, logo and banner images from SteamGridDB for "
                        "every game (Steam and Heroic too). Your own picks are kept.")
        fill.clicked.connect(lambda: self.request.emit(
            ['--fill-art'], "Fetching extra artwork from SteamGridDB", False, 'task'))
        key_row.addWidget(fill)
        self._add(form, "SteamGridDB API key", key_row)
        self.base = QLineEdit()
        self.base.setPlaceholderText(INSTALL_BASE_DEFAULT)
        self._add(form, "Install games to", self._with_browse(self.base))
        self.scan_dir = QLineEdit()
        self.scan_dir.setPlaceholderText(str(HOME / 'Downloads'))
        self._add(form, "Look for repacks in", self._with_browse(self.scan_dir))
        self.scan_more = QLineEdit()
        self.scan_more.setPlaceholderText("more folders, separated by ;  (e.g. ~/Games/Install)")
        self.scan_more.setToolTip("Nexus watches these too and tells you when a download there "
                                  "is ready to install")
        self._add(form, "Also look in", self.scan_more)
        self.runner = NoWheelCombo()
        for text, val in (("Automatic", 'auto'), ("umu-run + Proton", 'umu'),
                          ("Proton (direct)", 'proton'), ("System Wine", 'wine')):
            self.runner.addItem(text, val)
        self._add(form, "Runner", self.runner)
        self.proton = NoWheelCombo()
        self.proton.setToolTip("The Proton games are played with — in launch.sh, Steam and "
                               "Heroic. A game can override it on its Launch tab.")
        self._add(form, "Play with Proton", self.proton)
        self.account = QLineEdit()
        self.account.setPlaceholderText("auto-detect (last signed in)")
        self._add(form, "Steam account", self.account)
        self.steam = QCheckBox("Add new games to Steam")
        self.desktop = QCheckBox("Create app-menu entries for new games")
        self.heroic = QCheckBox("Add new games to Heroic")
        self.news = QCheckBox("Show the News page (experimental)")
        self.unattended = QCheckBox("Install repacks without the setup window")
        self.trash_repack = QCheckBox("Move a repack to the Trash once it's installed")
        iform = self._section(outer, "New games and pages")
        # A library preference (library.json), applied at once — not config.toml.
        self.show_owned = QCheckBox("Show games you own but haven't installed "
                                    "(Steam through Game-db, Epic and GOG through Heroic)")
        for c in (self.unattended, self.trash_repack, self.steam, self.desktop, self.heroic,
                  self.news, self.show_owned):
            self._add(iform, "", c)
        # Controls: what a double click and A / Enter do on a game. Library
        # preferences (library.json), applied at once like "show games you own".
        cform = self._section(outer, "Controls")
        self.ctl_double = NoWheelCombo()
        self.ctl_double.addItem("Play the game", 'play')
        self.ctl_double.addItem("Open its page", 'details')
        self._add(cform, "Double-click a game", self.ctl_double)
        self.ctl_a = NoWheelCombo()
        self.ctl_a.addItem("Open its page (press again to play)", 'details')
        self.ctl_a.addItem("Play the game", 'play')
        self._add(cform, "Enter / controller A", self.ctl_a)
        self.ctl_dedupe = NoWheelCombo()
        self.ctl_dedupe.addItem("Show it once (repack, then Steam, then Heroic)", True)
        self.ctl_dedupe.addItem("Show every copy", False)
        self._add(cform, "A game in several stores", self.ctl_dedupe)
        self.ctl_dedupe.activated.connect(lambda _i: self.controls_changed.emit(
            {'dedupe': self.ctl_dedupe.currentData()}))
        self.ctl_attract = NoWheelCombo()
        for m in ATTRACT_MINUTES:
            self.ctl_attract.addItem("Never" if not m else f"After {m} minutes idle", m)
        self._add(cform, "Theatre screensaver", self.ctl_attract)
        self.ctl_attract.activated.connect(lambda _i: self.controls_changed.emit(
            {'attract': self.ctl_attract.currentData()}))
        self._add(cform, "", label("Applied right away, in Theatre too. A game that's running, "
                                   "busy or not installed always opens its page.", "Muted",
                                   wrap=True))
        self.ctl_double.activated.connect(lambda _i: self.controls_changed.emit(
            {'double_click': self.ctl_double.currentData()}))
        self.ctl_a.activated.connect(lambda _i: self.controls_changed.emit(
            {'pad_a': self.ctl_a.currentData()}))
        # Game-db: wishlist deals, upcoming releases, backlog picks.
        gform = self._section(outer, "Game-db")
        self.gdb_enabled = QCheckBox("Show the Deals page (wishlist deals, releases, backlog)")
        self._add(gform, "", self.gdb_enabled)
        self.gdb_url = QLineEdit()
        self.gdb_url.setPlaceholderText(gamedb.DEFAULT_URL)
        self._add(gform, "Game-db address", self.gdb_url)
        tok_row = QHBoxLayout()
        self.gdb_token = QLineEdit()
        self.gdb_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.gdb_token.setPlaceholderText("gdb_…")
        tshow = QCheckBox("Show")
        tshow.toggled.connect(lambda on: self.gdb_token.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        get = QPushButton("Create a token…")
        get.setToolTip("Opens your Game-db account page in the browser")
        get.clicked.connect(self._open_gamedb_account)
        tok_row.addWidget(self.gdb_token, 1)
        tok_row.addWidget(tshow)
        tok_row.addWidget(get)
        self._add(gform, "Desktop app token", tok_row)
        self.gdb_note = label("", "Muted", wrap=True)
        self._add(gform, "", self.gdb_note)
        # Torrent client: the Downloads page and "Grab" on Deals.
        tform = self._section(outer, "Torrent client")
        self.tor_client = QComboBox()
        for text, key in (("Off", None), ("Transmission", 'transmission'),
                          ("qBittorrent", 'qbittorrent')):
            self.tor_client.addItem(text, key)
        self._add(tform, "Client", self.tor_client)
        self.tor_url = QLineEdit()
        self.tor_url.setPlaceholderText("http://127.0.0.1:9091")
        self._add(tform, "Remote address", self.tor_url)
        self.tor_user = QLineEdit()
        self.tor_user.setPlaceholderText("only if the client asks for one")
        self._add(tform, "Username", self.tor_user)
        prow = QHBoxLayout()
        self.tor_pass = QLineEdit()
        self.tor_pass.setEchoMode(QLineEdit.EchoMode.Password)
        pshow = QCheckBox("Show")
        pshow.toggled.connect(lambda on: self.tor_pass.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        prow.addWidget(self.tor_pass, 1)
        prow.addWidget(pshow)
        self._add(tform, "Password", prow)
        self.tor_dir = QLineEdit()
        self.tor_dir.setPlaceholderText("the client's own download folder")
        self._add(tform, "Download to", self._with_browse(self.tor_dir))
        self.tor_ratio = QLineEdit()
        self.tor_ratio.setPlaceholderText("keep seeding (the client's setting)")
        self._add(tform, "Stop seeding at ratio", self.tor_ratio)
        trow = QHBoxLayout()
        self.tor_test = QPushButton("Test connection")
        self.tor_test.setToolTip("Asks the client for its version (save first)")
        self.tor_test.clicked.connect(self._test_torrent)
        self.tor_result = label("", "Muted", wrap=True)
        trow.addWidget(self.tor_test)
        trow.addWidget(self.tor_result, 1)
        self._add(tform, "", trow)
        self._add(tform, "", label(
            "On this machine only. In Transmission turn on Edit → Preferences → Remote → "
            "Allow remote access; in qBittorrent, Tools → Options → Web UI.", "Muted",
            wrap=True))
        row = QHBoxLayout()
        self.dirty_note = label("Unsaved changes", "Muted")
        self.dirty_note.setVisible(False)
        row.addWidget(self.dirty_note)
        row.addStretch()
        revert = QPushButton("Revert")
        revert.clicked.connect(self.load)
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("Primary")
        self.btn_save.setToolTip("Save (Ctrl+S)")
        self.btn_save.clicked.connect(self._save)
        save_sc = QShortcut(QKeySequence("Ctrl+S"), self)
        save_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save_sc.activated.connect(self._save)
        row.addWidget(revert)
        row.addWidget(self.btn_save)
        w = QWidget()
        w.setMaximumWidth(720)
        w.setLayout(row)
        outer.addWidget(w)
        outer.addStretch()
        self.cfg = {}
        self._proton_current = None
        self.dirty = self._loading = False
        # Anything edited marks the page dirty: leaving keeps the edits (and
        # says so) instead of quietly reloading over them.
        for e in (self.key, self.base, self.scan_dir, self.scan_more, self.account,
                  self.gdb_url, self.gdb_token, self.tor_url, self.tor_user, self.tor_pass,
                  self.tor_dir, self.tor_ratio):
            e.textChanged.connect(self._mark_dirty)
        for c in (self.steam, self.desktop, self.heroic, self.news, self.unattended,
                  self.trash_repack, self.gdb_enabled):
            c.toggled.connect(self._mark_dirty)
        for cb in (self.runner, self.proton, self.tor_client):
            cb.activated.connect(self._mark_dirty)

    def set_controls(self, controls: dict):
        for box, key in ((self.ctl_double, 'double_click'), (self.ctl_a, 'pad_a'),
                         (self.ctl_dedupe, 'dedupe'), (self.ctl_attract, 'attract')):
            box.setCurrentIndex(max(0, box.findData(controls.get(key))))

    def _mark_dirty(self, *_):
        if not self._loading and not self.dirty:
            self.dirty = True
            self.dirty_note.setVisible(True)

    LABEL_W = 160

    def _section(self, outer, title):
        """Heading + card, with every form sharing one label column width."""
        head = label(title, "ShelfTitle")
        head.setMaximumWidth(720)
        outer.addWidget(head)
        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(720)
        form = QFormLayout(card)
        form.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        form.setHorizontalSpacing(SP['3'])
        form.setVerticalSpacing(SP['2_5'])
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(card)
        return form

    def _add(self, form, text, field):
        lab = label(text)
        lab.setFixedWidth(self.LABEL_W)
        form.addRow(lab, field)

    def _open_gamedb_account(self):
        try:
            base = gamedb.check_url(self.gdb_url.text() or gamedb.DEFAULT_URL)
        except gamedb.GameDbError as e:
            self.error.setText(str(e))
            self.error.setVisible(True)
            return
        QDesktopServices.openUrl(QUrl(base + '/account'))

    def _test_torrent(self):
        """Ask the saved client for its version (unsaved edits aren't used)."""
        self.tor_test.setEnabled(False)
        self.tor_result.setText("Asking…")

        def done(data, err):
            try:
                self.tor_test.setEnabled(True)
            except RuntimeError:
                return
            if data and data.get('ok'):
                where = f" — downloads to {data['download_dir']}" if data.get('download_dir') else ''
                self.tor_result.setText(f"✓ {data.get('client')} {data.get('version')}{where}")
            else:
                msg = ((data or {}).get('error') or {}).get('message') or err or "no answer"
                self.tor_result.setText(f"✗ {msg}")
        cli_json(['--torrent-test', '--json'], done, 15000)

    def _with_browse(self, edit):
        row = QHBoxLayout()
        row.addWidget(edit)
        b = QPushButton("…")
        b.setFixedWidth(36)
        b.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(
            self, "Choose folder", edit.text() or edit.placeholderText())) and edit.setText(d))
        row.addWidget(b)
        return row

    def load(self):
        self._loading = True
        try:
            self._load()
        finally:
            self._loading = False
        self.dirty = False
        self.dirty_note.setVisible(False)

    def _load(self):
        c = self.cfg = load_config()
        self.error.setText(f"config.toml can't be read, so it can't be saved from here until "
                           f"it's fixed: {c['error']}" if c.get('error') else "")
        self.error.setVisible(bool(c.get('error')))
        self.btn_save.setEnabled(not c.get('error'))
        self.key.setText(c.get('api_key') or "")
        self.base.setText(c.get('install_base') or "")
        self.scan_dir.setText(c.get('scan_dir') or "")
        self.scan_more.setText("; ".join(c.get('scan_dirs') or []))
        self.unattended.setChecked(c.get('unattended', True))
        self.trash_repack.setChecked(c.get('trash_repack', False))
        self.runner.setCurrentIndex(max(0, self.runner.findData(c.get('runner') or 'auto')))
        self.account.setText(c.get('steam_account') or "")
        self.steam.setChecked(c.get('steam', True))
        self.desktop.setChecked(c.get('desktop', True))
        self.heroic.setChecked(c.get('heroic', True))
        self.news.setChecked(c.get('news', True))
        self.gdb_enabled.setChecked(c.get('gamedb_cfg_enabled', False))
        self.gdb_url.setText(c.get('gamedb_url') or "")
        env_token = bool(os.environ.get('NEXUS_GAMEDB_TOKEN'))
        self.gdb_token.setText("" if env_token else (c.get('gamedb_token') or ""))
        self.gdb_token.setEnabled(not env_token)
        self.gdb_token.setPlaceholderText("set by NEXUS_GAMEDB_TOKEN" if env_token else "gdb_…")
        self.gdb_note.setText(
            "Read-only. Make a token on your Game-db account page (Desktop app tokens). "
            "It's kept in config.toml (readable only by you); data is cached 45 minutes.")
        self.tor_client.setCurrentIndex(max(0, self.tor_client.findData(c.get('torrent_client'))))
        self.tor_url.setText(c.get('torrent_url') or "")
        self.tor_user.setText(c.get('torrent_username') or "")
        self.tor_pass.setText(c.get('torrent_password') or "")
        self.tor_dir.setText(c.get('torrent_download_dir') or "")
        r = c.get('torrent_seed_ratio')
        self.tor_ratio.setText("" if r is None else f"{r:g}")
        self.tor_result.setText("")
        self._proton_current = c.get('proton') or c.get('heroic_proton')
        self.proton.clear()
        self.proton.addItem("Newest installed", None)
        if self._proton_current:
            self.proton.addItem(self._proton_current, self._proton_current)
            self.proton.setCurrentIndex(1)

        def done(data, err):
            try:
                if data is None:
                    # Say so, rather than offering only "Newest installed".
                    self.proton.addItem(f"Couldn't list installed Protons ({err or 'error'})")
                    self.proton.model().item(self.proton.count() - 1).setEnabled(False)
                    return
                for p in data.get('protons', []):
                    if self.proton.findData(p['name']) < 0:
                        self.proton.addItem(p['name'], p['name'])
            except RuntimeError:
                pass
        cli_json(['--list-protons', '--json'], done)

    def _save(self):
        c = self.cfg
        u = {}

        def put(section, key, new, old):
            if new != old:
                u[(section, key)] = new
        put('steamgriddb', 'api_key', self.key.text().strip() or None, c.get('api_key'))
        put('integrations', 'steam', self.steam.isChecked(), c.get('steam', True))
        put('integrations', 'desktop', self.desktop.isChecked(), c.get('desktop', True))
        put('integrations', 'heroic', self.heroic.isChecked(), c.get('heroic', True))
        put('news', 'enabled', self.news.isChecked(), c.get('news', True))
        gurl = self.gdb_url.text().strip() or None
        if gurl:
            try:
                gurl = gamedb.check_url(gurl)
            except gamedb.GameDbError as e:
                self.error.setText(f"Settings not saved: {e}")
                self.error.setVisible(True)
                return
        gtok = self.gdb_token.text().strip() or None
        if gtok and not gamedb.TOKEN_RE.match(gtok):
            self.error.setText("Settings not saved: a Game-db token looks like gdb_ followed "
                               "by 43 letters, digits, - or _")
            self.error.setVisible(True)
            return
        gamedb_changed = (gurl != c.get('gamedb_url') or
                          (self.gdb_token.isEnabled() and gtok != c.get('gamedb_token')))
        put('gamedb', 'enabled', self.gdb_enabled.isChecked(), c.get('gamedb_cfg_enabled'))
        put('gamedb', 'url', gurl, c.get('gamedb_url'))
        if self.gdb_token.isEnabled():
            put('gamedb', 'token', gtok, c.get('gamedb_token'))
        turl = self.tor_url.text().strip().rstrip('/') or None
        if turl and not re.match(r'^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$', turl):
            self.error.setText("Settings not saved: the torrent client must be on this machine "
                               "(http://127.0.0.1:<port>)")
            self.error.setVisible(True)
            return
        ratio = self.tor_ratio.text().strip().replace(',', '.') or None
        if ratio is not None:
            try:
                if float(ratio) < 0:
                    raise ValueError
            except ValueError:
                self.error.setText("Settings not saved: the seeding ratio is a number, like 1.5")
                self.error.setVisible(True)
                return
        put('torrent', 'client', self.tor_client.currentData(), c.get('torrent_client'))
        put('torrent', 'url', turl, c.get('torrent_url'))
        put('torrent', 'username', self.tor_user.text().strip() or None,
            c.get('torrent_username'))
        put('torrent', 'password', self.tor_pass.text() or None, c.get('torrent_password'))
        put('torrent', 'download_dir', self.tor_dir.text().strip() or None,
            c.get('torrent_download_dir'))
        old_ratio = c.get('torrent_seed_ratio')
        if (float(ratio) if ratio is not None else None) != old_ratio:
            u[('torrent', 'seed_ratio')] = ratio
        put('integrations', 'steam_account', self.account.text().strip() or None,
            c.get('steam_account'))
        put('install', 'base', self.base.text().strip() or None, c.get('install_base'))
        put('install', 'scan_dir', self.scan_dir.text().strip() or None, c.get('scan_dir'))
        more = [d.strip() for d in self.scan_more.text().split(';') if d.strip()]
        put('install', 'scan_dirs', more or None, c.get('scan_dirs') or None)
        put('install', 'unattended', self.unattended.isChecked(), c.get('unattended', True))
        put('install', 'trash_repack', self.trash_repack.isChecked(),
            c.get('trash_repack', False))
        put('runner', 'backend', self.runner.currentData(), c.get('runner') or 'auto')
        proton = self.proton.currentData()
        changed = proton != self._proton_current
        if changed:
            u[('runner', 'proton')] = proton
            u[('heroic', 'proton')] = None     # legacy key; [runner] proton replaces it
        if u:
            try:
                save_config(u)
            except (ConfigError, OSError) as e:
                self.error.setText(f"Settings not saved: {e}")
                self.error.setVisible(True)
                return
        if gamedb_changed:
            gamedb.clear_cache()               # old account's data off the disk too
        self.gamedb_changed = gamedb_changed
        self.load()
        self.saved.emit(changed)


# --------------------------------------------------------------------------
# Install page (was a dialog)
# --------------------------------------------------------------------------

def protongen_has_default() -> bool:
    """Whether protongen has a default profile saved (`global_profile`)."""
    try:
        import tomllib
        with open(NX.PROTONGEN_CONFIG / 'state.toml', 'rb') as f:
            return isinstance(tomllib.load(f).get('global_profile'), dict)
    except (OSError, ValueError):
        return False


class RepackWatcher(QObject):
    """Notices repack downloads in the scan folders. A QFileSystemWatcher
    catches new folders; while one is still downloading a slow poll follows
    it. A folder is announced (`ready`) once it's complete and its size held
    still between two scans, and only once (LibraryPrefs.seen_repacks).
    Every scan's candidates also go out as `scanned` (update detection)."""
    ready = pyqtSignal(dict)
    scanned = pyqtSignal(list)

    POLL_MS = 15000

    def __init__(self, prefs, parent=None):
        super().__init__(parent)
        self.prefs = prefs
        self.fs = QFileSystemWatcher(self)
        self.fs.directoryChanged.connect(self._soon)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(3000)
        self.debounce.timeout.connect(self.scan)
        self.poll = QTimer(self)
        self.poll.setInterval(self.POLL_MS)
        self.poll.timeout.connect(self.scan)
        self._skip: set[str] = set()                  # folders Nexus installs itself
        self._sizes: dict[str, int] = {}
        self._busy = False

    def start(self, delay_ms: int = 8000):
        """First scan a little after startup (a child timer: it dies with us)."""
        self.debounce.start(delay_ms)

    def _soon(self, _path=None):
        self.debounce.start(3000)

    def scan(self):
        if self._busy:
            return
        self._busy = True
        cli_json(['--scan', '--json'], self._done, 120000)

    def _done(self, data, _err):
        self._busy = False
        if data is None:
            return
        try:
            self.fs.directories()
        except RuntimeError:                   # the window closed while we scanned
            return
        dirs = [d for d in (data.get('scan_dirs') or [data.get('scan_dir')])
                if d and Path(d).is_dir()]
        old = set(self.fs.directories())
        if old - set(dirs):
            self.fs.removePaths(list(old - set(dirs)))
        if set(dirs) - old:
            self.fs.addPaths(sorted(set(dirs) - old))
        cands = data.get('candidates') or []
        self.scanned.emit(cands)
        self.handle(cands)

    def mark_seen(self, path: str):
        """A folder Nexus is already installing (a finished torrent): never
        offer it as a new download too."""
        self._skip.add(str(Path(path)))

    def handle(self, cands: list[dict]):
        first = self.prefs.seen_repacks is None
        seen = set() if first else set(self.prefs.seen_repacks)
        waiting = False
        for c in cands:
            key = f"{c['path']}|{c.get('fingerprint') or ''}"
            if key in seen:
                continue
            if str(Path(c['path'])) in self._skip:
                seen.add(key)
                continue
            if first or (c.get('installed') and not c.get('update_for')):
                seen.add(key)                      # there already / nothing to offer
                continue
            size, prev = c.get('size_bytes'), self._sizes.get(c['path'])
            self._sizes[c['path']] = size
            if not c.get('complete', True) or prev != size:
                waiting = True                     # still arriving: look again soon
                continue
            seen.add(key)
            self.ready.emit(c)
        if seen != self.prefs.seen_repacks:
            self.prefs.seen_repacks = seen
            self.prefs.save()
        if waiting and not self.poll.isActive():
            self.poll.start()
        elif not waiting:
            self.poll.stop()


class InstallPage(QWidget):
    submit = pyqtSignal(list, str, str)     # (argv, game name, slug or '')
    back = pyqtSignal()
    reindex = pyqtSignal()
    torrent = pyqtSignal(str, bool)         # (magnet or .torrent path, install when done)

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        outer.setSpacing(SP['3'])
        head = QHBoxLayout()
        b = QPushButton("← Back")
        b.setObjectName("Ghost")
        b.clicked.connect(self.back)
        head.addWidget(b)
        head.addWidget(label("Install a repack", "PageTitle"))
        head.addStretch()
        found = QPushButton("Already installed? Add existing games")
        found.setToolTip("Adds games that are in the install folder but missing from the "
                         "library (e.g. after moving machines)")
        found.clicked.connect(self.reindex)
        head.addWidget(found)
        outer.addLayout(head)

        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(720)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        lay.setSpacing(SP['2'])
        srow = QHBoxLayout()
        self.scan_label = label("Found in your repack folders", "StatLabel")
        srow.addWidget(self.scan_label, 1)
        self.btn_all = QPushButton("Install all")
        self.btn_all.setToolTip("Queue every finished download that isn't installed yet, "
                                "with the options below; they install one after another")
        self.btn_all.clicked.connect(self._install_all)
        self.btn_all.setVisible(False)
        srow.addWidget(self.btn_all)
        lay.addLayout(srow)
        self.scan_list = QVBoxLayout()
        self.scan_list.setSpacing(SP['1'])
        lay.addLayout(self.scan_list)
        # Downloads handed to the torrent client from here.
        lay.addWidget(label("Download with your torrent client", "StatLabel"))
        trow = QHBoxLayout()
        self.magnet = QLineEdit()
        self.magnet.setPlaceholderText("Paste a magnet link, or pick a .torrent file")
        self.magnet.returnPressed.connect(self._send_torrent)
        tfile = QPushButton("File…")
        tfile.clicked.connect(self._pick_torrent)
        tgo = QPushButton("Download")
        tgo.clicked.connect(self._send_torrent)
        trow.addWidget(self.magnet, 1)
        trow.addWidget(tfile)
        trow.addWidget(tgo)
        lay.addLayout(trow)
        self.auto_install = QCheckBox("Install it when the download finishes")
        self.auto_install.setChecked(True)
        lay.addWidget(self.auto_install)
        self.dl_list = QVBoxLayout()
        self.dl_list.setSpacing(SP['1'])
        lay.addLayout(self.dl_list)
        lay.addWidget(label("Repack folder", "StatLabel"))
        frow = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Pick the folder that contains setup.exe")
        self.folder.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        frow.addWidget(self.folder, 1)
        frow.addWidget(browse)
        lay.addLayout(frow)
        lay.addWidget(label("Name", "StatLabel"))
        self.name = QLineEdit()
        self.name.textChanged.connect(lambda _: self._check())
        lay.addWidget(self.name)
        self.warning = label("", None, wrap=True)
        self.warning.setStyleSheet(f"color: {P['warning']};")
        self.warning.setVisible(False)
        lay.addWidget(self.warning)
        # The safety look at the folder (`--check-repack`).
        self.safety = label("", None, wrap=True)
        self.safety.setVisible(False)
        lay.addWidget(self.safety)
        srow = QHBoxLayout()
        self.safety_ok = QCheckBox("I've looked at it — install anyway")
        self.safety_ok.setVisible(False)
        self.safety_ok.toggled.connect(lambda on: self.btn_go.setEnabled(on))
        srow.addWidget(self.safety_ok)
        srow.addStretch()
        self.btn_clam = QPushButton("Scan with ClamAV")
        self.btn_clam.setVisible(False)
        self.btn_clam.clicked.connect(lambda: self._safety_check(clamav=True))
        srow.addWidget(self.btn_clam)
        lay.addLayout(srow)

        opts = QHBoxLayout()
        col1, col2 = QVBoxLayout(), QVBoxLayout()
        self.steam = QCheckBox("Add to Steam")
        self.heroic = QCheckBox("Add to Heroic")
        self.desktop = QCheckBox("App-menu entry")
        self.artwork = QCheckBox("Fetch artwork")
        self.artwork.setChecked(True)
        self.verify = QCheckBox("Verify checksums first (slow)")
        self.import_save = QCheckBox("Import a previous save backup")
        self.pg_default = QCheckBox("Use my protongen defaults")
        self.skipmem = QCheckBox("Skip memory check")
        self.skipdisk = QCheckBox("Skip disk-space check")
        self.silent = QCheckBox("Install without the setup window")
        self.silent.setToolTip("FitGirl's installer runs silently with its default choices "
                               "(languages, optional parts) into the game's own folder. "
                               "Untick to click through the setup window yourself.")
        col1.addWidget(self.silent)
        for c in (self.steam, self.heroic, self.desktop, self.artwork, self.pg_default):
            col1.addWidget(c)
        for c in (self.verify, self.import_save, self.skipmem, self.skipdisk):
            col2.addWidget(c)
        opts.addLayout(col1)
        opts.addLayout(col2)
        lay.addLayout(opts)
        self.deps = QLineEdit()
        self.deps.setPlaceholderText("Extra winetricks verbs, usually none (e.g. vcrun2019)")
        lay.addWidget(self.deps)
        outer.addWidget(card)
        row = QHBoxLayout()
        row.addStretch()
        self.btn_go = QPushButton("Install")
        self.btn_go.setObjectName("Primary")
        self.btn_go.clicked.connect(self._go)
        row.addWidget(self.btn_go)
        w = QWidget()
        w.setMaximumWidth(720)
        w.setLayout(row)
        outer.addWidget(w)
        outer.addStretch()
        self._detected = {}

    def open(self, config):
        self.folder.clear()
        self.name.clear()
        self.name.setPlaceholderText("")
        self.warning.setVisible(False)
        self.safety.setVisible(False)
        self.safety_ok.setVisible(False)
        self.btn_clam.setVisible(False)
        self.btn_go.setEnabled(True)
        self.steam.setChecked(config.get('steam', True))
        self.heroic.setChecked(config.get('heroic', True))
        self.desktop.setChecked(config.get('desktop', True))
        self.silent.setChecked(config.get('unattended', True))
        has_pg = protongen_has_default()
        self.pg_default.setEnabled(has_pg)
        self.pg_default.setChecked(has_pg)
        self.pg_default.setToolTip(
            "Launch options and Proton from protongen's default profile, so the game is "
            "tuned from its first launch" if has_pg else
            "No default profile in protongen yet (Settings → Save as default profile)")
        clear_layout(self.scan_list)
        self.scan_list.addWidget(label("Scanning…", "Muted"))
        self.scan_label.setVisible(True)
        cli_json(['--scan', '--json'], self._scanned, 120000)

    def _scanned(self, data, err):
        try:
            clear_layout(self.scan_list)
        except RuntimeError:
            return
        if data is None:                       # say why the list is empty
            self.scan_label.setVisible(False)
            self.scan_list.addWidget(label(f"Couldn't look for repacks — {err or 'unknown error'}. "
                                           "Pick a folder with Browse instead.", "Muted",
                                           wrap=True))
            return
        cands = (data or {}).get('candidates', [])
        dirs = (data or {}).get('scan_dirs') or []
        self.scan_label.setVisible(bool(cands))
        self.scan_label.setToolTip("\n".join(dirs))
        self._fresh = [c for c in cands if c.get('complete', True)
                       and (not c.get('installed') or c.get('update_for'))]
        self.btn_all.setText(f"Install all {len(self._fresh)}")
        self.btn_all.setVisible(len(self._fresh) > 1)
        for c in cands:
            text = f"{c['name']}  ·  {fmt_size(c.get('size_bytes')) or '?'}"
            if c.get('update_for'):
                text += "  ·  new release of an installed game"
            elif c.get('installed'):
                text += "  ·  already installed"
            if not c.get('complete', True):
                text += "  ·  still downloading"
            b = QPushButton(text)
            b.setObjectName("ToolRow")
            b.setStyleSheet("text-align: left;")
            b.clicked.connect(lambda _=False, c=c: self._pick(c['path'], c['name']))
            self.scan_list.addWidget(b)

    def _pick_torrent(self):
        f, _ = QFileDialog.getOpenFileName(self, "Pick a .torrent file", str(HOME / 'Downloads'),
                                           "Torrent files (*.torrent)")
        if f:
            self.magnet.setText(f)
            self._send_torrent()

    def _send_torrent(self):
        src = self.magnet.text().strip()
        if not src:
            return
        if not (src.lower().startswith('magnet:?') or src.lower().endswith('.torrent')):
            self.warning.setText("That isn't a magnet link or a .torrent file.")
            self.warning.setVisible(True)
            return
        self.torrent.emit(src, self.auto_install.isChecked())
        self.magnet.clear()

    def set_downloads(self, rows: list[dict]):
        """The torrents added from Nexus, with their progress."""
        try:
            clear_layout(self.dl_list)
        except RuntimeError:
            return
        for r in rows:
            if r.get('installed') or r.get('gone'):
                continue
            row = QHBoxLayout()
            name = plain_label(r.get('name') or r['hash'][:12], None)
            name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(name, 1)
            state = ("Error: " + r['error'] if r.get('error') else
                     "Downloaded" + (" — installing" if r.get('auto_install') else "")
                     if r.get('done') else "Paused" if r.get('paused') else
                     f"{r.get('percent', 0):.0f}%" + (f" · {fmt_bytes(r['rate'])}/s"
                                                       if r.get('rate') else ''))
            row.addWidget(label(state, "Muted"))
            self.dl_list.addLayout(row)
            bar = QProgressBar()
            bar.setObjectName("CardProgress")
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setRange(0, 1000)
            bar.setValue(int(10 * (r.get('percent') or 0)))
            self.dl_list.addWidget(bar)

    def _pick(self, path, name=None):
        self.folder.setText(path)
        self._detected = {}
        self.name.clear()
        self.name.setPlaceholderText(name or '')
        self._check_now()
        self._safety_check()

    def _safety_check(self, clamav: bool = False):
        folder = self.folder.text().strip()
        self.safety_ok.setVisible(False)
        self.btn_go.setEnabled(True)
        if not folder:
            self.safety.setVisible(False)
            return
        self.safety.setText("Scanning with ClamAV…" if clamav else "Looking the repack over…")
        self.safety.setStyleSheet(f"color: {P['muted']};")
        self.safety.setVisible(True)
        cli_json(['--check-repack', folder, '--json'] + (['--clamav'] if clamav else []),
                 lambda r, _e, f=folder: self._safety_done(f, r or {}), 900000)

    def _safety_done(self, folder, r):
        try:
            if folder != self.folder.text().strip():
                return
        except RuntimeError:
            return
        v = r.get('verdict')
        if not v:
            self.safety.setVisible(False)
            return
        head = {'ok': "Looks like a normal FitGirl repack.",
                'warn': "Some things look unusual — check them before installing:",
                'danger': "This repack looks tampered with. Don't install it unless you're "
                          "sure:"}[v]
        items = "".join(f"<br>• <b>{html.escape(f['file'])}</b>: {html.escape(f['why'])}"
                        for f in r.get('findings') or [] if f['level'] != 'info' or v == 'ok')
        clam = r.get('clamav') or {}
        if clam.get('available') and not clam.get('infected') and not clam.get('error'):
            items += "<br>ClamAV found nothing."
        self.safety.setText(head + items)
        self.safety.setVisible(True)
        self.safety.setStyleSheet(f"color: {P[{'ok': 'success', 'warn': 'warning', 'danger': 'danger'}[v]]};")
        self.btn_clam.setVisible(bool(r.get('clamav_available')) and not clam.get('available'))
        danger = v == 'danger'
        self.safety_ok.setChecked(False)
        self.safety_ok.setVisible(danger)
        self.btn_go.setEnabled(not danger)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select repack folder", str(HOME / 'Downloads'))
        if d:
            self._pick(d)

    def _check(self):
        """Typing a name: ask the CLI (in the background) once typing pauses."""
        if not hasattr(self, '_check_timer'):
            self._check_timer = QTimer(self)
            self._check_timer.setSingleShot(True)
            self._check_timer.setInterval(250)
            self._check_timer.timeout.connect(self._check_now)
        self._check_timer.start()

    def _check_now(self):
        """What the folder would install as (name, slug, already installed,
        a new release), from `--detect` — the CLI's own slug logic."""
        folder = self.folder.text().strip()
        if not folder:
            self.warning.setVisible(False)
            return
        name = self.name.text().strip()
        cli_json(['--detect', folder, *(['--name', name] if name else [])],
                 lambda info, _err, want=(folder, name): self._detected_now(want, info or {}),
                 15000)

    def _detected_now(self, want, info):
        try:
            if want != (self.folder.text().strip(), self.name.text().strip()):
                return                             # an answer for an older folder/name
        except RuntimeError:
            return
        self._detected = info
        if not want[1] and not self.name.placeholderText():
            self.name.setPlaceholderText(info.get('name') or '')
        if info.get('update'):
            self.warning.setText(f"This is a new release of {info.get('name')}. Installing it "
                                 f"updates the game in place; saves and settings stay.")
            self.btn_go.setText("Install update")
        elif info.get('installed'):
            self.warning.setText(f"{info.get('name')} is already installed. Installing again "
                                 f"reuses its prefix — useful for an update or DLC repack.")
            self.btn_go.setText("Install on top")
        else:
            self.btn_go.setText("Install")
        self.warning.setVisible(bool(info.get('installed')))

    def _go(self):
        folder = self.folder.text().strip()
        if not folder:
            self.warning.setText("Pick a repack folder first.")
            self.warning.setVisible(True)
            return
        name = self.name.text().strip()
        cmd = self._command(folder, name)
        if self.safety_ok.isVisible() and self.safety_ok.isChecked():
            cmd.append('--ignore-safety')
        shown = name or self.name.placeholderText() or Path(folder).name
        self.submit.emit(cmd, shown, '' if name else self._detected.get('slug') or '')

    def _install_all(self):
        for c in list(getattr(self, '_fresh', [])):
            self.submit.emit(self._command(c['path'], ''), c['name'], c.get('slug') or '')

    def _command(self, folder: str, name: str) -> list[str]:
        """The install command line for the options on this page. Steam never
        holds it up: the shortcut is added afterwards, once Steam is closed."""
        cmd = [CLI_BIN, folder, "--yes", "--log", "--defer-steam"]
        if name:
            cmd += ["--name", name]
        if not self.silent.isChecked():
            cmd.append("--interactive")
        deps = self.deps.text().strip()
        if deps:
            cmd += ["--deps", deps]
        for box, flag in ((self.verify, "--verify"), (self.skipmem, "--skip-mem-check"),
                          (self.skipdisk, "--skip-disk-check"),
                          (self.import_save, "--import-save"),
                          (self.pg_default, "--use-protongen-default")):
            if box.isChecked():
                cmd.append(flag)
        for box, flag in ((self.steam, "--no-steam"), (self.heroic, "--no-heroic"),
                          (self.desktop, "--no-desktop"), (self.artwork, "--no-artwork")):
            if not box.isChecked():
                cmd.append(flag)
        return cmd


# --------------------------------------------------------------------------
# Config / CLI helpers
# --------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def _read_config_toml() -> dict:
    """Parsed config.toml ({} if missing). Raises ConfigError if it exists
    but isn't valid TOML — callers must then not write over it."""
    import tomllib
    try:
        with open(CONFIG_PATH, 'rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"{CONFIG_PATH}: {e}") from None


def load_config() -> dict:
    """Flat view of config.toml. Values are None when unset, so the Settings
    dialog can tell "not configured" from "configured to the default"."""
    cfg = {'api_key': None, 'steam': True, 'steam_account': None,
           'desktop': True, 'heroic': True, 'proton': None, 'news': True,
           'heroic_proton': None, 'install_base': None,
           'scan_dir': None, 'scan_dirs': [], 'unattended': True, 'trash_repack': False,
           'runner': 'auto', 'error': None}
    try:
        data = _read_config_toml()
    except ConfigError as e:
        cfg['error'] = str(e)
        return cfg
    integ = data.get('integrations', {})
    cfg['api_key'] = data.get('steamgriddb', {}).get('api_key') or None
    for k in ('steam', 'desktop', 'heroic'):
        v = integ.get(k, True)
        cfg[k] = v if isinstance(v, bool) else str(v).lower() in ('1', 'true', 'yes', 'on')
    cfg['steam_account'] = integ.get('steam_account') or None
    cfg['heroic_proton'] = data.get('heroic', {}).get('proton') or None
    cfg['install_base'] = data.get('install', {}).get('base') or None
    cfg['scan_dir'] = data.get('install', {}).get('scan_dir') or None
    dirs = data.get('install', {}).get('scan_dirs') or []
    cfg['scan_dirs'] = [str(d) for d in dirs] if isinstance(dirs, list) else [str(dirs)]
    cfg['unattended'] = data.get('install', {}).get('unattended', True) is not False
    cfg['trash_repack'] = data.get('install', {}).get('trash_repack', False) is True
    cfg['runner'] = (data.get('runner', {}).get('backend') or 'auto').lower()
    cfg['proton'] = data.get('runner', {}).get('proton') or None
    cfg['news'] = data.get('news', {}).get('enabled', True) is not False
    gd = data.get('gamedb', {}) if isinstance(data.get('gamedb'), dict) else {}
    cfg['gamedb_url'] = gd.get('url') or None
    cfg['gamedb_token'] = gd.get('token') or None
    cfg['gamedb_cfg_enabled'] = gd.get('enabled', False) is True
    cfg['gamedb_enabled'] = gamedb.settings()['enabled']       # config, or the env token
    tr = data.get('torrent', {}) if isinstance(data.get('torrent'), dict) else {}
    client = str(tr.get('client') or '').lower()
    cfg['torrent_client'] = client if client in ('transmission', 'qbittorrent') else None
    for k in ('url', 'username', 'password', 'download_dir'):
        cfg[f'torrent_{k}'] = str(tr[k]) if tr.get(k) else None
    try:
        cfg['torrent_seed_ratio'] = float(tr['seed_ratio']) if 'seed_ratio' in tr else None
    except (TypeError, ValueError):
        cfg['torrent_seed_ratio'] = None
    return cfg


def _toml_literal(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (list, tuple)):
        return '[' + ', '.join(json.dumps(str(x), ensure_ascii=False) for x in v) + ']'
    return json.dumps(str(v), ensure_ascii=False)   # a valid TOML basic string


def toml_update(text: str, updates: dict) -> str:
    """Set (or, with None, remove) simple `key = value` lines in [section]s,
    leaving comments, unknown keys, ordering and inline comments alone.
    `updates` maps (section, key) -> value."""
    lines = text.splitlines()
    header = re.compile(r'^\s*\[([^\[\]]+)\]\s*(#.*)?$')
    for (section, key), value in updates.items():
        start = next((i for i, l in enumerate(lines)
                      if (m := header.match(l)) and m.group(1).strip() == section), None)
        if start is None:
            if value is not None:
                if lines and lines[-1].strip():
                    lines.append('')
                lines += [f'[{section}]', f'{key} = {_toml_literal(value)}']
            continue
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])),
                   len(lines))
        kre = re.compile(rf'^(\s*{re.escape(key)}\s*=\s*)(\[[^\]]*\]|"(?:[^"\\]|\\.)*"|[^#\s]+)'
                         rf'(\s*#.*)?$')
        hit = next((i for i in range(start + 1, end) if kre.match(lines[i])), None)
        if hit is not None:
            if value is None:
                del lines[hit]
            else:
                m = kre.match(lines[hit])
                lines[hit] = f"{m.group(1)}{_toml_literal(value)}{m.group(3) or ''}"
        elif value is not None:
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, f'{key} = {_toml_literal(value)}')
    return '\n'.join(lines) + '\n'


def save_config(updates: dict) -> None:
    """Apply `updates` ((section, key) -> value|None) to config.toml in place.
    Raises ConfigError instead of overwriting a file that doesn't parse."""
    import tomllib
    _read_config_toml()                          # raises if currently invalid
    try:
        text = CONFIG_PATH.read_text()
    except FileNotFoundError:
        text = ''
    new = toml_update(text, updates)
    try:
        tomllib.loads(new)
    except ValueError as e:
        raise ConfigError(f"refusing to write invalid TOML: {e}") from None
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(f'.{CONFIG_PATH.name}.tmp')
    # 0600 from the first byte: it holds the SteamGridDB key and Game-db token
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(new)
    os.chmod(tmp, 0o600)                         # an older tmp may have kept wider bits
    os.replace(tmp, CONFIG_PATH)


def fmt_size(disk_bytes) -> str | None:
    if not disk_bytes:
        return None
    return f"{disk_bytes / 1e9:.1f} GB"


def fmt_bytes(n) -> str:
    """Human size for small and large things alike (cleanup rows)."""
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1000 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit in ('B', 'KB') else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_relative(iso_ts: str | None) -> str | None:
    """'3d ago' / 'today' style label for an ISO timestamp, or None."""
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    days = (datetime.now() - then).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def steam_running() -> bool:
    """Same test as the CLI's: the client or its web helper — read from
    /proc directly (two pgrep runs cost ~15 ms; this is a few)."""
    try:
        with os.scandir('/proc') as it:
            pids = [e.name for e in it if e.name.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f'/proc/{pid}/comm', 'rb') as f:
                if f.read().strip() in (b'steam', b'steamwebhelper'):
                    return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------
# Controller + keyboard navigation
# --------------------------------------------------------------------------
#
# A controller is turned into the same key presses the keyboard uses, and the
# arrow keys get *spatial* navigation across the whole window (focus moves to
# the nearest control in that direction), so everything reachable with a
# mouse is reachable with a D-pad — and keyboard users get it for free.

GLYPHS = {
    'xbox': {'a': 'A', 'b': 'B', 'x': 'X', 'y': 'Y'},
    'ps': {'a': '✕', 'b': '○', 'x': '□', 'y': '△'},
    'nintendo': {'a': 'B', 'b': 'A', 'x': 'Y', 'y': 'X'},
    'generic': {'a': 'A', 'b': 'B', 'x': 'X', 'y': 'Y'},
}


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


INPUT: 'InputMode | None' = None        # set by MainWindow


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
    = TV mode (fullscreen)."""
    connected = pyqtSignal(str, str)       # name, kind
    disconnected = pyqtSignal(str)
    used = pyqtSignal()
    action = pyqtSignal(str)               # page-prev/next, view, tv, search

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
            self.action.emit('tv')

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
        if name in ('Chip', 'SegItem', 'TopIcon', 'ShelfArrow', 'GlassIcon', 'PlayRound'):
            return r.height() / 2
        if isinstance(w, (GameTile, ArtThumb)):
            return float(R['tile'])
        if name in ('Card', 'DashCard', 'TheatreCard') or isinstance(w, (DealCard, NewsTile, NewsFeature)):
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


# --------------------------------------------------------------------------
# Theatre mode — full screen, controller first, for the TV
# --------------------------------------------------------------------------

THEATRE_SCALE = 1.4


class TheatreCard(QFrame):
    """One glass card on Theatre's game page (Stats, Compatibility, Game-db,
    News). Focusable with the D-pad; A opens its longer version (`more`) in
    the page's reader."""
    open = pyqtSignal(object)

    def __init__(self, key: str, title: str, glyph: str):
        super().__init__()
        self.key, self.title, self.more = key, title, ''
        self.setObjectName("TheatreCard")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(330)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['4'])
        lay.setSpacing(SP['1_5'])
        head = QHBoxLayout()
        head.setSpacing(SP['1_5'])
        head.addWidget(icon_label(glyph, 16, P['accent']))
        head.addWidget(label(title.upper(), "TheatreCardTitle"))
        head.addStretch()
        lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(SP['1'])
        lay.addLayout(self.body)
        lay.addStretch()

    def set_lines(self, lines: list, more: str = '', bar: float | None = None):
        """lines: (text, colour or None, big?) — the first is usually big."""
        clear_layout(self.body)
        for text, color, big in lines:
            lab = label(text, "TheatreCardBig" if big else "TheatreCardLine", wrap=True)
            if color:
                lab.setStyleSheet(f"color: {color};")
            self.body.addWidget(lab)
        if bar is not None:
            pb = QProgressBar()
            pb.setObjectName("CardProgress")
            pb.setTextVisible(False)
            pb.setFixedHeight(6)
            pb.setRange(0, 1000)
            pb.setValue(int(max(0.0, min(1.0, bar)) * 1000))
            self.body.addWidget(pb)
        self.more = more
        self.setVisible(bool(lines))

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.open.emit(self)
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.open.emit(self)
        super().mouseReleaseEvent(ev)


def _news_html(items: list[dict]) -> str:
    out = []
    for i in items:
        when = fmt_relative(i.get('date')) or ''
        out.append(f"<h3>{html.escape(i.get('title') or '')}</h3>"
                   f"<p style='color:{P['subtext']}'>{html.escape(when)}</p>"
                   f"<p>{html.escape(i.get('summary') or '')}</p>")
    return "".join(out)


class TheatreDetails(QWidget):
    """A game, full screen: its art behind, logo, a few facts, a big Play
    button and the handful of things you'd do from the couch."""
    play = pyqtSignal(dict)
    action = pyqtSignal(str, dict)
    closed = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.game: dict = {}
        self._pm = None
        lay = QVBoxLayout(self)
        m = int(SP['8'] * 2.2)
        lay.setContentsMargins(m, m, m, m)
        lay.setSpacing(SP['3'])
        lay.addStretch()
        self.kicker = label("", "HeroKicker")
        lay.addWidget(self.kicker)
        self.logo = QLabel()
        lay.addWidget(self.logo)
        self.title = label("", "HeroTitle", wrap=True)
        self.title.setStyleSheet("font-size: 56px;")
        lay.addWidget(self.title)
        self.facts = QHBoxLayout()
        self.facts.setSpacing(SP['2'])
        lay.addLayout(self.facts)
        lay.addSpacing(SP['4'])
        row = QHBoxLayout()
        row.setSpacing(SP['3'])
        self.btn_play = QPushButton("Play")
        self.btn_play.setObjectName("PlayBig")
        set_glyph_icon(self.btn_play, G['play'], 28, 'on_accent', hover_role=None)
        self.btn_play.setMinimumSize(260, 64)
        self.btn_play.clicked.connect(lambda: self.play.emit(self.game))
        row.addWidget(self.btn_play)
        self.btn_fav = QPushButton()
        self.btn_fav.setObjectName("Glass")
        self.btn_fav.setMinimumHeight(64)
        self.btn_fav.clicked.connect(lambda: self.action.emit('favorite', self.game))
        row.addWidget(self.btn_fav)
        self.btn_opts = QPushButton("Options")
        self.btn_opts.setObjectName("Glass")
        set_glyph_icon(self.btn_opts, G['more'], 24, 'on_art', hover_role=None)
        self.btn_opts.setMinimumHeight(64)
        self.btn_opts.clicked.connect(lambda: self.action.emit('options', self.game))
        row.addWidget(self.btn_opts)
        back = QPushButton("Back")
        back.setObjectName("Glass")
        back.setMinimumHeight(64)
        back.clicked.connect(self.closed)
        row.addWidget(back)
        row.addStretch()
        lay.addLayout(row)
        lay.addSpacing(SP['6'])
        # Below the actions: glanceable cards, or one of them opened up.
        self.lower = QStackedWidget()
        self.lower.setFixedHeight(260)
        cards_host = QWidget()
        col = QVBoxLayout(cards_host)               # cards share the tallest one's height
        col.setContentsMargins(0, 0, 0, 0)
        crow = QHBoxLayout()
        crow.setSpacing(SP['4'])
        col.addLayout(crow)
        col.addStretch()
        self.cards = {}
        for key, title, glyph in (('stats', "Stats", G['clock']),
                                  ('compat', "Compatibility", G['proton']),
                                  ('achievements', "Achievements", G['medal']),
                                  ('gamedb', "Game-db", G['trophy']),
                                  ('news', "News", G['news'])):
            c = TheatreCard(key, title, glyph)
            c.open.connect(self._open_card)
            c.setVisible(False)
            crow.addWidget(c)
            self.cards[key] = c
        crow.addStretch()
        self.lower.addWidget(cards_host)
        self.reader = QTextBrowser()
        self.reader.setObjectName("TheatreReader")
        self.reader.setOpenLinks(False)          # no browser hand-off on a TV
        self.reader.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lower.addWidget(self.reader)
        lay.addWidget(self.lower)
        self.tips: dict | None = None            # --game-tips for this game
        self._ach_html: dict[tuple, str] = {}    # (slug, unlocked) -> the list, as HTML
        self.launch: dict | None = None          # --show-launch (repacks)
        self.news: list[dict] = []
        self.setVisible(False)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu:                 # X: the options sheet
            self.action.emit('options', self.game)
        else:
            super().keyPressEvent(ev)

    def open(self, g: dict, refocus: bool = True):
        same = g.get('slug') == self.game.get('slug') and self.isVisible()
        self.game = g
        playing = bool(g.get('playing'))
        self.kicker.setText(("Now playing" if playing else SOURCE_LABEL.get(
            g.get('source', 'nexus'), '')).upper())
        logo, url = art_source(g, LOGO_KINDS)
        self._set_logo(logo)
        if not logo and url:
            slug = g.get('slug')
            ART.fetch(url, lambda p, slug=slug: self.game.get('slug') == slug
                      and self._set_logo(p))
        clear_layout(self.facts)
        tier = g.get('protondb')
        for text, color in ((fmt_relative(g.get('last_played')) and
                             f"Played {fmt_relative(g.get('last_played'))}", None),
                            (fmt_hours(g.get('playtime_minutes')), None),
                            (tier.capitalize() if tier in TIER_COLORS else None,
                             TIER_COLORS.get(tier)),
                            (fmt_size(g.get('disk_bytes')), None),
                            ("Update ready" if g.get('update_pending') else None, P['warning'])):
            if text:
                lab = label(text, None)
                lab.setStyleSheet(pill_qss(P['glass'], color or P['on_art'], 34, 14)
                                  + " font-size: 17px;")
                lab.setFixedHeight(34)
                self.facts.addWidget(lab)
        self.facts.addStretch()
        self.btn_play.setText(play_label(g))
        self.btn_play.setEnabled(play_enabled(g))
        self.btn_fav.setText("★  Favourite" if g.get('favorite') else "☆  Add to favourites")
        if not same:
            self.tips, self.launch, self.news = None, None, []
            self.lower.setCurrentIndex(0)
        self.fill_cards()
        path, url = art_source(g, HERO_KINDS)
        if not same or path:
            self._set_hero(path)
        if not path and url:
            slug = g.get('slug')
            ART.fetch(url, lambda p, slug=slug: self.game.get('slug') == slug
                      and self._set_hero(p))
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.setVisible(True)
        if (refocus or not same) and (t := self.default_focus()) is not None:
            QTimer.singleShot(0, t.setFocus)

    def default_focus(self):
        return self.btn_play if self.btn_play.isEnabled() else self.btn_fav

    def set_info(self, tips=None, launch=None, news=None):
        """Data for the cards as it arrives (each part is optional)."""
        if tips is not None:
            self.tips = tips
        if launch is not None:
            self.launch = launch
        if news is not None:
            self.news = news
        self.fill_cards()

    def fill_cards(self):
        g, tips, launch = self.game, self.tips or {}, self.launch or {}
        # Stats
        hours = fmt_hours(g.get('playtime_minutes'))
        stats = [(hours or "Not played yet", None, True)]
        if fmt_relative(g.get('last_played')):
            stats.append((f"Last played {fmt_relative(g.get('last_played'))}", None, False))
        if fmt_size(g.get('disk_bytes')):
            stats.append((f"{fmt_size(g.get('disk_bytes'))} on disk", None, False))
        added = g.get('added_at') or g.get('installed_at')
        if fmt_relative(added):
            stats.append((f"Installed {fmt_relative(added)}", None, False))
        gd = tips.get('gamedb') or {}
        hl = gd.get('hltb') or {}
        bar = None
        if hl.get('main') and gd.get('list') != 'wishlist':
            played = (g.get('playtime_minutes') or 0) / 60
            bar = played / hl['main']
            stats.append((f"About {hl['main']:g} h to beat (HowLongToBeat)", P['subtext'], False))
        self.cards['stats'].set_lines(stats, bar=bar, more="".join(
            f"<p>{html.escape(t)}</p>" for t, _c, _b in stats))
        # Compatibility
        compat, more = [], []
        pdb = tips.get('protondb') or {}
        tier = pdb.get('tier') or g.get('protondb')
        if tier:
            compat.append((f"ProtonDB {tier.capitalize()}", TIER_COLORS.get(tier), True))
            if pdb.get('trending') and pdb['trending'] != tier:
                compat.append((f"Trending {pdb['trending']}", None, False))
            if pdb.get('total'):
                compat.append((f"{pdb['total']} reports", P['subtext'], False))
        facts, facts_more = compat_facts(tips)
        compat.extend(facts)
        if facts_more:
            more.append(facts_more)
        proton = launch.get('proton') or g.get('proton')
        if proton:
            compat.append((f"Plays with {proton}"
                           + (" (pinned)" if launch.get('proton_pinned') else ""), None, False))
        if launch.get('launch_options') or g.get('launch_options'):
            opts = launch.get('launch_options') or g.get('launch_options')
            compat.append(("Custom launch options", None, False))
            more.append(f"<p><b>Launch options</b><br>{html.escape(opts)}</p>")
        recipes = tips.get('recipes') or []
        if recipes:
            compat.append((f"{len(recipes)} tuning suggestion{'s' if len(recipes) != 1 else ''}"
                           f" for {tips.get('gpu') or 'your GPU'}", P['subtext'], False))
            more.append("<p><b>Suggestions</b></p>" + "".join(
                f"<p>{html.escape(r['name'])}<br><span style='color:{P['subtext']}'>"
                f"{html.escape(r.get('description') or r.get('launch_options') or '')}</span></p>"
                for r in recipes))
        self.cards['compat'].set_lines(compat, more="".join(
            [f"<p>{html.escape(t)}</p>" for t, _c, _b in compat] + more))
        # Achievements (Steam games, and repacks' emulator files): the count
        # now, the list when it arrives.
        ach = g.get('achievements') if g.get('source', 'nexus') in ('steam', 'nexus') else None
        if ach:
            done, total = ach
            self.cards['achievements'].set_lines(
                [(f"{done} of {total}", P['success'] if done == total else None, True),
                 (f"{round(100 * done / total)}% unlocked", P['subtext'], False)],
                bar=done / total, more=self._ach_html.get((g['slug'], done), ''))
            if (g['slug'], done) not in self._ach_html:
                key = (g['slug'], done)
                cli_json(['--achievements', str(g['appid']) if g.get('source') == 'steam'
                          else g['slug'], '--json'],
                         lambda data, _e, key=key: self._ach_arrived(key, data or {}),
                         30000, ttl=120)
        else:
            self.cards['achievements'].set_lines([])
        # Game-db
        gl = []
        if gd.get('list') == 'wishlist':
            price = gd.get('price') or {}
            gl.append(("On your wishlist", None, True))
            if price.get('currentStr'):
                gl.append((price['currentStr'] + (f" (−{int(price['cutPct'])}%)"
                                                  if price.get('cutPct') else ""),
                           P['success'] if price.get('atHistoricalLow') else None, False))
            if gd.get('release'):
                gl.append((gamedb.release_label(gd['release']), P['subtext'], False))
        elif gd:
            c = gd.get('completion') or {}
            if c.get('state') == 'in-progress':
                gl.append((f"{int(c.get('fraction', 0) * 100)}% through", None, True))
            elif c.get('label'):
                gl.append((c['label'], None, True))
            if hl.get('main'):
                gl.append((f"Main story ~{hl['main']:g} h", None, False))
            if hl.get('extra'):
                gl.append((f"With extras ~{hl['extra']:g} h", P['subtext'], False))
        self.cards['gamedb'].set_lines(gl, more="".join(
            f"<p>{html.escape(t)}</p>" for t, _c, _b in gl))
        # News for this game
        mine = [i for i in self.news if g.get('appid') and i.get('appid') == g.get('appid')][:3]
        self.cards['news'].set_lines(
            [(i.get('title') or '', None, n == 0) for n, i in enumerate(mine[:2])]
            + ([(fmt_relative(mine[0].get('date')) or '', P['subtext'], False)] if mine else []),
            more=_news_html(mine))

    def _ach_arrived(self, key, data):
        rows = []
        for a in data.get('items') or []:
            secret = a.get('hidden') and not a.get('unlocked')
            title = "Hidden achievement" if secret else a.get('title', '')
            when = (datetime.fromtimestamp(a['time']).strftime('%d %b %Y')
                    if a.get('time') else '')
            color = P['text'] if a.get('unlocked') else P['muted']
            rows.append(f"<p><span style='color:{color}'>{'✓ ' if a.get('unlocked') else ''}"
                        f"{html.escape(title)}</span>"
                        + (f" <span style='color:{P['subtext']}'>· {when}</span>" if when else "")
                        + (f"<br><span style='color:{P['subtext']}'>"
                           f"{html.escape(a.get('desc') or '')}</span>" if not secret else "")
                        + "</p>")
        self._ach_html[key] = "".join(rows)
        try:
            if (self.game.get('slug'), (self.game.get('achievements') or [None])[0]) == key:
                self.cards['achievements'].more = self._ach_html[key]
        except RuntimeError:
            pass

    def _open_card(self, card: TheatreCard):
        if not card.more:
            return
        self.reader.setHtml(f"<h2>{html.escape(card.title)}</h2>{card.more}")
        self.lower.setCurrentIndex(1)
        self._reader_from = card
        self.reader.setFocus(Qt.FocusReason.TabFocusReason)

    def back(self) -> bool:
        """B on the page: close an opened card first. True if handled."""
        if self.lower.currentIndex() == 1:
            self.lower.setCurrentIndex(0)
            card = getattr(self, '_reader_from', None)
            if card is not None and card.isVisible():
                focus_and_reveal(card)
            return True
        return False

    def _set_logo(self, path):
        pm = QPixmap(path) if path else QPixmap()
        if not pm.isNull():
            self.logo.setPixmap(pm.scaled(640, 220, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        self.logo.setVisible(not pm.isNull())
        self.title.setText(self.game.get('display_name', ''))
        self.title.setVisible(pm.isNull())

    def _set_hero(self, path):
        self._pm = QPixmap(path) if path else None
        self._scaled = None
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        pt.fillRect(r, QColor(P['bg']))
        if self._pm is not None and not self._pm.isNull():
            key = (self.width(), self.height())
            if self._scaled is None or self._scaled[0] != key:
                self._scaled = (key, self._pm.scaled(
                    self.width(), self.height(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation))
            s = self._scaled[1]
            pt.drawPixmap((self.width() - s.width()) // 2, -(s.height() - self.height()) // 3, s)
        base = QColor(P['bg'])
        g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
        c = QColor(base); c.setAlphaF(0.92); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.30); g.setColorAt(0.6, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(1.0, c)
        pt.fillRect(r, QBrush(g))
        g = QLinearGradient(0.0, r.bottom(), 0.0, r.top())
        c = QColor(base); c.setAlphaF(0.95); g.setColorAt(0.0, c)
        c = QColor(base); c.setAlphaF(0.0); g.setColorAt(0.55, c)
        pt.fillRect(r, QBrush(g))
        pt.end()


_SEARCH_JUNK = re.compile(r"[™®©:'’\-_.,!?]+")


def _search_norm(text: str) -> str:
    return ' '.join(_SEARCH_JUNK.sub(' ', (text or '').lower()).split())


def search_games(games: list[dict], query: str, limit: int = 18) -> list[dict]:
    """Games whose name holds every word of the query, best first: the name
    starting with it, then a word starting with it, then anywhere; installed
    before owned, then most played."""
    q = _search_norm(query)
    if not q:
        return []
    words = q.split()
    hits = []
    for g in games:
        name = _search_norm(g.get('display_name') or g.get('slug'))
        if not all(w in name for w in words):
            continue
        rank = 0 if name.startswith(q) else \
            1 if any(n.startswith(words[0]) for n in name.split()) else 2
        hits.append((rank, not is_installed(g), -(g.get('playtime_minutes') or 0),
                     name, g))
    hits.sort(key=lambda h: h[:4])
    return [h[-1] for h in hits[:limit]]


class TheatreSearch(QWidget):
    """Search for the couch: the query, an on-screen keyboard you drive with
    the D-pad (A types, X deletes, Y adds a space, B closes) and the matching
    games as big tiles under it. A real keyboard types straight in too."""
    picked = pyqtSignal(dict)
    closed = pyqtSignal()
    ROWS = ("1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm-:.")
    TILE = 200

    def __init__(self, parent):
        super().__init__(parent)
        self.games: list[dict] = []
        self.query = ''
        self._entry = None                 # set: naming something, not searching
        lay = QVBoxLayout(self)
        m = int(SP['8'] * 2)
        lay.setContentsMargins(m, SP['6'], m, SP['4'])
        lay.setSpacing(SP['4'])
        head = QHBoxLayout()
        head.addWidget(icon_label(G['search'], 34, P['accent']))
        self.text = label("", "SearchQuery")
        head.addWidget(self.text, 1)
        self.count = label("", "TheatreClock")
        head.addWidget(self.count)
        lay.addLayout(head)
        keys = QGridLayout()
        keys.setSpacing(SP['2'])
        self.keys: dict[str, QPushButton] = {}
        for r, row in enumerate(self.ROWS):
            for c, ch in enumerate(row):
                keys.addWidget(self._key(ch.upper(), lambda _=False, ch=ch: self.type(ch), ch),
                               r, c)
        r = len(self.ROWS)
        keys.addWidget(self._key("Space", lambda: self.type(' '), 'space'), r, 0, 1, 4)
        keys.addWidget(self._key("⌫  Delete", self.backspace, 'back'), r, 4, 1, 2)
        keys.addWidget(self._key("Clear", self.clear, 'clear'), r, 6, 1, 2)
        keys.addWidget(self._key("Done", self._done, 'done'), r, 8, 1, 2)
        kb = QWidget()
        kb.setLayout(keys)
        kb.setMaximumWidth(10 * 78 + 9 * SP['2'])
        lay.addWidget(kb, 0, Qt.AlignmentFlag.AlignHCenter)
        self.results = QScrollArea()
        self.results.setWidgetResizable(True)
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.flow = FlowLayout(host, margin=SP['2'], spacing=SP['4'])
        self.results.setWidget(host)
        lay.addWidget(self.results, 1)
        self.hint = label("", "TheatreClock")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)
        self.setVisible(False)
        parent.installEventFilter(self)                 # cover the parent as it resizes

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.fillRect(self.rect(), QColor(P['bg']))
        glow = QRadialGradient(float(self.width() * 0.5), 0.0, float(max(self.width(), 1)))
        c = QColor(P['accent']); c.setAlphaF(0.10)
        end = QColor(P['accent']); end.setAlphaF(0.0)
        glow.setColorAt(0.0, c)
        glow.setColorAt(0.6, end)
        pt.fillRect(self.rect(), QBrush(glow))
        pt.end()

    def _key(self, text, fn, key):
        b = QPushButton(text)
        b.setObjectName("Key")
        b.setMinimumSize(60, 62)
        b.clicked.connect(fn)
        self.keys[key] = b
        return b

    # ---- open / close ----
    def open(self, games: list[dict]):
        self.games = games
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.setVisible(True)
        self._refresh()
        QTimer.singleShot(0, lambda: alive(self.keys['q'])
                          and self.keys['q'].setFocus(Qt.FocusReason.TabFocusReason))

    def open_entry(self, title: str, initial: str, on_done):
        """The keyboard for typing a name with a controller (a collection):
        Done hands the text to on_done, B drops it. No search results."""
        self._entry = on_done
        self.games = []
        self.query = initial
        self.results.setVisible(False)
        self.open([])
        self.hint.setText(f"{title} — Done saves, B cancels")

    def _done(self):
        fn, name = self._entry, self.query.strip()
        self.close_search()
        if fn is not None and name:
            fn(name[:1].upper() + name[1:])

    def close_search(self):
        if self.isVisible():
            self.setVisible(False)
            self._entry = None
            self.results.setVisible(True)
            self.closed.emit()

    def default_focus(self):
        return self.keys['q']

    # ---- typing ----
    def type(self, ch: str):
        if len(self.query) < (MAX_COLLECTION_NAME if self._entry else 60):
            self.query += ch
            self._refresh()

    def backspace(self):
        self.query = self.query[:-1]
        self._refresh()

    def clear(self):
        self.query = ''
        self._refresh()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_Menu or k == Qt.Key.Key_Backspace:     # X on a pad
            self.backspace()
        elif self._entry is not None and k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not isinstance(QApplication.focusWidget(), QPushButton):
            self._done()
        elif ev.text() and ev.text().isprintable() and not ev.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            self.type(ev.text().lower())
        else:
            super().keyPressEvent(ev)

    # ---- results ----
    def _refresh(self):
        self.text.setText((self.query or ("Type a name" if self._entry else
                                          "Type a game's name")) + "▏")
        self.text.setStyleSheet("" if self.query else f"color: {P['muted']};")
        if self._entry is not None:
            self.count.setText("")
            return
        while self.flow.count():
            w = self.flow.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        found = search_games(self.games, self.query)
        for g in found:
            t = GameTile(g, width=self.TILE, theatre=True)
            t.activated.connect(self.picked)
            t.selected.connect(lambda game, _m: self.picked.emit(game))
            self.flow.addWidget(t)
        self.count.setText(f"{len(found)} found" if self.query else "")
        self.hint.setText("" if found or not self.query else "Nothing matches yet")


class AttractOverlay(QWidget):
    """Theatre's screensaver: after a while with nothing pressed, your games
    one after another — hero art drifting slowly, or (when the store has
    one) the game's trailer, muted. Video frames come through a QVideoSink
    and are painted like the art, so the name can sit on top. Any key or
    click ends it; A / Enter opens the game on screen."""
    open_game = pyqtSignal(dict)
    SLIDE_MS = 14000
    TRAILER_MS = 30000

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.games: list[dict] = []
        self.game: dict = {}
        self._pix: QPixmap | None = None
        self._frame: QImage | None = None
        self._recent: list[str] = []
        self._rng = random.Random()
        self.player = None
        self._zoom = QVariantAnimation(self)
        self._zoom.setStartValue(1.0)
        self._zoom.setEndValue(1.08)
        self._zoom.valueChanged.connect(self.update)
        self._next = QTimer(self)
        self._next.setSingleShot(True)
        self._next.timeout.connect(self._advance)
        parent.installEventFilter(self)
        self.hide()

    @property
    def running(self) -> bool:
        return not self.isHidden()

    def start(self, games: list[dict]):
        self.games = [g for g in games if is_installed(g) and not g.get('hidden')
                      and not g.get('dup_of')]
        if not self.games:
            return
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._advance()

    def stop(self):
        self._next.stop()
        self._zoom.stop()
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self._frame = None
        self.hide()

    def _advance(self):
        g = surprise_pick(self.games, self._rng, self._recent[-6:])
        if g is None:
            self.stop()
            return
        self._recent.append(g['slug'])
        self.game, self._pix, self._frame = g, None, None
        if self.player is not None:
            self.player.stop()
        path, url = art_source(g, HERO_KINDS)
        slug = g['slug']
        if path:
            self._set_pix(path, slug)
        elif url:
            ART.fetch(url, lambda p, slug=slug: p and self._set_pix(p, slug))
        # A trailer, when the store info is already known (cached on disk).
        cli_json(['--store-info', slug, '--json'],
                 lambda d, _e, slug=slug: self._maybe_trailer(d or {}, slug), 20000, ttl=3600)
        self._zoom.stop()
        self._zoom.setDuration(self.SLIDE_MS)
        self._zoom.start()
        self._next.start(self.SLIDE_MS)
        self.update()

    def _set_pix(self, path: str, slug: str):
        if slug == self.game.get('slug') and not self.isHidden():
            pm = QPixmap(path)
            if not pm.isNull():
                self._pix = pm
                self.update()

    def _maybe_trailer(self, info: dict, slug: str):
        if slug != self.game.get('slug') or self.isHidden():
            return
        trailers = ((info.get('store') or {}).get('trailers') or [])
        url = trailers[0]['url'] if trailers else ''
        if not url.startswith('https://') or not self._ensure_player():
            return
        self.player.setSource(QUrl(url))
        self.player.play()
        self._next.start(self.TRAILER_MS)

    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        except ImportError:
            return False
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)
        self.player.durationChanged.connect(self._skip_intro)
        return True

    def _skip_intro(self, ms: int):
        """Trailers open on rating cards and logos: start a little in."""
        if ms > 40000 and self.player.position() < 1000:
            self.player.setPosition(8000)

    def _on_frame(self, frame):
        if not self.isHidden() and frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self._frame = img
                self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pt.fillRect(self.rect(), QColor(0, 0, 0))
        w, h = self.width(), self.height()
        src = self._frame if self._frame is not None else self._pix
        if src is not None and not src.isNull():
            z = 1.0 if self._frame is not None else float(self._zoom.currentValue() or 1.0)
            sw, sh = src.width(), src.height()
            scale = max(w / sw, h / sh) * z
            tw, th = sw * scale, sh * scale
            target = QRectF((w - tw) / 2, (h - th) / 2, tw, th)
            if isinstance(src, QImage):
                pt.drawImage(target, src)
            else:
                pt.drawPixmap(target, src, QRectF(src.rect()))
        grad = QLinearGradient(0.0, h * 0.55, 0.0, float(h))
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, 200))
        pt.fillRect(self.rect(), QBrush(grad))
        pad = int(SP['8'] * THEATRE_SCALE)
        f = QFont(UI_FONT, -1, QFont.Weight.Bold)
        f.setPixelSize(int(FS['display'] * THEATRE_SCALE * 1.6))
        pt.setFont(f)
        pt.setPen(QColor(255, 255, 255))
        pt.drawText(QRectF(pad, h - pad - 120, w - 2 * pad, 70),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                    self.game.get('display_name', ''))
        f.setWeight(QFont.Weight.Normal)
        f.setPixelSize(int(FS['base'] * THEATRE_SCALE))
        pt.setFont(f)
        pt.setPen(QColor(255, 255, 255, 190))
        pt.drawText(QRectF(pad, h - pad - 44, w - 2 * pad, 40),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    "Press A to open it · any button to go back")
        pt.end()

    def keyPressEvent(self, ev):
        g = self.game
        self.stop()
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and g:
            self.open_game.emit(g)

    def mousePressEvent(self, ev):
        self.stop()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and not self.isHidden():
            self.setGeometry(obj.rect())
        return False


class TheatreView(QWidget):
    """Theatre mode: one big hero, rows of big tiles, a clock, and nothing
    that needs a mouse. LB/RB switch Home and Library, A opens a game, B goes
    back, Start leaves. Styled with the same tokens at THEATRE_SCALE."""
    play = pyqtSignal(dict)
    action = pyqtSignal(str, dict)
    menu = pyqtSignal(dict, QPoint)
    exit = pyqtSignal()
    surprise = pyqtSignal()

    TILE = 230
    WIDE = 440

    def __init__(self):
        super().__init__()
        self.setObjectName("Theatre")
        self.setStyleSheet(build_stylesheet(THEATRE_SCALE))
        self.games: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['8'] * 2, SP['6'], SP['8'] * 2, 0)
        lay.setSpacing(SP['4'])
        top = QHBoxLayout()
        top.addWidget(Wordmark("Nexus", 30))
        top.addSpacing(SP['6'])
        self.tabs = SegmentedControl('underline', 56)
        self.tabs.add('home', "Home")
        self.tabs.add('library', "Library")
        self.tabs.changed.connect(self._tab)
        top.addWidget(self.tabs)
        top.addStretch()
        self.btn_search = icon_button(G['search'], "Search (Y)", 52, "GlassIcon")
        self.btn_search.clicked.connect(self.open_search)
        top.addWidget(self.btn_search)
        self.btn_surprise = icon_button(G['dice'], "Surprise me — pick a game for me", 52,
                                        "GlassIcon")
        self.btn_surprise.clicked.connect(self.surprise)
        top.addWidget(self.btn_surprise)
        top.addSpacing(SP['4'])
        self.clock = label("", "TheatreClock")
        top.addWidget(self.clock)
        lay.addLayout(top)
        self.pages = QStackedWidget()
        lay.addWidget(self.pages, 1)
        # Home: hero + rows
        self.home = QScrollArea()
        self.home.setWidgetResizable(True)
        self.home.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.home.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.home_lay = QVBoxLayout(host)
        self.home_lay.setContentsMargins(0, 0, 0, SP['8'])
        self.home_lay.setSpacing(SP['6'])
        self.hero = HeroCarousel(scale=THEATRE_SCALE)
        self.hero.play.connect(self.play)
        self.hero.open.connect(self.open_game)
        self.hero.menu.connect(self.menu)
        # No ⋮ here: the game page's Options (or X) opens the controller sheet.
        self.hero.btn_more.hide()
        self.hero.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.home_lay.addWidget(self.hero)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['6'])
        self.home_lay.addLayout(self.rows)
        self.home_lay.addStretch()
        self.home.setWidget(host)
        self.pages.addWidget(self.home)
        # Library: every game, big tiles
        self.lib = QScrollArea()
        self.lib.setWidgetResizable(True)
        self.lib.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lhost = QWidget()
        self.lib_flow = FlowLayout(lhost, margin=SP['2'], spacing=SP['5'], justify=True)
        self.lib.setWidget(lhost)
        self.pages.addWidget(self.lib)
        self.search = TheatreSearch(self)
        self.search.picked.connect(self._search_picked)
        self.search.closed.connect(self._search_closed)
        self.everything: list[dict] = []
        self.details = TheatreDetails(self)
        self._tips: dict[str, dict] = {}
        self._launch: dict[str, dict] = {}
        self._asked: set[str] = set()
        self.news_items = lambda: []          # MainWindow hands over the News page's items
        self.details.play.connect(self.play)
        self.details.action.connect(self._detail_action)
        self.details.closed.connect(self.close_game)
        self._clock = QTimer(self)
        self._clock.setInterval(20000)
        self._clock.timeout.connect(self._tick)
        self._tick()

    # ---- data ----
    def set_games(self, everything: list[dict]):
        games = [g for g in everything if is_installed(g)]
        owned = sorted((g for g in everything if not is_installed(g)),
                       key=lambda g: (not g.get('installing'), -(g.get('playtime_minutes') or 0),
                                      (g.get('display_name') or '').lower()))
        self.games = games
        self.everything = everything
        self.hero.set_games(featured_games(games))
        self.hero.setVisible(bool(games))
        clear_layout(self.rows)
        played = sorted((g for g in games if g.get('last_played')),
                        key=lambda g: g['last_played'], reverse=True)
        added = sorted((g for g in games if g.get('added_at')),
                       key=lambda g: g['added_at'], reverse=True)
        cols = [(n, [g for g in games if n in (g.get('collections') or ())], 'cover')
                for n in COLLECTION_NAMES]
        for title, items, kind in (("Continue playing", played[:12], 'wide'),
                                   ("Favourites", [g for g in games if g.get('favorite')],
                                    'cover'),
                                   *cols,
                                   ("Recently installed", added[:14], 'cover'),
                                   ("Not played yet",
                                    [g for g in games if not g.get('last_played')], 'cover'),
                                   ("Ready to install", owned[:20], 'cover')):
            if not items:
                continue
            w = self.WIDE if kind == 'wide' else self.TILE
            row = ShelfRow(title, w, kind)
            for g in items:
                row.add(self._tile(g, w, kind))
            row.finish()
            self.rows.addWidget(row)
        spare: dict[str, GameTile] = {}
        while self.lib_flow.count():
            w = self.lib_flow.takeAt(0).widget()
            if isinstance(w, GameTile):
                spare[w.game.get('slug')] = w
        for g in sorted(games, key=lambda g: (g.get('display_name') or '').lower()):
            t = spare.pop(g['slug'], None)
            if t is not None:
                t.set_game(g)
            else:
                t = self._tile(g, self.TILE, 'cover')
            self.lib_flow.addWidget(t)
        for w in spare.values():
            w.hide()
            w.deleteLater()
        if self.details.isVisible():
            cur = next((g for g in everything
                        if g['slug'] == self.details.game.get('slug')), None)
            if cur:
                self.details.open(cur, refocus=False)

    # Set by the main window from Settings → Controls: whether A (and a
    # double click) play a game straight away instead of opening its page.
    a_plays = False
    double_plays = True

    def _activated(self, g):
        if self.a_plays and can_quick_play(g):
            self.play.emit(g)
        else:
            self.open_game(g)

    def _double(self, g):
        if self.double_plays and can_quick_play(g):
            self.play.emit(g)
        else:
            self.open_game(g)

    def _tile(self, g, w, kind):
        t = GameTile(g, width=w, kind=kind, theatre=True)
        t.selected.connect(lambda game, _m: self.open_game(game))
        t.activated.connect(self._activated)
        t.double.connect(self._double)
        t.play.connect(self.play)
        t.menu.connect(self.menu)
        return t

    # ---- navigation ----
    def _tab(self, key):
        self.pages.setCurrentWidget(self.home if key == 'home' else self.lib)
        QTimer.singleShot(0, lambda: alive(self) and focus_and_reveal(self.default_focus()))

    def cycle_tab(self, d):
        keys = self.tabs.keys()
        i = (keys.index(self.tabs.current()) + d) % len(keys)
        self.tabs.set_current(keys[i], emit=True)

    def open_game(self, g):
        # What's behind the game page is out of reach (D-pad and Tab alike).
        self.pages.setEnabled(False)
        self.tabs.setEnabled(False)
        self.details.open(g)
        self._load_info(g)

    def _load_info(self, g):
        """The cards' data: cached per game for the session (the CLI caches
        ProtonDB / Game-db itself), fetched in the background."""
        slug = g['slug']
        tips = self._tips.get(slug)
        launch = self._launch.get(slug)
        self.details.set_info(tips, launch, self.news_items())
        if tips is None and slug not in self._asked:
            self._asked.add(slug)
            cli_json(['--game-tips', slug, '--json'],
                     lambda info, _e, slug=slug: self._got(self._tips, slug, info or {}),
                     ttl=300)
            if g.get('source', 'nexus') == 'nexus':
                cli_json(['--show-launch', slug, '--json'],
                         lambda info, _e, slug=slug: self._got(self._launch, slug, info or {}))

    def _got(self, store, slug, info):
        store[slug] = info
        try:
            if self.details.isVisible() and self.details.game.get('slug') == slug:
                self.details.set_info(self._tips.get(slug), self._launch.get(slug))
        except RuntimeError:
            pass

    def news_changed(self):
        if self.details.isVisible():
            self.details.set_info(news=self.news_items())

    def close_game(self):
        self._from_search = False
        slug = self.details.game.get('slug')
        self.details.setVisible(False)
        self.pages.setEnabled(True)
        self.tabs.setEnabled(True)
        tile = next((t for t in self.findChildren(GameTile)
                     if t.game.get('slug') == slug and not t.isHidden()
                     and self.pages.currentWidget().isAncestorOf(t)), None)
        focus_and_reveal(tile or self.default_focus())

    # ---- search ----
    def open_search(self):
        self.pages.setEnabled(False)
        self.tabs.setEnabled(False)
        self.search.open(self.everything or self.games)

    def _search_picked(self, g):
        self.search.setVisible(False)          # the game page replaces it
        self._from_search = True
        self.open_game(g)

    def _search_closed(self):
        if not self.details.isVisible():
            self.pages.setEnabled(True)
            self.tabs.setEnabled(True)
            focus_and_reveal(self.btn_search)

    def back(self) -> bool:
        """B: close the search or the game page; otherwise go to Home. True
        if handled."""
        if self.search.isVisible():
            self.search.close_search()
            return True
        if self.details.isVisible() and getattr(self, '_from_search', False) \
                and self.details.lower.currentIndex() == 0:
            # Back from a game found by search: to the search, as it was.
            self._from_search = False
            self.details.setVisible(False)
            self.search.open(self.everything or self.games)
            return True
        if self.details.isVisible():
            if not self.details.back():
                self.close_game()
            return True
        if self.tabs.current() != 'home':
            self.tabs.set_current('home', emit=True)
            return True
        return False

    def _detail_action(self, act, g):
        if act == 'options':
            self.menu.emit(g, QPoint())
        else:
            self.action.emit(act, g)    # a favourite comes back through set_games

    def default_focus(self):
        if self.search.isVisible():
            return self.search.default_focus()
        if self.details.isVisible():
            return self.details.default_focus()
        if self.pages.currentWidget() is self.home:
            if not self.hero.isHidden() and self.hero.btn.isEnabled():
                return self.hero.btn
        page = self.pages.currentWidget()
        return next((t for t in page.findChildren(GameTile) if not t.isHidden()), None)

    def _tick(self):
        self.clock.setText(datetime.now().strftime('%H:%M'))

    def showEvent(self, ev):
        super().showEvent(ev)
        self._tick()
        self._clock.start()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._clock.stop()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.hero.setFixedHeight(int(max(420, min(720, self.height() * 0.55))))
        if self.details.isVisible():
            self.details.setGeometry(self.rect())
        if self.search.isVisible():
            self.search.setGeometry(self.rect())


class CursorHider(QObject):
    """Theatre mode hides the pointer until the mouse actually moves, and
    again after a few idle seconds."""

    def __init__(self, parent):
        super().__init__(parent)
        self.active = False
        self.hidden = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.hide)

    def start(self):
        self.active = True
        QApplication.instance().installEventFilter(self)
        self.hide()

    def stop(self):
        self.active = False
        QApplication.instance().removeEventFilter(self)
        self.timer.stop()
        self.show()

    def hide(self):
        if self.active and not self.hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self.hidden = True

    def show(self):
        if self.hidden:
            QApplication.restoreOverrideCursor()
            self.hidden = False

    def eventFilter(self, obj, ev):
        if not self.active:
            return False
        if ev.type() == QEvent.Type.ToolTip:
            return True                          # Theatre: no tooltips
        if ev.type() == QEvent.Type.MouseMove:
            self.show()
            self.timer.start()
        return False


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class Wordmark(QWidget):
    """"Nexus" in the dusk gradient."""

    def __init__(self, text="Nexus", px=20):
        super().__init__()
        self.text = text
        self.font_ = QFont(UI_FONT, -1, QFont.Weight.Bold)
        self.font_.setPixelSize(px)
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font_)
        self.setFixedSize(fm.horizontalAdvance(text) + 4, fm.height() + 4)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font_)
        path.addText(QPointF(2.0, float(fm.ascent() + 2)), self.font_, self.text)
        g = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        a, b = (QColor(c) for c in GRAD['dusk'])
        g.setColorAt(0.0, a)
        g.setColorAt(1.0, b)
        pt.fillPath(path, QBrush(g))
        pt.end()


class TopIcon(QToolButton):
    """A round icon button in the top bar, with an optional count badge and
    (for Storage) a disk-usage ring around it."""

    SIZE = 36

    def __init__(self, glyph, tip):
        super().__init__()
        self.setObjectName("TopIcon")
        self.setText(glyph)
        self.setToolTip(tip)
        self.setCheckable(True)
        self.setFixedSize(self.SIZE + 6, self.SIZE + 6)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"border-radius: {pill(self.SIZE + 6)}px;")
        self._badge, self._role = '', 'accent'
        self.usage: float | None = None        # 0..1, Storage only

    def set_badge(self, text, role='accent'):
        self._badge, self._role = (str(text) if text not in (None, '', 0) else ''), role
        self.update()

    def badge(self) -> str:
        return self._badge

    def paintEvent(self, ev):
        super().paintEvent(ev)
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.usage is not None:
            r = QRectF(3.0, 3.0, self.width() - 6.0, self.height() - 6.0)
            track = QColor(P['overlay']); track.setAlphaF(0.9)
            pt.setPen(QPen(track, 2.0))
            pt.drawEllipse(r)
            low = self.usage > 0.90
            pen = QPen(QColor(P['danger'] if low else P['accent']), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pt.setPen(pen)
            pt.drawArc(r, 90 * 16, -int(self.usage * 360 * 16))
        if self._badge:
            f = QFont(UI_FONT, -1, QFont.Weight.Bold)
            f.setPixelSize(FS['2xs'])
            pt.setFont(f)
            w = max(16.0, pt.fontMetrics().horizontalAdvance(self._badge) + 8.0)
            rect = QRectF(self.width() - w, 0.0, w, 16.0)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(P.get(self._role, P['accent'])))
            pt.drawRoundedRect(rect, 8.0, 8.0)
            pt.setPen(QColor(P['on_accent']))
            pt.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._badge)
        pt.end()


class _TabHandle:
    """Lets the rest of the window treat a top-bar tab like the old nav
    buttons (badges, visibility) without knowing it's a SegmentedControl item."""

    def __init__(self, seg: 'SegmentedControl', key):
        self.seg, self.key = seg, key

    def set_badge(self, text, role='accent'):
        self.seg.set_badge(self.key, str(text) if text not in (None, '', 0) else '', role)

    def setVisible(self, on):
        self.seg.set_item_visible(self.key, bool(on))

    def isHidden(self):
        return self.seg.item(self.key).isHidden()

    def setChecked(self, on):
        pass                                   # the control tracks its own current


class TopBar(QFrame):
    """The app's navigation: wordmark, the four content tabs, then search,
    the system pages as icons, and Install. A faint gradient hairline sits
    under it."""

    def __init__(self):
        super().__init__()
        self.setObjectName("TopBar")
        self.setFixedHeight(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['5'], 0, SP['5'], 0)
        lay.setSpacing(SP['2'])
        self.wordmark = Wordmark()
        lay.addWidget(self.wordmark)
        lay.addSpacing(SP['4'])
        self.tabs = SegmentedControl('underline', 44)
        lay.addWidget(self.tabs)
        lay.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("TopSearch")
        self.search.setPlaceholderText("Search your games   /")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(340)
        self.search.setFixedHeight(36)
        # Not in the Tab / D-pad chain: it would grab focus at start-up and
        # swallow arrows and Escape. "/" and Ctrl+F (or a click) focus it.
        self.search.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        lay.addWidget(self.search, 1)
        lay.addSpacing(SP['2'])
        # A connected controller's battery, only while one reports it.
        self.battery = QWidget()
        bl = QHBoxLayout(self.battery)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(SP['1'])
        self.battery_icon = icon_label(G['battery'], 16, P['muted'])
        bl.addWidget(self.battery_icon)
        self.battery_text = label("", "Muted")
        bl.addWidget(self.battery_text)
        self.battery.setVisible(False)
        lay.addWidget(self.battery)
        self.icons_lay = QHBoxLayout()
        self.icons_lay.setSpacing(SP['1'])
        lay.addLayout(self.icons_lay)
        lay.addSpacing(SP['2'])
        self.install = QPushButton("Install")
        self.install.setObjectName("Primary")
        set_glyph_icon(self.install, G['plus'], 16, 'on_accent', hover_role=None)
        self.install.setFixedHeight(36)
        self.install.setToolTip("Install a repack (Ctrl+N)")
        lay.addWidget(self.install)

    def set_battery(self, pads: list[dict]):
        """Show the lowest controller battery (gamepad.battery())."""
        self.battery.setVisible(bool(pads))
        if not pads:
            return
        b = pads[0]
        pct = b['percent']
        glyph = (G['battery_charging'] if b['charging'] else G['battery_full'] if pct >= 80
                 else G['battery_medium'] if pct >= 40 else G['battery_low'])
        role = 'danger' if pct < 20 and not b['charging'] else 'muted'
        self.battery_icon.setText(glyph)
        self.battery_icon.setStyleSheet(f"color: {P[role]};")
        self.battery_text.setText(f"{pct}%")
        self.battery.setToolTip(
            "Controller battery" + (" (charging)" if b['charging'] else '')
            + (f" — {len(pads)} controllers, lowest shown" if len(pads) > 1 else ''))

    def add_icon(self, glyph, tip) -> TopIcon:
        b = TopIcon(glyph, tip)
        self.icons_lay.addWidget(b)
        return b

    def set_compact(self, on: bool):
        self.tabs.set_compact(on)
        self.install.setText("" if on else "Install")
        self.search.setMaximumWidth(220 if on else 340)
        self.search.setPlaceholderText("Search   /" if on else "Search your games   /")

    def paintEvent(self, ev):
        super().paintEvent(ev)
        pt = QPainter(self)
        g = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        a, b = (QColor(c) for c in GRAD['dusk'])
        edge = QColor(P['border'])
        a.setAlphaF(0.45); b.setAlphaF(0.30)
        g.setColorAt(0.0, edge); g.setColorAt(0.25, a); g.setColorAt(0.75, b); g.setColorAt(1.0, edge)
        pt.fillRect(QRectF(0, self.height() - 1, self.width(), 1), QBrush(g))
        pt.end()


class CollectionChip(QPushButton):
    """A Library chip for one collection: click filters; right-click or the
    Menu key (X on a pad) offers rename / delete."""
    manage = pyqtSignal(str)

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name
        self.setObjectName("Chip")
        self.setCheckable(True)
        self.setToolTip("Right-click to rename or delete")

    def contextMenuEvent(self, ev):
        self.manage.emit(self.name)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Menu:
            self.manage.emit(self.name)
        else:
            super().keyPressEvent(ev)


class MainWindow(QMainWindow):
    # Page keys. Pages are registered by key (see _add_page), so the order
    # they're added in no longer matters.
    (HOME, LIBRARY, SYSTEM, SETTINGS, INSTALL, PROTON, NEWS, DEALS, ARTWORK) = (
        'home', 'library', 'system', 'settings', 'install', 'proton', 'news', 'deals',
        'artwork')
    DOWNLOADS = 'downloads'
    TABS = (HOME, LIBRARY, DEALS, NEWS)          # content tabs; the rest are icons
    # Sections reached by their old page keys: go(ACTIVITY) opens System on
    # Activity, go(UPCOMING) opens Deals on Upcoming.
    ACTIVITY, STORAGE, HEALTH, UPCOMING = 'activity', 'storage', 'health', 'upcoming'
    PAGE_ALIASES = {ACTIVITY: (SYSTEM, 'activity'), STORAGE: (SYSTEM, 'storage'),
                    HEALTH: (SYSTEM, 'health'), UPCOMING: (DEALS, 'upcoming')}
    LAUNCH_WAIT = 120                # s a launch may take to show up as running

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus")
        self.setMinimumSize(720, 480)
        self.resize(1180, 760)
        self.config = load_config()
        self.prefs = LibraryPrefs()
        self._disks = []
        self._loaded = False
        self._playing: set[str] = set()
        self._launching: dict[str, float] = {}       # slug -> monotonic start
        self._busy: frozenset[str] = frozenset()     # slugs a job is changing
        self._lib_inflight = self._lib_again = self._steam_resumed = False
        self._handoff_query = self._filter_dirty = False
        self._page_focus = QTimer(self)
        self._page_focus.setSingleShot(True)
        self._page_focus.setInterval(0)
        self._page_focus.timeout.connect(self._focus_new_page)
        self._back: list[str] = []                   # page history (go_back / go_forward)
        self._forward: list[str] = []
        self._lib_retry = QTimer(self)               # the queued follow-up scan
        self._lib_retry.setSingleShot(True)
        self._lib_retry.setInterval(0)
        self._lib_retry.timeout.connect(self.reload_library)
        self._handoff: dict | None = None            # the launch whose window we focus
        self._return_to: dict[str, int] = {}         # slug -> Nexus window to come back to
        self._view = 'home'
        self.jobs = JobManager()
        self.jobs.finished.connect(self._job_done)
        self.jobs.changed.connect(self._jobs_changed)
        self._quitting = False
        self._shown_games: list = []
        self._surprise_recent: list[str] = []
        COLLECTION_NAMES[:] = list(self.prefs.collections)
        self._quit_deadline = 0.0
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(100)
        self._quit_timer.timeout.connect(self._quit_poll)
        self._games: list = []
        self._disk = None
        self._load_error = ''
        self._pending_ids: tuple = ()
        self._health_stale = True
        self._health_problems = (0, 0)
        global INPUT
        self.input = INPUT = InputMode(self)
        self.input.changed.connect(lambda _m: self._sync_hints())
        self.input.activity.connect(self._on_activity)
        self._steam_seen = (0.0, False)
        self._install_req: dict[str, float] = {}     # slug -> when Install was pressed
        self._install_query = False
        self._running_query = False
        self._watch_finish: set[str] = set()
        self._protondb: dict[str, str] = {}             # asked once per session
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(5000)
        self._install_timer.timeout.connect(self._install_tick)

        central = GradientBackground()
        self.background = central
        outer_root = QVBoxLayout(central)
        outer_root.setContentsMargins(0, 0, 0, 0)
        outer_root.setSpacing(0)
        # Two shells over one background: the desktop app, and Theatre mode.
        self.shell = QStackedWidget()
        outer_root.addWidget(self.shell, 1)
        self.desk = QWidget()
        self.shell.addWidget(self.desk)
        root = QVBoxLayout(self.desk)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())
        self.gate = SteamGate()
        gate_wrap = QWidget()
        gw = QVBoxLayout(gate_wrap)
        gw.setContentsMargins(SP['5'], SP['3'], SP['5'], 0)
        gw.addWidget(self.gate)
        root.addWidget(gate_wrap)
        # Pages on the left, the game's details docked on the right — shared
        # by Home and Library, hidden on the other pages.
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.split.addWidget(self.stack)
        self.details = DetailsPanel()
        self.split.addWidget(self.details)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setCollapsible(0, False)
        self.details.setVisible(False)
        root.addWidget(self.split, 1)
        # The details panel's width, as last dragged (saved once it settles).
        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.setInterval(600)
        self._split_timer.timeout.connect(self._save_details_width)
        self.split.splitterMoved.connect(self._split_moved)
        self.strip = ActivityStrip(self.jobs)
        root.addWidget(self.strip)
        self.theatre = TheatreView()
        self.attract = AttractOverlay(self.theatre)
        self.attract.open_game.connect(self.theatre.open_game)
        self._idle_timer = QTimer(self)                 # Theatre's screensaver
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._maybe_attract)
        self._apply_controls()
        self.setAcceptDrops(True)
        self.theatre.play.connect(lambda g: self.on_game_action('launch', g))
        self.theatre.surprise.connect(self.surprise)
        self.theatre.action.connect(self._theatre_action)
        self.theatre.menu.connect(self._show_menu)
        self.theatre.hero.tint.connect(self.background.set_tint)
        self.shell.addWidget(self.theatre)
        # The controller's game menu, one per shell (Theatre's is TV-sized).
        self.trailer_view = TrailerOverlay(self.desk)
        self.sheet = ActionSheet(self.desk)
        self.sheet.chosen.connect(self.on_game_action)
        self.theatre_sheet = ActionSheet(self.theatre, THEATRE_SCALE)
        self.theatre_sheet.chosen.connect(self._theatre_action)
        for sh in (self.sheet, self.theatre_sheet):
            sh.closed.connect(self._update_hints)
        # Y on the desktop without Steam's on-screen keyboard: Theatre's
        # D-pad keyboard, over the desk.
        self.desk_search = TheatreSearch(self.desk)
        self.desk_search.picked.connect(self._desk_search_picked)
        self.desk_search.closed.connect(self._desk_search_closed)
        self._theatre = False
        self._was_state = Qt.WindowState.WindowNoState
        self.cursor_hider = CursorHider(self)
        self.hints = HintBar()
        outer_root.addWidget(self.hints)
        self._battery_timer = QTimer(self)          # first read soon, then every minute
        self._battery_timer.timeout.connect(self._poll_battery)
        self._battery_timer.start(0)

        self.selected: list[str] = []
        self.details.request.connect(self._on_request)
        self.details.play.connect(lambda g: self.on_game_action('launch', g))
        self.details.menu.connect(self._show_menu)
        self.details.closed.connect(self.clear_selection)
        self.details.toast.connect(self.toast)
        self.details.reveal.connect(self._reveal_game)
        self.details.notes_changed.connect(self._notes_changed)
        self.details.trailer.connect(lambda t: self.trailer_view.play(t, t.get('game', '')))
        self.strip.open_activity.connect(self._open_job)

        self.homeview = HomeView('home')
        self._wire_view(self.homeview)
        self.homeview.see_all.connect(self._see_all)
        self.homeview.tint.connect(self.background.set_tint)
        self.dashboard = Dashboard(self)
        self.homeview.extra.addWidget(self.dashboard)
        self._dash_pending: set[str] = set()
        self._dash_timer = QTimer(self)
        self._dash_timer.setSingleShot(True)
        self._dash_timer.setInterval(250)
        self._dash_timer.timeout.connect(self._flush_dashboard)
        self._home_sig = None
        self._add_page(self.HOME, self.homeview)
        self._add_page(self.LIBRARY, self._build_library())
        self.activity = ActivityPage(self.jobs)
        self.activity.exe_chosen.connect(lambda slug, path: self.run(
            ['--set-exe', slug, path], f"Starting {Path(path).name} from now on", slug))
        self.storage = StoragePage()
        self.storage.request.connect(self._on_request)
        self.storage.select_game.connect(self._reveal_game)
        self.health = HealthPage()
        self.health.request.connect(self._on_request)
        self.health.counts.connect(self._health_counts)
        self.health.toast.connect(self.toast)
        self.playhistory = PlayHistoryPage()
        self.playhistory.reveal.connect(self._reveal_game)
        self.system = SystemPage({'activity': self.activity, 'play': self.playhistory,
                                  'storage': self.storage, 'health': self.health})
        self.system.section_changed.connect(self._on_system_section)
        self._add_page(self.SYSTEM, self.system)
        self.settings = SettingsPage()
        self.settings.saved.connect(self._settings_saved)
        self.settings.show_owned.setChecked(self.prefs.show_owned)
        self.settings.show_owned.toggled.connect(self._set_show_owned)
        self.settings.set_controls(self.prefs.controls)
        self.settings.controls_changed.connect(self._apply_controls)
        self.settings.request.connect(self._on_request)
        self.settings.appearance.load(self.prefs.appearance)
        # Re-styling ~2000 widgets takes about a second: coalesce quick clicks
        # through the swatches into one apply.
        self._ap_pending = None
        self._ap_timer = QTimer(self)
        self._ap_timer.setSingleShot(True)
        self._ap_timer.setInterval(160)
        self._ap_timer.timeout.connect(self._flush_appearance)
        self.settings.appearance.changed.connect(self._queue_appearance)
        self._add_page(self.SETTINGS, self.settings)
        self.install = InstallPage()
        self.install.submit.connect(self._start_install)
        self.install.torrent.connect(self._add_torrent)
        self._torrents_started: set[str] = set()
        self._torrent_polling = self._torrent_detecting = False
        self._torrent_timer = QTimer(self)               # the torrent client
        self._torrent_timer.timeout.connect(self._poll_torrents)
        self._torrent_timer.start(5000)                  # a first look soon after start
        self.install.back.connect(lambda: self.go_back() or self.go(self.HOME))
        self.install.reindex.connect(lambda: self.run(['--reindex'],
                                                      "Looking for installed games"))
        self._add_page(self.INSTALL, self.install)
        self.protonpage = ProtonPage()
        self.protonpage.request.connect(self._on_request)
        self.protonpage.toast.connect(self.toast)
        self.protonpage.update_available.connect(
            lambda on: (self.nav[self.PROTON].set_badge('1' if on else '', 'accent'),
                        self.dash_update('system'), self._proton_update_toast(on)))
        self._add_page(self.PROTON, self.protonpage)
        self.newspage = NewsPage()
        self.newspage.toast.connect(self.toast)
        self.newspage.show_game.connect(self._reveal_game)
        self.newspage.loaded.connect(lambda: self.dash_update('news'))
        self.theatre.news_items = lambda: (getattr(self.newspage, 'data', None) or {}).get(
            'items', [])
        self.newspage.loaded.connect(self.theatre.news_changed)
        self._add_page(self.NEWS, self.newspage)
        self.dealspage = DealsPage(self.prefs)
        self.dealspage.toast.connect(self.toast)
        self.dealspage.counts.connect(self._deals_count)
        self.dealspage.loaded.connect(lambda: self.dash_update('deals', 'upcoming'))
        self.dealspage.open_settings.connect(self._open_settings)
        self.dealspage.repack.connect(self._deal_repack)
        self._add_page(self.DEALS, self.dealspage)
        self.downloads = DownloadsPage()
        self.downloads.toast.connect(self.toast)
        self.downloads.control.connect(self._torrent_control)
        self.downloads.add.connect(self._add_torrent)
        self.downloads.row_action.connect(self._download_action)
        self.downloads.open_settings.connect(self._open_settings)
        self.downloads.use_client.connect(self._use_torrent_client)
        self._add_page(self.DOWNLOADS, self.downloads)
        self.artpage = ArtPickerPage()
        self.artpage.request.connect(self._on_request)
        self.artpage.back.connect(self._art_back)
        self.artpage.open_settings.connect(self._open_settings)
        self._add_page(self.ARTWORK, self.artpage)
        self._art_from = self.HOME
        self._deals_warmed = False

        self.nav[self.NEWS].setVisible(self.config.get('news', True))
        self.nav[self.DEALS].setVisible(self.config.get('gamedb_enabled', False))
        self.setCentralWidget(central)
        self.toasts = ToastHost(central)
        self._shortcuts()
        # Arrow keys (and the controller) move focus spatially everywhere.
        self.spatial = SpatialNav(self)
        QApplication.instance().installEventFilter(self.spatial)
        self.ctrl = ControllerInput(self)
        self.ctrl.connected.connect(self._pad_connected)
        self.ctrl.disconnected.connect(self._pad_disconnected)
        self.ctrl.action.connect(self._pad_action)
        self.ctrl.used.connect(lambda: self.input.set('pad'))
        self.ctrl.used.connect(self._pad_used)
        self.focus_ring = FocusRing(self)
        self.ctrl.used.connect(self.focus_ring.nudge)
        # A bound method, not a lambda: the app outlives this window, and PyQt
        # only drops a dead receiver's connection for bound methods.
        QApplication.instance().focusChanged.connect(self._focus_moved)
        self.go(self.HOME)
        self.reload_library()
        QTimer.singleShot(0, lambda: alive(self) and (t := self.default_focus()) and t.setFocus())
        QTimer.singleShot(1500, self.health.refresh)    # sidebar badge, in the background
        # Repack downloads: "ready to install" toasts and repack updates.
        self._repack_updates: dict[str, str] = {}          # slug -> new repack folder
        self._session_start: dict[str, datetime] = {}      # Steam/Heroic play sessions
        self._feed_timer = QTimer(self)                     # FitGirl feed: newer repacks
        self._feed_timer.timeout.connect(self._check_repack_feed)
        self.repacks = RepackWatcher(self.prefs, self)
        self.repacks.ready.connect(self._repack_ready)
        self.repacks.scanned.connect(self._repack_scanned)
        self.repacks.start()
        QTimer.singleShot(4000, self.protonpage.refresh)  # GE update badge (6 h cache)
        # "Playing now": a cheap /proc scan, only while the window is visible.
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(15000)
        self._run_timer.timeout.connect(self._poll_running)
        self._run_timer.start()
        QTimer.singleShot(800, self._poll_running)
        # While a game is starting: poll every 2 s until it's running, and
        # watch for its window to hand focus over.
        self._fast_timer = QTimer(self)
        self._fast_timer.setInterval(2000)
        self._fast_timer.timeout.connect(self._poll_running)
        self._handoff_timer = QTimer(self)
        self._handoff_timer.setInterval(800)
        self._handoff_timer.timeout.connect(self._handoff_tick)
        # Last: everything the library handler touches exists by now.
        self._paint_cached_library()

    # ---- layout ----
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # niri and friends often give the window half a screen: fold the tab
        # labels to icons so search and the content keep their room.
        compact = self.width() < 1000
        if compact != getattr(self, '_compact', None):
            self._compact = compact
            self.topbar.set_compact(compact)

    def _add_page(self, key, widget):
        self.pages[key] = widget
        self.stack.addWidget(widget)

    def current(self) -> str | None:
        w = self.stack.currentWidget()
        return next((k for k, v in self.pages.items() if v is w), None)

    def _build_topbar(self):
        bar = TopBar()
        self.topbar = bar
        self.tabs = bar.tabs
        self.nav = {}
        for page, glyph, text in ((self.HOME, G['home'], "Home"),
                                  (self.LIBRARY, G['library'], "Library"),
                                  (self.DEALS, G['deals'], "Deals"),
                                  (self.NEWS, G['news'], "News")):
            self.tabs.add(page, text, glyph)
            self.nav[page] = _TabHandle(self.tabs, page)
        self.tabs.changed.connect(self.go)
        self.icons = {}
        for page, glyph, tip in ((self.DOWNLOADS, G['download'], "Downloads"),
                                 (self.SYSTEM, G['system'], "System (Ctrl+5)"),
                                 (self.PROTON, G['proton'], "Proton"),
                                 (self.SETTINGS, G['settings'], "Settings (Ctrl+,)")):
            b = bar.add_icon(glyph, tip)
            b.clicked.connect(lambda _=False, p=page: self.go(p))
            self.icons[page] = b
            self.nav[page] = b
        bar.install.clicked.connect(self.open_install)
        self.btn_install = bar.install
        self.search = bar.search
        # Debounced: rebuilding the grid per keystroke is wasted work. Typing
        # anywhere searches the Library.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(140)
        self._search_timer.timeout.connect(self.apply_filter)
        self.search.textChanged.connect(self._search_typed)
        return bar

    def _search_typed(self, text):
        if text and self.current() != self.LIBRARY:
            self.go(self.LIBRARY)
            self.search.setFocus()
        self._search_timer.start()

    def _wire_view(self, v):
        v.clicked.connect(self._tile_clicked)
        v.play.connect(lambda g: self.on_game_action('launch', g))
        v.menu.connect(self._show_menu)
        v.pending_clicked.connect(self._open_job)
        v.activated.connect(self._tile_activated)
        v.double.connect(self._tile_double)

    def _see_all(self, chip, sort):
        self.go(self.LIBRARY)
        self.search.clear()
        self.filter.setCurrentIndex(0)
        i = self.sort.findData(sort)
        if i >= 0:
            self.sort.setCurrentIndex(i)
        if self._view == 'home':
            self.set_view('grid', save=False)
        own = 'owned' if chip == 'owned' else 'installed'
        self.own_seg.set_current(own, animate=False)
        self.prefs.owned_view = own
        self._set_chip(chip if chip in self.chips else 'all')

    def _build_library(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        bl.setSpacing(SP['2'])
        bl.addWidget(label("Library", "PageTitle"))
        self.count_label = label("", "ShelfCount")
        bl.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        bl.addStretch()
        self.filter = QComboBox()
        for text, key in (("Any status", 'all'), ("Update pending", 'update'),
                          ("With launch options", 'options'), ("Not in Steam", 'no-steam'),
                          ("Not in Heroic", 'no-heroic'), ("Hidden games", 'hidden')):
            self.filter.addItem(text, key)
        self.filter.currentIndexChanged.connect(self.apply_filter)
        self.sort = QComboBox()
        for text, key in (("Recently played", 'played'), ("Name", 'name'),
                          ("Most played", 'most'), ("Recently added", 'added'),
                          ("Size on disk", 'size')):
            self.sort.addItem(text, key)
        self.sort.currentIndexChanged.connect(self._sort_changed)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(140, 260)
        self.size_slider.setFixedWidth(90)
        self.size_slider.setToolTip("Tile size")
        self.size_slider.setValue(self.prefs.tile)
        # Applied a moment after it stops moving: drag, arrows and D-pad alike.
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.setInterval(250)
        self._tile_timer.timeout.connect(self._tile_size_changed)
        self.size_slider.valueChanged.connect(self._tile_timer.start)
        self.view_seg = SegmentedControl('pill', 34)
        for key, glyph, tip in (('home', G['shelves'], "Shelves — one row per source"),
                                ('grid', G['grid'], "Grid"),
                                ('list', G['list'], "List")):
            self.view_seg.add(key, glyph=glyph, tip=tip)
        self.view_seg.changed.connect(self.set_view)
        refresh = icon_button(G['refresh'], "Refresh (Ctrl+R)", 34, "Ghost", round_=True)
        refresh.clicked.connect(self.refresh_library)
        self.btn_refresh = refresh
        dice = icon_button(G['dice'], "Surprise me — pick from what's shown (Ctrl+D)", 34,
                           "Ghost", round_=True)
        dice.clicked.connect(lambda: self.surprise(self._shown_games))
        self.btn_surprise = dice
        # Right-anchored, with the view switch last before refresh: controls
        # that hide per view are on its left, so the switch itself never moves.
        for w in (self.filter, self.sort, self.size_slider, self.view_seg, dice, refresh):
            bl.addWidget(w)
        lay.addWidget(bar)

        chips = QWidget()
        cl = QHBoxLayout(chips)
        cl.setContentsMargins(SP['5'], SP['3'], SP['5'], 0)
        cl.setSpacing(SP['1_5'])
        self.chips = {}
        for key, text in (('all', "All"), ('nexus', "Repacks"), ('steam', "Steam"),
                          ('heroic', "Heroic"), ('favorite', "★ Favourites")):
            c = QPushButton(text)
            c.setObjectName("Chip")
            c.setCheckable(True)
            c.clicked.connect(lambda _=False, k=key: self._set_chip(k))
            cl.addWidget(c)
            self.chips[key] = c
        self.chips['all'].setChecked(True)
        self._chip = 'all'
        self._col_chips = QHBoxLayout()            # one per collection, rebuilt as they change
        self._col_chips.setContentsMargins(0, 0, 0, 0)
        self._col_chips.setSpacing(SP['1_5'])
        cl.addLayout(self._col_chips)
        self._rebuild_collection_chips()
        cl.addStretch()
        # Installed / not installed (owned on Steam, Epic, GOG) / everything.
        self.own_seg = SegmentedControl('pill', 30)
        for key, text in (('installed', "Installed"), ('owned', "Not installed"),
                          ('all', "Everything")):
            self.own_seg.add(key, text)
        self.own_seg.set_current(self.prefs.owned_view, animate=False)
        self.own_seg.changed.connect(self._set_owned_view)
        cl.addWidget(self.own_seg)
        self.own_seg.setVisible(self.prefs.show_owned)
        lay.addWidget(chips)
        self._restore_library_state()
        self.filter.currentIndexChanged.connect(self._save_library_state)

        self.views = QStackedWidget()
        self.library = LibraryView()
        self.library.tile_width = self.prefs.tile
        self.home = HomeView('shelves')
        self.listview = ListView()
        self._views = {'grid': self.library, 'home': self.home, 'list': self.listview}
        for v in (self.home, self.library, self.listview):
            self._wire_view(v)
            self.views.addWidget(v)
        self.home.see_all.connect(self._see_all)
        lay.addWidget(self.views, 1)
        self.set_view(self.prefs.view, save=False)
        return page

    # ---- views / selection ----
    @property
    def view(self):
        return self._views[self._view]

    def _shown_view(self):
        """The view on screen: Shelves give way to the grid while a search
        or filter is on, so this isn't always `view`."""
        return self.views.currentWidget()

    def set_view(self, key, save=True):
        self._view = key if key in self._views else 'home'
        self.view_seg.set_current(self._view)
        self.views.setCurrentWidget(self.view)
        self.size_slider.setVisible(self._view == 'grid')
        self.sort.setVisible(self._view != 'home')
        if save:
            self.prefs.view = self._view
            self.prefs.save()
        if self._games or save:
            self.apply_filter()

    def _sort_changed(self):
        self.listview.reset_sort()
        self._save_library_state()
        self.apply_filter()

    def _tile_size_changed(self):
        if self.size_slider.value() == self.prefs.tile:
            return
        self.prefs.tile = self.size_slider.value()
        self.prefs.save()
        self.library.tile_width = self.prefs.tile
        self.apply_filter()

    def _set_show_owned(self, on: bool):
        self.prefs.show_owned = on
        self.prefs.save()
        self.own_seg.setVisible(on)
        self.reload_library()

    def _set_owned_view(self, key):
        if self.own_seg.current() != key:
            self.own_seg.set_current(key, animate=False)
        self.prefs.owned_view = key
        self.prefs.save()
        self.apply_filter()

    def _rebuild_collection_chips(self):
        for key in [k for k in self.chips if k.startswith('col:')]:
            c = self.chips.pop(key)
            self._col_chips.removeWidget(c)
            c.hide()
            c.deleteLater()
        for name in self.prefs.collections:
            c = CollectionChip(name)
            c.clicked.connect(lambda _=False, k=f'col:{name}': self._set_chip(k))
            c.manage.connect(self._manage_collection)
            c.setChecked(self._chip == f'col:{name}')
            self._col_chips.addWidget(c)
            self.chips[f'col:{name}'] = c

    def _set_chip(self, key):
        key = key if key in self.chips else 'all'
        self._chip = key
        for k, c in self.chips.items():
            c.setChecked(k == key)
        self._save_library_state()
        self.apply_filter()

    def _restore_library_state(self):
        st = self.prefs.library_state
        for box, key in ((self.sort, 'sort'), (self.filter, 'status')):
            i = box.findData(st.get(key))
            if i >= 0:
                box.blockSignals(True)
                box.setCurrentIndex(i)
                box.blockSignals(False)
        chip = st.get('chip')
        if chip in self.chips:
            self._chip = chip
            for k, c in self.chips.items():
                c.setChecked(k == chip)

    def _save_library_state(self, *_):
        st = {'sort': self.sort.currentData(), 'status': self.filter.currentData(),
              'chip': self._chip, 'details_w': self.prefs.library_state.get('details_w')}
        if st != self.prefs.library_state:
            self.prefs.library_state = st
            self.prefs.save()

    def _split_moved(self, *_):
        if self.details.isVisible():
            self._split_timer.start()

    def _save_details_width(self):
        if self.details.isVisible() and self.details.width() > 0:
            self.prefs.library_state = {**self.prefs.library_state,
                                        'details_w': self.details.width()}
            self.prefs.save()

    def _tile_clicked(self, game, mods):
        slug = game.get('slug')
        if not slug:
            return
        if mods & Qt.KeyboardModifier.ControlModifier and game.get('source', 'nexus') == 'nexus':
            self.selected = [s for s in self.selected if s != slug] if slug in self.selected \
                else [*self.selected, slug]
        else:
            self.selected = [slug]
        self._sync_selection()

    def _sync_selection(self):
        by = {g['slug']: g for g in self._games}
        self.selected = [s for s in self.selected if s in by]
        self._shown_view().mark_selected(set(self.selected))
        self.homeview.mark_selected(set(self.selected))
        self._selection_changed([by[s] for s in self.selected])

    def clear_selection(self):
        self.selected = []
        self._sync_selection()

    def _shortcuts(self):
        def sc(keys, fn, theatre=False):
            # Desktop shortcuts would act on the hidden desk behind Theatre.
            for k in keys:
                s = QShortcut(QKeySequence(k), self)
                s.activated.connect(fn if theatre else
                                    (lambda fn=fn: None if self._theatre else fn()))
        sc(["/", "Ctrl+F"], lambda: self.theatre.open_search() if self._theatre
           and not self.theatre.details.isVisible() else
           None if self._theatre else self._focus_search(), theatre=True)
        sc(["F2"], self._rename_selected)
        sc(["Delete"], self._uninstall_selected)
        sc(["Ctrl+R", "F5"], self.refresh_library, theatre=True)
        sc(["Ctrl+D"], lambda: self.surprise(
            self._shown_games if self._showing(self.LIBRARY) and not self._theatre else None),
           theatre=True)
        sc(["F11"], self._toggle_tv, theatre=True)
        sc(["Ctrl+N"], self.open_install)
        sc(["Ctrl+`"], lambda: (self.go(self.ACTIVITY), self.activity.show_debug(True)))
        sc(["Escape"], self._escape, theatre=True)
        sc(["Alt+Left", "Back"], lambda: not self._theatre and self.go_back())
        sc(["Alt+Right", "Forward"], lambda: not self._theatre and self.go_forward())
        for i, page in enumerate(self.TABS, start=1):
            sc([f"Ctrl+{i}"], lambda p=page: not self.nav[p].isHidden() and self.go(p))
        sc(["Ctrl+5"], lambda: self.go(self.ACTIVITY))
        sc(["Ctrl+,"], lambda: self.go(self.SETTINGS))

    def _selected_repack(self) -> dict | None:
        g = self.game(self.selected[0]) if len(self.selected) == 1 else None
        return g if g is not None and g.get('source', 'nexus') == 'nexus' \
            and self.current() in (self.HOME, self.LIBRARY) else None

    def _rename_selected(self):
        g = self._selected_repack()
        if g is not None:
            self.details.open_section('launch')
            self.details.ed_name.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.details.ed_name.selectAll()

    def _uninstall_selected(self):
        # Line edits keep Delete for themselves (Qt gives them the key first).
        g = self._selected_repack()
        if g is not None:
            self.on_game_action('uninstall', g)

    # ---- navigation ----
    HISTORY = 30

    def go(self, page, _history=True):
        page, section = self.PAGE_ALIASES.get(page, (page, None))
        if page not in self.pages:
            return
        cur = self.current()
        if _history and cur is not None and cur != page:
            # Back / Forward (mouse buttons, Alt+←/→, Esc on a side page).
            self._back.append(cur)
            del self._back[:-self.HISTORY]
            self._forward.clear()
        if self.current() == self.SETTINGS and page != self.SETTINGS and self.settings.dirty:
            self.toast("Your settings changes aren't saved yet.", 'warn',
                       [("Save", self.settings._save), ("Discard", self.settings.load)], 10000)
        self.stack.setCurrentWidget(self.pages[page])
        self.tabs.set_current(page if page in self.TABS else None)
        for p, b in self.icons.items():
            b.setChecked(p == page)
        if page == self.LIBRARY and self._filter_dirty:
            self.apply_filter()
        # The details panel belongs to the game pages.
        if page in (self.HOME, self.LIBRARY):
            if self.selected and not self.details.isVisible():
                self._sync_selection()
        else:
            self.details.setVisible(False)
        if page == self.SYSTEM:
            self.system.show_section(section or self.system.section())
        elif page == self.SETTINGS:
            if not self.settings.dirty:          # edits left unsaved are still there
                self.settings.load()
        elif page == self.PROTON:
            self.protonpage.refresh()
        elif page == self.NEWS:
            self.newspage.refresh()
        elif page == self.DEALS:
            if section:
                self.dealspage.show_section(section)
            self.dealspage.refresh()
        elif page == self.DOWNLOADS:
            self._poll_torrents()
        if self.input.mode in ('keyboard', 'pad') and not self._theatre:
            self._page_focus.start()            # keys/pad: land somewhere on the new page

    def _focus_new_page(self):
        f = QApplication.focusWidget()
        here = self.stack.currentWidget()
        if f is not None and (here.isAncestorOf(f) or self.topbar.isAncestorOf(f)
                              or self.details.isAncestorOf(f) or self.nav_scope() is not self):
            return
        t = self.default_focus()
        if t is not None:
            focus_and_reveal(t)

    def go_back(self) -> bool:
        cur = self.current()
        while self._back:
            page = self._back.pop()
            if page != cur and page in self.pages and self._page_shown(page):
                if cur is not None:
                    self._forward.append(cur)
                self.go(page, _history=False)
                return True
        return False

    def go_forward(self) -> bool:
        cur = self.current()
        while self._forward:
            page = self._forward.pop()
            if page != cur and page in self.pages and self._page_shown(page):
                if cur is not None:
                    self._back.append(cur)
                self.go(page, _history=False)
                return True
        return False

    def _page_shown(self, page) -> bool:
        """A tab that's switched off (News, Deals) isn't a place to go back to."""
        b = self.nav.get(page) if hasattr(self, 'nav') else None
        return b is None or not b.isHidden()

    def _on_system_section(self, key):
        """Each System section brings itself up to date when shown."""
        if key == 'play':
            self.playhistory.refresh()
        elif key == 'health' and self._health_stale:
            self._health_stale = False
            self.health.refresh()
        elif key == 'storage':
            self.storage.set_library([g for g in self._games if is_installed(g)], self._disks)
            self.storage.load_orphans()

    def _showing(self, key) -> bool:
        """Whether that page (or System / Deals section) is on screen now."""
        page, section = self.PAGE_ALIASES.get(key, (key, None))
        if self.current() != page:
            return False
        return section is None or self.pages[page].section() == section

    def _default_install_cmd(self, path: str) -> list[str]:
        """An install with the Settings defaults (what the Install page would
        start with), for one-click installs from a toast."""
        cmd = [CLI_BIN, path, '--yes', '--log', '--defer-steam']
        if not self.config.get('unattended', True):
            cmd.append('--interactive')
        if protongen_has_default():
            cmd.append('--use-protongen-default')
        return cmd

    def _repack_ready(self, c: dict):
        name, path = c['name'], c['path']
        if c.get('update_for'):
            text, verb = f"A new release of {name} has finished downloading.", "Update"
        else:
            text, verb = f"{name} has finished downloading and is ready to install.", "Install"
        self.toast(text, 'info', [
            (verb, lambda: self._start_install(self._default_install_cmd(path), name,
                                               c.get('slug') or '')),
            ("Options…", lambda: (self.open_install(), self.install._pick(path, name)))],
            30000)
        if not self.isActiveWindow():
            notify_desktop(f"{name} is ready to install", "Open Nexus to install it.")

    def _add_torrent(self, src: str, auto: bool):
        def done(data, err):
            if data is None or (data or {}).get('error'):
                msg = ((data or {}).get('error') or {}).get('message') or err or "it failed"
                self.toast(f"Couldn't hand it to the torrent client — {msg}", 'error', None, 0)
                return
            self.toast(f"Downloading {data.get('name') or 'the repack'}"
                       + (" — it installs when it's done." if auto else "."), 'success', None, 5000)
            self._poll_torrents()
        cli_json(['--torrent-add', src, '--json'] + (['--auto-install'] if auto else []),
                 done, 30000)

    def _poll_torrents(self):
        """Follow the torrent client: every torrent for the Downloads page,
        Nexus's own on the Install page and Deals cards, and install a
        finished one that asked for it. Every 2 s while Downloads is on
        screen, 10 s while something downloads, a minute otherwise; stops
        when no client is set up."""
        if self._torrent_polling:
            return
        self._torrent_polling = True

        def done(data, err):
            self._torrent_polling = False
            if not alive(self):
                return
            if data is None and err:
                data = {'error': {'kind': 'torrent', 'message': err}, 'torrents': []}
            data = data or {}
            rows = data.get('torrents') or []
            mine = [r for r in rows if r.get('tracked')]
            try:
                self.downloads.set_data(data)
                self.install.set_downloads(mine)
                self.dealspage.set_downloads(mine)
            except RuntimeError:
                return
            if data.get('configured') is False and self.downloads._detected is None \
                    and not self._torrent_detecting:
                self._torrent_detecting = True
                cli_json(['--torrent-detect', '--json'], self._torrent_detected, 10000)
            for r in mine:
                # Once: the install marks the download installed only when it
                # succeeds, and a failed one is retried by hand (Install it now).
                if r.get('done') and r.get('auto_install') and not r.get('installed') \
                        and not r.get('install_started') \
                        and r['hash'] not in self._torrents_started \
                        and r.get('path') and Path(r['path']).is_dir():
                    self._install_download(r)
            active = self.downloads.active_count()
            self.icons[self.DOWNLOADS].set_badge(active or None)
            if data.get('configured') is False:
                self._torrent_timer.stop()
            elif self._showing(self.DOWNLOADS):
                self._torrent_timer.start(2000)
            elif active or any(not r.get('done') and r.get('auto_install') for r in mine):
                self._torrent_timer.start(10_000)
            else:
                self._torrent_timer.start(60_000)
        cli_json(['--torrents', '--all', '--json'], done, 20000)

    def _install_download(self, r: dict):
        """Install a finished download; the CLI records on the download that
        it started, and that it's installed once it succeeds."""
        self._torrents_started.add(r['hash'])
        self.repacks.mark_seen(r['path'])
        self._start_install(self._default_install_cmd(r['path']) + ['--from-torrent', r['hash']],
                            r.get('title') or r.get('name') or Path(r['path']).name, '')

    def _torrent_detected(self, data, _err):
        self._torrent_detecting = False
        if alive(self):
            self.downloads.set_detected((data or {}).get('found') or False)

    def _use_torrent_client(self, found: dict):
        """"Use Transmission": write [torrent] client/url, then look again."""
        try:
            save_config({('torrent', 'client'): found['client'],
                         ('torrent', 'url'): found['url']})
        except (ConfigError, OSError) as e:
            self.toast(f"Couldn't save the setting — {e}", 'error', None, 0)
            return
        self.config = load_config()
        self.toast(f"Nexus now manages your {found['client'].capitalize()} downloads.",
                   'success', None, 5000)
        self._poll_torrents()

    def _torrent_control(self, action: str, hashes: list, delete: bool):
        if action == 'remove' and delete:
            n = len(hashes)
            what = (next((r.get('name') for r in self.downloads.data.get('torrents') or []
                          if r['hash'] == hashes[0]), None) if n == 1 else f"{n} torrents")
            self.confirm(f"Delete {what or 'this torrent'}?",
                         "The torrent is removed from the client and its downloaded files "
                         "are deleted. This can't be undone.", "Remove and delete files",
                         lambda: self._torrent_send(action, hashes, True))
            return
        self._torrent_send(action, hashes, False)

    def _torrent_send(self, action: str, hashes: list, delete: bool):
        def done(data, err):
            if data is None or (data or {}).get('error'):
                msg = ((data or {}).get('error') or {}).get('message') or err or "it failed"
                self.toast(f"The torrent client said no — {msg}", 'error', None, 0)
            self._poll_torrents()
        cli_json([f'--torrent-{action}', *hashes, '--json']
                 + (['--delete-data'] if delete else []), done, 20000)

    def _download_action(self, action: str, r: dict):
        path = r.get('path')
        if action == 'folder' and path:
            target = Path(path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(
                str(target if target.is_dir() else target.parent)))
        elif action == 'install' and path and Path(path).is_dir():
            self._install_download(r)
        elif action == 'forget':
            cli_json(['--torrent-forget', r['hash']], lambda *_: self._poll_torrents(), 10000)

    # ---- FitGirl repacks of wishlist games (Deals) ----
    def _deal_repack(self, action: str, item: dict, match: dict):
        url = match.get('url') or ''
        if action == 'page':
            if url.startswith('https://fitgirl-repacks.site/'):   # never a clone domain
                QDesktopServices.openUrl(QUrl(url))
        elif action == 'reject':
            appid = str(item.get('appid'))
            cli_json(['--repack-reject', appid, match['slug']], lambda *_: None, 10000)
            self.dealspage.forget_repack(appid)
            self.toast(f"Won't suggest “{match.get('title')}” for {item.get('title')} again.",
                       'info', None, 5000)
        elif action == 'grab':
            self.toast(f"Reading the repack page for {item.get('title')}…", 'info', None, 3000)
            cli_json(['--repack-page', match['slug'], '--json'],
                     lambda data, err: self._grab_sheet(item, match, data, err), 40000)

    def _grab_sheet(self, item: dict, match: dict, page, err):
        if not alive(self):
            return
        if page is None or page.get('error'):
            self.toast("Couldn't read the repack page — "
                       f"{(page or {}).get('error') or err}", 'error', None, 0)
            return
        title = item.get('title') or match.get('title')
        bits = [f"{page.get('repack_size') or '?'} download",
                f"{page.get('original_size') or '?'} installed"]
        need, free = page.get('size_bytes'), page.get('free_bytes')
        if page.get('dest'):
            bits.append(f"{fmt_bytes(free)} free in {page['dest']}" if free is not None
                        else f"to {page['dest']}")
        sub = " · ".join(bits)
        if need and free is not None and free < need:
            sub += " — not enough space"
        sheet = self.theatre_sheet if self._theatre else self.sheet
        items = []
        if not page.get('client'):
            items.append(("Set up a torrent client first", G['settings'],
                          lambda: self.go(self.DOWNLOADS)))
        elif not page.get('magnets'):
            sub += " — the page has no magnet link"
        else:
            items += [("Download — install when it's done", G['download'],
                       lambda: self._grab(item, match, True), True),
                      ("Download only", G['download'], lambda: self._grab(item, match, False))]
        items.append(("Open the repack page", G['external'],
                      lambda: self._deal_repack('page', item, match)))
        sheet.choose(f"{title}: {page.get('title') or match.get('title')}", items, sub)
        self._update_hints()

    def _grab(self, item: dict, match: dict, auto: bool):
        def done(data, err):
            if data is None or (data or {}).get('error'):
                msg = ((data or {}).get('error') or {}).get('message') or err or "it failed"
                self.toast(f"Couldn't start the download — {msg}", 'error', None, 0)
                return
            self.toast(f"Downloading {item.get('title')} ({data.get('repack_size') or '?'})"
                       + (" — it installs when it's done." if auto else "."), 'success',
                       [("Downloads", lambda: self.go(self.DOWNLOADS))], 6000)
            self._poll_torrents()
        cli_json(['--repack-grab', match['slug'], '--appid', str(item.get('appid')), '--json']
                 + (['--auto-install'] if auto else []), done, 40000)

    def _notes_changed(self, slug: str, text: str):
        if text.strip():
            self.prefs.notes[slug] = text
        else:
            self.prefs.notes.pop(slug, None)
        self.prefs.save()
        g = self.game(slug)
        if g is not None:
            g['notes'] = text

    def _proton_update_toast(self, on: bool):
        """Once per new GE-Proton release: a toast that installs it in one
        click (the Proton page keeps its banner either way)."""
        ge = (self.protonpage.info or {}).get('families', {}).get('ge', {})
        tag = ge.get('latest')
        if not on or not tag or tag == getattr(self, '_proton_toasted', None) \
                or tag in (self.protonpage.info or {}).get('protongen_dismissed', []):
            return
        self._proton_toasted = tag
        self.toast(f"{tag} is out.", 'info', [
            ("Install", lambda: self.run(['--proton-install', tag], f"Installing {tag}", None,
                                         'install-proton')),
            ("Details", lambda: self.go(self.PROTON))], 12000)

    def _check_repack_feed(self):
        """Ask the CLI whether FitGirl posted newer repacks of installed
        games (it fetches at most every 6 h); reload the library when the
        answer differs from what the tiles show."""
        self._feed_timer.start(6 * 3600 * 1000)
        if self.config.get('gamedb_enabled'):
            self._sync_repack_index()
        if not any(g.get('source', 'nexus') == 'nexus' for g in self._games):
            return

        def done(data, _err):
            if data is None:
                return
            found = data.get('updates') or {}
            shown = {g['slug']: g.get('repack_online') for g in self._games
                     if g.get('source', 'nexus') == 'nexus' and g.get('repack_online')}
            if found != shown:
                fresh = [p['title'] for k, p in found.items() if k not in shown]
                self.reload_library()
                if fresh:
                    self.toast(f"FitGirl posted an update for {', '.join(fresh[:2])}"
                               + (f" and {len(fresh) - 2} more" if len(fresh) > 2 else '')
                               + ".", 'info', None, 8000)
        cli_json(['--repack-feed', '--json'], done, 120000)

    def _sync_repack_index(self):
        """FitGirl's catalogue against the wishlist (the CLI syncs at most
        every 6 h, and pushes new matches to the phone): toast what's new,
        and show the matches on Deals."""
        def done(data, _err):
            if not data or not alive(self):
                return
            news = data.get('new_matches') or []
            if news:
                names = [n.get('title') or n['slug'] for n in news]
                text = (f"{len(names)} wishlist games are on FitGirl." if data.get('first')
                        else f"On FitGirl: {', '.join(names[:2])}"
                        + (f" and {len(names) - 2} more." if len(names) > 2 else "."))
                self.toast(text, 'info', [("Show", self._show_fitgirl_deals)], 10000)
            if news or (data.get('matches') and not self.dealspage.repacks()):
                self.dealspage._loaded = False         # pick the matches up from the CLI
                if self._showing(self.DEALS):
                    self.dealspage.refresh()
        cli_json(['--repack-index', '--json'], done, 300000)

    def _show_fitgirl_deals(self):
        self.dealspage._filter = 'fitgirl'
        self.dealspage.show_section('deals')
        self.go(self.DEALS)
        self.dealspage._fill_tabs()
        self.dealspage._fill()

    def _repack_scanned(self, cands: list[dict]):
        updates = {c['update_for']: c['path'] for c in cands
                   if c.get('update_for') and c.get('complete', True)}
        if updates == self._repack_updates:
            return
        self._repack_updates = updates
        for g in self._games:
            if g.get('source', 'nexus') != 'nexus':
                continue
            g['repack_update'] = updates.get(g['slug'])
            if g['repack_update']:
                g['update_pending'] = True
        self._home_sig = None
        self.apply_filter()
        self.dash_update('updates')

    def open_install(self, path: str | None = None):
        self.install.open(self.config)
        self.go(self.INSTALL)
        if path:
            self.install._pick(path)

    @staticmethod
    def _dropped_folder(ev) -> str | None:
        md = ev.mimeData()
        urls = md.urls() if md is not None and md.hasUrls() else []
        p = Path(urls[0].toLocalFile()) if len(urls) == 1 and urls[0].isLocalFile() else None
        return str(p) if p is not None and p.is_dir() else None

    def dragEnterEvent(self, ev):
        # A repack folder dropped on the window opens it on the Install page.
        if not self._theatre and self._dropped_folder(ev):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dropEvent(self, ev):
        folder = self._dropped_folder(ev)
        if folder and not self._theatre:
            ev.acceptProposedAction()
            self.open_install(folder)
        else:
            super().dropEvent(ev)

    def _focus_moved(self, _old, _new):
        self._update_hints()

    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _escape(self):
        """Back, one step at a time: leave the details panel (focus returns
        to the game), then close it; leave the search; leave full screen;
        from other pages, go back to the library."""
        sheet = self._open_sheet()
        if sheet is not None:
            sheet.close_sheet()
            return
        focus = QApplication.focusWidget()
        in_details = focus is not None and self.details.isAncestorOf(focus)
        if self._theatre:
            self.theatre.back()               # Start / F11 leave Theatre, not B
            return
        if self.desk_search.isVisible():
            self.desk_search.close_search()
            return
        if self.current() == self.NEWS and self.newspage.reader_open():
            self.newspage.close_reader()
        elif self.current() == self.HOME and self.dashboard.editing:
            self.dashboard.set_editing(False)
        elif self.current() == self.INSTALL:
            if not self.go_back():
                self.go(self.HOME)
        elif self.current() == self.ARTWORK:
            self._art_back()
        elif self.search.hasFocus():
            if self.search.text():
                self.search.clear()
            else:
                self._focus_view()
        elif in_details and self.selected:
            self._focus_view(self.selected[0])
        elif self.details.isVisible():
            slug = self.selected[0] if self.selected else None
            self.clear_selection()
            self._focus_view(slug)
        elif self.current() == self.LIBRARY and self.search.text():
            self.search.clear()                 # a search first, then the page
            self._focus_view()
        elif self.current() in (self.SETTINGS, self.PROTON, self.SYSTEM) and self.go_back():
            self._focus_view()                  # side pages: back where you came from
        elif self.current() != self.HOME:
            self.go(self.HOME)
            self._focus_view()
        elif self.isFullScreen():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)

    def _unminimize(self):
        """Back from the taskbar as it was: showNormal() would also drop a
        maximised window to its old size."""
        if self._theatre:
            self.showFullScreen()
        else:
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
            self.show()

    # ---- controller / keyboard focus ----
    def nav_scope(self) -> QWidget:
        """Where the D-pad may go: an overlay on top keeps focus inside it
        (Theatre's game page covers tiles that are still "visible")."""
        sheet = self._open_sheet()
        if sheet is not None:
            return sheet
        if not self._theatre and self.desk_search.isVisible():
            return self.desk_search
        if self._theatre:
            if self.theatre.search.isVisible():
                return self.theatre.search
            return self.theatre.details if self.theatre.details.isVisible() else self.theatre
        return self

    def _open_sheet(self) -> 'ActionSheet | None':
        sheet = self.theatre_sheet if self._theatre else self.sheet
        return sheet if sheet.isVisible() else None

    def default_focus(self) -> QWidget | None:
        """Where focus lands when nothing in the window has it."""
        sheet = self._open_sheet()
        if sheet is not None:
            return sheet.default_focus()
        if not self._theatre and self.desk_search.isVisible():
            return self.desk_search.default_focus()
        if self._theatre:
            return self.theatre.default_focus()
        cur = self.current()
        if cur == self.HOME:
            hero = self.homeview.hero
            if not hero.isHidden() and hero.btn.isEnabled():
                return hero.btn
            return self._first_tile() or hero.btn_details
        if cur == self.LIBRARY:
            return self._first_tile() or self.search
        page = self.stack.currentWidget()
        for w in page.findChildren(QWidget):
            if w.isVisible() and w.isEnabled() and w.focusPolicy() & Qt.FocusPolicy.TabFocus \
                    and not isinstance(w, (QScrollBar, QAbstractScrollArea)):
                return w
        return self.icons.get(cur) or self.tabs.item(cur)

    def _first_tile(self, slug=None):
        view = self.homeview if self.current() == self.HOME else self.views.currentWidget()
        if isinstance(view, ListView):
            return view if view.rowCount() else None
        tiles = getattr(view, 'tiles', {})
        if slug and tiles.get(slug):
            return tiles[slug][0]
        # isHidden, not isVisible: tiles added a moment ago aren't "visible"
        # until the event loop shows them; replaced ones were hide()-ed.
        for w in view.findChildren(GameTile):
            if not w.isHidden() and not w.pending:
                return w
        return None

    def _focus_view(self, slug=None):
        t = self._first_tile(slug)
        if t is not None:
            focus_and_reveal(t)
            if isinstance(t, ListView) and slug:
                t.reveal(slug)

    def _tile_activated(self, game):
        """Enter / A on a game: open its page and put focus on Play, the way a
        console launcher does; a second press plays. (Settings → Controls can
        make it play straight away.)"""
        if self.prefs.controls['pad_a'] == 'play' and can_quick_play(game):
            self.on_game_action('launch', game)
            return
        self._open_on_play(game)

    def _tile_double(self, game):
        """A double click: plays (Settings → Controls can make it open the
        game's page instead). Something that can't play right now — running,
        busy, not installed — opens the page."""
        if self.prefs.controls['double_click'] == 'play' and can_quick_play(game):
            self.on_game_action('launch', game)
            return
        self._open_on_play(game)

    def _on_activity(self):
        if getattr(self, '_theatre', False) and self.prefs.controls['attract']:
            self._idle_timer.start(self.prefs.controls['attract'] * 60_000)

    def _maybe_attract(self):
        """Start the screensaver, unless a game runs or something's open."""
        if not self._theatre or self._playing or self._launching \
                or not self.isActiveWindow() or self.trailer_view.isVisible() \
                or self.theatre_sheet.isVisible() or self.attract.running:
            self._on_activity()                         # look again later
            return
        self.attract.start(self._games)

    def _apply_controls(self, controls: dict | None = None):
        """Settings → Controls, applied at once (a library preference)."""
        if controls is not None:
            self.prefs.controls = {**self.prefs.controls, **controls}
            self.prefs.save()
            if 'dedupe' in controls:
                mark_duplicates(self._games, self.prefs.controls['dedupe'])
                self._home_sig = None
                self.apply_filter()
        self.theatre.a_plays = self.prefs.controls['pad_a'] == 'play'
        if not self.prefs.controls['attract']:
            self._idle_timer.stop()
        else:
            self._on_activity()
        self.theatre.double_plays = self.prefs.controls['double_click'] == 'play'
        if hasattr(self, 'hints'):
            self._update_hints()

    def _open_on_play(self, game):
        self._tile_clicked(game, Qt.KeyboardModifier.NoModifier)
        QTimer.singleShot(0, lambda: alive(self.details.btn_play)
                          and self.details.btn_play.isVisible()
                          and self.details.btn_play.isEnabled()
                          and focus_and_reveal(self.details.btn_play))

    def _poll_battery(self):
        """Controller battery for the top bar and the hint bar (sysfs, cheap):
        every minute, and whenever a pad comes or goes."""
        from nexus import gamepad
        self._battery_timer.start(60_000)
        pads = gamepad.battery()
        self.topbar.set_battery(pads)
        b = pads[0] if pads else None
        self.hints.set_battery(f"Controller {b['percent']}%" + (" ⚡" if b['charging'] else '')
                               if b else '')
        low = b and b['percent'] < 15 and not b['charging']
        if low and not getattr(self, '_battery_warned', False):
            self.toast(f"Controller battery low ({b['percent']}%).", 'warning', None, 6000)
        self._battery_warned = bool(low)

    def _pad_connected(self, name, kind):
        self._battery_timer.start(1500)                 # sysfs appears just after
        self.hints.kind = kind
        self.input.set('pad')
        self._sync_hints()
        self.toast(f"{name} connected — D-pad to move, {GLYPHS.get(kind, GLYPHS['xbox'])['a']} "
                   f"to select.", 'info', None, 4000)
        if QApplication.focusWidget() is None:
            t = self.default_focus()
            t and focus_and_reveal(t)

    def _pad_disconnected(self, name):
        self._battery_timer.start(500)
        self._sync_hints()
        self.toast(f"{name} disconnected.", 'info', None, 3000)

    def _pad_used(self):
        if QApplication.focusWidget() is None or QApplication.focusWidget().window() is not self:
            t = self.default_focus()
            t and focus_and_reveal(t)

    def _nav_order(self):
        """LB / RB cycle the content tabs (the system pages are one press
        of B away, back on Home)."""
        return [p for p in self.TABS if not self.nav[p].isHidden()]

    def _pad_action(self, act):
        if self._theatre and act in ('page-prev', 'page-next', 'view'):
            self.theatre.cycle_tab(-1 if act == 'page-prev' else 1)
            return
        if self._theatre and act == 'search':
            if self.theatre.search.isVisible():
                self.theatre.search.type(' ')        # Y on the keyboard: a space
            elif not self.theatre.details.isVisible():
                self.theatre.open_search()
            self._update_hints()
            return
        if act in ('page-prev', 'page-next'):
            order = self._nav_order()
            cur = self.current()
            i = order.index(cur) if cur in order else 0
            self.go(order[(i + (1 if act == 'page-next' else -1)) % len(order)])
            QTimer.singleShot(50, lambda: alive(self) and focus_and_reveal(self.default_focus()))
        elif act == 'view' and self.current() == self.LIBRARY:
            views = ['home', 'grid', 'list']
            self.set_view(views[(views.index(self._view) + 1) % 3])
            QTimer.singleShot(50, self._focus_view)
        elif act == 'tv':
            self._toggle_tv()
        elif act == 'search':
            if self.desk_search.isVisible():
                self.desk_search.type(' ')           # Y on the keyboard: a space
            elif self._steam_up():
                self._focus_search()
                self._pad_action('keyboard')
            else:
                self.sheet.close_sheet(restore=False)
                self.desk_search.open([g for g in self._games if not g.get('hidden')])
            self._update_hints()
        elif act == 'keyboard':
            # Steam's on-screen keyboard, when Steam is running (Big Picture's).
            if self._steam_up():
                QProcess.startDetached('steam', ['steam://open/keyboard'])

    def _desk_search_picked(self, g):
        self.desk_search.setVisible(False)
        self._reveal_game(g['slug'])
        self._open_on_play(g)
        self._update_hints()

    def _desk_search_closed(self):
        self._update_hints()
        t = self.default_focus()
        if t is not None and self.input.mode != 'mouse':
            focus_and_reveal(t)

    def _steam_up(self) -> bool:
        """steam_running(), remembered for a few seconds (hints ask often)."""
        t, up = self._steam_seen
        if time.monotonic() - t > 10:
            up = steam_running()
            self._steam_seen = (time.monotonic(), up)
        return up

    def _toggle_tv(self):
        self.set_theatre(not self._theatre)

    def handle_remote(self, argv: list):
        """Another `nexus` launch handed us its arguments (see InstanceServer)."""
        if '--leave-theatre' in argv:
            self.set_theatre(False)
            return
        if '--theatre' in argv:
            self.set_theatre(True)
        self.bring_to_front()

    def bring_to_front(self):
        if self.isMinimized() or not self.isVisible():
            self._unminimize()
        self.raise_()
        self.activateWindow()
        # Wayland ignores activateWindow() from a background app; niri can do it.
        wid = winfocus.own_window(winfocus.windows(), pid=os.getpid())
        if wid is not None:
            winfocus.focus(wid)

    def set_theatre(self, on: bool):
        """Theatre mode: full screen, controller first, for the TV."""
        if on == self._theatre:
            return
        self._theatre = on
        if on:
            self.desk_search.close_search()
            self._was_state = self.windowState() & ~Qt.WindowState.WindowMinimized
            self.theatre.set_games([g for g in self._games if not g.get('hidden')])

            if self.config.get('news', True) and not self.newspage._loaded:
                self.newspage.refresh()           # the game page's News card (CLI-cached)
            self.shell.setCurrentWidget(self.theatre)
            self.showFullScreen()
            self.hints.setStyleSheet(build_stylesheet(THEATRE_SCALE))
            self.sheet.close_sheet(restore=False)
            self.focus_ring.scale = 1.5
            self.focus_ring.nudge()
            self.cursor_hider.start()
            self._on_activity()                   # the screensaver clock starts
        else:
            self._idle_timer.stop()
            self.attract.stop()
            self.cursor_hider.stop()
            self.theatre_sheet.close_sheet(restore=False)
            self.theatre.search.setVisible(False)
            self.theatre.details.setVisible(False)
            self.theatre.pages.setEnabled(True)
            self.theatre.tabs.setEnabled(True)
            self.shell.setCurrentWidget(self.desk)
            self.hints.setStyleSheet("")
            self.focus_ring.scale = 1.0
            was = self._was_state
            if not was & Qt.WindowState.WindowFullScreen:
                self.setWindowState(was)         # maximised stays maximised
        self._sync_hints()
        QTimer.singleShot(80, lambda: alive(self) and focus_and_reveal(self.default_focus()))

    # Actions that open a page of the desktop app: Theatre steps aside first,
    # or they'd happen invisibly behind it.
    DESK_ACTIONS = ('artwork', 'settings', 'view-log', 'uninstall')

    def _theatre_action(self, act, game):
        if act in self.DESK_ACTIONS:
            self.set_theatre(False)
        self.on_game_action(act, game)

    def _a_label(self, f) -> str:
        game = f.game if isinstance(f, GameTile) else {}
        return "Play" if self.prefs.controls['pad_a'] == 'play' and game \
            and can_quick_play(game) else "Open"

    def _sync_hints(self):
        """Button hints are for a controller: always in Theatre, and on the
        desktop only while a pad is what's being used."""
        ctrl = getattr(self, 'ctrl', None)
        show = self._theatre or (self.input.mode == 'pad' and bool(ctrl and ctrl.pads.pads))
        self.hints.setVisible(show)
        self._update_hints()

    def _update_hints(self):
        if not self.hints.isVisible():
            return
        f = QApplication.focusWidget()
        if self._open_sheet() is not None:
            self.hints.show_hints([('a', 'Select'), ('b', 'Close')])
            return
        if not self._theatre and self.desk_search.isVisible():
            self.hints.show_hints(([('a', 'Open')] if isinstance(f, GameTile) else
                                   [('a', 'Type')]) + [('x', 'Delete'), ('y', 'Space'),
                                                       ('b', 'Close')])
            return
        if self._theatre:
            if self.theatre.search.isVisible():
                hints = ([('a', 'Open')] if isinstance(f, GameTile) else [('a', 'Type')]) \
                    + [('x', 'Delete'), ('y', 'Space'), ('b', 'Close')]
                self.hints.show_hints(hints)
                return
            if self.theatre.details.isVisible():
                hints = [('a', 'Select'), ('x', 'Options'), ('b', 'Back')]
            elif isinstance(f, GameTile):
                hints = [('a', self._a_label(f)), ('x', 'Options'), ('b', 'Home')]
            else:
                hints = [('a', 'Select'), ('b', 'Home')]
            hints += [('y', 'Search'), ('LB RB', 'Home / Library'), ('Menu', 'Exit Theatre')]
            self.hints.show_hints(hints)
            return
        if isinstance(f, GameTile) or isinstance(f, ListView):
            hints = [('a', self._a_label(f)), ('x', 'Menu'), ('y', 'Search'), ('b', 'Back')]
        elif isinstance(f, DealCard):
            hints = [('a', f.primary_label), ('x', 'Links'), ('b', 'Back')]
        elif f is not None and self.details.isAncestorOf(f):
            hints = [('a', 'Select'), ('b', 'Back to game'), ('x', 'Menu')]
        elif isinstance(f, QLineEdit):
            hints = ([('a', 'Keyboard')] if not f.isReadOnly() and self._steam_up() else []) \
                + [('b', 'Back')]
        else:
            hints = [('a', 'Select'), ('b', 'Back')]
        hints += [('LB RB', 'Pages')]
        if self.current() == self.LIBRARY:
            hints.append(('View', 'Switch view'))
        hints.append(('Menu', 'Theatre mode'))
        self.hints.show_hints(hints)

    def _open_job(self, job):
        self.go(self.ACTIVITY)
        self.activity.focus_job(job)

    def _reveal_game(self, slug):
        """Show one game in the Library, selected. The search and filters
        stay as they are when the game is already in what they show."""
        self.go(self.LIBRARY)
        view = self._shown_view()
        if slug not in getattr(view, 'tiles', {}) and slug not in getattr(view, 'games', {}):
            self.search.clear()
            self.filter.setCurrentIndex(0)
            g = self.game(slug)
            if g is not None and self.prefs.show_owned and not is_installed(g) \
                    and self.prefs.owned_view == 'installed':
                self._set_owned_view('all')
            self._set_chip('all')
        self.selected = [slug]
        self._sync_selection()
        self._shown_view().reveal(slug)

    # ---- library ----
    # ---- collections ----
    def _collections_changed(self):
        """After a collection was made, renamed, deleted or a game moved in
        or out: game dicts, chips, Home, Theatre and the details panel."""
        COLLECTION_NAMES[:] = list(self.prefs.collections)
        for g in self._games:
            g['collections'] = self.prefs.collections_of(g['slug'])
        self._rebuild_collection_chips()
        if not self._chip.startswith('col:') or self._chip in self.chips:
            self.apply_filter()
        else:
            self._set_chip('all')               # its collection is gone
        if self._theatre:
            self.theatre.set_games([g for g in self._games if not g.get('hidden')])
        self._sync_selection()

    def ask_collection_name(self, title: str, initial: str, on_done):
        """A name: the on-screen keyboard for a controller (and in Theatre),
        else a text field in the sheet."""
        if self._theatre:
            self.theatre.search.open_entry(title, initial, on_done)
        elif self.input.mode == 'pad':
            self.desk_search.open_entry(title, initial, on_done)
        else:
            self.sheet.prompt(title, initial, "Save", on_done)
        self._update_hints()

    def _create_collection(self, cname, slug=None):
        made = self.prefs.add_collection(cname)
        if made is None:
            return
        if slug and slug not in self.prefs.collections[made]:
            self.prefs.toggle_member(made, slug)
        self._collections_changed()
        g = self.game(slug) if slug else None
        self.toast(f"Added {g['display_name']} to “{made}”." if g else
                   f"Made the collection “{made}”.", 'success', None, 2500)

    def _manage_collection(self, cname):
        sheet = self.theatre_sheet if self._theatre else self.sheet
        sheet.choose(cname, [
            ("Rename…", G['pencil'], lambda: self.ask_collection_name(
                "Rename collection", cname, lambda new: self._rename_collection(cname, new))),
            ("Delete collection", G['trash'], lambda: self.confirm(
                f"Delete “{cname}”?", "Its games stay in your library — only the "
                "collection goes.", "Delete collection",
                lambda: self._delete_collection(cname)))],
            sub=f"{len(self.prefs.collections.get(cname, []))} games")
        self._update_hints()

    def _rename_collection(self, old, new):
        made = self.prefs.rename_collection(old, new)
        if made is None:
            self.toast(f"There's already a collection called “{new}”.", 'warn', None, 3500)
            return
        if self._chip == f'col:{old}':
            self._chip = f'col:{made}'
        self._collections_changed()
        self._save_library_state()

    def _delete_collection(self, cname):
        self.prefs.delete_collection(cname)
        self._collections_changed()
        self.toast(f"Deleted the collection “{cname}”.", 'info', None, 2500)

    # ---- surprise me ----
    def surprise(self, pool=None):
        """Pick a game (from `pool`, else everything installed) and open its
        page with Play focused; the toast offers another pick."""
        import random
        games = pool if pool is not None else self._games
        g = surprise_pick(games, random, tuple(self._surprise_recent))
        if g is None:
            self.toast("Nothing to pick from here — every game shown is running, hidden "
                       "or not installed." if games else "Install a game first.", 'info',
                       None, 3500)
            return
        self._surprise_recent = [*self._surprise_recent, g['slug']][-3:]
        if self._theatre:
            self.theatre.open_game(g)
        else:
            if not (self._showing(self.HOME) or self._showing(self.LIBRARY)):
                self.go(self.HOME)
            self._open_on_play(g)
        self.toast(f"How about {g.get('display_name') or g['slug']}?", 'info',
                   ("Pick again", lambda p=pool: self.surprise(p)), 6000)

    def refresh_library(self):
        """The Refresh button / Ctrl+R: rescan, and retry art that failed."""
        ART.forget_failures()
        self.reload_library()

    def reload_library(self):
        """Fetch the library from the CLI in the background. One scan at a
        time: asking again while one is out queues exactly one more, so a
        late answer can't overwrite a newer one."""
        if self._lib_inflight:
            self._lib_again = True
            return
        self._lib_inflight = True
        self.btn_refresh.setEnabled(False)
        cli_json(self._library_args(), self._on_library)

    def _library_args(self):
        return ['--library', '--json'] + (['--owned'] if self.prefs.show_owned else [])

    # The last scan, painted at start-up while the real one runs: the window
    # shows the library at once instead of "Loading…". Games from it can't
    # be played until the fresh scan confirms them (g['stale']).
    LIB_CACHE = NX.CACHE_DIR / 'library.json'

    def _paint_cached_library(self):
        try:
            blob = json.loads(self.LIB_CACHE.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(blob, dict) or blob.get('args') != self._library_args() \
                or not isinstance((blob.get('data') or {}).get('games'), list):
            return
        self._on_library(blob['data'], '', cached=True)

    def _save_library_cache(self, data):
        tmp = self.LIB_CACHE.with_name(f'.{self.LIB_CACHE.name}.{os.getpid()}.tmp')
        try:
            self.LIB_CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({'args': self._library_args(), 'data': data}))
            os.replace(tmp, self.LIB_CACHE)
        except (OSError, TypeError, ValueError):
            with contextlib.suppress(OSError):   # a cache is never worth an error
                tmp.unlink(missing_ok=True)

    def _on_library(self, data, err, cached=False):
        if cached:
            if self._loaded:                 # the real scan already answered
                return
        else:
            self._lib_inflight = False
            if self._lib_again:
                self._lib_again = False
                self._lib_retry.start()
            else:
                self.btn_refresh.setEnabled(True)
            if data is not None:
                self._save_library_cache(data)
        if data is None:
            # Keep showing what we had: an empty library would also read as
            # "every game stopped" to the running-state poll.
            self._load_error = err or 'unknown error'
            if self._loaded:
                self.toast(f"Couldn't refresh the library — {self._load_error}", 'error',
                           ("Retry", self.reload_library), 10000)
        else:
            self._games, self._load_error = data.get('games', []), ''
            for g in self._games:
                g['stale'] = cached
            self._disk = data.get('disk')
            self._disks = data.get('disks') or ([self._disk] if self._disk else [])
            self._loaded = True
            self.newspage.set_games(self._games)
            self._busy = frozenset(g['slug'] for g in self._games if self.jobs.busy(g['slug']))
            self._mark_states()
            for g in self._games:
                g['install_requested'] = g['slug'] in self._install_req and not is_installed(g) \
                    and not g.get('installing')
                g['display_name'] = ' '.join((g.get('display_name') or g['slug']).split())
                ov = self.prefs.overrides.get(g['slug'])
                if ov:
                    g['store_name'] = g['display_name']
                    g['display_name'] = ov['name']
                g['favorite'] = g['slug'] in self.prefs.favorites
                g['notes'] = self.prefs.notes.get(g['slug'], '')
                g['hidden'] = g['slug'] in self.prefs.hidden
                g['collections'] = self.prefs.collections_of(g['slug'])
                # Repacks record when Nexus installed them; Steam games when
                # their manifest was written — one "added" date for both.
                g['added_at'] = g.get('added_at') or g.get('installed_at')
                if g['slug'] in self._repack_updates and g.get('source', 'nexus') == 'nexus':
                    g['repack_update'] = self._repack_updates[g['slug']]
                    g['update_pending'] = True
                if g.get('repack_online'):             # FitGirl posted a newer one
                    g['update_pending'] = True
            mark_duplicates(self._games, self.prefs.controls['dedupe'])
            for g in [] if cached else self._games:
                if g['slug'] in self._watch_finish and is_installed(g):
                    self._install_req.pop(g['slug'], None)
                    self.toast(f"{g['display_name']} is installed — ready to play.", 'success',
                               ("Play", lambda g=g: self.on_game_action('launch', g)), 8000)
            if not cached:
                self._watch_finish = set()
            if not self._steam_resumed and not cached:
                self._steam_resumed = True
                self._resume_steam_pending()
        self._sync_system()
        self.apply_filter()
        self._sync_selection()
        if cached:
            return
        self._load_protondb()
        self._watch_installs()
        if data is not None and not self._feed_timer.isActive():
            self._feed_timer.start(8000)            # first check soon, then every 6 h
        if data is not None and not getattr(self, '_art_filled', False) \
                and self.config.get('api_key'):
            # Once a day, quietly fill missing hero/logo/banner art (the CLI
            # skips it when it already ran today); reload only if it found any.
            self._art_filled = True
            QTimer.singleShot(6000, lambda: alive(self) and cli_json(
                ['--fill-art', '--if-stale', '--json'],
                lambda d, _e: (d or {}).get('filled') and alive(self) and self.reload_library(),
                600000))
        if not self._deals_warmed and self.config.get('gamedb_enabled'):
            # One cached call (45 min): keeps the sidebar badge and the details
            # panel's deal line warm without opening the Deals page.
            self._deals_warmed = True
            QTimer.singleShot(2500, self.dealspage.refresh)
        if self._showing(self.STORAGE):
            self.storage.set_library([g for g in self._games if is_installed(g)], self._disks)

    def _poll_running(self):
        # Keep polling while minimised if a game is starting or running: that's
        # how Nexus notices it has finished (and comes back).
        if (not self.isVisible() or self.isMinimized()) \
                and not (self._launching or self._playing):
            return
        now = time.monotonic()
        expired = [s for s, t0 in self._launching.items() if now - t0 > self.LAUNCH_WAIT]
        if expired:                                  # never showed up as running
            self._launch_expired(expired)

        if self._running_query:
            return                                   # the last one is still out
        # A /proc read on the thread pool: no subprocess, nothing re-scanned.
        keys = ('slug', 'source', 'exe', 'steam_appid', 'appid', 'install_dir')
        games = [{k: g.get(k) for k in keys} for g in self._games if is_installed(g)]
        self._running_query = True
        in_thread(lambda: match_running(games, steamlib.running()), self._on_running)

    def _on_running(self, playing, error):
        self._running_query = False
        if error is None and playing is not None:
            self._apply_running(set(playing))

    def _apply_running(self, playing: set):
        """A --running result: flip Starting… to Playing, notice games that
        closed (and come back to Nexus), refresh what changed."""
        started = playing - self._playing
        stopped = self._playing - playing
        now = datetime.now()
        for slug in started:
            self._launching.pop(slug, None)
            self._session_start[slug] = now
        for slug in stopped:
            t0 = self._session_start.pop(slug, None)
            g = self.game(slug)
            # Repacks are recorded by their launch tracker; the rest here.
            if t0 and g and (g.get('source', 'nexus') != 'nexus'
                             or g.get('launch_via') == 'steam'):
                history.record(slug, g['source'], t0, now, g.get('display_name'))
        if not self._launching:
            self._fast_timer.stop()
        changed = playing != self._playing or started
        self._playing = playing
        self._mark_states()
        for slug in stopped:
            self._game_stopped(slug)
        if started and not winfocus.available() and self._theatre:
            self.showMinimized()                 # no compositor help: step aside
        if changed or stopped:
            try:
                self.apply_filter()
                if self.selected:
                    self._sync_selection()
            except RuntimeError:
                pass

    def _launch_expired(self, slugs):
        for slug in slugs:
            self._launching.pop(slug, None)
            g = next((x for x in self._games if x['slug'] == slug), None)
            if g is None:
                continue
            name = g.get('display_name', slug)
            action = ("Show log", lambda g=g: self.on_game_action('view-log', g)) \
                if g.get('source', 'nexus') == 'nexus' else None
            self.toast(f"{name} doesn't seem to have started.", 'warn', action, 10000)
        if not self._launching:
            self._fast_timer.stop()
        self._mark_states()
        self._home_sig = None
        self.apply_filter()
        if self.selected:
            self._sync_selection()

    def _mark_states(self):
        for g in self._games:
            g['playing'] = g['slug'] in self._playing
            g['launching'] = g['slug'] in self._launching and not g['playing']
            g['busy'] = g['slug'] in self._busy

    # ---- installs through Steam / Heroic ----
    def _resume_install(self, game):
        """Run an unfinished install again with the arguments it started with
        (the CLI recorded them); it reuses what's already in the prefix."""
        slug, name = game['slug'], game.get('display_name', game['slug'])
        if any(j.active and j.kind == 'install' and j.slug == slug for j in self.jobs.jobs):
            self.toast(f"{name} is already installing.", 'info', None, 3000)
            return
        argv = list(game['incomplete'].get('argv') or [])
        if not argv:
            self.toast(f"Nexus doesn't know how {name} was started — install it again from "
                       f"the Install page.", 'warn')
            return
        for flag in ('--yes', '--log', '--defer-steam'):
            if flag not in argv:
                argv.append(flag)
        game['resuming'] = True
        self._start_install([CLI_BIN, *argv], name, slug)

    def _request_install(self, game):
        """Install pressed on a game you own: Steam's install dialog (or
        Heroic's prompt) opens; Nexus then follows the download."""
        slug, name = game['slug'], game.get('display_name', game['slug'])
        store = "Steam" if game.get('source') == 'steam' else "Heroic"
        self._install_req[slug] = time.monotonic()
        game['install_requested'] = True
        self.run(['--open-url', game['install_url']], f"Installing {name} with {store}",
                 slug, 'launch')
        self.toast(f"{store} will ask where to install {name} — Nexus follows the download.",
                   'info', None, 5000)
        self._home_sig = None
        self.apply_filter()
        self._game_changed(slug)
        self._watch_installs()

    def _watch_installs(self):
        """Poll while something is downloading or was just asked for."""
        now = time.monotonic()
        for slug, t0 in list(self._install_req.items()):
            if now - t0 > 600:                         # the dialog was closed, most likely
                del self._install_req[slug]
        busy = self._install_req or any(g.get('installing') for g in self._games)
        if busy and not self._install_timer.isActive():
            self._install_timer.start()
        elif not busy:
            self._install_timer.stop()

    def _install_tick(self):
        """While Steam/Heroic download something: read just their manifests
        and queue (in-process, on the thread pool). Progress repaints the
        tile; a download starting, finishing or vanishing reloads the
        library once."""
        if self._install_query:
            return
        self._install_query = True
        cfg = steamlib.find_heroic_config()
        in_thread(lambda: steamlib.downloads(NX.STEAM_ROOT, cfg), self._on_downloads)

    def _on_downloads(self, d, error):
        self._install_query = False
        if error is not None or d is None:
            return
        steam, done = d.get('steam') or {}, set(d.get('steam_installed') or [])
        queued = set(d.get('heroic') or [])
        changed = False
        for g in self._games:
            if is_installed(g):
                continue
            if g.get('source') == 'steam':
                new = steam.get(g.get('appid'))
                if g.get('appid') in done or bool(new) != bool(g.get('installing')):
                    changed = True
                elif new and new != g.get('installing'):
                    g['installing'] = new                # just progress: repaint in place
                    self._game_changed(g['slug'])
            elif g.get('source') == 'heroic':
                if (g.get('app_name') in queued) != bool(g.get('installing')):
                    changed = True
        if changed:
            # Whatever was downloading (or asked for) and is installed after
            # the reload gets a "ready to play" toast (see _on_library).
            self._watch_finish = {g['slug'] for g in self._games if not is_installed(g)
                                  and (g.get('installing') or g['slug'] in self._install_req)}
            self._home_sig = None
            self.reload_library()
            return
        self._watch_installs()

    def _game_changed(self, slug):
        """One game's state moved (download progress): repaint just it."""
        for t in self.findChildren(GameTile):
            if t.game.get('slug') == slug:
                t.update()
        g = next((x for x in self._games if x['slug'] == slug), None)
        if g is None:
            return
        if self.details.game.get('slug') == slug:
            self.details.btn_play.setText(play_label(g))
            self.details.btn_play.setEnabled(play_enabled(g))
        td = self.theatre.details
        if td.isVisible() and td.game.get('slug') == slug:
            td.btn_play.setText(play_label(g))
            td.btn_play.setEnabled(play_enabled(g))

    def _begin_launch(self, game):
        """Play pressed: show "Starting…" right away, poll until the game is
        really running, and get ready to give its window the focus."""
        slug = game['slug']
        self._launching[slug] = time.monotonic()
        self._mark_states()
        game['launching'] = True
        wins = winfocus.windows()
        self._handoff = {'game': game, 'before': {w.get('id') for w in wins},
                         'since': time.monotonic(),
                         'nexus': winfocus.own_window(wins, pid=os.getpid())}
        if winfocus.available():
            self._handoff_timer.start()
        self._fast_timer.start()
        QTimer.singleShot(1200, self._poll_running)
        self._home_sig = None
        self.apply_filter()
        if self.selected:
            self._sync_selection()

    def _handoff_tick(self):
        h = self._handoff
        if not h or time.monotonic() - h['since'] > 120:
            self._handoff_timer.stop()
            self._handoff = None
            return
        if self._handoff_query:
            return
        # `niri msg` on the thread pool: a busy compositor (it's starting a
        # game, after all) must not stall the window every 800 ms.
        self._handoff_query = True
        game, before = dict(h['game']), set(h['before'])
        in_thread(lambda: winfocus.pick_game_window(winfocus.windows(), before, game),
                  lambda wid, _err, h=h: self._handoff_found(h, wid))

    def _handoff_found(self, h, wid):
        self._handoff_query = False
        if wid is None or h is not self._handoff:
            return
        in_thread(lambda: winfocus.focus(wid), lambda *_: None)
        if h.get('nexus') is not None:
            self._return_to[h['game']['slug']] = h['nexus']
        self._handoff_timer.stop()
        self._handoff = None

    def _game_stopped(self, slug):
        """The game closed: come back to Nexus (where focus was handed off)."""
        nexus = self._return_to.pop(slug, None)
        if nexus is not None:
            winfocus.focus(nexus)
        elif self.isMinimized():
            self._unminimize()
        QTimer.singleShot(400, lambda: alive(self) and focus_and_reveal(self.default_focus()))

    def _load_protondb(self):
        """ProtonDB tiers for Steam games (cached a day by the CLI), painted on
        tiles when they arrive — never blocks the library."""
        known = self._protondb               # appid -> tier ('' = ProtonDB has none)
        for g in self._games:
            if known.get(str(g.get('appid'))):
                g['protondb'] = known[str(g.get('appid'))]
        ids = [str(g['appid']) for g in self._games if g.get('source') == 'steam'
               and is_installed(g) and str(g['appid']) not in known]
        if not ids:
            return

        def done(data, _err):
            if data is None:
                return
            for i in ids:
                known[i] = (data.get(i) or {}).get('tier') or ''
            for g in self._games:
                if known.get(str(g.get('appid'))):
                    g['protondb'] = known[str(g.get('appid'))]
            try:
                view = self._shown_view()
                for tiles in view.tiles.values() if hasattr(view, 'tiles') else ():
                    for t in tiles:
                        t.update()
            except RuntimeError:
                pass
        cli_json(['--protondb', *ids, '--json'], done, 60000)

    def _sync_system(self):
        """The System icon: a ring for how full the install disk is, one
        badge (running jobs, else health problems), section badges, and a
        tooltip that sums it up. Also refreshes the Home System widget."""
        d, icon = self._disk, self.icons[self.SYSTEM]
        n = len(self.jobs.active())
        problems, fails = self._health_problems
        parts = []
        if n:
            parts.append(f"{n} running")
        if not d or not d.get('total'):
            icon.usage = None
        else:
            icon.usage = d['used'] / d['total']
            parts.append(f"{fmt_size(d['free'])} free of {fmt_size(d['total'])} on {d['path']}")
        if problems:
            parts.append(f"{problems} health issue{'s' if problems != 1 else ''}")
        icon.setToolTip("System (Ctrl+5)" + (" — " + " · ".join(parts) if parts else ""))
        icon.set_badge(*system_badge(n, problems, fails))
        self.system.set_badge('activity', str(n) if n else '', 'accent')
        self.system.set_badge('health', str(problems) if problems else '',
                              'danger' if fails else 'warning')
        icon.update()
        self.dash_update('system')

    def apply_filter(self):
        if self._loaded and not self._showing(self.LIBRARY) and not self._theatre:
            # Off screen, the Library's views wait: a game starting or a job
            # finishing only rebuilds what's visible (Home); go(LIBRARY)
            # catches up.
            self._filter_dirty = True
            pending = [j for j in self.jobs.jobs if j.kind == 'install' and j.active]
            self._pending_ids = tuple(id(j) for j in pending)
            empty = (f"Couldn't load the library — {self._load_error}"
                     if self._load_error and not self._games else None)
            self._refresh_home(empty, pending)
            return
        self._filter_dirty = False
        q = self.search.text().strip().lower()
        status = self.filter.currentData()
        keep = {
            'all': lambda g: not g.get('hidden'),
            'hidden': lambda g: g.get('hidden'),
            'update': lambda g: g.get('update_pending') and not g.get('hidden'),
            'options': lambda g: g.get('launch_options') and not g.get('hidden'),
            'no-steam': lambda g: g.get('source', 'nexus') == 'nexus' and not g.get('steam')
            and not g.get('hidden'),
            'no-heroic': lambda g: g.get('source', 'nexus') == 'nexus' and not g.get('heroic')
            and not g.get('hidden'),
        }[status]
        own = self.prefs.owned_view if self.prefs.show_owned else 'installed'
        in_own = {'installed': is_installed, 'owned': lambda g: not is_installed(g),
                  'all': lambda g: True}[own]
        chip = self._chip
        in_chip = (lambda g: True) if chip == 'all' else \
            (lambda g: g.get('favorite')) if chip == 'favorite' else \
            (lambda g, n=chip[4:]: n in (g.get('collections') or ())) \
            if chip.startswith('col:') else \
            (lambda g: g.get('source', 'nexus') == chip)
        # Every word of the query, punctuation aside: "witcher 3" finds
        # "The Witcher® 3: Wild Hunt" (Theatre's search matches the same way).
        words = _search_norm(q).split()

        def found(g):
            name = _search_norm(g.get('display_name') or '') + ' ' + g.get('slug', '')
            return all(w in name for w in words)
        games = [g for g in self._games if keep(g) and in_chip(g) and in_own(g)
                 and (not words or found(g)) and not (g.get('dup_of') and status != 'hidden')]
        self._shown_games = games                  # what "Surprise me" picks from here
        # A search or status filter reads better as a grid than as shelves.
        if self._view == 'home' and (q or status != 'all' or own != 'installed'):
            self.views.setCurrentWidget(self.library)
            target = self.library
        else:
            self.views.setCurrentWidget(self.view)
            target = self.view
        self.sort.setVisible(target is not self._views['home'])
        key = self.sort.currentData()
        name = lambda g: (g.get('display_name') or g.get('slug', '')).lower()
        games.sort(key=name)
        if key == 'played':
            games.sort(key=lambda g: g.get('last_played') or '', reverse=True)
        elif key == 'most':
            games.sort(key=lambda g: g.get('playtime_minutes') or 0, reverse=True)
        elif key == 'added':
            games.sort(key=lambda g: g.get('added_at') or '', reverse=True)
        elif key == 'size':
            games.sort(key=lambda g: g.get('disk_bytes') or 0, reverse=True)
        if self._load_error and not self._games:
            empty = f"Couldn't load the library — {self._load_error}"
        elif not self._loaded:
            empty = "Loading your library…"
        elif self._games and not games:
            empty = "No games match the search or filter."
        else:
            empty = None
        pending = [j for j in self.jobs.jobs if j.kind == 'install' and j.active]
        self._pending_ids = tuple(id(j) for j in pending)
        hidden = status == 'hidden'
        n, total = len(games), len([g for g in self._games if bool(g.get('hidden')) == hidden
                                    and in_own(g)])
        self.count_label.setText(f"{n} of {total}" if n != total else
                                 f"{n} game{'s' if n != 1 else ''}")
        if isinstance(target, HomeView):            # shelves are rebuilt: keep the place
            with KeepPlace(target):
                target.set_games(games, empty, pending)
        else:
            target.set_games(games, empty, pending)
        target.set_jobs(self.jobs)
        target.mark_selected(set(self.selected))
        self._refresh_home(empty, pending)

    def _refresh_home(self, empty=None, pending=()):
        """Home shows the whole library, so it only rebuilds when the library
        (or a playing/favourite/job state) changes — not on every search key."""
        everything = [g for g in self._games if not g.get('hidden')]
        visible = [g for g in everything if is_installed(g)]
        struct = (self._loaded, self._load_error, tuple(id(j) for j in pending),
                  tuple(COLLECTION_NAMES),
                  tuple((g['slug'], g.get('favorite'), tuple(g.get('collections') or ()),
                         g.get('last_played'), is_installed(g),
                         bool(g.get('installing')), g.get('install_requested'),
                         g.get('update_pending'), g.get('protondb')) for g in everything))
        live = tuple((g['slug'], g.get('playing'), g.get('launching')) for g in everything)
        sig = (struct, live)
        if sig == self._home_sig:
            return
        if self._home_sig is not None and self._home_sig[0] == struct:
            # Only "playing" / "starting" moved (a game launched or quit): the
            # tiles show it on their next paint and the hero re-picks; nothing
            # is rebuilt (Theatre included — its rows don't depend on it).
            self._home_sig = sig
            views = (self.homeview, self.theatre) if self._theatre else (self.homeview,)
            for view in views:
                for t in view.findChildren(GameTile):
                    t._sync_buttons()
                    t.update()
                view.hero.set_games(featured_games(visible))
            return
        self._home_sig = sig
        if self._theatre:
            with KeepPlace(self.theatre):
                self.theatre.set_games(everything)
        with KeepPlace(self.homeview):
            self.homeview.set_games(visible, empty if not visible else None, pending)
            self.dashboard.refresh()
        self.homeview.set_jobs(self.jobs)
        self.homeview.mark_selected(set(self.selected))

    def dash_update(self, *kinds):
        """Coalesce dashboard refreshes (job output can arrive many times a
        second)."""
        self._dash_pending.update(kinds)
        self._dash_timer.start()

    def _flush_dashboard(self):
        kinds, self._dash_pending = self._dash_pending, set()
        try:
            self.dashboard.refresh(kinds)
        except RuntimeError:
            pass

    def _selection_changed(self, games):
        if not games or self.current() not in (self.HOME, self.LIBRARY):
            self.details.setVisible(False)
            return
        self.details.show_games(games)
        if not self.details.isVisible():
            self.details.setVisible(True)
            total = self.split.width()
            want = self.prefs.library_state.get('details_w') or 380
            w = max(320, min(int(want), total - 300))
            self.split.setSizes([max(total - w, 300), w])

    def _show_menu(self, game, pos):
        """The game menu: the desktop QMenu for mouse and keyboard; for a
        controller (and anything in Theatre) the ActionSheet instead."""
        if self._theatre:
            self.theatre_sheet.open(game, game_actions(game, theatre=True), theatre=True)
        elif self.input.mode == 'pad':
            self.sheet.open(game, game_actions(game))
        else:
            m = build_game_menu(self, game, self.on_game_action)
            m.exec(pos if not pos.isNull() else QCursor.pos())
            m.deleteLater()                      # its closures hold the game dict
            return
        self._update_hints()

    # ---- actions ----
    def run(self, args, title, slug=None, kind='task', steam=False, name=None, on_skip=None):
        """Queue a CLI job. With steam=True it waits (via the banner) until
        Steam is closed, since Steam overwrites its files when it exits."""
        if kind != 'launch' and (self.jobs.find([CLI_BIN, *args]) or self.gate.waiting(title)):
            self.toast(f"“{title}” is already queued.", 'info', None, 3000)
            return

        def submit():
            if not self.jobs.find([CLI_BIN, *args]):
                self.jobs.submit(Job([CLI_BIN, *args], title, slug, kind, name))
        if steam:
            self.gate.run(title, submit, on_skip)
            if self._theatre and self.gate.pending:
                self.toast(f"Steam is running — “{title}” continues once Steam is closed.",
                           'warn', None, 6000)
        else:
            submit()

    def _on_request(self, args, title, steam, kind):
        if args and args[0] == '__action__':
            self.on_game_action(args[1], self.details.game)
            return
        slugs = {g['slug'] for g in self._games}
        slug = next((a for a in args[1:] if a in slugs), None)
        self.run(args, title, slug, kind, steam)

    def game(self, slug) -> dict | None:
        return next((g for g in self._games if g['slug'] == slug), None)

    def on_game_action(self, action, game):
        slug, name = game['slug'], game.get('display_name', game['slug'])
        src = game.get('source', 'nexus')
        live = self.game(slug) or game              # the panel's copy can be stale
        if action in NOT_WHILE_PLAYING and (live.get('playing') or live.get('launching')):
            self.toast(f"{name} is running — close it first.", 'warn', None, 4000)
            return
        if action in NOT_WHILE_BUSY and self.jobs.busy(slug):
            self.toast(f"{name} is busy — wait for its current job to finish.", 'warn',
                       None, 4000)
            return
        if action == 'restore-saves' and not getattr(self, '_confirmed', False):
            self.confirm(f"Restore {name}'s saves?",
                         "Copies the newest save backup over the saves in its prefix. The "
                         "current saves are backed up first, so this can be undone.",
                         "Restore saves", lambda: self._confirmed_action(action, game))
            return
        appid = game.get('appid')
        steam_urls = {'steam-page': 'steam://nav/games/details/{}',
                      'steam-props': 'steam://gameproperties/{}',
                      'steam-verify': 'steam://validate/{}',
                      'steam-uninstall': 'steam://uninstall/{}'}
        if action.startswith('col:'):
            cname = action[4:]
            on = self.prefs.toggle_member(cname, slug)
            self._collections_changed()
            self.toast(f"{name} {'added to' if on else 'removed from'} “{cname}”.",
                       'success', None, 2500)
        elif action == 'col-new':
            self.ask_collection_name("New collection", "",
                                     lambda n: self._create_collection(n, slug))
        elif action in ('favorite', 'hide'):
            on = self.prefs.toggle(action, slug)
            for g in self._games:
                if g['slug'] == slug:
                    g['favorite' if action == 'favorite' else 'hidden'] = on
            self.apply_filter()
            if slug in self.selected:
                self._sync_selection()            # the star in the details panel
            self.toast(f"{name} {'added to' if on else 'removed from'} "
                       f"{'favourites' if action == 'favorite' else 'hidden games'}.",
                       'success', None, 2500)
        elif action == 'launch' and game.get('incomplete'):
            self._resume_install(game)
        elif action == 'discard-incomplete':
            self.run(['--discard-incomplete', slug], f"Discarding the unfinished install "
                     f"of {name}", slug)
        elif action == 'install-update' and game.get('repack_update'):
            self._start_install([CLI_BIN, game['repack_update'], '--yes', '--log',
                                 '--defer-steam'], name, slug)
        elif action == 'launch' and not play_enabled(game):
            return                                  # already starting / running
        elif action == 'launch' and not is_installed(game):
            self._request_install(game)
        elif action == 'launch' and src != 'nexus':
            self._begin_launch(game)
            self.run(['--open-url', game['launcher']], f"Launching {name}", slug, 'launch')
        elif action in steam_urls and appid:
            self.run(['--open-url', steam_urls[action].format(appid)],
                     f"Opening {name} in Steam", slug, 'launch')
        elif action == 'rename-local':
            def done(new, slug=slug):
                new = ' '.join(new.split())[:MAX_TITLE]
                if new:
                    self.prefs.overrides[slug] = {'name': new}
                    self.prefs.save()
                    self.reload_library()
            if self._theatre:
                self.theatre.search.open_entry("Name in your library", name, done)
            elif self.input.mode == 'pad':
                self.desk_search.open_entry("Name in your library", name, done)
            else:
                self.sheet.prompt("Name in your library", name, "Save", done,
                                  "Only Nexus shows this name.", max_len=MAX_TITLE)
            self._update_hints()
        elif action == 'rename-reset':
            if self.prefs.overrides.pop(slug, None) is not None:
                self.prefs.save()
                self.reload_library()
        elif action == 'repack-page':
            url = (game.get('repack_online') or {}).get('url') or ''
            if url.startswith('https://fitgirl-repacks.site/'):   # the pinned site only
                QDesktopServices.openUrl(QUrl(url))
        elif action in ('add-sunshine', 'remove-sunshine'):
            add = action == 'add-sunshine'
            self.run(['--sunshine' if add else '--remove-sunshine', slug],
                     f"{'Adding' if add else 'Removing'} {name} "
                     f"{'to' if add else 'from'} Sunshine", slug, 'task')
        elif action == 'protongen':
            self._open_protongen(game)
        elif action == 'artwork':
            self.open_artwork(game)
        elif action == 'open-folder' and src != 'nexus':
            if game.get('install_dir') and Path(game['install_dir']).is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(game['install_dir']))
        elif src != 'nexus':
            return                                  # Nexus-only action
        elif action == 'launch':
            self._begin_launch(game)
            self.run(['--launch', slug], f"Launching {name}", slug, 'launch')
        elif action == 'open-folder':
            if game.get('exe') and Path(game['exe']).parent.is_dir():
                target = Path(game['exe']).parent
            elif game.get('wine_prefix'):
                target = Path(game['wine_prefix'])
            elif game.get('launcher'):
                target = Path(game['launcher']).parent
            else:
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        elif action == 'pause-toggle':
            self.run(['--pause-toggle', slug], f"Pausing / resuming {name}", slug, 'tool')
        elif action == 'benchmark':
            self._begin_launch(game)
            self.run(['--benchmark', slug], f"Benchmarking {name}", slug, 'launch')
            self.toast("Logging starts 30 s after launch and runs 60 s (Shift+F2 starts it "
                       "by hand). Results appear in the game's Log section.", 'info', None, 8000)
        elif action == 'settings':
            self._reveal_game(slug)
            self.details.open_section('launch')
        elif action == 'view-log':
            self._reveal_game(slug)
            self.details.open_section('log')
        elif action.startswith('tool:'):
            tool = action.split(':', 1)[1]
            self.run(['--prefix-tool', slug, tool],
                     "Stopping" if tool == 'kill' else tool, slug, 'tool')
        elif action == 'uninstall':
            self._reveal_game(slug)
            self.details.ask_uninstall()
        else:
            spec = {
                'add-steam': (['--steam', slug], f"Adding {name} to Steam", True),
                'remove-steam': (['--remove-steam', slug], f"Removing {name} from Steam", True),
                'add-heroic': (['--heroic', slug], f"Adding {name} to Heroic", False),
                'remove-heroic': (['--remove-heroic', slug], f"Removing {name} from Heroic", False),
                'refetch-art': (['--refetch-art', slug], f"Fetching artwork for {name}", False),
                'restore-saves': (['--restore-saves', slug], f"Restoring saves for {name}", False),
                'check-integrity': (['--check-integrity', slug], f"Checking {name}'s files", False),
                'pg-default': (['--protongen-default', slug],
                               f"Applying protongen defaults to {name}", bool(game.get('steam'))),
            }.get(action)
            if spec:
                self.run(spec[0], spec[1], slug, 'task', spec[2])

    def _confirmed_action(self, action, game):
        self._confirmed = True
        try:
            self.on_game_action(action, game)
        finally:
            self._confirmed = False

    def confirm(self, title, text, yes, on_yes, danger=True):
        """Ask before something that can't simply be clicked back: the
        action sheet as a two-row question, for mouse, keyboard and pad
        alike (and in Theatre)."""
        sheet = self.theatre_sheet if self._theatre else self.sheet
        sheet.ask(title, text, yes, on_yes, danger)
        self._update_hints()

    def open_artwork(self, game, kind='hero'):
        cur = self.current()
        self._art_from = cur if cur in (self.HOME, self.LIBRARY) else self.HOME
        self._art_slug = game.get('slug')
        self.artpage.open(game, kind)
        self.go(self.ARTWORK)

    def _art_back(self):
        self.go(self._art_from)
        slug = getattr(self, '_art_slug', None)
        if slug:
            self.selected = [slug]
            self._sync_selection()

    def _queue_appearance(self, ap: dict):
        self._ap_pending = ap
        self._ap_timer.start()

    def _flush_appearance(self):
        ap, self._ap_pending = self._ap_pending, None
        if ap is not None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.set_appearance(ap)
            finally:
                QApplication.restoreOverrideCursor()

    def set_appearance(self, ap: dict):
        """Apply a new appearance live: palette, stylesheet, icons, painted
        widgets, then rebuild the game views so inline-styled bits follow."""
        self.prefs.appearance = apply_appearance(ap)
        self.prefs.save()
        app = QApplication.instance()
        app.setStyleSheet(build_stylesheet())
        self.theatre.setStyleSheet(build_stylesheet(THEATRE_SCALE))
        if self._theatre:
            self.hints.setStyleSheet(build_stylesheet(THEATRE_SCALE))
        refresh_icons()
        for hero in (self.homeview.hero, self.theatre.hero):
            hero._frames.clear()
            if hero.games:
                hero._show(animate=False)
        self.background.set_tint(None)
        self._home_sig = None
        self.apply_filter()
        self.dashboard.refresh()
        if self.selected:
            self._sync_selection()
        for w in app.allWidgets():
            w.update()

    def _open_protongen(self, game):
        """Open protongen on this game (`protongen --game <id>`: the Steam
        appid, or protongen's hash of the Heroic app name)."""
        pg = shutil.which('protongen') or str(HOME / '.local/bin/protongen')
        gid = game.get('protongen_id') or game.get('appid')
        args = ['--game', str(gid)] if gid else []
        QApplication.clipboard().setText(game.get('display_name') or game['slug'])
        if QProcess.startDetached(pg, args):
            self.toast(f"Opened protongen on {game.get('display_name')}.", 'info', None, 3000)
        else:
            self.toast(f"Couldn't start {pg}.", 'error')

    def _start_install(self, cmd, name, slug=''):
        """Installs start at once: with --defer-steam the CLI leaves the Steam
        shortcut for later if Steam is running (see _install_followups)."""
        src = cmd[1] if len(cmd) > 1 else None
        if any(j.active and j.kind == 'install' and ((slug and j.slug == slug)
                                                      or (src and j.argv[1:2] == [src]))
               for j in self.jobs.jobs):
            self.toast(f"{name} is already being installed.", 'info', None, 3000)
            return
        self.jobs.submit(Job(cmd, f"Installing {name}", slug or None, 'install', name))
        # From the Install page, on to the library to watch it; started from a
        # toast or a menu, stay where you are (the bottom strip shows it).
        if self.current() == self.INSTALL:
            self.go(self.LIBRARY)

    def _install_followups(self, job):
        """What an install job's events ask for once it ends: the success
        toast (with a way to change the picked .exe and to bin the repack),
        a deferred Steam shortcut, or the right kind of retry."""
        ev, name = job.events, job.name
        if job.state == 'done':
            inst = ev.get('installed') or {}
            slug = inst.get('slug') or job.slug
            picked = ev.get('exe-candidates') or {}
            cands = picked.get('candidates') or []
            actions = []
            if len(cands) > 1:
                actions.append(("Change game file", lambda: self._open_job(job)))
            src = inst.get('source')
            if src and not inst.get('repack_trashed'):
                actions.append(("Trash repack", lambda: self.run(
                    ['--trash-repack-dir', src], f"Moving the {name} repack to the Trash")))
            if slug:
                actions.insert(0, ("Play", lambda: (g := self.game(slug)) is not None
                                   and self.on_game_action('launch', g)))
            text = f"{name} is installed"
            if len(cands) > 1 and picked.get('chosen'):
                text += f" — it starts {Path(picked['chosen']).name}"
            self.toast(text, 'success', actions or None, 12000)
            if not self.isActiveWindow():
                notify_desktop(f"{name} is installed", "Ready to play.")
            if inst.get('steam_pending') and slug:
                self._add_to_steam_later(slug, name)
        elif job.state == 'failed':
            argv = [a for a in job.argv if a not in ('--interactive', '--unattended')]
            log = ("Show log", lambda: self._open_job(job))
            if 'retry-interactive' in ev:
                actions = [("Retry with setup window", lambda: self._resubmit(
                    job, [*argv, '--interactive'])), log]
            else:
                actions = [("Retry", lambda: self._resubmit(job, job.argv))]
                if '--verify' not in job.argv:
                    actions.append(("Verify & retry", lambda: self._resubmit(
                        job, [*job.argv, '--verify'])))
                actions.append(log)
            self.toast(f"{job.title} — failed", 'error', actions, 15000)
            if not self.isActiveWindow():
                notify_desktop(f"{job.title} failed", job.detail or "Open Nexus for the log.")

    def _resubmit(self, job, argv):
        self.jobs.submit(Job(list(argv), job.title, job.slug, job.kind, job.name))

    STEAM_PENDING = DATA_DIR / 'steam-pending.json'

    def _steam_pending(self, slug=None, name=None, add=True) -> list[dict]:
        """Shortcuts still owed to Steam, on disk: quitting Nexus before
        Steam doesn't lose them (they're picked up at the next start)."""
        try:
            items = json.loads(self.STEAM_PENDING.read_text())
            items = [i for i in items if isinstance(i, dict) and i.get('slug')]
        except (OSError, ValueError):
            items = []
        if slug is not None:
            items = [i for i in items if i['slug'] != slug]
            if add:
                items.append({'slug': slug, 'name': name or slug})
            try:
                tmp = self.STEAM_PENDING.with_name(f'.{self.STEAM_PENDING.name}.tmp')
                tmp.write_text(json.dumps(items))
                os.replace(tmp, self.STEAM_PENDING)
            except OSError:
                pass
        return items

    def _resume_steam_pending(self):
        known = {g['slug']: g for g in self._games if g.get('source', 'nexus') == 'nexus'}
        for i in self._steam_pending():
            g = known.get(i['slug'])
            if g is None or g.get('steam'):          # gone, or added some other way
                self._steam_pending(i['slug'], add=False)
            else:
                self._add_to_steam_later(i['slug'], i.get('name') or i['slug'])

    def _add_to_steam_later(self, slug, name):
        """The Steam shortcut an install skipped because Steam was running:
        written as soon as Steam is closed (the banner waits for it)."""
        self._steam_pending(slug, name)

        def go():
            self._steam_pending(slug, add=False)
            j = Job([CLI_BIN, '--steam', slug], f"Adding {name} to Steam", slug, 'task', name)
            j.done.connect(lambda code: code == 0 and self.toast(
                f"{name} is in Steam now.", 'success', ("Start Steam", start_steam), 10000))
            self.jobs.submit(j)
        self.gate.run(f"add {name} to Steam", go)

    # ---- job feedback ----
    def _jobs_changed(self):
        self._sync_system()
        busy = frozenset(g['slug'] for g in self._games if self.jobs.busy(g['slug']))
        if busy != self._busy:
            # A game's files are being changed (or stopped being): what Play
            # and the menu offer for it changes with that.
            self._busy = busy
            self._mark_states()
            if self.selected:
                self._sync_selection()
        pending = tuple(id(j) for j in self.jobs.jobs if j.kind == 'install' and j.active)
        if pending != self._pending_ids:
            self.apply_filter()
        else:
            self.views.currentWidget().set_jobs(self.jobs)
            self.homeview.set_jobs(self.jobs)

    def _job_done(self, job):
        name = job.name
        if job.kind == 'open':                    # a steam:// / heroic:// hand-off
            if job.state == 'failed':
                self.toast(f"{job.title} — failed: {job.detail or 'see the log'}", 'error',
                           ("Details", lambda: self._open_job(job)))
            return
        if job.kind == 'launch' and job.state == 'done' and job.slug in self._launching:
            # --launch has handed the game off; if nothing shows up within a
            # minute it isn't coming (a crash on start, a dialog in Steam).
            self._launching[job.slug] = min(self._launching[job.slug],
                                            time.monotonic() - (self.LAUNCH_WAIT - 60))
        if job.kind == 'launch':
            if job.state == 'failed' and job.slug:
                self._launching.pop(job.slug, None)
                if self._handoff and self._handoff['game'].get('slug') == job.slug:
                    self._handoff = None
                self._mark_states()
                self._home_sig = None
                self.apply_filter()
            if job.state == 'failed':
                self.toast(f"{job.title.replace('Launching ', '')} didn't start — "
                           f"{job.detail or 'see the log'}", 'error',
                           ("Details", lambda: self._open_job(job)))
            else:
                self.toast(f"{job.title.replace('Launching ', '')} is starting…", 'success', None,
                           3500)
            QTimer.singleShot(4000, self.reload_library)       # last-played updates
            return
        if job.kind == 'install':
            self._install_followups(job)
        elif job.state == 'done':
            self.toast(f"{job.title} — done", 'success')
        elif job.state == 'failed':
            self.toast(f"{job.title} — failed", 'error', ("Show log", lambda: self._open_job(job)),
                       10000)
            if not self.isActiveWindow():
                notify_desktop(f"{job.title} failed", job.detail or "Open Nexus for "
                               "the log.")
        if job.kind == 'install-proton' or (job.argv[1:2] and job.argv[1].startswith('--proton')):
            self.protonpage.refresh()
            if job.state == 'done' and job.kind == 'install-proton':
                self.toast("New Proton installed. Restart Steam to see it there; pick it per "
                           "game or as the default in Settings.", 'success', None, 8000)
        if job.slug:                            # Theatre's game page reads it afresh
            self.theatre._launch.pop(job.slug, None)
            self.theatre._tips.pop(job.slug, None)
        if job.state == 'done' and '--set-sgdb' in job.argv and self._showing(self.ARTWORK):
            self.artpage.reload()
        if job.kind != 'tool':
            cli_cache_clear()
            self.reload_library()
            self._health_stale = True
            if self._showing(self.HEALTH):
                self._health_stale = False
                self.health.refresh()
            elif self._showing(self.STORAGE):
                self.storage.load_orphans()

    def toast(self, text, kind='info', action=None, timeout=6000):
        self.toasts.show_toast(text, kind, action, timeout)

    def _health_counts(self, problems, fails):
        self._health_problems = (problems, fails)
        self._sync_system()

    def _deals_count(self, n):
        self.nav[self.DEALS].set_badge(n or '', 'accent')
        self.dash_update('deals', 'upcoming')

    def _open_settings(self):
        self.go(self.SETTINGS)

    def _settings_saved(self, proton_changed):
        self.config = load_config()
        self.nav[self.NEWS].setVisible(self.config.get('news', True))
        self.nav[self.DEALS].setVisible(self.config.get('gamedb_enabled', False))
        if getattr(self.settings, 'gamedb_changed', False) or not self.config.get(
                'gamedb_enabled'):
            self.dealspage.res, self.dealspage._loaded = {}, False
            self.nav[self.DEALS].set_badge('', 'accent')
            if self.config.get('gamedb_enabled'):
                self.dealspage.refresh()
        self.dash_update('news', 'deals', 'upcoming')
        self._poll_torrents()                  # the torrent client may have changed
        self.toast("Settings saved.", 'success', None, 3000)
        if proton_changed and self._games:
            self.toast("Point every game at the new Proton now?", 'info',
                       ("Apply to all", lambda: self.run(
                           ['--apply-launch', 'all'], "Re-applying settings to every game",
                           steam=True)), 15000)

    # ---- closing ----
    QUIT_GRACE_MS = 6000          # cancelled jobs get this long to stop before SIGKILL

    def closeEvent(self, ev):
        active = self.jobs.active()
        waiting = [p[0] for p in self.gate.pending]
        if self._quitting or (not active and not waiting):
            ev.accept()
            return
        ev.ignore()
        parts = []
        if active:
            parts.append(f"{len(active)} job(s) are still running ({active[0].title}). "
                         f"Quitting stops them.")
        if waiting:
            parts.append(f"Waiting for Steam to close: {', '.join(waiting)}. New games are "
                         f"added to Steam next time Nexus starts; anything else is dropped.")
        self.raise_()
        self.activateWindow()
        self.confirm("Still working", "\n\n".join(parts), "Quit anyway",
                     self.quit_after_jobs)

    def quit_after_jobs(self):
        """Cancel every job, then close once they've all stopped (or after
        QUIT_GRACE_MS, killing what's left). Closing straight away killed
        setup.exe/wine mid-write: the SIGTERM → SIGKILL grace never ran."""
        self._quitting = True
        for j in self.jobs.active():
            j.cancel()
        if self.jobs.active():
            self.toast("Stopping running jobs…", 'info', None, self.QUIT_GRACE_MS)
        self._quit_deadline = time.monotonic() + self.QUIT_GRACE_MS / 1000
        self._quit_timer.start()

    def _quit_poll(self):
        left = self.jobs.active()
        if left and time.monotonic() < self._quit_deadline:
            return
        self._quit_timer.stop()
        for j in left:
            if j.proc is not None and j.proc.state() != QProcess.ProcessState.NotRunning:
                j.proc.kill()
                j.proc.waitForFinished(1000)
        self.close()


# --------------------------------------------------------------------------
# Packaging (icon + .desktop) + entry point
# --------------------------------------------------------------------------

def ensure_icon() -> str:
    """The app icon: assets/nexus.svg rendered to a 256 px PNG (docks,
    notifications and the desktop entry want a raster file). Re-rendered
    whenever the SVG is newer than the PNG. Needs a live QApplication."""
    try:
        svg_mtime = LOGO_SVG.stat().st_mtime
    except OSError:
        svg_mtime = None
    try:
        if ICON_PATH.exists() and (svg_mtime is None or ICON_PATH.stat().st_mtime >= svg_mtime):
            return str(ICON_PATH)
    except OSError:
        pass
    if svg_mtime is None:
        return str(ICON_PATH) if ICON_PATH.exists() else "applications-games"
    try:
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:                       # qt6-svg missing: use the theme's icon
        return str(ICON_PATH) if ICON_PATH.exists() else "applications-games"
    renderer = QSvgRenderer(str(LOGO_SVG))
    if not renderer.isValid():
        return str(ICON_PATH) if ICON_PATH.exists() else "applications-games"
    size = 256
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(pt, QRectF(0, 0, size, size))
    pt.end()
    # Qt's SVG renderer ignores the rounded clip-path, so round the corners
    # here (the logo's radius is 8 of 64).
    vb = renderer.viewBoxF()
    pm = rounded_pixmap(pm, size * 8 / (vb.width() or 64))
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ICON_PATH.with_suffix('.tmp.png')
        if pm.save(str(tmp), 'PNG'):
            os.replace(tmp, ICON_PATH)
            return str(ICON_PATH)
    except OSError:
        pass
    return "applications-games"


def manager_exec() -> str:
    """Desktop-entry Exec for this app: `nexus` on PATH if there is one (e.g.
    the ~/.local/bin symlink), else the repo's bin/nexus — quoted per the
    Desktop Entry spec."""
    path = shutil.which('nexus') or str(_REPO / 'bin' / 'nexus')
    for ch in ('\\', '"', '`', '$'):
        path = path.replace(ch, '\\' + ch)
    return f'"{path}"'.replace('\\', '\\\\').replace('%', '%%')


# One Nexus per user. A second launch (nirinit restoring the session, Sunshine
# starting Theatre, a double click on the icon) hands its arguments to the
# running window and exits instead of opening another one.
# In the user's private runtime dir. No socket options on purpose: with them Qt
# builds the socket elsewhere and renames it over ours, so a second listen()
# would silently steal the name instead of failing.
# Without a runtime dir, a 0700 folder of our own, never a name in shared /tmp
# another user could take first.
INSTANCE = os.environ.get("NEXUS_INSTANCE") or (
    os.path.join(os.environ['XDG_RUNTIME_DIR'], 'nexus.sock')
    if os.path.isdir(os.environ.get('XDG_RUNTIME_DIR') or '')
    else str(NX.CACHE_DIR / 'run' / 'nexus.sock'))
INSTANCE_MAX = 64 << 10           # bytes one forwarded launch may send


def forward_to_running(argv: list[str]) -> bool:
    """Give argv to an already-running Nexus. True if one took it."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE)
    if not sock.waitForConnected(300):
        return False
    sock.write(json.dumps({'argv': argv}).encode() + b'\n')
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


class InstanceServer(QObject):
    """The running Nexus's end: emits each later launch's argv."""
    request = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept)

    def listen(self, argv: list[str]) -> bool:
        """Become the instance. False if another one answered meanwhile (two
        launches at once) and took argv instead."""
        if os.path.isabs(INSTANCE):
            try:
                os.makedirs(os.path.dirname(INSTANCE), mode=0o700, exist_ok=True)
            except OSError:
                pass
        if self.server.listen(INSTANCE):
            return True
        if forward_to_running(argv):
            return False
        QLocalServer.removeServer(INSTANCE)     # a stale socket from a crash
        self.server.listen(INSTANCE)
        return True

    def _accept(self):
        while (sock := self.server.nextPendingConnection()) is not None:
            buf = bytearray()

            def read(sock=sock, buf=buf):
                buf.extend(bytes(sock.readAll()))
                if len(buf) > INSTANCE_MAX:         # not a launch: drop it
                    buf.clear()
                    sock.abort()
                    return
                if b'\n' in buf:
                    line = bytes(buf).split(b'\n', 1)[0]
                    buf.clear()
                    try:
                        argv = json.loads(line).get('argv') or []
                    except (ValueError, AttributeError):
                        argv = []
                    self.request.emit([str(a) for a in argv])
            sock.readyRead.connect(read)
            sock.disconnected.connect(sock.deleteLater)
            if sock.bytesAvailable():
                read()


def install_desktop_entry(icon: str):
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_PATH.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Nexus\n"
        "GenericName=Game launcher\n"
        "Comment=Play and manage your games, install repacks, update Proton\n"
        f"Exec={manager_exec()}\n"
        f"Icon={icon}\n"
        "Categories=Game;\n"
        "Keywords=games;launcher;steam;proton;fitgirl;\n"
        "Terminal=false\n"
        f"StartupWMClass={NX.APP_ID}\n"
        "SingleMainWindow=true\n"
        "Actions=Theatre;\n"
        "\n"
        "[Desktop Action Theatre]\n"
        "Name=Theatre mode (TV)\n"
        f"Exec={manager_exec()} --theatre\n"
    )
    DESKTOP_PATH.chmod(0o644)
    try:
        LEGACY_DESKTOP.unlink()           # the pre-rename "FitGirl Manager" entry
    except OSError:
        pass
    if shutil.which('update-desktop-database'):
        try:
            subprocess.run(['update-desktop-database', str(APPLICATIONS_DIR)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass


def install_excepthook(win: 'MainWindow'):
    """An exception escaping a Qt slot aborts PyQt6 (qFatal) unless an
    excepthook is set. Log it (stderr, the Activity debug log) and say so in
    a toast; the app keeps running."""
    import traceback
    busy = []

    def hook(etype, value, tb):
        if issubclass(etype, KeyboardInterrupt):
            sys.__excepthook__(etype, value, tb)
            return
        text = ''.join(traceback.format_exception(etype, value, tb))
        sys.stderr.write(text)
        if busy:                             # failing inside the hook: stop here
            return
        busy.append(1)
        try:
            win.activity._debug_append(None, f"\n[GUI error]\n{text}")
            win.toast(f"Something went wrong: {value or etype.__name__}", 'error',
                      ("Details", lambda: (win.go(win.ACTIVITY),
                                           win.activity.show_debug(True))), 10000)
        except Exception:                    # noqa: BLE001 — never raise from the hook
            pass
        finally:
            busy.clear()
    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    argv = sys.argv[1:]
    instance = None
    if "--install-desktop" not in argv:
        if forward_to_running(argv):
            return
        if "--leave-theatre" in argv:         # nothing running: nothing to leave
            return
        instance = InstanceServer(app)
        if not instance.listen(argv):
            return
    app.setStyle('Fusion')
    NX.migrate()                          # fitgirl-install dirs -> nexus (once)
    app.setApplicationName("Nexus")
    # The Wayland app-id: lets niri/docks match the icon and lets users write
    # window rules for app-id="nexus".
    app.setDesktopFileName(NX.APP_ID)
    resolve_fonts()                       # before anything reads UI_FONT
    apply_appearance(LibraryPrefs().appearance)   # colours before the stylesheet
    base = QFont(UI_FONT)
    base.setPixelSize(FS['sm'])
    app.setFont(base)
    app.setStyleSheet(build_stylesheet())
    app.popup_rounder = PopupRounder(app)
    app.installEventFilter(app.popup_rounder)

    icon = ensure_icon()
    app.setWindowIcon(QIcon(icon))
    stale = DESKTOP_PATH.exists() and 'SingleMainWindow' not in DESKTOP_PATH.read_text()
    if not DESKTOP_PATH.exists() or stale or LEGACY_DESKTOP.exists() \
            or "--install-desktop" in sys.argv:
        install_desktop_entry(icon)
    if "--install-desktop" in sys.argv:
        print(f"Installed {DESKTOP_PATH}")
        return

    win = MainWindow()
    install_excepthook(win)
    win.setWindowIcon(QIcon(icon))
    win.show()
    QTimer.singleShot(0, win.ctrl.open)       # controllers once the window is up
    if instance is not None:
        instance.request.connect(win.handle_remote)
    if "--theatre" in argv:
        win.set_theatre(True)
    code = app.exec()
    # Tear the window down while Qt is still fully alive: left to PyQt's
    # atexit cleanup, a large widget tree can segfault in sip on the way out.
    win.deleteLater()
    app.processEvents()
    del win
    sys.exit(code)


if __name__ == '__main__':
    main()
