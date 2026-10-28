"""The look: palette and design tokens, fonts, and the Qt stylesheet."""

import re
from pathlib import Path
from string import Template
from types import SimpleNamespace

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap

from nexus.icons import ICONS

from .common import _REPO, DATA_DIR


# --- Fonts -----------------------------------------------------------------
# The spec asks for Lexend + JetBrains Mono. Neither name is guaranteed to be
# installed, so each role resolves at runtime against a preference list. The
# icon role is separate and deliberate: icons are private-use glyphs from the
# bundled Lucide font (nexus/icons.py), so it must never fall back to the UI face.
SANS_STACK = ['Lexend', 'Inter', 'Inter Display', 'Cantarell', 'Noto Sans',
              'DejaVu Sans']
# Cascadia Mono and Consolas: Windows has none of the others, and its
# 'monospace' resolves to Tahoma, which isn't one.
MONO_STACK = ['JetBrainsMono Nerd Font', 'JetBrains Mono', 'Fira Code',
              'Noto Sans Mono', 'DejaVu Sans Mono', 'Cascadia Mono', 'Consolas']
FONT_DIR = _REPO / 'assets' / 'fonts'
ICON_TTF = FONT_DIR / 'lucide.ttf'
# Bundled so a machine without them (Windows) looks the same: Lexend (OFL)
# and a two-glyph subset of Symbols Nerd Font (see ICON_FALLBACK_STACK).
BUNDLED_TTF = [FONT_DIR / 'lexend.ttf', FONT_DIR / 'nerd-symbols.ttf']
# Lucide lacks brand logos and filled shapes; those few icons keep their
# Material Design glyph, from the bundled Symbols Nerd Font subset (or an
# installed Nerd Font). The symbols-only one first: its metrics are made for
# icons, not for text.
ICON_FALLBACK_STACK = ['Symbols Nerd Font', 'Symbols Nerd Font Mono',
                       'JetBrainsMono Nerd Font', 'MesloLGL Nerd Font',
                       'FantasqueSansM Nerd Font']

# The faces in use; resolve_fonts() fills them in at startup. One shared
# namespace (FONTS.ui, not a FONTS.ui global) so every module sees the result.
FONTS = SimpleNamespace(ui='Noto Sans', mono='Noto Sans Mono', icon='lucide')


def _first_installed(candidates: list[str], fallback: str) -> str:
    have = set(QFontDatabase.families())
    for name in candidates:
        if name in have:
            return name
    return fallback


def resolve_fonts() -> None:
    """Pick the best installed face for each role. Must run after QApplication
    exists (QFontDatabase needs a live app). The bundled faces in
    assets/fonts are registered first, so they win over installed ones."""
    for ttf in BUNDLED_TTF:
        QFontDatabase.addApplicationFont(str(ttf))
    FONTS.ui = _first_installed(SANS_STACK, QFont().defaultFamily())
    FONTS.mono = _first_installed(MONO_STACK, 'monospace')
    fid = QFontDatabase.addApplicationFont(str(ICON_TTF))
    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    FONTS.icon = fams[0] if fams else 'lucide'
    # Glyphs the icon face lacks come from here, not whatever fontconfig picks.
    fallback = _first_installed(ICON_FALLBACK_STACK, '')
    if fallback:
        QFont.insertSubstitution(FONTS.icon, fallback)


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
    'scrim':         PAL['base'],
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
    'bar':       'rgba(38,35,58,0.72)',
})

# Radius (§2.6) / spacing (§2.4), in px. Semantic names first: every
# rounded thing picks one of these (or pill() for capsules), never a literal.
R = {'sm': 6, 'control': 10, 'lg': 10, 'xl': 12, 'tile': 12, 'popover': 12,
     'card': 16, 'panel': 16, 'full': 9999}


R_BASE = dict(R)

# --- Appearance: base themes, accents, and the function that applies them ---
# Each theme is the ladder the UI is built from (base → surface → overlay →
# overlay_hi) plus its text tones. Dark themes keep Rosé Pine's status
# colours (success, danger, warning…); a light theme ('kind': 'light')
# brings its own, darker ones, and cards sit lighter than the page.
DARK_STATUS = {'iris': '#c4a7e7', 'foam': '#9ccfd8', 'love': '#eb6f92', 'gold': '#f6c177',
               'rose': '#ebbcba', 'pine': '#31748f'}
