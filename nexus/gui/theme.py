"""The look: palette and design tokens, fonts, and the Qt stylesheet."""

import re
from pathlib import Path
from string import Template
from types import SimpleNamespace

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap

from nexus.icons import COLLECTION_ICONS, ICONS

from .common import _REPO, COLLECTION_LOOK, DATA_DIR


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
    'live_on_art':   PAL['foam'],     # "Now playing" over art (white on a light theme)
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
LATTE_STATUS = {'iris': '#8839ef', 'foam': '#179299', 'love': '#d20f39', 'gold': '#df8e1d',
                'rose': '#dc8a78', 'pine': '#1e66f5'}
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
    'forest':    ("Forest", {'base': '#111a16', 'surface': '#17221d', 'overlay': '#213029',
                             'overlay_hi': '#2e4138', 'text': '#e2ece5', 'subtle': '#c3d3c8',
                             'muted': '#88a194'}),
    'ember':     ("Ember", {'base': '#1a1412', 'surface': '#221a17', 'overlay': '#30241f',
                            'overlay_hi': '#43332c', 'text': '#f1e6df', 'subtle': '#d8c8be',
                            'muted': '#a69389'}),
    'ocean':     ("Ocean", {'base': '#0c1322', 'surface': '#111a2d', 'overlay': '#1a253d',
                            'overlay_hi': '#283552', 'text': '#e2e8f5', 'subtle': '#c3cde1',
                            'muted': '#8a98b4'}),
    # High contrast: black page, white text, borders you can see. 'tokens'
    # overrides semantic colours after the theme is applied.
    'contrast':  ("High contrast", {'base': '#000000', 'surface': '#0a0a0a', 'overlay': '#1c1c1c',
                                    'overlay_hi': '#4d4d4d', 'text': '#ffffff',
                                    'subtle': '#ededed', 'muted': '#c8c8c8',
                                    'tokens': {'border': '#6b6b6b', 'border_card': '#5a5a5a',
                                               'info': '#6cb6d9'}}),
    'dawn':      ("Dawn", {'base': '#faf4ed', 'surface': '#fffaf3', 'overlay': '#f2e9e1',
                           'overlay_hi': '#dfdad9', 'text': '#575279', 'subtle': '#6e6a86',
                           'muted': '#857f99', 'kind': 'light', **DAWN_STATUS}),
    'paper':     ("Paper", {'base': '#f3f3f1', 'surface': '#ffffff', 'overlay': '#e6e5e2',
                            'overlay_hi': '#d4d2cd', 'text': '#1d1c1a', 'subtle': '#45433f',
                            'muted': '#6f6c66', 'kind': 'light', **DAWN_STATUS}),
    'latte':     ("Latte", {'base': '#eff1f5', 'surface': '#ffffff', 'overlay': '#e4e7ee',
                            'overlay_hi': '#ccd0da', 'text': '#4c4f69', 'subtle': '#5c5f77',
                            'muted': '#6c6f85', 'kind': 'light', **LATTE_STATUS}),
    'mist':      ("Mist", {'base': '#edf1f5', 'surface': '#ffffff', 'overlay': '#e0e6ed',
                           'overlay_hi': '#c8d1dc', 'text': '#1b2633', 'subtle': '#3c4a5b',
                           'muted': '#5f6d80', 'kind': 'light', **DAWN_STATUS}),
}
# First-run setup shows these; Settings shows them all.
SETUP_THEMES = ('rose-pine', 'moon', 'midnight', 'forest', 'dawn', 'paper', 'contrast')
SCALES = (0.9, 1.0, 1.1, 1.25, 1.5)   # Settings → Appearance → Size
ACCENTS = {'iris': '#c4a7e7', 'rose': '#ebbcba', 'love': '#eb6f92', 'coral': '#ff8a65',
           'gold': '#f6c177', 'mint': '#a6e3a1', 'teal': '#4fd1b5', 'foam': '#9ccfd8',
           'sky': '#89b4fa', 'blue': '#5b8def', 'pine': '#31748f', 'silver': '#d5dae3'}
