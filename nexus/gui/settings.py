"""The Settings page: an Overview, then one category at a time (Look and
feel, Library, Playing, Installing, Alerts, Accounts and keys, System),
each in groups with an Advanced disclosure at its foot, and a search across
all of them. Everything saves itself — a switch or a menu when it
changes, a text field when you leave it — so there is no Save button to
find and nothing is lost by walking away."""

import html
import json
import os
import re
import time
from pathlib import Path

from PyQt6.QtCore import (pyqtSignal, QEasingCurve, QPointF, QPropertyAnimation, QRectF,
    Qt, QTimer, QUrl)
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QIcon, QKeySequence, QLinearGradient,
    QPainter, QPen, QRadialGradient, QShortcut)
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QCheckBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLayout, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget)

from nexus import __version__, gamedb, platform, stats, updates
from nexus.features import has

from .common import ATTRACT_MINUTES, CONFIG_PATH, HOME, INSTALL_BASE_DEFAULT, VOLUME_STEPS
from .theme import (_hex_ok, _mix, accent_hex, accent_pair, ACCENTS, APPEARANCE_DEFAULT,
    CORNERS, G, normalize_appearance, P, pill, PRESET_KEYS, PRESETS, R, R_BASE, SCALES, SP,
    THEMES)

SGDB_KEY_URL = 'https://www.steamgriddb.com/profile/preferences/api'
STEAM_KEY_URL = 'https://steamcommunity.com/dev/apikey'
STEAM_PRIVACY_URL = 'https://steamcommunity.com/my/edit/settings'
STEAM_KEY_ENV = 'STEAM_WEB_API_KEY'
STEAM_KEY_RE = re.compile(r'^[0-9A-Fa-f]{32}$')
STEAMID_RE = re.compile(r'^7656119\d{10}$')
LOCAL_URL_RE = re.compile(r'^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$')
FIELD_W = (200, 340)           # text fields on the right of a row: they share a narrow one
MOTION = not os.environ.get('NEXUS_NO_MOTION')
BLOCKED = {'NEXUS_NO_STATS': "NEXUS_NO_STATS", 'DO_NOT_TRACK': "DO_NOT_TRACK",
           'relay': "Send reports anonymously being off"}