DAWN_STATUS = {'iris': '#907aa9', 'foam': '#56949f', 'love': '#b4637a', 'gold': '#ea9d34',
               'rose': '#d7827e', 'pine': '#286983'}
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
    'dawn':      ("Dawn", {'base': '#faf4ed', 'surface': '#fffaf3', 'overlay': '#f2e9e1',
                           'overlay_hi': '#dfdad9', 'text': '#575279', 'subtle': '#6e6a86',
                           'muted': '#857f99', 'kind': 'light', **DAWN_STATUS}),
    'paper':     ("Paper", {'base': '#f3f3f1', 'surface': '#ffffff', 'overlay': '#e6e5e2',
                            'overlay_hi': '#d4d2cd', 'text': '#1d1c1a', 'subtle': '#45433f',
                            'muted': '#6f6c66', 'kind': 'light', **DAWN_STATUS}),
}
SCALES = (0.9, 1.0, 1.1, 1.25, 1.5)   # Settings → Appearance → Size
ACCENTS = {'iris': '#c4a7e7', 'rose': '#ebbcba', 'love': '#eb6f92', 'gold': '#f6c177',
           'foam': '#9ccfd8', 'sky': '#89b4fa', 'mint': '#a6e3a1', 'pine': '#31748f'}
APPEARANCE_DEFAULT = {'theme': 'rose-pine', 'accent': 'iris', 'partner': 'love',
                      'glow': 'subtle', 'corners': 'default', 'art_tint': True, 'scale': 1.0}
GLOW = {'off': 0.0, 'subtle': 1.0, 'vivid': 1.9}
CORNERS = {'sharp': 0.45, 'default': 1.0, 'round': 1.45}
LOOK = {'glow': 1.0, 'art_tint': True, 'light': False, 'scale': 1.0}   # read by the painters


def is_light() -> bool:
    return LOOK['light']


def ui_scale() -> float:
    """Settings → Appearance → Size, for sizes set in code (not QSS)."""
    return LOOK['scale']


def scaled(px: float) -> int:
    return int(round(px * LOOK['scale']))


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
    if ap.get('scale') in SCALES:
        out['scale'] = float(ap['scale'])
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
    theme = THEMES[ap['theme']][1]
    light_theme = theme.get('kind') == 'light'
    PAL.update(DARK_STATUS)                    # a light theme overrides these below
    PAL.update({k: v for k, v in theme.items() if k != 'kind'})
    accent = accent_hex(ap['accent'])
    if light_theme and QColor(accent).lightnessF() > 0.62:
        # A pastel accent (the dark themes' defaults) washes out on a light
        # page: the same hue, deep enough to read as text and as a fill.
        c = QColor(accent)
        accent = QColor.fromHslF(c.hslHueF(), min(1.0, c.hslSaturationF() * 0.9 + 0.1),
                                 0.42).name()
    partner = accent if ap['partner'] == 'none' else accent_hex(ap['partner'])
    if light_theme and partner != accent and QColor(partner).lightnessF() > 0.62:
        c = QColor(partner)
        partner = QColor.fromHslF(c.hslHueF(), min(1.0, c.hslSaturationF() * 0.9 + 0.1),
                                  0.45).name()
    light = QColor(accent).lightnessF() > 0.55
    # Raised surfaces: lighter than the page on a dark theme (towards
    # overlay); on a light one cards are the white `surface` and the page
    # the tinted base.
    if light_theme:
        surf = {'surface': PAL['surface'], 'surface_1': _mix(PAL['surface'], PAL['overlay'], 0.35),
                'surface_3': _mix(PAL['overlay'], PAL['overlay_hi'], 0.6),
                'border_card': _mix(PAL['overlay'], PAL['overlay_hi'], 0.5),
                'tile_scrim': 'rgba(24,22,32,0.78)',    # captions over art stay dark
                'scrim': '#18161f',                     # what white text over art sits on
                # Glass sits over art with white text on it: dark in every theme.
                'glass': 'rgba(24,22,32,0.50)', 'glass_hi': 'rgba(24,22,32,0.66)',
                'bar': _rgba(PAL['surface'], 0.88)}      # the top bar, over the page
    else:
        surf = {'surface': _mix(PAL['base'], PAL['overlay'], 0.35),
                'surface_1': _mix(PAL['base'], PAL['overlay'], 0.30),
                'surface_3': _mix(PAL['overlay'], PAL['overlay_hi'], 0.45),
                'border_card': _mix(PAL['base'], PAL['overlay'], 0.45),
                'tile_scrim': _rgba(PAL['base'], 0.84), 'scrim': PAL['base'],
                'glass': _rgba(PAL['overlay'], 0.72), 'glass_hi': _rgba(PAL['overlay_hi'], 0.82),
                'bar': _rgba(PAL['overlay'], 0.72)}
    P.update({
        'bg': PAL['base'], 'mantle': PAL['surface'] if not light_theme else PAL['overlay'],
        'surface_solid': PAL['surface'], 'surface_2': PAL['overlay'],
        'overlay': PAL['overlay_hi'], 'border': PAL['overlay'] if not light_theme
        else PAL['overlay_hi'],
        'text': PAL['text'], 'subtext': PAL['subtle'], 'muted': PAL['muted'],
        'accent': accent, 'on_accent': PAL['base'] if light and not light_theme else '#ffffff',
        'accent_09': _rgba(accent, 0.09), 'accent_16': _rgba(accent, 0.16),
        'accent_22': _rgba(accent, 0.22), 'accent_35': _rgba(accent, 0.35),
        'success': PAL['foam'], 'danger': PAL['love'], 'warning': PAL['gold'],
        'attention': PAL['rose'], 'info': PAL['pine'],
        'success_18': _rgba(PAL['foam'], 0.18), 'danger_18': _rgba(PAL['love'], 0.18),
        'warning_18': _rgba(PAL['gold'], 0.18),
        **surf,
    })
    GRAD['dusk'] = (accent, partner)
    GRAD['aurora'] = (accent, PAL['foam'])
    SRC_HUE['nexus'] = accent
    k = CORNERS[ap['corners']]
    R.update({key: max(2, int(round(v * k))) for key, v in R_BASE.items() if key != 'full'})
    LOOK['glow'] = GLOW[ap['glow']] * (0.6 if light_theme else 1.0)
    LOOK['art_tint'] = ap['art_tint']
    LOOK['light'] = light_theme
    LOOK['scale'] = ap['scale']
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
      'base': 16, 'lg': 18, 'display': 20,
      # Icon glyphs and the few big numbers / headlines.
      'stat_icon': 17, 'icon': 20, 'headline': 26, 'big': 28}