# A collection's colour is one of these too (w-52), as the role 'col_<key>';
# apply_appearance deepens the pastel ones on a light theme, like the accent.
P.update({f'col_{k}': v for k, v in ACCENTS.items()})
APPEARANCE_DEFAULT = {'theme': 'rose-pine', 'accent': 'iris', 'partner': 'love',
                      'glow': 'subtle', 'corners': 'default', 'art_tint': True, 'scale': 1.0}
GLOW = {'off': 0.0, 'subtle': 1.0, 'vivid': 1.9}
CORNERS = {'sharp': 0.45, 'default': 1.0, 'round': 1.45}
# One-click looks: theme, accent, gradient partner, glow and corners together
# (Size and the art tint stay as they are).
PRESET_KEYS = ('theme', 'accent', 'partner', 'glow', 'corners')
PRESETS = {
    'neon':     ("Neon", {'theme': 'midnight', 'accent': 'teal', 'partner': 'love',
                          'glow': 'vivid', 'corners': 'round'}),
    'campfire': ("Campfire", {'theme': 'ember', 'accent': 'coral', 'partner': 'gold',
                              'glow': 'vivid', 'corners': 'default'}),
    'woodland': ("Woodland", {'theme': 'forest', 'accent': 'mint', 'partner': 'teal',
                              'glow': 'subtle', 'corners': 'round'}),
    'daylight': ("Daylight", {'theme': 'mist', 'accent': 'blue', 'partner': 'none',
                              'glow': 'off', 'corners': 'sharp'}),
    'contrast': ("High contrast", {'theme': 'contrast', 'accent': 'gold', 'partner': 'none',
                                   'glow': 'off', 'corners': 'sharp'}),
}
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


THEME_META = ('kind', 'tokens')       # theme keys that aren't palette colours


def _luminance(hex_color: str) -> float:
    c = QColor(hex_color)
    ch = [x / 255 for x in (c.red(), c.green(), c.blue())]
    ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours (1 to 21)."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _deepen(hex_color: str, lightness: float, page: str, min_ratio: float = 3.5) -> str:
    """The same hue, deep enough for a light page: pastels (the dark themes'
    accents) drop to `lightness`, and anything still too pale for white text
    on it, or for it as text on `page`, keeps darkening until it reads."""
    c = QColor(hex_color)
    h, s, ll = c.hslHueF(), c.hslSaturationF(), c.lightnessF()
    if h < 0:                                   # a grey: no hue to keep
        h, s = 0.0, 0.0
    if ll > 0.62:
        s, ll = min(1.0, s * 0.9 + 0.1), lightness
    out = QColor.fromHslF(h, s, ll).name()
    while ll > 0.2 and min(contrast('#ffffff', out), contrast(page, out)) < min_ratio:
        ll -= 0.02
        out = QColor.fromHslF(h, s, ll).name()
    return out


def accent_pair(theme_key: str, accent: str, partner: str) -> tuple[str, str]:
    """(accent, gradient partner) as hex the way `theme_key` shows them: a
    light theme deepens pale ones so they read on its page."""
    theme = THEMES.get(theme_key, THEMES[APPEARANCE_DEFAULT['theme']])[1]
    a = accent_hex(accent)
    b = a if partner == 'none' else accent_hex(partner)
    if theme.get('kind') == 'light':
        same = b == a
        a = _deepen(a, 0.42, theme['base'])
        b = a if same else _deepen(b, 0.45, theme['base'], 3.0)
    return a, b