class Swatch(QAbstractButton):
    """A colour (or theme preview, look preset, or gradient) to pick, painted;
    checkable and focusable so the controller can choose too."""

    def __init__(self, kind: str, value, tip: str, size=(30, 30)):
        super().__init__()
        self.kind, self.value = kind, value
        self.setCheckable(True)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setFixedSize(*size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Theme and preset previews paint at R_BASE (they show a look, not the
        # current one); the focus ring reads this radius, so it matches.
        rad = R_BASE['control'] if kind in ('theme', 'preset') else pill(size[1])
        self.setStyleSheet(f"border-radius: {rad}px;")

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        pt.setPen(Qt.PenStyle.NoPen)
        if self.kind in ('theme', 'preset'):
            if self.kind == 'preset':
                look = PRESETS[self.value][1]
                t = THEMES[look['theme']][1]
                a, b = accent_pair(look['theme'], look['accent'], look['partner'])
                k = CORNERS[look['corners']]
            else:
                t, (a, b), k = THEMES[self.value][1], (P['accent'], P['accent']), 1.0
            rad = float(R_BASE['control'])
            pt.setBrush(QColor(t['base']))
            pt.drawRoundedRect(r, rad, rad)
            if self.kind == 'preset' and look['glow'] != 'off':
                pt.save()               # the glow, clipped to the preview
                pt.setClipRect(r.adjusted(1, 1, -1, -1))
                glow = QRadialGradient(QPointF(r.right() - 10, r.top() + 6), r.height() * 0.9)
                c = QColor(b)
                c.setAlphaF(0.55 if look['glow'] == 'vivid' else 0.3)
                glow.setColorAt(0.0, c)
                c.setAlphaF(0.0)
                glow.setColorAt(1.0, c)
                pt.setBrush(QBrush(glow))
                pt.drawRoundedRect(r, rad, rad)
                pt.restore()
            card = QRectF(r.left() + 8, r.top() + 8, r.width() * 0.55, r.height() - 16)
            pt.setBrush(QColor(_mix(t['base'], t['overlay'], 0.6)))
            pt.drawRoundedRect(card, 4 * k, 4 * k)
            pt.setBrush(QColor(t['text']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.top() + 7, card.width() - 18, 4), 2, 2)
            pt.setBrush(QColor(t['muted']))
            pt.drawRoundedRect(QRectF(card.left() + 6, card.top() + 15, card.width() - 28, 3), 1.5, 1.5)
            bar = QRectF(card.left() + 6, card.bottom() - 11, 26, 6)
            g = QLinearGradient(bar.topLeft(), bar.topRight())
            g.setColorAt(0.0, QColor(a))
            g.setColorAt(1.0, QColor(b))
            pt.setBrush(QBrush(g))
            pt.drawRoundedRect(bar, 3 * min(k, 1.0), 3 * min(k, 1.0))
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
            if self.kind in ('theme', 'preset'):
                pt.drawRoundedRect(ring, float(R_BASE['control']), float(R_BASE['control']))
            else:
                pt.drawEllipse(ring)
        pt.end()


class Switch(QCheckBox):
    """An on/off switch. A QCheckBox underneath (checked, toggled, setChecked
    all work as before), painted as a track and knob so a row of settings
    reads at a glance. Its row's title is its accessible name."""
    W, H = 40, 22

    def __init__(self, name: str = ''):
        super().__init__()
        self.setObjectName("Switch")
        self.setAccessibleName(name)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on, enabled = self.isChecked(), self.isEnabled()
        off = _mix(P['overlay'], P['muted'], 0.3)
        track = QColor(P['accent'] if on else off)
        if self.underMouse() and enabled and not on:
            track = QColor(_mix(off, P['accent'], 0.3))
        if not enabled:
            track.setAlphaF(0.35)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(track)
        pt.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.right() - 3 - d if on else r.left() + 3
        knob = QColor(P['on_accent'] if on else P['subtext'])
        if not enabled:
            knob.setAlphaF(0.5)
        pt.setBrush(knob)
        pt.drawEllipse(QRectF(x, r.top() + 3, d, d))
        pt.end()


class SettingRow(QWidget):
    """One setting: its name and a line about it on the left, the control on
    the right (or underneath, `wide`). Clicking the words flips a switch.
    Search matches the title, the description and any `keywords`. `key`
    names it for links ('runner'; the page makes it 'playing.runner')."""

    def __init__(self, title: str, desc: str = '', control=None, wide: bool = False,
                 keywords: str = '', field: bool = False, key: str = ''):
        super().__init__()
        self.key = key
        self.available = True             # False: doesn't apply right now (stays hidden)
        self.gate: SettingRow | None = None     # the row that makes this one apply
        self._match = True
        self._keys = f"{title} {desc} {keywords}".lower()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.rule = QFrame()
        self.rule.setObjectName("RowRule")
        self.rule.setFixedHeight(1)
        outer.addWidget(self.rule)
        lay = QVBoxLayout()
        lay.setContentsMargins(0, SP['3'], 0, SP['3'])
        lay.setSpacing(SP['2'])
        outer.addLayout(lay)
        top = QHBoxLayout()
        top.setSpacing(SP['4'])
        texts = QVBoxLayout()
        texts.setSpacing(SP['0_5'])
        self.title = label(title, "RowTitle", wrap=True)
        texts.addWidget(self.title)
        self.desc = label(desc, "Muted", wrap=True)
        texts.addWidget(self.desc)
        top.addLayout(texts, 1)
        self.switch = control if isinstance(control, Switch) else None
        if self.switch is not None and not control.accessibleName():
            control.setAccessibleName(title)
        holder = None
        if control is not None:
            holder = control if isinstance(control, QWidget) else QWidget()
            if isinstance(control, QLayout):
                control.setContentsMargins(0, 0, 0, 0)
                holder.setLayout(control)
            if field and not wide:
                holder.setMinimumWidth(FIELD_W[0])
                holder.setMaximumWidth(FIELD_W[1])
        if holder is not None and not wide:
            top.addWidget(holder, 1 if field else 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(top)
        if holder is not None and wide:
            lay.addWidget(holder)
        self.error = label("", "RowError", wrap=True)
        lay.addWidget(self.error)
        self.note = label("", None, wrap=True)       # a caution, not an error
        self.note.setStyleSheet(f"color: {P['warning']};")
        lay.addWidget(self.note)
        # Only now: until `top` joined the row, its labels had no parent, and
        # showing one then would flash it up as a window of its own.
        self.desc.setHidden(not desc)
        self.error.hide()
        self.note.hide()

    def set_note(self, text: str | None):
        self.note.setText(text or "")
        self.note.setVisible(bool(text))

    def set_description(self, text: str):
        self.desc.setText(text)
        self.desc.setVisible(bool(text))

    def set_error(self, text: str | None):
        self.error.setText(text or "")
        self.error.setVisible(bool(text))

    def matches(self, q: str) -> bool:
        return all(w in self._keys for w in q.split())

    def set_match(self, on: bool):
        self._match = on
        self.setVisible(self.available and on)

    def set_available(self, on: bool):
        self.available = on
        self.setVisible(on and self._match)

    def mouseReleaseEvent(self, ev):
        sw = self.switch
        if sw is not None and sw.isEnabled() and ev.button() == Qt.MouseButton.LeftButton:
            sw.toggle()
            sw.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mouseReleaseEvent(ev)


class SettingsSection(QWidget):
    """A heading (icon, title, optional buttons on the right), a line saying
    what the group is for, and a card of SettingRows with a hairline
    between each two that are showing. No title: the card alone (Advanced)."""

    def __init__(self, key: str, title: str, glyph: str, desc: str = ''):
        super().__init__()
        self.key, self.title, self.glyph = key, title, glyph
        self.config_backed = True       # greyed while config.toml can't be read
        self._keys = f"{title} {desc}".lower() if title else ''
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        if title:
            head.addWidget(icon_label(G[glyph], 17, P['accent']))
            head.addWidget(label(title, "ShelfTitle"))
            head.addStretch()
            lay.addLayout(head)
        self.actions = head
        self.desc = label(desc, "Muted", wrap=True)
        lay.addWidget(self.desc)
        self.desc.setHidden(not desc)
        self.card = QFrame()
        self.card.setObjectName("Card")
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(SP['4'], SP['1'], SP['4'], SP['1'])
        self.body.setSpacing(0)
        lay.addWidget(self.card)
        self.rows: list[SettingRow] = []

    def add(self, row: SettingRow) -> SettingRow:
        self.body.addWidget(row)
        self.rows.append(row)
        self.relayout()
        return row

    def relayout(self):
        first = True
        for row in self.rows:
            if row.available and row._match:
                row.rule.setVisible(not first)
                first = False

    def match(self, q: str, whole: bool = False):
        """Mark the rows matching `q` (all of them when the group's own
        name matches, or `whole`)."""
        whole = whole or not q or bool(self._keys) and all(w in self._keys for w in q.split())
        for row in self.rows:
            row.set_match(whole or row.matches(q))

    def settle(self) -> int:
        """Show the group if any row is left showing; how many are."""
        n = sum(1 for r in self.rows if r.available and r._match)
        self.setVisible(n > 0)
        self.relayout()
        return n


class SettingsCategory(QWidget):
    """One place in the Settings list (Playing, Installing…): a heading,
    its groups, and an Advanced disclosure at the foot for the rarer rows.
    A group, or Advanced, hides while none of its rows applies."""

    def __init__(self, key: str, title: str, glyph: str, grad: str, desc: str):
        super().__init__()
        self.key, self.title, self.glyph, self.grad = key, title, glyph, grad
        self._own = f"{title} {desc}".lower()   # the category's own words
        self._keys = self._own                  # and every row's (the Ctrl+K palette)
        self._searching = False                 # Advanced opened by a search
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(SP['6'])
        head = QVBoxLayout()
        head.setSpacing(SP['1'])
        head.addWidget(label(title, "CategoryTitle"))
        head.addWidget(label(desc, "Muted", wrap=True))
        self.lay.addLayout(head)
        self.groups: list[SettingsSection] = []
        self.adv_body = SettingsSection(f'{key}.adv', '', 'tune')
        self.adv = Disclosure(f'settings-{key}-adv', "Advanced", G['tune'], self.adv_body)
        self.lay.addWidget(self.adv)

    def group(self, sec: SettingsSection) -> SettingsSection:
        self.lay.insertWidget(self.lay.count() - 1, sec)     # above Advanced
        self.groups.append(sec)
        return sec

    @property
    def rows(self) -> list[SettingRow]:
        return [r for sec in (*self.groups, self.adv_body) for r in sec.rows]

    def filter(self, q: str) -> int:
        """Show the rows matching `q` ('' = all that apply), opening Advanced
        while a search finds something in it; how many rows show."""
        whole = not q or all(w in self._own for w in q.split())
        secs = (*self.groups, self.adv_body)
        for sec in secs:
            sec.match(q, whole)
        for row in self.rows:                   # found, but off: show what turns it on
            if row.gate is not None and row._match and not row.available:
                row.gate.set_match(True)
        n = sum(sec.settle() for sec in self.groups)
        # Advanced's card: the Disclosure shows or hides it (open or not).
        more = sum(1 for r in self.adv_body.rows if r.available and r._match)
        self.adv_body.relayout()
        self.adv.setVisible(more > 0)
        self.adv.set_summary(f"{more} more")
        if q and more:
            if not self.adv.is_open:
                self.adv.set_open(True, remember=False)
                self._searching = True
        elif self._searching:
            self.adv.set_open(Disclosure.OPEN.get(self.adv.key, False), remember=False)
            self._searching = False
        return n + more


class AppearanceCard(SettingsSection):
    """Looks (one-click presets), theme, accent, gradient, glow, corners —
    applied as you click, saved straight away like everything else here."""
    changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__('appearance', "Appearance", 'palette',
                         "How Nexus looks. Changes show at once.")
        self.ap = dict(APPEARANCE_DEFAULT)
        reset = QPushButton("Reset")
        reset.setObjectName("Ghost")
        reset.setToolTip("Back to the default look")
        set_glyph_icon(reset, G['reset'], 14)
        reset.clicked.connect(lambda: self.load(dict(APPEARANCE_DEFAULT), emit=True))
        self.actions.addWidget(reset)

        self.groups: dict[str, list[Swatch]] = {}
        # Looks: a preview of each preset with its name under it.
        flow = FlowLayout(None, 0, SP['3'])
        for key, (name, _look) in PRESETS.items():
            cell = QWidget()
            col = QVBoxLayout(cell)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(SP['1'])
            sw = Swatch('preset', key, name, (112, 60))
            sw.clicked.connect(lambda _=False, k=key: self._preset(k))
            col.addWidget(sw)
            col.addWidget(label(name, "Muted"), 0, Qt.AlignmentFlag.AlignHCenter)
            flow.addWidget(cell)
            self.groups.setdefault('preset', []).append(sw)
        self.preset_row = self.add(SettingRow(
            "Looks", "Theme, accent, gradient, glow and corners in one click.", flow,
            wide=True, keywords="preset style quick neon contrast accessibility", key='looks'))
        flow = FlowLayout(None, 0, SP['2'])
        for key, (name, _t) in THEMES.items():
            sw = Swatch('theme', key, name, (96, 56))
            sw.clicked.connect(lambda _=False, k=key: self._set('theme', k))
            flow.addWidget(sw)
            self.groups.setdefault('theme', []).append(sw)
        self.theme_row = self.add(SettingRow("Theme", "", flow, wide=True,
                                             keywords="dark light colours palette high contrast",
                                             key='theme'))

        row = FlowLayout(None, 0, SP['1_5'])     # wraps in a narrow window
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
        row.addWidget(self.custom)
        self.add(SettingRow("Accent colour", "Buttons, highlights and the focus ring.", row,
                            wide=True, keywords="color hex", key='accent'))

        row = FlowLayout(None, 0, SP['1_5'])
        self.partner_row = row
        self.add(SettingRow("Gradient", "A second colour the accent blends into.", row,
                            wide=True, keywords="color", key='gradient'))

        self.glow = SegmentedControl('pill', 32)
        for k, t in (('off', "Off"), ('subtle', "Subtle"), ('vivid', "Vivid")):
            self.glow.add(k, t)
        self.glow.changed.connect(lambda k: self._set('glow', k))
        self.add(SettingRow("Background glow", "Soft colour behind the pages.", self.glow,
                            key='glow'))
        self.art_tint = Switch()
        self.art_tint.toggled.connect(lambda on: self._set('art_tint', on))
        self.add(SettingRow("Tint the glow with game art",
                            "Takes its colour from the featured game. Needs the glow on.",
                            self.art_tint, key='art_tint'))

        self.size = SegmentedControl('pill', 32)
        for v in SCALES:
            self.size.add(str(v), f"{int(round(v * 100))}%")
        self.size.changed.connect(lambda k: self._set('scale', float(k)))
        self.add(SettingRow("Size", "Text and spacing everywhere — larger for a TV or a "
                            "high-resolution screen.", self.size,
                            keywords="scale zoom font bigger smaller dpi", key='size'))

        self.corners = SegmentedControl('pill', 32)
        for k, t in (('sharp', "Sharp"), ('default', "Soft"), ('round', "Round")):
            self.corners.add(k, t)
        self.corners.changed.connect(lambda k: self._set('corners', k))
        self.add(SettingRow("Corners", "How round cards, buttons and fields are.", self.corners,
                            keywords="radius", key='corners'))

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
        self.theme_row.set_description(THEMES[self.ap['theme']][0])
        on = self._current_preset()
        for sw in self.groups['preset']:
            sw.setChecked(sw.value == on)
        self.glow.set_current(self.ap['glow'], animate=False)
        self.corners.set_current(self.ap['corners'], animate=False)
        self.size.set_current(str(self.ap['scale']), animate=False)
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

    def _current_preset(self) -> str | None:
        return next((k for k, (_n, look) in PRESETS.items()
                     if all(self.ap[f] == look[f] for f in PRESET_KEYS)), None)

    def _preset(self, key: str):
        """Apply a look: one change (one re-style), not five."""
        look = PRESETS[key][1]
        if all(self.ap[f] == look[f] for f in PRESET_KEYS):
            self._sync()
            return
        self.ap.update({f: look[f] for f in PRESET_KEYS})
        self._sync()
        self.changed.emit(dict(self.ap))

    def _custom(self):
        v = self.custom.text().strip()
        if not v.startswith('#'):
            v = '#' + v
        if _hex_ok(v):
            self._set('accent', v.lower())


def _combo(items, min_w: int = 260) -> 'NoWheelCombo':
    box = NoWheelCombo()
    for text, val in items:
        box.addItem(text, val)
    box.setMinimumWidth(min_w)
    return box


SESSIONS_NOTE = ("What you play in Nexus, with the store and title, goes to Game-db, where "
                 "your group sees it.")
LIBRARY_NOTE = ("Once a day, the Epic, GOG and Amazon games Heroic says you own, so Game-db "
                "can fill in their game pages here too.")
TOKEN_NOTE = "Kept in config.toml, readable only by you; data is cached 45 minutes."
FRIENDS_DESC = ("What your friends play and own, on Home, game pages and the Library. Nexus "
                "only reads what Steam already shows; nothing about you is sent anywhere.")
FRIENDS_KEY_NOTE = ("Free from Steam; any domain name works, such as localhost. Kept in "
                    "config.toml, readable only by you.")
FRIENDS_PRIVACY_NOTE = ("Friends show what they own only when their game details are public "
                        "on Steam (\u201cfriends only\u201d counts as private). If Steam won't "
                        "list your friends, make your own friends list public too.")


def _ago_text(iso: str | None, now: float | None = None) -> str | None:
    """'updated 3 min ago' for an ISO time from the CLI."""
    if not iso:
        return None
    from datetime import datetime
    try:
        ts = datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None
    mins = int(((time.time() if now is None else now) - ts) // 60)
    if mins < 1:
        return "updated just now"
    if mins < 60:
        return f"updated {mins} min ago"
    if mins < 48 * 60:
        return f"updated {mins // 60} h ago"
    return f"updated {mins // 1440} days ago"


def friends_status_text(data: dict | None, err: str = '', now: float | None = None) -> str:
    """The Settings status line for a `--friends --json` answer:
    '10 friends · 7 public · 3 private · updated 2 min ago'. Its error is
    shown apart (friends_problem)."""
    if not data:
        return f"Couldn't ask: {err or 'no answer'}"
    c = data.get('counts') or {}
    total = c.get('total') or 0
    error = data.get('error') or None
    parts = []
    if total:
        parts.append(f"{total} friend{'' if total == 1 else 's'} \u00b7 {c.get('public', 0)} "
                     f"public \u00b7 {c.get('private', 0)} private")
    elif data.get('updated') and not error:
        parts.append("No friends on this Steam account")
    ago = _ago_text(data.get('updated'), now)
    if ago and parts:
        parts.append(ago)
    if parts:
        return " \u00b7 ".join(parts)
    return "Nothing found yet." if error else "Not asked yet: Refresh now asks Steam."


def friends_problem(data: dict | None) -> tuple[str | None, str | None]:
    """(error, note) for a `--friends` answer: a refused key, no account or a
    private list is an error to fix; too many requests or no network only a
    note (the last good copy is still shown)."""
    e = (data or {}).get('error') or None
    if not e:
        return None, None
    msg = e.get('message') or e.get('kind') or "error"
    return (msg, None) if e.get('kind') in ('bad-key', 'no-account', 'private-list') \
        else (None, msg)


def token_note(scopes) -> str:
    """The token row's description: what this token may do, as Game-db's
    v2/meta last said (meta.json; None = not asked yet)."""
    if scopes is None:
        return TOKEN_NOTE
    can = [what for scope, what in ((gamedb.SCOPE_SESSIONS, "upload play sessions"),
                                    (gamedb.SCOPE_LIBRARY, "send your library"))
           if scope in scopes]
    return ("Can read" + ''.join(f", {c}" for c in can[:-1])
            + (f" and {can[-1]}" if can else " only") + ". " + TOKEN_NOTE)


def torrent_problem(data: dict | None) -> str | None:
    """What the last torrent poll said went wrong (None: it answered, or
    no client is set up)."""
    data = data or {}
    if data.get('configured') is False:
        return None
    return (data.get('error') or {}).get('message') or None


# Old section keys (links, palette history) and where they live now.
OLD_KEYS = {'appearance': ('look', None), 'controls': ('playing', None),
            'running': ('playing', 'proton'), 'artwork': ('accounts', 'sgdb_key'),
            'gamedb': ('accounts', 'deals'), 'torrent': ('installing', 'torrent_client'),
            'background': ('alerts', 'agent'), 'backup': ('system', 'backup'),
            'about': ('system', 'version')}

# The six category cards on Overview (System gets quiet links instead).
OVERVIEW_CATS = ('look', 'library', 'playing', 'installing', 'alerts', 'accounts')


class SettingsOverview(QWidget):
    """Where Settings opens: who this Nexus is (a placeholder until the
    profile, phase 5), up to three things worth a look, a card per category
    saying what is set there, and quiet links into System."""
    wanted = pyqtSignal(str, str)       # (category, row) to open
    setup_wanted = pyqtSignal()

    def __init__(self, cats: dict[str, 'SettingsCategory']):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['6'])
        card = QFrame()
        card.setObjectName("Card")
        h = QHBoxLayout(card)
        h.setContentsMargins(SP['5'], SP['5'], SP['5'], SP['5'])
        h.setSpacing(SP['4'])
        h.addWidget(GlyphBadge(G['settings'], 'dusk', 56), 0, Qt.AlignmentFlag.AlignVCenter)
        v = QVBoxLayout()
        v.setSpacing(SP['1'])
        v.addWidget(label("Your Nexus", "CategoryTitle"))
        v.addWidget(label(f"Nexus {__version__} on {platform.OS_NAME.capitalize()}", "Muted",
                          wrap=True))
        h.addLayout(v, 1)
        lay.addWidget(card)

        # Worth a look: three fixed rows, filled (or hidden) by set_worth.
        self.worth = SettingsSection('worth', "Worth a look", 'sparkles')
        self._targets: list[tuple[str, str]] = []
        self.worth_rows: list[SettingRow] = []
        for i in range(3):
            b = QPushButton()
            b.clicked.connect(lambda _=False, i=i: self.wanted.emit(*self._targets[i]))
            row = self.worth.add(SettingRow("", "", b))
            row.button = b
            self.worth_rows.append(row)
            self._targets.append(('overview', ''))
        lay.addWidget(self.worth)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(SP['3'])
        self.grid.setVerticalSpacing(SP['3'])
        hold = QWidget()
        hold.setLayout(self.grid)
        lay.addWidget(hold)
        self.cards: dict[str, ClickRow] = {}
        self.lines: dict[str, ElideLabel] = {}
        for key in OVERVIEW_CATS:
            cat = cats[key]
            row = ClickRow()
            row.setObjectName("OverviewCard")
            row.setAccessibleName(cat.title)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(SP['4'], SP['3'], SP['3'], SP['3'])
            rl.setSpacing(SP['3'])
            rl.addWidget(GlyphBadge(G[cat.glyph], cat.grad, 36), 0,
                         Qt.AlignmentFlag.AlignVCenter)
            tv = QVBoxLayout()
            tv.setSpacing(SP['0_5'])
            tv.addWidget(label(cat.title, "RowTitle"))
            line = ElideLabel("", "Muted")
            tv.addWidget(line)
            rl.addLayout(tv, 1)
            rl.addWidget(icon_label(G['chevron_right'], 16, P['muted']))
            for w in row.findChildren(QWidget):
                w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row.clicked.connect(lambda k=key: self.wanted.emit(k, ''))
            self.cards[key] = row
            self.lines[key] = line
        self.set_columns(2)

        # System is a place you go for a task, not to browse: quiet links.
        links = QHBoxLayout()
        links.setContentsMargins(0, 0, 0, 0)
        links.setSpacing(SP['1'])
        links.addStretch()
        for text, glyph, target in (("Back up & move", 'save', 'backup'),
                                    ("Check for updates", 'update', 'check_updates'),
                                    ("Run setup again", 'refresh', None)):
            b = QPushButton(amp(text))
            b.setObjectName("Ghost")
            set_glyph_icon(b, G[glyph], 14, 'subtext', hover_role='text')
            if target:
                b.clicked.connect(lambda _=False, r=target: self.wanted.emit('system', r))
            else:
                b.clicked.connect(self.setup_wanted)
            links.addWidget(b)
        hold = QWidget()
        hold.setLayout(links)
        self.links = hold
        lay.addWidget(hold)

    def set_columns(self, n: int):
        """Two columns of cards, one in a narrow window."""
        for i, key in enumerate(OVERVIEW_CATS):
            card = self.cards[key]
            self.grid.removeWidget(card)
            self.grid.addWidget(card, i // n, i % n)
        for c in range(2):
            self.grid.setColumnStretch(c, 1 if c < n else 0)

    def set_states(self, lines: dict[str, str]):
        for key, text in lines.items():
            if key in self.lines:
                self.lines[key].setText(text)
                self.lines[key].setToolTip(text)

    def set_worth(self, items: list[tuple[str, str, str, str, str]]):
        """(title, line, button, category, row), at most three; none hides it."""
        for i, row in enumerate(self.worth_rows):
            item = items[i] if i < len(items) else None
            row.set_available(item is not None)
            if item is None:
                continue
            title, desc, button, cat, key = item
            row.title.setText(title)
            row.set_description(desc)
            row.button.setText(amp(button))
            self._targets[i] = (cat, key)
        self.worth.settle()


class SettingsPage(QWidget):
    """Edits the config.toml keys the GUI owns; everything else in the file
    (comments, unknown keys) is preserved by save_config(). Library
    preferences (controls, show-owned, appearance) go to library.json through
    the window, as signals."""
    request = pyqtSignal(list, str, bool, str)
    saved = pyqtSignal(bool)            # proton_changed
    controls_changed = pyqtSignal(dict)  # a library preference, applied at once
    background_changed = pyqtSignal(str, bool)   # ('agent' | 'tray', on)
    import_wanted = pyqtSignal(str)              # a settings file to look at, then import
    setup_wanted = pyqtSignal()                  # "Run setup again"
    whats_new_wanted = pyqtSignal()              # About: "What's new"
    update_found = pyqtSignal(dict)              # "Check for updates" found one (the Updater)
    update_wanted = pyqtSignal()                 # About: Update / Restart to update
    sound_test_wanted = pyqtSignal()             # Controls: "Test sound"

    NAV_W, RAIL_W = 228, 60
    COMPACT_BELOW = 960            # narrower: the list becomes an icon rail

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._compact = False
        page = QHBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)
        page.addWidget(self._build_nav())
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.scroll.setWidget(host)
        page.addWidget(self.scroll, 1)
        center = QHBoxLayout(host)
        center.setContentsMargins(SP['6'], SP['4'], SP['6'], SP['8'])
        center.addStretch(1)
        col = QWidget()
        col.setMaximumWidth(780)
        col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        center.addWidget(col, 6)
        center.addStretch(1)
        self.content = QVBoxLayout(col)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(SP['6'])
        self.error = label("", "Banner", wrap=True)
        self.error.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        self.error.setVisible(False)
        self.content.addWidget(self.error)
        self.no_match = label("", "Empty", wrap=True)
        self.no_match.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.no_match.setVisible(False)
        self.content.addWidget(self.no_match)

        # One category shows at a time (Overview first); a search shows
        # every category with a match, one under the other.
        self.categories: dict[str, SettingsCategory] = {}
        self.groups: dict[str, SettingsSection] = {}    # 'installing.torrent'
        self.row_of: dict[QWidget, SettingRow] = {}
        self.appearance = AppearanceCard()
        self._build_look()
        self._build_library()
        self._build_playing()
        self._build_installing()
        self._build_alerts()
        self._build_accounts()
        self._build_system()
        for control, feature in ((self.heroic, 'heroic'), (self.runner, 'proton'),
                                 (self.proton, 'proton'), (self.al_proton, 'proton')):
            if not has(feature):                # a Linux-only setting
                self.row_of[control].set_available(False)
        self._index_rows()
        self.overview = SettingsOverview(self.categories)
        self.content.insertWidget(2, self.overview)
        self.overview.wanted.connect(self.open_at)
        self.overview.setup_wanted.connect(self.setup_wanted)
        self.appearance.changed.connect(self._appearance_changed)
        self._build_nav_items()
        self.content.addStretch()

        bar = self.scroll.verticalScrollBar()
        self._anim = QPropertyAnimation(bar, b"value", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(2500)
        self._flash.timeout.connect(self._saved_note_idle)
        for keys, slot in (("Ctrl+S", self.commit), ("Ctrl+F", self._focus_search)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

        self.cfg = {}
        self._current = 'overview'
        self._scroll_row: SettingRow | None = None
        self._torrent_problem: str | None = None
        self._proton_current = None
        self._loading = False
        self._loaded = False                    # nothing saves before the first load()
        self._pending = False                   # a text field edited, not yet saved
        self._invalid: dict[QWidget, str] = {}  # fields whose text can't be saved
        self.gamedb_changed = False
        self._agent_state = None
        self._wire()
        self._apply_filter()

    # ---- building ----
    def _build_nav(self) -> QWidget:
        nav = self.nav = QWidget()
        nav.setObjectName("SettingsNav")
        nav.setFixedWidth(self.NAV_W)
        lay = QVBoxLayout(nav)
        lay.setContentsMargins(SP['5'], SP['4'], SP['3'], SP['4'])
        lay.setSpacing(SP['1'])
        # Title, save note and search: atop the list, or atop the content
        # when the window is narrow (see _set_compact).
        self.head = QWidget()
        head = QVBoxLayout(self.head)
        head.setContentsMargins(0, 0, 0, SP['2'])
        head.setSpacing(SP['1'])
        head.addWidget(label("Settings", "PageTitle"))
        self.saved_note = label("", "SavedNote", wrap=True)
        head.addWidget(self.saved_note)
        self._saved_note_idle()
        head.addSpacing(SP['2'])
        lay.addWidget(self.head)
        self.search = QLineEdit()
        self.search.setObjectName("SettingsSearch")
        self.search.setPlaceholderText("Search settings")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(36)            # r_search is a pill at 36
        self.search.addAction(QIcon(glyph_pixmap(G['search'], P['muted'], 14)),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._filter)
        head.addWidget(self.search)
        self.nav_lay = lay
        self.nav_btns: dict[str, QPushButton] = {}
        lay.addStretch()                        # the foot: the profile, later (phase 5)
        return nav

    def _build_nav_items(self):
        """Overview, the six categories, then System, each set a little apart."""
        at = self.nav_lay.indexOf(self.head) + 1
        items = [('overview', "Overview", 'grid'),
                 *((k, c.title, c.glyph) for k, c in self.categories.items())]
        for i, (key, title, glyph) in enumerate(items):
            if key in ('look', 'system'):
                self.nav_lay.insertSpacing(at, SP['3'])
                at += 1
            b = QPushButton(amp(title))
            b.setObjectName("NavItem")
            b.setAccessibleName(title)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            set_glyph_icon(b, G[glyph], 16, 'muted', on_role='accent')
            b.clicked.connect(lambda _=False, k=key: self.open_at(k))
            self.nav_lay.insertWidget(at, b)
            at += 1
            self.nav_btns[key] = b

    def _add_category(self, key: str, title: str, glyph: str, grad: str,
                      desc: str) -> SettingsCategory:
        cat = SettingsCategory(key, title, glyph, grad, desc)
        self.content.addWidget(cat)             # parented first, then hidden
        cat.hide()
        self.categories[key] = cat
        return cat

    def _group(self, cat: SettingsCategory, key: str, title: str, glyph: str,
               desc: str = '', config_backed: bool = True) -> SettingsSection:
        sec = cat.group(SettingsSection(key, title, glyph, desc))
        sec.config_backed = config_backed
        self.groups[f'{cat.key}.{key}'] = sec
        return sec

    def _index_rows(self):
        """Every row by its full key ('playing.runner'), for links and tests;
        the palette finds a category by its rows' words too."""
        self.rows: dict[str, SettingRow] = {}
        for cat in self.categories.values():
            for row in cat.rows:
                row.key = f'{cat.key}.{row.key}'
                self.rows[row.key] = row
                cat._keys += ' ' + row._keys

    def _row(self, sec, title, desc, control, **kw) -> SettingRow:
        row = sec.add(SettingRow(title, desc, control, **kw))
        widget = control if isinstance(control, QWidget) else None
        if widget is not None:
            self.row_of[widget] = row
        return row

    def _field_row(self, sec, title, desc, edit, browse=False, extra=(), **kw) -> SettingRow:
        """A text field (plus a folder button or other buttons) on the right."""
        lay = QHBoxLayout()
        lay.setSpacing(SP['1_5'])
        lay.addWidget(edit, 1)
        if browse:
            b = QPushButton()
            b.setToolTip("Choose a folder")
            b.setFixedWidth(36)
            set_glyph_icon(b, G['folder'], 15)
            b.clicked.connect(lambda: self._browse(edit))
            lay.addWidget(b)
        for w in extra:
            lay.addWidget(w)
        row = sec.add(SettingRow(title, desc, lay, field=True, **kw))
        self.row_of[edit] = row
        return row

    @staticmethod
    def _secret(edit: QLineEdit) -> QLineEdit:
        """A password-style field with an eye at its end to show it."""
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        eye = edit.addAction(QIcon(glyph_pixmap(G['eye'], P['muted'], 15)),
                             QLineEdit.ActionPosition.TrailingPosition)
        eye.setToolTip("Show")

        def flip():
            hidden = edit.echoMode() == QLineEdit.EchoMode.Password
            edit.setEchoMode(QLineEdit.EchoMode.Normal if hidden else
                             QLineEdit.EchoMode.Password)
            eye.setIcon(QIcon(glyph_pixmap(G['eye_off' if hidden else 'eye'], P['muted'], 15)))
            eye.setToolTip("Hide" if hidden else "Show")
        eye.triggered.connect(flip)
        return edit

    def _build_look(self):
        cat = self._add_category('look', "Look and feel", 'palette', 'dusk',
                                 "Theme, accent, glow, size and corners, for the whole app.")
        cat.group(self.appearance)
        self.appearance.config_backed = False   # library.json, not config.toml
        self.groups['look.appearance'] = self.appearance

    def _build_library(self):
        cat = self._add_category('library', "Library", 'library', 'aurora',
                                 "Which games the library shows, the News page and artwork.")
        sec = self._group(cat, 'shows', "What it shows", 'library', config_backed=False)
        self.show_owned = Switch()
        self._row(sec, "Show games you own but haven't installed",
                  "Steam games through Game-db, Epic and GOG through Heroic.", self.show_owned,
                  keywords="owned uninstalled epic gog", key='owned')
        from .dashboard import DUP_ORDER           # the order a duplicate is kept in
        names = {'nexus': "repack", 'steam': "Steam", 'heroic': "Heroic", 'epic': "Epic"}
        order = ", then ".join(names[k] for k in DUP_ORDER if k != 'heroic' or has('heroic'))
        self.ctl_dedupe = _combo(((f"Show it once ({order})", True),
                                  ("Show every copy", False)))
        self._row(sec, "A game in several stores", "Which copies the library shows.",
                  self.ctl_dedupe, keywords="duplicate dedupe", key='dedupe')
        self.news = Switch()
        self._row(sec, "News page", "FitGirl's latest posts and news for your games. "
                  "Experimental.", self.news, keywords="feed", key='news')
        sec = self._group(cat, 'artwork', "Artwork", 'image',
                          "Covers, heroes and logos from SteamGridDB.")
        fill = QPushButton("Fetch now")
        set_glyph_icon(fill, G['download'], 14)
        fill.clicked.connect(lambda: self.request.emit(
            ['--fill-art'], "Fetching extra artwork from SteamGridDB", False, 'task'))
        self.fill_art = fill
        self._row(sec, "Fetch missing artwork", "For every game, Steam and Heroic too. "
                  "Art you picked yourself is kept.", fill, keywords="images download",
                  key='fill_art')

    def _build_playing(self):
        cat = self._add_category('playing', "Playing", 'gamepad', 'tide',
                                 "How games start from the mouse, the keyboard or the couch, "
                                 "and how Windows games run.")
        sec = self._group(cat, 'keys', "Mouse and keys", 'play',
                          "What clicks, keys and the controller's A do on a game. A game "
                          "that's running, busy or not installed always opens its page.",
                          config_backed=False)
        self.ctl_double = _combo((("Play the game", 'play'), ("Open its page", 'details')))
        self._row(sec, "Double-click a game", "", self.ctl_double, keywords="mouse",
                  key='double_click')
        self.ctl_a = _combo((("Open its page (press again to play)", 'details'),
                             ("Play the game", 'play')))
        self._row(sec, "Enter or controller A", "", self.ctl_a,
                  keywords="keyboard gamepad button", key='pad_a')
        # Theatre's own settings (w-63); its sheet on the couch shows the same.
        sec = self._group(cat, 'theatre', "Controller and Theatre", 'tv', config_backed=False)
        self.ctl_on_pad = Switch()
        self._row(sec, "Switch to Theatre when a controller connects",
                  "Only when you plug one in or turn it on.", self.ctl_on_pad,
                  keywords="gamepad tv auto", key='theatre_on_pad')
        self.ctl_start = _combo((("Never", 'never'), ("On a TV", 'tv'), ("Always", 'always')))
        self._row(sec, "Start Nexus in Theatre", "On a TV: when the screen is 32\" or bigger.",
                  self.ctl_start, keywords="tv couch startup launch", key='theatre_start')
        self.ctl_attract = _combo([("Never" if not m else f"After {m} minutes idle", m)
                                   for m in ATTRACT_MINUTES])
        self._row(sec, "Theatre screensaver", "Shows off your games when Theatre mode sits "
                  "untouched.", self.ctl_attract, keywords="attract idle tv", key='attract')
        self.ctl_week = Switch()
        self._row(sec, "Your week on Theatre's Home", "Your play time this week, under "
                  "Jump back in.", self.ctl_week, keywords="playtime stats streak",
                  key='week_home')
        self.ctl_sounds = Switch()
        self.sounds_row = self._row(sec, "Menu sounds in Theatre", "Soft clicks as you move "
                                    "and select. They play in Theatre mode only.",
                                    self.ctl_sounds, keywords="audio sfx", key='sounds')
        self.ctl_volume = _combo([(f"{v}%", v) for v in VOLUME_STEPS])
        # Test sound plays here, outside Theatre and with sounds off (w-73).
        self.sound_test = QPushButton("Test sound")
        set_glyph_icon(self.sound_test, G['play'], 14)
        self.sound_test.clicked.connect(self.sound_test_wanted)
        vol = QHBoxLayout()
        vol.setSpacing(SP['1_5'])
        vol.addWidget(self.sound_test)
        vol.addWidget(self.ctl_volume)
        self.volume_row = self._row(sec, "Menu sound volume", "", vol,
                                    keywords="audio loud test", key='volume')
        self.row_of[self.ctl_volume] = self.volume_row
        self.ctl_sounds.toggled.connect(self.ctl_volume.setEnabled)
        sec = self._group(cat, 'windows', "Windows games", 'proton')
        self.proton = _combo(())
        self.proton.setToolTip("A game can override it on its page, under Launch")
        self._row(sec, "Proton version", "Used in launch.sh, Steam and Heroic. A game can "
                  "override it on its page.", self.proton, keywords="ge-proton compatibility",
                  key='proton')
        self.runner = _combo((("Automatic", 'auto'), ("umu-run + Proton", 'umu'),
                              ("Proton (direct)", 'proton'), ("System Wine", 'wine')))
        self._row(cat.adv_body, "Runner", "How Windows games start. Automatic uses umu-run "
                  "with Proton when it's installed, then Proton, then Wine.", self.runner,
                  keywords="umu wine backend", key='runner')

    def set_sound_problem(self, text: str):
        """Why menu sounds can't play ('' when they can), under the switch."""
        self.sounds_row.set_note(text)

    def _build_installing(self):
        cat = self._add_category('installing', "Installing", 'download', 'ember',
                                 "Where games go, repacks, the torrent client and the "
                                 "shortcuts each new game gets.")
        adv = cat.adv_body
        sec = self._group(cat, 'where', "Where games go", 'folder')
        self.base = QLineEdit()
        self.base.setPlaceholderText(INSTALL_BASE_DEFAULT)
        self._field_row(sec, "Install games to", "Each game gets its own folder here.",
                        self.base, browse=True, keywords="folder path location", key='base')
        self.base.textChanged.connect(self._check_base)    # typed or picked
        self._check_base()
        sec = self._group(cat, 'repacks', "Repacks", 'download')
        self.repacks = Switch()
        self._row(sec, "Repack features", "Installing repacks, watching download folders, "
                  "FitGirl matches on Deals, repack update alerts and the torrent client. Off: "
                  "Nexus is a launcher for your other games.", self.repacks,
                  keywords="fitgirl repack torrent downloads", key='repacks')
        self._repack_rows = []
        self.scan_dir = QLineEdit()
        self.scan_dir.setPlaceholderText(str(HOME / 'Downloads'))
        self._repack_rows.append(self._field_row(
            sec, "Look for repacks in", "Nexus says when a download here is ready to install.",
            self.scan_dir, browse=True, keywords="downloads folder scan", key='scan_dir'))
        self.scan_more = QLineEdit()
        self.scan_more.setPlaceholderText(f"e.g. {os.path.join('~', 'Games', 'Install')}")
        self._repack_rows.append(self._field_row(
            adv, "Also look in", "More folders to watch, separated by ;", self.scan_more,
            keywords="folders scan", key='scan_more'))
        self.unattended = Switch()
        self._repack_rows.append(self._row(
            sec, "Install without the setup window", "Runs FitGirl's installer silently; "
            "follow it in Activity.", self.unattended, keywords="silent unattended",
            key='unattended'))
        self.trash_repack = Switch()
        self._repack_rows.append(self._row(
            sec, "Move the repack to the Trash afterwards", "Frees the space once the game is "
            "installed. You can still restore it from the Trash.", self.trash_repack,
            keywords="delete cleanup", key='trash_repack'))
        self._build_torrent(cat)
        sec = self._group(cat, 'shortcuts', "Shortcuts for new games", 'link')
        self.steam = Switch()
        self._row(sec, "Add new games to Steam", "As a non-Steam game, with its artwork.",
                  self.steam, keywords="shortcut", key='steam')
        self.heroic = Switch()
        self._row(sec, "Add new games to Heroic", "", self.heroic, key='heroic')
        self.desktop = Switch()
        if has('desktop_entry'):
            self._row(sec, "Add new games to the app menu", "A launcher entry for each game.",
                      self.desktop, keywords="desktop entry", key='desktop')
        else:                                   # Windows: the same key, as .lnk shortcuts
            self._row(sec, "Start Menu shortcuts", "A shortcut for each new game, in the "
                      "Start Menu's Nexus folder.", self.desktop,
                      keywords="start menu shortcut lnk desktop app menu", key='desktop')

    def _build_torrent(self, cat: SettingsCategory):
        """Installing's Torrent client group; its login and seed ratio go
        under Advanced. Every row but Client needs a client chosen."""
        sec = self._group(cat, 'torrent', "Torrent client", 'magnet',
                          "For the Downloads page and Grab on Deals. The client must run on "
                          "this machine with remote control on: Transmission, Edit → "
                          "Preferences → Remote; qBittorrent, Tools → Options → Web UI.")
        adv = cat.adv_body
        self.tor_client = _combo((("Off", None), ("Transmission", 'transmission'),
                                  ("qBittorrent", 'qbittorrent')))
        gate = self._row(sec, "Client", "", self.tor_client,
                         keywords="transmission qbittorrent", key='torrent_client')
        self.tor_url = QLineEdit()
        self.tor_url.setPlaceholderText("http://127.0.0.1:9091")
        self.tor_rows = [self._field_row(sec, "Remote address", "", self.tor_url,
                                         keywords="url port rpc", key='torrent_url')]
        self.tor_dir = QLineEdit()
        self.tor_dir.setPlaceholderText("the client's own folder")
        self.tor_rows.append(self._field_row(sec, "Download to", "", self.tor_dir, browse=True,
                                             keywords="folder", key='torrent_dir'))
        self.tor_test = QPushButton("Test connection")
        self.tor_test.setToolTip("Asks the client for its version")
        self.tor_test.clicked.connect(self._test_torrent)
        self.tor_test_row = self._row(sec, "Check it works", "", self.tor_test,
                                      key='torrent_test')
        self.tor_result = self.tor_test_row.desc
        self.tor_rows.append(self.tor_test_row)
        self.tor_user = QLineEdit()
        self.tor_user.setPlaceholderText("only if the client asks for one")
        self.tor_rows.append(self._field_row(adv, "Torrent username", "", self.tor_user,
                                             keywords="torrent client login",
                                             key='torrent_user'))
        self.tor_pass = self._secret(QLineEdit())
        self.tor_rows.append(self._field_row(adv, "Torrent password", "", self.tor_pass,
                                             keywords="torrent client login",
                                             key='torrent_password'))
        self.tor_ratio = QLineEdit()
        self.tor_ratio.setPlaceholderText("the client's setting")
        self.tor_rows.append(self._field_row(adv, "Stop seeding at ratio",
                                             "A number like 1.5. Empty keeps seeding as the "
                                             "client says.", self.tor_ratio,
                                             keywords="seed share torrent", key='seed_ratio'))
        for row in self.tor_rows:
            row.gate = gate                     # a search finds them with the client to set

    def _build_alerts(self):
        cat = self._add_category('alerts', "Alerts", 'bell', 'dawn',
                                 "What Nexus tells you about, where, and whether it keeps "
                                 "watching while it's closed.")
        sec = self._group(cat, 'what', "What to tell you", 'bell',
                          "Each alert comes once, on the desktop and to your phone.")
        self.al_atl = Switch()
        self._row(sec, "A wishlist game hits its all-time low", "", self.al_atl,
                  keywords="price deal atl", key='atl')
        self.al_sale = _combo((("Off", 0), ("25% off or more", 25), ("50% off or more", 50),
                               ("75% off or more", 75)), 180)
        self._row(sec, "Any wishlist sale", "", self.al_sale, keywords="discount price deal",
                  key='sale')
        self.al_fitgirl = Switch()
        self._repack_rows.append(self._row(sec, "A wishlist game appears on FitGirl", "",
                                           self.al_fitgirl, keywords="repack", key='fitgirl'))
        self.al_updates = Switch()
        self._repack_rows.append(self._row(sec, "An installed game gets a repack update", "",
                                           self.al_updates, keywords="fitgirl",
                                           key='repack_updates'))
        self.al_proton = Switch()
        self._row(sec, "A new GE-Proton is out", "", self.al_proton, key='proton')
        sec = self._group(cat, 'phone', "Send to your phone", 'cast',
                          "The ntfy topic or KDE Connect devices in config.toml [notify].")
        test = QPushButton("Send a test")
        set_glyph_icon(test, G['bell'], 14)
        test.clicked.connect(lambda: self.request.emit(
            ['--notify-test'], "Sending a test notification", False, 'task'))
        self._row(sec, "Try it", "Pushes a test message to your phone.", test,
                  keywords="notify ntfy kde connect", key='test')
        self._build_background(cat)

    def _build_background(self, cat: SettingsCategory):
        sec = self._group(cat, 'closed', "While Nexus is closed", 'power', config_backed=False)
        self.agent = Switch()
        self.agent.toggled.connect(self._agent_toggled)
        if has('systemd_agent'):
            self.agent.setToolTip("Runs `nexus-cli --agent` as a systemd user service: "
                                  "finished torrents install, ready repacks and alerts are "
                                  "announced, the FitGirl feed stays fresh")
            self.agent_row = self._row(sec, "Keep working while Nexus is closed", "",
                                       self.agent,
                                       keywords="agent systemd service alerts downloads",
                                       key='agent')
        else:
            # Windows: a Startup-folder shortcut to `nexus --hidden`; the
            # window then does the agent's work from the tray.
            self.agent.setToolTip("A shortcut in your Startup folder (shell:startup) that "
                                  "starts Nexus in the tray")
            self.agent_row = self._row(sec, "Start with Windows", "", self.agent,
                                       keywords="startup sign in login boot agent background "
                                       "alerts downloads tray", key='agent')
        self.agent_note = self.agent_row.desc
        self.tray = Switch()
        self.tray.toggled.connect(lambda on: self.controls_changed.emit({'close_to_tray': on}))
        self.tray_row = self._row(sec, "Close to the tray", "", self.tray,
                                  keywords="system tray minimise hidden", key='tray')
        self.tray_note = self.tray_row.desc

    def _build_accounts(self):
        cat = self._add_category('accounts', "Accounts and keys", 'link', 'aurora',
                                 "Game-db, SteamGridDB, the Steam account Nexus uses and "
                                 "your Steam friends.")
        sec = self._group(cat, 'gamedb', "Game-db", 'deals',
                          "Wishlist deals, upcoming releases and your backlog, from your "
                          "Game-db account.")
        adv = cat.adv_body
        self.gdb_enabled = Switch()
        self._row(sec, "Deals page", "Show it in the top bar.", self.gdb_enabled,
                  keywords="wishlist releases backlog", key='deals')
        self.gdb_url = QLineEdit()
        self.gdb_url.setPlaceholderText(gamedb.EXAMPLE_URL)
        self._field_row(sec, "Address", "", self.gdb_url, keywords="url server game-db",
                        key='gamedb_url')
        self.gdb_token = self._secret(QLineEdit())
        self.gdb_token.setPlaceholderText("gdb_…")
        get = QPushButton("Create one")
        get.setToolTip("Opens your Game-db account page in the browser")
        get.clicked.connect(self._open_gamedb_account)
        self.gdb_token_row = self._field_row(
            sec, "Desktop app token", TOKEN_NOTE, self.gdb_token, extra=(get,),
            keywords="key scope game-db", key='gamedb_token')
        self.gdb_games = Switch()
        self.gdb_games_row = self._row(
            sec, "Game details from Game-db", "", self.gdb_games,
            keywords="protondb howlongtobeat hltb igdb reviews early access", key='gamedb_games')
        self.gdb_sessions = Switch()
        self.gdb_sessions_row = self._row(
            sec, "Upload play sessions", SESSIONS_NOTE, self.gdb_sessions,
            keywords="play time history upload share group", key='sessions')
        self.gdb_library = Switch()
        self.gdb_library_row = self._row(
            sec, "Send my Heroic library", LIBRARY_NOTE, self.gdb_library,
            keywords="epic gog amazon heroic library igdb upload", key='heroic_library')
        self.gdb_other = Switch()
        self.gdb_other_row = self._row(
            adv, "Include games not from a store", "Repacks and other games too, as their "
            "title only: never a folder or a repack name.", self.gdb_other,
            keywords="repack play sessions upload game-db", key='sessions_other')
        self.gdb_sessions.toggled.connect(self.gdb_other.setEnabled)
        self.gdb_device = QLineEdit()
        self.gdb_device.setMaxLength(64)
        self.gdb_device.setPlaceholderText(gamedb.device_label())
        self._field_row(adv, "This PC's name", "How Game-db lists this Nexus on its account "
                        "page.", self.gdb_device, keywords="device hostname game-db",
                        key='device')
        sec = self._group(cat, 'sgdb', "SteamGridDB", 'image',
                          "Covers, heroes and logos for every game.")
        self.key = self._secret(QLineEdit())
        self.key.setPlaceholderText("not set")
        get = QPushButton("Get a key")
        get.setToolTip("Opens SteamGridDB's API page (free, needs an account)")
        get.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SGDB_KEY_URL)))
        self._field_row(sec, "SteamGridDB API key", "Free from your SteamGridDB preferences.",
                        self.key, extra=(get,), keywords="sgdb token artwork", key='sgdb_key')
        sec = self._group(cat, 'steam', "Steam", 'steam')
        self.account = QLineEdit()
        self.account.setPlaceholderText("last signed in")
        self._field_row(sec, "Steam account", "Whose Steam gets the shortcuts. Leave empty for "
                        "the last one signed in.", self.account, keywords="profile user",
                        key='steam_account')
        self._build_friends(cat)

    def _build_friends(self, cat):
        """Steam friends (w-80): the key, whose friends, and what the CLI last
        found. The page never asks Steam; Refresh now runs `--friends`."""
        sec = self._group(cat, 'friends', "Steam friends", 'friends', FRIENDS_DESC)
        words = "friends social who owns play together playing now steam"
        self.fr_enabled = Switch()
        self.fr_enabled_row = self._row(
            sec, "Show friends", "Needs a Steam Web API key.", self.fr_enabled,
            keywords=words, key='friends')
        self.fr_key = self._secret(QLineEdit())
        self.fr_key.setPlaceholderText("not set")
        get = QPushButton("Get a key")
        get.setToolTip("Opens Steam's Web API key page (free, needs a Steam account)")
        get.clicked.connect(self._open_steam_key_page)
        self.fr_key_row = self._field_row(
            sec, "Steam Web API key", FRIENDS_KEY_NOTE, self.fr_key, extra=(get,),
            keywords=words + " web api key token", key='steam_web_api_key')
        self.fr_whose_row = self._row(
            sec, "Whose friends", "The Steam account above.", None,
            keywords=words + " account profile steamid", key='friends_whose')
        self.fr_refresh = QPushButton("Refresh now")
        self.fr_refresh.setToolTip("Asks Steam for your friends now")
        self.fr_refresh.clicked.connect(self._friends_refresh)
        self.fr_status_row = self._row(
            sec, "Friends found", "Add a key to see your friends.", self.fr_refresh,
            keywords=words + " refresh status public private", key='friends_status')
        privacy = QPushButton("Privacy on Steam")
        privacy.setToolTip("Opens your Steam privacy settings in the browser")
        privacy.clicked.connect(self._open_steam_privacy)
        self._row(sec, "Who Nexus can see", FRIENDS_PRIVACY_NOTE, privacy,
                  keywords=words + " private public game details friends list privacy",
                  key='friends_privacy')
        self.fr_steamid = QLineEdit()
        self.fr_steamid.setMaxLength(17)
        self.fr_steamid.setPlaceholderText("the Steam account above")
        self._field_row(cat.adv_body, "Whose friends (SteamID64)", "17 digits, 7656119\u2026 "
                        "Leave empty for the Steam account above.", self.fr_steamid,
                        keywords=words + " steamid64 account", key='friends_steamid')
        self.fr_key.textChanged.connect(self._friends_key_changed)

    def _open_steam_key_page(self):
        QDesktopServices.openUrl(QUrl(STEAM_KEY_URL))

    def _open_steam_privacy(self):
        QDesktopServices.openUrl(QUrl(STEAM_PRIVACY_URL))

    def _friends_have_key(self) -> bool:
        return bool(self.fr_key.text().strip() or os.environ.get(STEAM_KEY_ENV))

    def _friends_key_changed(self, *_):
        """Show friends and Refresh now wait for a key."""
        have = self._friends_have_key()
        self.fr_enabled.setEnabled(have)
        self.fr_enabled_row.set_description("" if have else "Needs a Steam Web API key.")
        self.fr_refresh.setEnabled(have)

    def _friends_status(self, refresh: bool = False):
        """Ask the CLI (in the background, never Steam from here) what it has;
        `refresh` makes it ask Steam now."""
        if not self._friends_have_key():
            self.fr_status_row.set_description("Add a key to see your friends.")
            return
        self.fr_refresh.setEnabled(False)
        self.fr_status_row.set_description("Asking Steam\u2026" if refresh else "Looking\u2026")
        self.fr_status_row.set_error(None)
        self.fr_status_row.set_note(None)
        args = ['--friends', *(['--refresh'] if refresh else []), '--json']
        cli_json(args, self._friends_done, 120000 if refresh else 60000)

    def _friends_refresh(self):
        self.commit()                           # a key typed but not yet saved
        if self.fr_key in self._invalid:
            return
        self._friends_status(refresh=True)

    def _friends_done(self, data, err):
        try:
            self.fr_refresh.setEnabled(self._friends_have_key())
        except RuntimeError:                    # the page is gone
            return
        FRIENDS.set_answer(data)                # the game page and Library see it too
        if data:
            acct = data.get('account') or {}
            who = acct.get('persona') or data.get('me')
            if acct.get('source') == 'config' and data.get('me'):
                self.fr_whose_row.set_description(f"SteamID64 {data['me']} (set under Advanced).")
            elif who:
                self.fr_whose_row.set_description(f"{who}, the Steam account above.")
            else:
                self.fr_whose_row.set_description("No Steam account found on this PC.")
        self.fr_status_row.set_description(friends_status_text(data, err))
        error, note = friends_problem(data)
        self.fr_status_row.set_error(error)
        self.fr_status_row.set_note(note)

    def _build_system(self):
        cat = self._add_category('system', "System", 'system', 'tide',
                                 "Back up and move, setup again, privacy, the version and updates.")
        sec = self._group(cat, 'backup', "Back up & move", 'save',
                          "Your settings in one file, for a new PC or a reinstall. Saves "
                          "aren't in it — each game has its own save backups.")
        row = QHBoxLayout()
        exp = QPushButton("Export…")
        set_glyph_icon(exp, G['download'], 14)
        exp.clicked.connect(self._export)
        imp = QPushButton("Import…")
        set_glyph_icon(imp, G['update'], 14)
        imp.clicked.connect(self._import)
        row.addWidget(exp)
        row.addWidget(imp)
        self._row(sec, "Settings file", "Settings, favourites, collections, notes, the "
                  "dashboard, the look, your game list and play history.", row,
                  keywords="backup export import move new pc restore artwork secrets token",
                  key='backup')
        sec = self._group(cat, 'setup', "Setup", 'wrench')
        again = QPushButton("Run setup again")
        again.clicked.connect(self.setup_wanted)
        self._row(sec, "First-run setup", "The welcome steps: checks, folders, Proton, extras.",
                  again, keywords="welcome wizard onboarding first run", key='setup')
        self._build_privacy(cat)
        self._build_about(cat)
        # config.toml: what Settings doesn't show yet. Usable with a broken
        # file too: that is how it gets fixed.
        cat.adv_body.config_backed = False
        cfg = self.cfg_btn = QPushButton("Open")
        cfg.setAccessibleName("Open config.toml")
        cfg.setToolTip(str(CONFIG_PATH))
        set_glyph_icon(cfg, G['external'], 14)
        cfg.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(CONFIG_PATH))))
        self._row(cat.adv_body, "Open config.toml", f"{CONFIG_PATH}. Phone pushes ([notify]) "
                  "and a few rarer options live only here.", cfg,
                  keywords="config file toml edit notify ntfy", key='config_file')

    def _build_privacy(self, cat: SettingsCategory):
        """What leaves this computer, and only with a yes (w-102): the
        install count, what it sends, what it sent, and the reports relay."""
        sec = self._group(cat, 'privacy', "Privacy", 'shield',
                          "Nexus sends nothing about you on its own. These are the two things "
                          "it can send, both anonymous.")
        self.stats_count = Switch()
        self._stats_touched = False
        self.stats_row = self._row(
            sec, "Count this install",
            "Once a day at most: the version, the system and the language. No id, no "
            "games, no paths. It tells which systems Nexus is used on.", self.stats_count,
            keywords="privacy statistics stats telemetry analytics usage anonymous count "
                     "do not track", key='stats_count')
        self.stats_preview = self._peek_row(
            sec, "Show what is sent", "Exactly what the next count would carry.",
            'stats_preview', self._load_stats_preview)
        self.stats_sent = self._peek_row(
            sec, "Sent so far", "The counts sent from this computer, newest first (the "
            "last 100).", 'stats_sent', self._load_stats_sent)
        self.report_relay = Switch()
        self._row(sec, "Send reports anonymously",
                  "Report a problem can send a report through the Nexus relay, no GitHub "
                  "account needed. Off also stops the install count.", self.report_relay,
                  keywords="privacy bug report relay anonymous", key='report_relay')

    def _peek_row(self, sec, title: str, desc: str, key: str, fill) -> 'QLabel':
        """A row with a Show button that opens a read-only text under it,
        filled by `fill()` each time it opens."""
        btn = QPushButton("Show")
        btn.setCheckable(True)
        btn.setAccessibleName(title)
        row = self._row(sec, title, desc, btn, keywords="privacy install count", key=key)
        text = plain_label("", "ReportLine", wrap=True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.layout().addWidget(text)           # in the row first, then hidden
        text.setContentsMargins(0, 0, 0, SP['3'])
        text.hide()

        def flip(on: bool):
            btn.setText("Hide" if on else "Show")
            text.setVisible(on)
            if on:
                text.setText("Asking…")
                fill()
        btn.toggled.connect(flip)
        text.button = btn
        return text

    def _load_stats_preview(self):
        cli_json(['--stats-preview', '--json'], self._stats_previewed, 15000)

    def _stats_previewed(self, data, err):
        try:
            if not data:
                self.stats_preview.setText(f"Couldn't ask: {err or 'no answer'}")
                return
            state = {'on': "On", 'off': "Off", 'unasked': "Not answered yet, so off",
                     'blocked': "On, but stopped by " + BLOCKED.get(data.get('blocked'), "?")
                     }.get(data.get('state'), "?")
            where = (data.get('url') or '') + '/v1/count' if data.get('url') else "nowhere"
            self.stats_preview.setText(f"{state}. Sent to {where}:\n\n"
                                       + stats.pretty(data.get('payload') or {}))
        except RuntimeError:
            pass

    def _load_stats_sent(self):
        cli_json(['--stats-sent', '--json'], self._stats_sent_loaded, 15000)

    def _stats_sent_loaded(self, data, err):
        try:
            if data is None:
                self.stats_sent.setText(f"Couldn't ask: {err or 'no answer'}")
                return
            rows = data.get('sent') or []
            lines = []
            for r in rows:
                a = r.get('answer') or {}
                answer = "counted" if a.get('status') == 'counted' else \
                    f"not counted ({a.get('error') or '?'})"
                lines.append(f"{str(r.get('at') or '?').replace('T', ' ').rstrip('Z')}  "
                             f"{answer}\n" + json.dumps(r.get('payload') or {},
                                                        separators=(', ', ': ')))
            self.stats_sent.setText("\n\n".join(lines) if lines else "Nothing sent yet.")
        except RuntimeError:
            pass

    def stats_sent_changed(self):
        """A count went out (the window's ping): an open "Sent so far" shows it."""
        if self.stats_sent.button.isChecked():
            self._load_stats_sent()

    def _stats_toggled(self, _on: bool):
        if not self._loading:
            self._stats_touched = True
        self.commit()

    def _sync_stats_row(self, *_):
        """Say why the count can't send even when it's on."""
        why = stats.blocked({'report_relay': self.report_relay.isChecked()})
        self.stats_row.set_note(
            None if not why else
            f"Stopped: {why} is set in the environment." if why != 'relay' else
            "Stopped while Send reports anonymously is off.")

    def _build_about(self, cat: SettingsCategory):
        sec = self._group(cat, 'about', "About", 'info')
        self.version_row = self._row(sec, "Nexus version", "", None, keywords="about release",
                                     key='version')
        # Only while a newer Nexus is out (MainWindow.updater): the start-up
        # toast goes, this and Health's row stay until it's installed.
        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("Primary")
        set_glyph_icon(self.update_btn, G['update'], 14)
        self.update_btn.clicked.connect(self.update_wanted)
        self.update_row = self._row(sec, "Update available", "", self.update_btn,
                                    keywords="new release upgrade version install",
                                    key='update')
        self.update_row.set_available(False)
        self.upd_btn = QPushButton("Check for updates")
        set_glyph_icon(self.upd_btn, G['refresh'], 14)
        self.upd_btn.clicked.connect(self._check_updates)
        self.upd_row = self._row(sec, "Check for updates", "", self.upd_btn,
                                 keywords="new release upgrade version", key='check_updates')
        self.upd_result = self.upd_row.desc
        self.upd_result.setTextFormat(Qt.TextFormat.RichText)
        self.upd_result.setOpenExternalLinks(True)
        news = QPushButton("What's new")
        set_glyph_icon(news, G['news'], 14)
        news.clicked.connect(self.whats_new_wanted)
        self._row(sec, "What's new", "The short notes for this version.", news,
                  keywords="release notes changelog changes", key='whats_new')
        self._show_version()

    def _show_version(self):
        """The version row: this build, and when the update answer was fetched."""
        t = updates.last_checked()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"
        self.version_row.set_description(f"Nexus {__version__} · last checked {when}")

    def show_update(self, view: dict | None):
        """The Updater's state (Updater.view()); None hides the row."""
        self.update_row.set_available(view is not None)
        if view is not None:
            self.update_row.title.setText(view['title'])
            self.update_row.set_description(view['detail'])
            self.update_btn.setText(view['button'])
            self.update_btn.setEnabled(view['enabled'])
        self._refresh_overview()

    def _check_updates(self):
        """Ask GitHub (skipping the daily cache) off the GUI thread, then say what it found."""
        self.upd_btn.setEnabled(False)
        self.upd_row.set_description("Checking…")       # the row starts with no text, so its label is hidden
        in_thread(lambda: updates.check(force=True), self._updates_checked)

    def _updates_checked(self, r, err):
        self.upd_btn.setEnabled(True)
        r = r or {'state': 'failed'}
        state = r.get('state')
        if state == 'available' and updates.in_app():
            text = f"Nexus {html.escape(r['version'])} is available: Update is just above."
        elif state == 'available':
            text = (f"Nexus {html.escape(r['version'])} is available: "
                    f"<a href=\"{html.escape(r['url'], quote=True)}\">"
                    f"{'Download the installer' if r['url'].endswith('.exe') else 'Release page'}"
                    f"</a>")
        elif state == 'current':
            text = f"Nexus is up to date ({html.escape(r['version'])})."
        elif state == 'off':
            text = "Update checks are off ([updates] check = false in config.toml)."
        else:
            text = "Couldn't check for updates. Are you online?"
        self.upd_row.set_description(text)
        self._show_version()
        if state == 'available':
            self.update_found.emit({k: r.get(k, '') for k in
                                    ('version', 'url', 'page', 'installer', 'sums')})

    def _export(self):
        """Export…: a modal (w-67) with what goes in the file, both off each
        time; its Export opens the system's save dialog, then the CLI writes
        the file."""
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['3'])
        art, secrets = Switch(), Switch()
        for sw, title, desc in (
                (art, "Include artwork images",
                 "The covers and heroes you picked, not just which ones (bigger file)."),
                (secrets, "Include keys and passwords",
                 "SteamGridDB key, Game-db token, torrent password, ntfy token. Off: the "
                 "file is safe to share; this machine's own are kept on import.")):
            line = QHBoxLayout()
            line.setSpacing(SP['3'])
            col = QVBoxLayout()
            col.setSpacing(SP['0_5'])
            col.addWidget(label(title, "RowTitle", wrap=True))
            col.addWidget(label(desc, "Muted", wrap=True))
            line.addLayout(col, 1)
            sw.setAccessibleName(title)
            line.addWidget(sw, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addLayout(line)
        m = open_modal(self, "Export your settings", body, "Export",
                       lambda: self._export_to(art.isChecked(), secrets.isChecked()),
                       glyph=G['download'], sub="One file for a new PC or a reinstall. "
                       "Saves aren't in it.")
        if m is not None:
            for sw in (art, secrets):
                sw.toggled.connect(m.touched)

    def _export_to(self, art: bool, secrets: bool):
        import datetime
        default = str(HOME / f"nexus-settings-{datetime.date.today():%Y-%m-%d}.tar.gz")
        path, _ = QFileDialog.getSaveFileName(self, "Save your Nexus settings", default,
                                              "Nexus settings (*.tar.gz)")
        if not path:
            return
        args = ['--export-settings', path]
        if art:
            args.append('--with-art')
        if secrets:
            args.append('--with-secrets')
        self.request.emit(args, "Saving your settings", False, 'task')

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Nexus settings", str(HOME),
                                              "Nexus settings (*.tar.gz *.tgz)")
        if path:
            self.import_wanted.emit(path)

    def _check_base(self, _text=None):
        warns = platform.install_base_warnings(
            Path(self.base.text().strip() or INSTALL_BASE_DEFAULT))
        self.row_of[self.base].set_note(
            "Not a good place for games: " + "; ".join(warns) + "." if warns else None)

    def _wire(self):
        """Switches and menus save when they change; text fields when you
        leave them (or press Enter). Library preferences go out as signals."""
        for e in (self.key, self.fr_key, self.fr_steamid, self.base, self.scan_dir,
                  self.scan_more, self.account,
                  self.gdb_url, self.gdb_token, self.gdb_device, self.tor_url, self.tor_user, self.tor_pass,
                  self.tor_dir, self.tor_ratio):
            e.textChanged.connect(self._edited)
            e.editingFinished.connect(self.commit)
        self.repacks.toggled.connect(self._show_repacks)
        for c in (self.repacks, self.steam, self.desktop, self.heroic, self.news, self.unattended,
                  self.trash_repack, self.gdb_enabled, self.gdb_games, self.gdb_sessions,
                  self.gdb_other, self.gdb_library, self.fr_enabled, self.al_atl, self.al_fitgirl,
                  self.al_updates, self.al_proton):
            c.toggled.connect(self.commit)
        # Not in the loop: while [stats] count is unset, only a real flip may
        # write it, or any other save would answer the ask with "no".
        self.stats_count.toggled.connect(self._stats_toggled)
        self.report_relay.toggled.connect(self._sync_stats_row)
        self.report_relay.toggled.connect(self.commit)
        for cb in (self.runner, self.proton, self.al_sale):
            cb.activated.connect(self.commit)
        self.tor_client.activated.connect(self._torrent_client_changed)
        for box, key in ((self.ctl_double, 'double_click'), (self.ctl_a, 'pad_a'),
                         (self.ctl_dedupe, 'dedupe'), (self.ctl_attract, 'attract'),
                         (self.ctl_start, 'theatre_start'), (self.ctl_volume, 'volume')):
            box.activated.connect(lambda _i, box=box, key=key: self.controls_changed.emit(
                {key: box.currentData()}))
        for sw, key in ((self.ctl_on_pad, 'theatre_on_pad'), (self.ctl_sounds, 'sounds'),
                        (self.ctl_week, 'week_home')):
            sw.toggled.connect(lambda on, key=key: self.controls_changed.emit({key: on}))

    # ---- navigation and search ----
    def _show_repacks(self, on: bool):
        """Repack features off: their rows and the torrent client go away."""
        for row in (*self._repack_rows, self.row_of[self.tor_client], *self.tor_rows):
            row.set_available(on)
        if on:
            self._sync_torrent_rows()           # its address rows only with a client
        self._apply_filter()

    def open_at(self, cat: str = 'overview', row: str | None = None):
        """Show a category (old section keys work too: 'gamedb', 'torrent');
        with `row`, scroll it into view, opening Advanced if it lives there.
        Deals, Downloads, the art picker and Theatre link here."""
        cat, alias = OLD_KEYS.get(cat, (cat, None))
        row = row or alias
        if cat != 'overview' and cat not in self.categories:
            return
        self._current = cat
        if self.search.text():
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self._apply_filter()
        self._anim.stop()
        self.scroll.verticalScrollBar().setValue(0)
        if cat == 'overview':
            self._refresh_overview()
        target = self.rows.get(f'{cat}.{row}') if row else None
        if target is None or not target.available:
            return                              # the category still opens
        c = self.categories[cat]
        if target in c.adv_body.rows and not c.adv.is_open:
            c.adv.set_open(True, remember=False)
        self._scroll_row = target
        # Once the layout has settled: a page just shown may not have its
        # final geometry yet.
        QTimer.singleShot(0, self._scroll_to_row)

    def _scroll_to_row(self):
        """The linked row a third of the way down the view."""
        row = self._scroll_row
        self._scroll_row = None
        if row is None or row.isHidden():
            return
        bar = self.scroll.verticalScrollBar()
        third = self.scroll.viewport().height() // 3
        target = min(bar.maximum(), max(0, self._top_of(row) - third))
        self._anim.stop()
        if MOTION and self.isVisible() and abs(bar.value() - target) > 2:
            self._anim.setStartValue(bar.value())
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            bar.setValue(target)

    def current_category(self) -> str:
        return self._current

    def unsaved_at(self) -> tuple[str, str | None]:
        """Where the first edit that can't be saved is: (category, row)."""
        for w in self._invalid:
            row = self.row_of.get(w)
            if row is not None:
                cat, _, key = row.key.partition('.')
                return cat, key
        return self._current, None

    def _top_of(self, w: QWidget) -> int:
        return w.mapTo(self.scroll.widget(), w.rect().topLeft()).y()

    def _show(self, keys: list[str]):
        """Show these pages (Overview, categories) and hide the rest: the
        new ones first, and focus out of one going away (else Qt throws it
        to the next focusable widget, wherever that is)."""
        pages = {'overview': self.overview, **self.categories}
        for k in keys:
            pages[k].show()
        fw = QApplication.focusWidget()
        for k, w in pages.items():
            if k in keys or w.isHidden():
                continue
            if fw is not None and w.isAncestorOf(fw):
                b = self.nav_btns.get(keys[0]) if keys else None
                (b if b is not None and not b.isHidden() else self.search).setFocus()
            w.hide()

    def _apply_filter(self):
        """Rows, groups, Advanced, pages and the list for the search now
        (or for the current category without one). Scroll stays put."""
        q = self.search.text().strip().lower()
        counts = {k: cat.filter(q) for k, cat in self.categories.items()}
        if not q:
            for b in self.nav_btns.values():
                b.show()
            self.no_match.hide()
            self._show([self._current])
            for k, b in self.nav_btns.items():
                b.setChecked(k == self._current)
            return
        found = [k for k, n in counts.items() if n]
        # Every match, each under its category's heading; the list keeps
        # only those categories, and clicking one opens it.
        for k, b in self.nav_btns.items():
            b.setChecked(False)
            if k in found:
                b.show()
        self._show(found)
        for k, b in self.nav_btns.items():
            if k not in found:
                b.hide()
        self.no_match.setText(f"No settings match “{self.search.text().strip()}”."
                              if not found else "")
        self.no_match.setVisible(not found)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._set_compact(self.width() < self.COMPACT_BELOW)

    def _set_compact(self, on: bool):
        if on == self._compact:
            return
        self._compact = on
        self.nav.setFixedWidth(self.RAIL_W if on else self.NAV_W)
        m = (SP['2'], SP['4'], SP['2'], SP['4']) if on else (SP['5'], SP['4'], SP['3'], SP['4'])
        self.nav_lay.setContentsMargins(*m)
        for b in self.nav_btns.values():
            name = b.accessibleName()
            b.setText("" if on else amp(name))
            b.setToolTip(name if on else "")
        if on:
            self.content.insertWidget(0, self.head)
        else:
            self.nav_lay.insertWidget(0, self.head)
        self.overview.set_columns(1 if on else 2)

    def _focus_search(self):
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _filter(self, _text: str):
        self._apply_filter()
        self._anim.stop()
        self.scroll.verticalScrollBar().setValue(0)

    # ---- Overview ----
    def _appearance_changed(self, _ap: dict):
        self._refresh_overview()

    def set_torrent_problem(self, text: str | None):
        """The last torrent poll's error (None: it answered), for Worth a look.
        Pushed by the window; Overview never asks the client itself."""
        self._torrent_problem = text or None
        self._refresh_overview()

    def _state_lines(self) -> dict[str, str]:
        """One line per category card: what is set there now."""
        ap = self.appearance.ap
        theme = THEMES[ap['theme']][0]
        accent = ap['accent'] if _hex_ok(ap['accent']) else ap['accent'].capitalize()
        lines = {'look': f"{theme} · {accent} · {int(round(ap['scale'] * 100))} %"}
        lines['library'] = " · ".join((
            "Owned games shown" if self.show_owned.isChecked() else "Installed games only",
            "News on" if self.news.isChecked() else "News off"))
        play = ("Double-click plays" if self.ctl_double.currentData() == 'play' else
                "Double-click opens the page")
        lines['playing'] = play + " · " + (f"Proton: {self.proton.currentText()}"
                                           if has('proton') else
                                           f"Theatre: {self.ctl_start.currentText()}")
        base = self.base.text().strip() or INSTALL_BASE_DEFAULT
        home = str(HOME)
        if base == home or base.startswith(home + os.sep):
            base = '~' + base[len(home):]
        lines['installing'] = base + " · " + ("repacks on" if self.repacks.isChecked() else
                                               "repacks off")
        switches = [sw for sw in (self.al_atl, self.al_fitgirl, self.al_updates, self.al_proton)
                    if self.row_of[sw].available]
        on = sum(sw.isChecked() for sw in switches) + bool(self.al_sale.currentData())
        lines['alerts'] = (f"{on} of {len(switches) + 1} on · " +
                           ("works while closed" if self.agent.isChecked() else
                            "only while Nexus is open"))
        lines['accounts'] = " · ".join((
            "Game-db connected" if self._gamedb_ready() else "Game-db off",
            "SteamGridDB key set" if self.key.text().strip() else "No SteamGridDB key"))
        return lines

    def _gamedb_ready(self) -> bool:
        return (self.gdb_enabled.isChecked() and bool(self.gdb_url.text().strip())
                and bool(self.gdb_token.text().strip() or os.environ.get('NEXUS_GAMEDB_TOKEN')))

    def _worth_a_look(self) -> list[tuple[str, str, str, str, str]]:
        """At most three things to see to, from what Nexus already knows
        (no check runs for this): (title, line, button, category, row)."""
        out = []
        if self.update_row.available:
            out.append((self.update_row.title.text(), self.update_row.desc.text(),
                        "Update", 'system', 'update'))
        if self.repacks.isChecked() and self.tor_client.currentData() is not None \
                and self._torrent_problem:
            out.append(("The torrent client didn't answer", self._torrent_problem,
                        "Check it", 'installing', 'torrent_test'))
        if not self.key.text().strip():
            out.append(("No SteamGridDB key", "Covers and heroes come from Steam only until "
                        "there is one.", "Add a key", 'accounts', 'sgdb_key'))
        if self.gdb_enabled.isChecked() and not (self.gdb_token.text().strip()
                                                 or os.environ.get('NEXUS_GAMEDB_TOKEN')):
            out.append(("Deals is on without a Game-db token", "Wishlist deals need one from "
                        "your Game-db account.", "Add a token", 'accounts', 'gamedb_token'))
        return out[:3]

    def _refresh_overview(self):
        ov = getattr(self, 'overview', None)
        if ov is None:                          # still building
            return
        ov.set_states(self._state_lines())
        ov.set_worth(self._worth_a_look())

    # ---- the background agent and the tray ----
    def _agent_toggled(self, on: bool):
        if self._agent_state is not None and on != self._agent_state.get('enabled'):
            self.background_changed.emit('agent', on)

    def refresh_agent(self):
        """Ask the CLI how the background agent is doing."""
        def done(data, _err):
            try:
                self.set_agent_status(data or {})
            except RuntimeError:
                pass
        cli_json(['--agent-status', '--json'], done, 30000)

    def set_agent_status(self, s: dict):
        self._agent_state = s
        self.agent.blockSignals(True)
        self.agent.setChecked(bool(s.get('enabled')))
        self.agent.blockSignals(False)
        self.agent.update()
        self._refresh_overview()
        if s.get('startup') or not has('systemd_agent'):    # Windows: Start with Windows
            self.agent.setEnabled(True)
            self.agent_row.set_description(
                "On: Nexus opens in the tray when you sign in, and installs finished "
                "downloads and sends your alerts from there." if s.get('enabled') else
                "Opens Nexus in the tray when you sign in. Off now: downloads install and "
                "alerts come only while Nexus is open.")
            return
        self.agent.setEnabled(s.get('systemd', True) is not False)
        if s.get('systemd') is False:
            note = "Needs systemd (systemctl --user), which this system doesn't have."
        elif s.get('running'):
            when = (s.get('updated_at') or '')[11:16]
            note = "Running" + (f" — last checked at {when}." if when else ".")
            if s.get('installing'):
                note += " Installing: " + ", ".join(s['installing']) + "."
        elif s.get('enabled'):
            note = "On, but not running right now — it starts again within a minute."
        else:
            note = ("Installs finished downloads and sends your alerts. Off now: that happens "
                    "only while this window is open.")
        self.agent_row.set_description(note)

    def set_controls(self, controls: dict):
        for box, key in ((self.ctl_double, 'double_click'), (self.ctl_a, 'pad_a'),
                         (self.ctl_dedupe, 'dedupe'), (self.ctl_attract, 'attract'),
                         (self.ctl_start, 'theatre_start'), (self.ctl_volume, 'volume')):
            box.setCurrentIndex(max(0, box.findData(controls.get(key))))
        for sw, key in ((self.ctl_on_pad, 'theatre_on_pad'), (self.ctl_sounds, 'sounds'),
                        (self.ctl_week, 'week_home')):
            sw.blockSignals(True)
            sw.setChecked(bool(controls.get(key)))
            sw.blockSignals(False)
        self.ctl_volume.setEnabled(bool(controls.get('sounds')))   # greyed while sounds are off
        self._refresh_overview()
        has_tray = tray_available()
        self.tray.blockSignals(True)
        self.tray.setChecked(bool(controls.get('close_to_tray')) and has_tray)
        self.tray.blockSignals(False)
        self.tray.setEnabled(has_tray)
        if not has('systemd_agent'):            # Windows: the notification area
            self.tray_row.set_description(
                "Closing the window keeps Nexus by the clock, with your recent games, "
                "downloads and Quit; notifications come from there too." if has_tray else
                "The notification area isn't available right now.")
            return
        self.tray_row.set_description(
            "Closing the window keeps Nexus in the tray, with your recent games, downloads "
            "and Quit. Start there with nexus\u00a0-\u2060-\u2060hidden." if has_tray else
            "No system tray here — on niri, a bar with a tray (waybar's \"tray\" module) "
            "provides one.")

    # ---- saving ----
    @property
    def dirty(self) -> bool:
        """An edit that isn't in config.toml yet: typed and not left, or
        left but not valid."""
        return self._pending or bool(self._invalid)

    def unsaved_reason(self) -> str:
        for w, msg in self._invalid.items():
            row = self.row_of.get(w)
            return f"{row.title.text()}: {msg}" if row else msg
        return ""

    def _edited(self, *_):
        if not self._loading:
            self._pending = True

    def _torrent_client_changed(self, *_):
        self._sync_torrent_rows()
        self.commit()

    def _sync_torrent_rows(self):
        on = self.tor_client.currentData() is not None and self.repacks.isChecked()
        for row in self.tor_rows:
            row.set_available(on)
        self._apply_filter()

    def _saved_note_idle(self):
        self.saved_note.setProperty('ok', False)
        self.saved_note.setText("Changes save as you make them.")
        self._repolish(self.saved_note)

    def _saved_note_flash(self):
        self.saved_note.setProperty('ok', True)
        self.saved_note.setText("✓ Saved")
        self._repolish(self.saved_note)
        self._flash.start()

    @staticmethod
    def _repolish(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def commit(self, *_):
        """Save whatever differs from config.toml now (a no-op when nothing
        does). Ctrl+S, leaving a field and leaving the page all end here."""
        if self._loaded and not self._loading:
            self._save()

    def _open_gamedb_account(self):
        try:
            if not self.gdb_url.text().strip():
                raise gamedb.GameDbError('config', "Enter your Game-db address first")
            base = gamedb.check_url(self.gdb_url.text())
        except gamedb.GameDbError as e:
            self.row_of[self.gdb_url].set_error(str(e))
            return
        QDesktopServices.openUrl(QUrl(base + '/account'))

    def _test_torrent(self):
        """Save what's typed, then ask the client for its version."""
        self.commit()
        bad = [w for w in (self.tor_url, self.tor_ratio) if w in self._invalid]
        if bad:
            self.tor_test_row.set_description("✗ Fix the fields above first.")
            return
        self.tor_test.setEnabled(False)
        self.tor_test_row.set_description("Asking…")

        def done(data, err):
            try:
                self.tor_test.setEnabled(True)
            except RuntimeError:
                return
            if data and data.get('ok'):
                where = f" — downloads to {data['download_dir']}" if data.get('download_dir') else ''
                self.tor_test_row.set_description(
                    f"✓ {data.get('client')} {data.get('version')}{where}")
            else:
                msg = ((data or {}).get('error') or {}).get('message') or err or "no answer"
                self.tor_test_row.set_description(f"✗ {msg}")
        cli_json(['--torrent-test', '--json'], done, 15000)

    def _browse(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Choose folder",
                                             edit.text() or edit.placeholderText())
        if d:
            edit.setText(d)
            self.commit()

    def load(self):
        self._loading = True
        try:
            self._load()
        finally:
            self._loading = False
        self._loaded = True
        self._pending = False
        self._invalid.clear()
        for row in self.row_of.values():
            row.set_error(None)
        self._refresh_overview()

    def _load(self):
        c = self.cfg = load_config()
        broken = bool(c.get('error'))
        self.error.setText(f"config.toml can't be read, so nothing here can be saved until "
                           f"it's fixed: {c['error']}" if broken else "")
        self.error.setVisible(broken)
        for sec in self.groups.values():
            if sec.config_backed:
                sec.card.setEnabled(not broken)
        for cat in self.categories.values():
            if cat.adv_body.config_backed:
                cat.adv_body.card.setEnabled(not broken)
        self.news.setEnabled(not broken)
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
        self.stats_count.setChecked(c.get('stats_count') is True)
        self._stats_touched = False
        self.report_relay.setChecked(c.get('report_relay', True))
        self._sync_stats_row()
        self.repacks.blockSignals(True)
        self.repacks.setChecked(c.get('repacks', True))
        self.repacks.blockSignals(False)
        self._show_repacks(self.repacks.isChecked())
        self.gdb_enabled.setChecked(c.get('gamedb_cfg_enabled', False))
        self.gdb_url.setText(c.get('gamedb_url') or "")
        env_token = bool(os.environ.get('NEXUS_GAMEDB_TOKEN'))
        self.gdb_token.setText("" if env_token else (c.get('gamedb_token') or ""))
        self.gdb_token.setEnabled(not env_token)
        self.gdb_token.setPlaceholderText("set by NEXUS_GAMEDB_TOKEN" if env_token else "gdb_…")
        self.gdb_games.setChecked(c.get('gamedb_games', True))
        self.gdb_device.setText(c.get('gamedb_device') or "")
        env_key = bool(os.environ.get(STEAM_KEY_ENV))
        self.fr_key.setText("" if env_key else (c.get('steam_web_api_key') or ""))
        self.fr_key.setEnabled(not env_key)
        self.fr_key.setPlaceholderText(f"set by {STEAM_KEY_ENV}" if env_key else "not set")
        self.fr_enabled.setChecked(c.get('friends_enabled', False))
        self.fr_steamid.setText(c.get('friends_steamid') or "")
        self._friends_key_changed()
        self._friends_status()
        self.gdb_token_row.set_description(token_note(gamedb.token_scopes()))
        self.gdb_sessions.setChecked(c.get('gamedb_upload_sessions', False))
        self.gdb_other.setChecked(c.get('gamedb_upload_other', False))
        self.gdb_other.setEnabled(self.gdb_sessions.isChecked())
        # Hidden when the server can't take sessions (meta.json; unknown shows them).
        ready = gamedb.write_status('sessions', gamedb.SCOPE_SESSIONS)
        for row in (self.gdb_sessions_row, self.gdb_other_row):
            row.set_available(ready != 'unavailable')
        self.gdb_sessions_row.set_description(
            gamedb.SCOPE_HINT[gamedb.SCOPE_SESSIONS] if ready == 'read_only' else SESSIONS_NOTE)
        self.gdb_library.setChecked(c.get('gamedb_upload_library', True))
        ready = gamedb.write_status('library', gamedb.SCOPE_LIBRARY)
        self.gdb_library_row.set_available(ready != 'unavailable' and has('heroic'))
        self.gdb_library_row.set_description(
            gamedb.SCOPE_HINT[gamedb.SCOPE_LIBRARY] if ready == 'read_only' else LIBRARY_NOTE)
        self._apply_filter()
        offered = gamedb.games_available()          # meta.json only; the GUI never asks Game-db
        self.gdb_games.setEnabled(offered is not False)
        self.gdb_games_row.set_description(
            "This Game-db doesn't offer them yet, so Nexus asks ProtonDB and HowLongToBeat "
            "itself." if offered is False else
            "ProtonDB, play length, reviews and Early Access for your library and wishlist "
            "come from Game-db (synced every 6 hours), so game pages don't ask those sites.")
        self.tor_client.setCurrentIndex(max(0, self.tor_client.findData(c.get('torrent_client'))))
        self._sync_torrent_rows()
        self.tor_url.setText(c.get('torrent_url') or "")
        self.tor_user.setText(c.get('torrent_username') or "")
        self.tor_pass.setText(c.get('torrent_password') or "")
        self.tor_dir.setText(c.get('torrent_download_dir') or "")
        r = c.get('torrent_seed_ratio')
        self.tor_ratio.setText("" if r is None else f"{r:g}")
        self.tor_test_row.set_description("")
        for box, k in ((self.al_atl, 'atl'), (self.al_fitgirl, 'fitgirl'),
                       (self.al_updates, 'repack_updates'), (self.al_proton, 'proton')):
            box.setChecked(c.get(f'alerts_{k}', True))
        cut = c.get('alerts_sale_min_cut', 0)
        if self.al_sale.findData(cut) < 0:               # a value set in config.toml by hand
            self.al_sale.addItem(f"{cut}% off or more", cut)
        self.al_sale.setCurrentIndex(max(0, self.al_sale.findData(cut)))
        self.refresh_agent()
        self._proton_current = c.get('proton') or c.get('heroic_proton')
        self.proton.clear()
        self.proton.addItem("Newest installed", None)
        if self._proton_current:
            self.proton.addItem(self._proton_current, self._proton_current)
            self.proton.setCurrentIndex(1)
        if not has('proton'):                   # no Proton row to fill (Windows)
            return

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

    def _check(self, widget, ok: bool, msg: str) -> bool:
        """Show or clear `widget`'s error; False keeps its value out of the save."""
        row = self.row_of.get(widget)
        if ok:
            self._invalid.pop(widget, None)
            if row:
                row.set_error(None)
        else:
            self._invalid[widget] = msg
            if row:
                row.set_error(msg)
        return ok

    def _save(self):
        c = self.cfg
        if c.get('error'):
            return
        u = {}

        def put(section, key, new, old):
            if new != old:
                u[(section, key)] = new
        put('steamgriddb', 'api_key', self.key.text().strip() or None, c.get('api_key'))
        put('integrations', 'steam', self.steam.isChecked(), c.get('steam', True))
        put('integrations', 'desktop', self.desktop.isChecked(), c.get('desktop', True))
        put('integrations', 'heroic', self.heroic.isChecked(), c.get('heroic', True))
        put('news', 'enabled', self.news.isChecked(), c.get('news', True))
        if self._stats_touched or c.get('stats_count') is not None:
            put('stats', 'count', self.stats_count.isChecked(), c.get('stats_count'))
        put('report', 'relay', self.report_relay.isChecked(), c.get('report_relay', True))
        put('repacks', 'enabled', self.repacks.isChecked(), c.get('repacks', True))
        gamedb_changed = False
        gurl = self.gdb_url.text().strip() or None
        msg = ""
        if gurl:
            try:
                gurl = gamedb.check_url(gurl)
            except gamedb.GameDbError as e:
                msg = str(e)
        if self._check(self.gdb_url, not msg, msg):
            gamedb_changed = gurl != c.get('gamedb_url')
            put('gamedb', 'url', gurl, c.get('gamedb_url'))
        gtok = self.gdb_token.text().strip() or None
        if self.gdb_token.isEnabled() and self._check(
                self.gdb_token, not gtok or bool(gamedb.TOKEN_RE.match(gtok)),
                "A Game-db token is gdb_ followed by 43 letters, digits, - or _."):
            gamedb_changed = gamedb_changed or gtok != c.get('gamedb_token')
            put('gamedb', 'token', gtok, c.get('gamedb_token'))
        put('gamedb', 'enabled', self.gdb_enabled.isChecked(), c.get('gamedb_cfg_enabled'))
        put('gamedb', 'games', self.gdb_games.isChecked(), c.get('gamedb_games', True))
        put('gamedb', 'upload_sessions', self.gdb_sessions.isChecked(),
            c.get('gamedb_upload_sessions', False))
        put('gamedb', 'upload_other', self.gdb_other.isChecked(),
            c.get('gamedb_upload_other', False))
        put('gamedb', 'upload_library', self.gdb_library.isChecked(),
            c.get('gamedb_upload_library', True))
        dev = self.gdb_device.text().strip() or None
        if self._check(self.gdb_device, not dev or gamedb.device_label(dev) == dev,
                       "A name without / or \\ and only plain letters, digits and "
                       "punctuation, up to 64."):
            put('gamedb', 'device', dev, c.get('gamedb_device'))
        turl = self.tor_url.text().strip().rstrip('/') or None
        if self._check(self.tor_url, not turl or bool(LOCAL_URL_RE.match(turl)),
                       "The client must be on this machine, like http://127.0.0.1:9091."):
            put('torrent', 'url', turl, c.get('torrent_url'))
        ratio = self.tor_ratio.text().strip().replace(',', '.') or None
        try:
            ok = ratio is None or float(ratio) >= 0
        except ValueError:
            ok = False
        if self._check(self.tor_ratio, ok, "A number, like 1.5 — or empty to keep seeding."):
            if (float(ratio) if ratio is not None else None) != c.get('torrent_seed_ratio'):
                u[('torrent', 'seed_ratio')] = ratio
        put('torrent', 'client', self.tor_client.currentData(), c.get('torrent_client'))
        put('torrent', 'username', self.tor_user.text().strip() or None,
            c.get('torrent_username'))
        put('torrent', 'password', self.tor_pass.text() or None, c.get('torrent_password'))
        put('torrent', 'download_dir', self.tor_dir.text().strip() or None,
            c.get('torrent_download_dir'))
        put('integrations', 'steam_account', self.account.text().strip() or None,
            c.get('steam_account'))
        n = len(u)
        fkey = self.fr_key.text().strip() or None
        if self.fr_key.isEnabled() and self._check(
                self.fr_key, not fkey or bool(STEAM_KEY_RE.match(fkey)),
                "A Steam Web API key is 32 letters and digits (0-9 and A-F)."):
            put('steam', 'web_api_key', fkey, c.get('steam_web_api_key'))
        put('friends', 'enabled', self.fr_enabled.isChecked(), c.get('friends_enabled', False))
        sid = self.fr_steamid.text().strip() or None
        if self._check(self.fr_steamid, not sid or bool(STEAMID_RE.match(sid)),
                       "A SteamID64 is 17 digits starting 7656119."):
            put('friends', 'steamid', sid, c.get('friends_steamid'))
        friends_changed = len(u) > n
        put('install', 'base', self.base.text().strip() or None, c.get('install_base'))
        put('install', 'scan_dir', self.scan_dir.text().strip() or None, c.get('scan_dir'))
        more = [d.strip() for d in self.scan_more.text().split(';') if d.strip()]
        put('install', 'scan_dirs', more or None, c.get('scan_dirs') or None)
        put('install', 'unattended', self.unattended.isChecked(), c.get('unattended', True))
        put('install', 'trash_repack', self.trash_repack.isChecked(),
            c.get('trash_repack', False))
        put('runner', 'backend', self.runner.currentData(), c.get('runner') or 'auto')
        for box, k in ((self.al_atl, 'atl'), (self.al_fitgirl, 'fitgirl'),
                       (self.al_updates, 'repack_updates'), (self.al_proton, 'proton')):
            put('alerts', k, box.isChecked(), c.get(f'alerts_{k}', True))
        put('alerts', 'sale_min_cut', self.al_sale.currentData(), c.get('alerts_sale_min_cut', 0))
        proton = self.proton.currentData()
        changed = proton != self._proton_current
        if changed:
            u[('runner', 'proton')] = proton
            u[('heroic', 'proton')] = None     # legacy key; [runner] proton replaces it
        self._pending = False
        if not u:
            return
        try:
            save_config(u)
        except (ConfigError, OSError) as e:
            self.error.setText(f"Couldn't save: {e}")
            self.error.setVisible(True)
            self._pending = True
            return
        self.error.setVisible(False)
        if gamedb_changed:
            gamedb.clear_cache()               # old account's data off the disk too
        self.gamedb_changed = gamedb_changed
        self._proton_current = proton
        self.cfg = load_config()
        if friends_changed:
            self._friends_status()
        self._saved_note_flash()
        self.saved.emit(changed)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .config import ConfigError, load_config, save_config  # noqa: E402
from .dashboard import ClickRow, GlyphBadge  # noqa: E402
from .details import Disclosure, ElideLabel  # noqa: E402
from .friends import FRIENDS  # noqa: E402
from .jobs import cli_json, in_thread  # noqa: E402
from .tray import tray_available  # noqa: E402
from .widgets import (amp, clear_layout, FlowLayout, glyph_pixmap, icon_label,  # noqa: E402
    label, NoWheelCombo, open_modal, plain_label, SegmentedControl, set_glyph_icon)