TILE_W = 200
TILE_LIFT = 4                    # hover translateY(-4px); reserved, not layout churn


# --------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------

def build_stylesheet(scale: float = 1.0) -> str:
    """The design system rendered as QSS. Every value comes from a token.
    `scale` enlarges type and spacing (Theatre mode, read from the couch),
    on top of Settings → Appearance → Size (LOOK['scale'])."""
    scale = scale * LOOK['scale']
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
    #TopBar { background: $bar; }
    QToolButton#TopIcon { background: transparent; border: none; color: $subtext;
                          font-size: ${fs_icon}px; padding: 0; }
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
                            font-size: ${fs_icon}px; padding-left: 4px; }
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
    #SmartRule { background: $border; border: none; }
    #SmartHead { color: $text; font-size: ${fs_base}px; font-weight: 600; }
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
                              font-size: ${fs_stat_icon}px; padding: 0; }
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
    QLabel#DashBig { color: $text; font-size: ${fs_big}px; font-weight: 600; }
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
    QLabel#NewsHeadline { color: $on_art; font-size: ${fs_headline}px; font-weight: 600; }
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
    QLabel#SearchQuery { color: $text; font-size: ${fs_display}px; font-weight: 600; }
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
    #SheetFoot { border-top: 1px solid $border; }
    #NoteCard { background: $surface_1; border: 1px solid $border; border-radius: ${r_xl}px; }
    #NoteIcon { background: $accent_16; border-radius: ${r_control}px; }
    QLabel#NoteTitle { color: $text; font-size: ${fs_sm}px; font-weight: 600; }
    QLabel#NoteText { color: $subtext; font-size: ${fs_xs}px; }
    QLabel#NoteLine { color: $text; font-size: ${fs_command}px; }

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

    /* ---- Settings: section list, rows ---------------------------------- */
    #SettingsNav { background: $mantle; border-right: 1px solid $border; }
    QPushButton#NavItem { background: transparent; border: none; color: $subtext;
                          border-radius: ${r_control}px; text-align: left;
                          padding: ${sp_2}px ${sp_3}px; font-size: ${fs_sm}px; font-weight: 500; }
    QPushButton#NavItem:hover { background: $surface_2; color: $text; }
    QPushButton#NavItem:checked { background: $accent_16; color: $accent; }
    QLineEdit#SettingsSearch { background: $surface_1; border: 1px solid $border;
                               border-radius: ${r_search}px; padding: 0 ${sp_2}px; }
    QLineEdit#SettingsSearch:focus { border: 1px solid $accent; background: $surface_2; }
    QLabel#SavedNote { color: $muted; font-size: ${fs_xs}px; }
    QLabel#SavedNote[ok="true"] { color: $success; }
    QLabel#RowTitle { color: $text; font-size: ${fs_sm}px; }
    QLabel#RowError { color: $danger; font-size: ${fs_xs}px; }
    #RowRule { background: $border; }
    QLabel#Banner { color: $text; font-size: ${fs_xs}px; }

    /* ---- First-run setup: step title, choice cards, check rows ---------- */
    QLabel#SetupTitle { color: $text; font-size: ${fs_display}px; font-weight: 600; }
    QPushButton#ChoiceCard { background: $surface_1; border: 1px solid $border;
                             border-radius: ${r_xl}px; padding: 0; text-align: left; }
    QPushButton#ChoiceCard:hover { background: $surface_2; border-color: $overlay; }
    QPushButton#ChoiceCard:focus { border-color: $accent_35; }
    QPushButton#ChoiceCard:checked { background: $accent_09; border: 1px solid $accent; }
    #ToggleCard { background: $surface_1; border: 1px solid $border;
                  border-radius: ${r_xl}px; }
    #ToggleCard:hover { background: $surface_2; border-color: $overlay; }
    #ToggleCard[on="true"] { background: $accent_09; border-color: $accent_35; }
    #SetupRow { background: $surface_1; border: 1px solid $border_card;
                border-radius: ${r_control}px; }
    QLabel#ChoiceTitle { color: $text; font-size: ${fs_sm}px; font-weight: 500; }
    QLabel#ChoiceNote { color: $muted; font-size: ${fs_xs}px; }

    /* ---- Misc ---------------------------------------------------------- */
    QLabel#Empty { color: $muted; font-size: ${fs_base}px; }
    QLabel#EmptyTitle { color: $text; font-size: ${fs_base}px; font-weight: 500; }
    QLabel#Hint  { color: $muted; font-size: ${fs_xs}px; }
    QStatusBar { color: $muted; font-size: ${fs_xs}px; background: $mantle;
                 border-top: 1px solid $border; }
    QMessageBox { background: $surface_solid; }
    """)
    return tpl.substitute(
        ui=FONTS.ui, mono=FONTS.mono, icon=FONTS.icon,
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


def _shade(hex_color: str, factor: float) -> str:
    """Multiply a hex color's channels — used for the accent hover state, which
    the spec expresses as `opacity: 90%` over a dark ground."""
    c = QColor(hex_color)
    return QColor(int(c.red() * factor), int(c.green() * factor),
                  int(c.blue() * factor)).name()


_ICON_CACHE: dict[str, str] = {}


def _stroke_icon(name: str, color: str, points, size=(18, 18), width=2.4) -> str:
    """QSS can't draw glyphs for indicators/arrows, so render a stroked
    polyline to a PNG once and reference it by path. A name@2x.png is
    written beside it, which Qt picks on a scaled display (125%, 150%, ...)
    so the stroke stays crisp."""
    cached = _ICON_CACHE.get(name)
    if cached and Path(cached).exists():
        return cached
    path = DATA_DIR / f'ui-{name}.png'
    for scale, out in ((1, path), (2, path.with_name(f'ui-{name}@2x.png'))):
        pm = QPixmap(size[0] * scale, size[1] * scale)
        pm.fill(Qt.GlobalColor.transparent)
        pt = QPainter(pm)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.scale(scale, scale)
        pen = QPen(QColor(color), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pt.setPen(pen)
        pt.drawPolyline(*(QPointF(x, y) for x, y in points))
        pt.end()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pm.save(str(out), 'PNG')
        except OSError:
            return ''
    # Forward slashes: in a stylesheet's url("...") a Windows backslash is
    # an escape, and Qt drops the whole application stylesheet.
    _ICON_CACHE[name] = path.as_posix()
    return path.as_posix()


def _check_icon_path() -> str:
    """Drawn in `on-accent` so it reads against the accent-filled indicator."""
    return _stroke_icon(f"check-{P['on_accent'][1:]}", P['on_accent'],
                        ((4, 9), (8, 13), (14, 5)))


def _chevron_icon_path(color_key: str) -> str:
    """Combo-box arrow — Fusion draws none once ::drop-down is styled."""
    return _stroke_icon(f"chevron-{P[color_key][1:]}", P[color_key],
                        ((3, 4.5), (6, 7.5), (9, 4.5)), (12, 12), 1.6)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

# Nerd Font (Font Awesome range) glyphs used across the UI.
G = dict(ICONS)


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


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import _TINTS  # noqa: E402