def apply_appearance(ap: dict) -> dict:
    """Rebuild the palette (PAL/P/GRAD/R) from an appearance in place, so every
    painter picks it up on its next paint; the caller re-applies the
    stylesheet. Returns the normalised appearance."""
    ap = normalize_appearance(ap)
    theme = THEMES[ap['theme']][1]
    light_theme = theme.get('kind') == 'light'
    PAL.update(DARK_STATUS)                    # a light theme overrides these below
    PAL.update({k: v for k, v in theme.items() if k not in THEME_META})
    accent, partner = accent_pair(ap['theme'], ap['accent'], ap['partner'])
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
        # The label on an accent fill: the page colour or white, whichever reads
        # better (a light theme's accent is always deep enough for white).
        'accent': accent, 'on_accent': '#ffffff' if light_theme or
        contrast('#ffffff', accent) >= contrast(PAL['base'], accent) else PAL['base'],
        'accent_09': _rgba(accent, 0.09), 'accent_16': _rgba(accent, 0.16),
        'accent_22': _rgba(accent, 0.22), 'accent_35': _rgba(accent, 0.35),
        'success': PAL['foam'], 'danger': PAL['love'], 'warning': PAL['gold'],
        # A light theme's green is too faint over dark art: white, with a
        # green dot in front (gameinfo.live_kicker).
        'live_on_art': PAL['white'] if light_theme else PAL['foam'],
        'attention': PAL['rose'], 'info': PAL['pine'],
        'success_18': _rgba(PAL['foam'], 0.18), 'danger_18': _rgba(PAL['love'], 0.18),
        'warning_18': _rgba(PAL['gold'], 0.18),
        **surf,
        **theme.get('tokens', {}),
    })
    for key, v in ACCENTS.items():              # collection colours, deepened like the accent
        P[f'col_{key}'] = _deepen(v, 0.42, theme['base']) if light_theme else v
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


def collection_look(name: str) -> tuple[str, str] | None:
    """A collection's (glyph, colour role) for chips, shelves, menus and the
    top bar; None when it has neither an icon nor a colour (it looks as it
    always did). A colour alone wears the bookmark."""
    look = COLLECTION_LOOK.get(name) or {}
    icon, color = look.get('icon'), look.get('color')
    if icon not in COLLECTION_ICONS and color not in ACCENTS:
        return None
    return (COLLECTION_ICONS.get(icon, COLLECTION_ICONS['bookmark']),
            f'col_{color}' if color in ACCENTS else 'subtext')


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
    QPushButton#TopPin { background: transparent; border: none; color: $subtext;
                         padding: 0 ${sp_3}px; font-size: ${fs_xs}px; font-weight: 500; }
    QPushButton#TopPin:hover { background: $surface_2; color: $text; }
    QPushButton#TopPin:checked { background: $accent_16; color: $text; }
    #TopSep { background: $border; }
    QToolButton#IconPick { background: transparent; border: 1px solid transparent; color: $subtext;
                           font-size: ${fs_icon}px; padding: 0; }
    QToolButton#IconPick:hover, QToolButton#IconPick:focus { background: $surface_2;
                                                             color: $text; }
    QToolButton#IconPick:checked { background: $accent_16; border-color: $accent_35; }
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
    QLabel#HeroKicker[live="true"] { color: $live_on_art; }
    QLabel#HeroTitle { color: $on_art; font-weight: 600; }
    QLabel#TheatreClock { color: $subtext; font-size: ${fs_lg}px; font-weight: 500; }
    #StatTile { background: $surface_1; border: 1px solid $border_card;
                border-radius: ${r_control}px; }
    QLabel#StatValue { color: $text; font-size: ${fs_sm}px; font-weight: 600; }
    QToolButton#QuickAction { background: $surface_2; color: $subtext; border: none;
                              font-size: ${fs_stat_icon}px; padding: 0; }
    QToolButton#QuickAction:hover { background: $accent_22; color: $accent; }
    QToolButton#QuickAction[on="true"] { color: $warning; }
    /* Game page context block (w-40): credits, genre tags, blurb, length. */
    QLabel#ContextCredits { color: $subtext; font-size: ${fs_xs}px; font-weight: 500; }
    QLabel#ContextBlurb { color: $subtext; font-size: ${fs_xs}px; }
    QLabel#ContextLength { color: $text; font-size: ${fs_xs}px; }
    QPushButton#TagChip { background: $surface_2; color: $subtext; border: 1px solid $border;
                          border-radius: ${r_tag}px; padding: 0 ${sp_2_5}px;
                          font-size: ${fs_xs}px; min-height: 20px; max-height: 20px; }
    QPushButton#TagChip:hover { background: $accent_22; color: $accent; border-color: $accent_35; }
    QPushButton#ContextMore { background: transparent; border: none; color: $accent;
                              padding: 0; font-size: ${fs_xs}px; text-align: left; }
    QPushButton#ContextMore:hover { text-decoration: underline; }
    /* Game page header and tabs (w-54). */
    QLabel#HeroName { color: $text; font-size: ${fs_headline}px; font-weight: 600; }
    QLabel#FactLine { color: $muted; font-size: ${fs_xs}px; }
    QLabel#MeterLabel { color: $muted; font-size: ${fs_xs}px; }
    QLabel#MeterValue { color: $text; font-size: ${fs_xs}px; font-weight: 500; }
    QLabel#ContextLede { color: $subtext; font-size: ${fs_sm}px; }
    QLabel#SectionKicker { color: $muted; font-size: ${fs_2xs}px; font-weight: 600; }
    QLabel#GlanceKey { color: $muted; font-size: ${fs_xs}px; }
    QLabel#GlanceValue { color: $subtext; font-size: ${fs_xs}px; }
    QLabel#MediaTitle { color: $text; font-weight: 500; }
    QFrame#MediaPlay { background: $glass_hi; border: 1px solid $glass; }
    #MediaRow { background: transparent; border: none; border-radius: ${r_control}px; }
    #MediaRow:hover { background: $surface_1; }
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

    /* ---- Theatre game page reader ---------------------------------------- */
    QTextBrowser#TheatreReader { background: $glass; color: $on_art; border: 1px solid $glass_hi;
                                 border-radius: ${r_card}px; padding: ${sp_4}px;
                                 font-size: ${fs_sm}px; }

    /* ---- Theatre: game page stage + drawer, spotlight Home (w-55) ------- */
    /* Scoped to Theatre (its sheet is built at THEATRE_SCALE); text sits on
       art behind a dark scrim, so it is on_art in every theme. */
    #Theatre QPushButton#PlayBig { font-size: ${fs_display}px; border-radius: ${r_card}px; }
    #Theatre QPushButton#TabItem { font-size: ${fs_base}px; }
    #Theatre[art="true"] QPushButton#TabItem, #TheatreDetails QPushButton#TabItem {
        color: $on_art; }
    #Theatre[art="true"] QPushButton#TabItem:checked,
    #TheatreDetails QPushButton#TabItem:checked { color: $on_art; font-weight: 600; }
    #Theatre #ShelfTitle { font-size: ${fs_lg}px; }
    QLabel#TheatreMeta { color: $on_art; font-size: ${fs_lg}px; font-weight: 500; }
    QLabel#TheatreAbout { color: $on_art; font-size: ${fs_base}px; }
    QLabel#TheatreSub { color: $on_art; font-size: ${fs_sm}px; }
    QLabel#TheatreBig { color: $on_art; font-size: ${fs_big}px; font-weight: 600; }
    QLabel#TheatreLabel { color: $on_art; font-size: ${fs_xs}px; font-weight: 600; }
    /* Theatre Discover (w-62) */
    #Theatre #TheatreCard { background: $glass; border: 1px solid $glass_hi;
                            border-radius: ${r_card}px; }
    #Theatre #TheatreCard:hover { background: $glass_hi; }
    #Theatre #SetupCard { background: $glass; border: 1px solid $glass_hi;
                          border-radius: ${r_card}px; }
    #Theatre #TheatreReaderScrim { background: rgba(0, 0, 0, 0.55); }
    #Theatre #TheatreReaderPanel { background: $bg; border-left: 1px solid $glass_hi; }
    /* Theatre Downloads rows and the ready banner (w-61) */
    #Theatre #TheatreMoment { background: $glass_hi; border: 1px solid $glass;
                              border-radius: ${r_card}px; }
    #Theatre #TheatreRow { background: $glass; border: 1px solid $glass_hi;
                           border-radius: ${r_card}px; }
    #Theatre #TheatreRow:hover { background: $glass_hi; }
    #Theatre QProgressBar#CardProgress { background: $glass_hi; }
    #Theatre QProgressBar#CardProgress[paused="true"]::chunk { background: $muted; }
    /* Theatre Library chip (w-56) */
    #Theatre QPushButton#StateChip { background: $glass; color: $on_art; border: 1px solid $glass_hi;
                                     border-radius: ${r_card}px; padding: ${sp_2}px ${sp_5}px;
                                     font-size: ${fs_base}px; font-weight: 500; }
    #Theatre QPushButton#StateChip:hover { background: $glass_hi; }
    /* Session end band (w-60) */
    QLabel#SessionBig { color: $on_art; font-size: ${fs_display}px; font-weight: 600; }
    QLabel#SessionUnit { color: $on_art; font-size: ${fs_xs}px; font-weight: 600; }
    #AchChip { background: $glass; border: 1px solid $glass_hi; border-radius: ${r_card}px; }
    QLabel#AchTitle { color: $on_art; font-size: ${fs_base}px; font-weight: 600; }
    #TheatreShot { background: $glass; border-radius: ${r_tile}px; }
    #TheatreDrawer { background: transparent; border: none; }
    #TheatreDrawer[open="true"] { background: $glass_hi; border: 1px solid $glass;
                                  border-bottom: none; border-top-left-radius: ${r_panel}px;
                                  border-top-right-radius: ${r_panel}px; }
    #TheatreNewsRow { background: $glass; border: 1px solid $glass_hi;
                      border-radius: ${r_card}px; }
    #TheatreNewsRow:hover { background: $glass_hi; }
    /* Dawn's surface_2 is cream: on dark glass the bar's track stays glass. */
    #TheatreDrawer QProgressBar#CardProgress { background: $glass; }

    /* ---- Theatre quick menu (w-57): Guide / Start / F10 ------------------ */
    /* The panel's rows are SheetRow; this styles the panel and its header. */
    #QuickPanel { background: $surface_solid; border-right: 1px solid $border; }
    #QuickPanel QLabel#QuickClock { color: $text; font-size: ${fs_quick_clock}px;
                                    font-weight: 600; }
    #QuickPanel QLabel#QuickDate { color: $subtext; font-size: ${fs_sm}px; }
    #QuickPanel #QuickPad { background: $surface_1; border-radius: ${r_card}px; }
    #QuickPanel QLabel#QuickPadName { color: $text; font-size: ${fs_sm}px; }
    #QuickPanel QLabel#QuickPadPct { color: $subtext; font-size: ${fs_sm}px; font-weight: 600; }
    /* Now playing (w-59): the running game above its Resume / Quit game. */
    #QuickPanel #NowCard { background: $surface_1; border-radius: ${r_card}px; }
    #QuickPanel QLabel#NowSub { color: $success; font-size: ${fs_xs}px; font-weight: 600; }
    #QuickPanel QLabel#NowTitle { color: $text; font-size: ${fs_base}px; font-weight: 600; }

    /* ---- Theatre settings sheet and Your week (w-63) --------------------- */
    /* The sheet is #Sheet (above); its rows are CouchRow: title left, value right. */
    QPushButton#CouchRow { background: transparent; border: 1px solid transparent;
                           border-radius: ${r_control}px; }
    QPushButton#CouchRow:hover, QPushButton#CouchRow:focus { background: $accent_16; }
    QLabel#CouchTitle { color: $text; font-size: ${fs_sm}px; }
    QLabel#CouchValue { color: $subtext; font-size: ${fs_sm}px; font-weight: 500; }
    QLabel#CouchTitle:disabled, QLabel#CouchValue:disabled { color: $muted; }
    QLabel#CouchHelp { color: $muted; font-size: ${fs_xs}px; }
    #WeekCard { background: $surface; border: 1px solid $border_card;
                border-radius: ${r_card}px; }
    QLabel#WeekKicker { color: $muted; font-size: ${fs_2xs}px; font-weight: 600; }
    QLabel#WeekBig { color: $text; font-size: ${fs_big}px; font-weight: 600; }
    QLabel#WeekLine { color: $subtext; font-size: ${fs_xs}px; }
    QLabel#WeekGame { color: $text; font-size: ${fs_sm}px; font-weight: 600; }

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
        r_scroll=pill(SP['2_5']), r_bar=pill(6), r_more=pill(28), r_tag=pill(22),
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
        fs_quick_clock=int(round(FS['big'] * 1.5 * scale)),
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
