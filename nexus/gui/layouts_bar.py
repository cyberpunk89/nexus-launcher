"""Customise Home's top panel (w-92): Start from (presets), Your layouts
(chips, Save as…, My old Home), the active layout's manage strip, and Add
widgets. Only shown while customising. Plus the Switch by time modal's body.

The panel only shows a `LayoutsView` and emits what was pressed; the
Dashboard's controller does the work. Chip buttons are kept across a
`show()` so a pad's focus stays on the chip it pressed."""

from dataclasses import dataclass, field

from PyQt6.QtCore import pyqtSignal, QRectF, QSize, Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .layouts import (clean_rule, dash_preset, hhmm, MAX_LAYOUTS, minutes, overlaps, PRESETS, preset_thumb,
    rule_text)
from .theme import CHIP_H, G, GRAD, P, R, scaled, SP
from .widgets import (amp, FlowLayout, icon_button, icon_label, label, plain_label,
    SegmentedControl, set_glyph_icon)

CHIP_TEXT_W = 150                # a layout chip's name, elided past this (scaled)
NAME_W = 120                     # the manage strip's name, elided past this (scaled)
DENSITIES = ('comfortable', 'compact')
DENSITY_NAMES = {'comfortable': "Comfortable", 'compact': "Compact"}
DENSITY_NOTE = "Spacing on Home only. Appearance → Size changes text and controls everywhere."
FULL_TIP = f"{MAX_LAYOUTS} layouts is the most. Delete one to save another."
FULL_NOTE = f"{MAX_LAYOUTS} of {MAX_LAYOUTS}. Delete one to save another."
EMPTY_NOTE = "Save this Home to switch back to it later, or by time of day."
PRESET_NOTE = "Your current Home is saved first"
AUTO_TIP = "Saved for you before a preset was picked"
DEFAULT_RULE = {'from': '20:00', 'to': '06:00'}


@dataclass
class LayoutsView:
    """What the panel shows: the saved layouts ({name, rule?, auto?}),
    the index of the one Home is on, whether My old Home is offered, and
    an inline note for Start from (empty: the usual one)."""
    layouts: list = field(default_factory=list)
    active: int | None = None
    offer_old: bool = False
    note: str = ''
    preset: str | None = None        # the preset Home started from (marked while no layout is on)

    def signature(self):
        return (tuple((lt['name'], rule_text(lt.get('rule')), bool(lt.get('auto')))
                      for lt in self.layouts), self.active, self.offer_old, self.note,
                self.preset)


def rule_line(lt: dict | None) -> str:
    """The manage strip's muted line for a layout."""
    if lt is None:
        return ''
    if clean_rule(lt.get('rule')):
        return f"switches {rule_text(lt['rule'])}"
    return "saved for you before a preset" if lt.get('auto') else "no time rule"


def rule_note(rule: dict) -> str:
    """The Switch by time modal's explanation for a window."""
    if rule['from'] == rule['to']:
        return "Pick two different times."
    cross = minutes(rule['to']) < minutes(rule['from'])
    return (f"Every day, {rule['from']} to {rule['to']}" + (" the next morning" if cross else "")
            + ". Home switches when it opens, never while you're using it.")


def overlap_note(rule: dict, others) -> str:
    hits = overlaps(rule, others) if rule['from'] != rule['to'] else []
    if not hits:
        return ''
    name, other = hits[0]
    more = f" and {len(hits) - 1} more" if len(hits) > 1 else ''
    return (f"Overlaps “{name}” ({other['from']} to {other['to']}){more}: "
            "the one that starts later wins.")


class PresetThumb(QWidget):
    """A preset in miniature (60x44): its shown widgets packed by the same
    flow as Home (`preset_thumb`), cells in the surface colour with an overlay
    border, the Stage in the tide gradient. Mouse-transparent: the row is the
    button."""
    W, H, PAD = 60, 44, 4

    def __init__(self, items, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(scaled(self.W), scaled(self.H))
        self.cells = preset_thumb(items)

    def anchor_ids(self) -> list[str]:
        return [c.id for c in self.cells if c.id == 'stage']

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        pt.setPen(QPen(QColor(P['border']), 1.0))
        pt.setBrush(QColor(P['surface_1']))
        pt.drawRoundedRect(box, R['sm'], R['sm'])
        if self.cells:
            pad = scaled(self.PAD)
            inner = box.adjusted(pad, pad, -pad, -pad)
            gap = scaled(2)
            colw = (inner.width() - 2 * gap) / 3
            total = max(c.y + c.h for c in self.cells)
            k = inner.height() / total
            for c in self.cells:
                r = QRectF(inner.left() + c.col * (colw + gap), inner.top() + c.y * k,
                           c.span * colw + (c.span - 1) * gap, max(2.0, c.h * k - gap))
                rad = min(float(R['sm']) / 2, r.height() / 2)
                if c.id == 'stage':
                    g = QLinearGradient(r.topLeft(), r.bottomRight())
                    a, b = (QColor(x) for x in GRAD['tide'])
                    g.setColorAt(0.0, a)
                    g.setColorAt(1.0, b)
                    pt.setPen(Qt.PenStyle.NoPen)
                    pt.setBrush(QBrush(g))
                else:
                    pt.setPen(QPen(QColor(P['overlay']), 1.0))
                    pt.setBrush(QColor(P['surface_2']))
                pt.drawRoundedRect(r, rad, rad)
        pt.end()


class PresetRow(QPushButton):
    """A Start from row: as tall as its name and wrapped tip need (height for
    width), never less than the thumbnail and padding, so a long tip, a bigger
    Size or other fonts never clip it."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("PresetRow")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        lay = self.layout()
        return lay.totalHeightForWidth(w) if lay is not None else super().heightForWidth(w)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.layout().totalMinimumSize().height())

    def sizeHint(self) -> QSize:
        w = max(self.width(), scaled(240))
        return QSize(w, self.heightForWidth(w))


class LayoutsBar(QFrame):
    """The Layouts tab of the Customise panel: Start from (a row per preset),
    Your layouts (chips and the manage strip) and Density. Stacked, one
    section under another."""
    preset = pyqtSignal(str)
    switch = pyqtSignal(int)
    save_as = pyqtSignal()
    adopt_old = pyqtSignal()
    rename = pyqtSignal()
    delete = pyqtSignal()
    edit_rule = pyqtSignal()
    density = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("DashLayouts")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        self._sig = None
        # Start from: a row per preset
        lay.addWidget(label("Start from".upper(), "LayoutKicker"))
        self.preset_btns: dict[str, QPushButton] = {}
        self.thumbs: dict[str, PresetThumb] = {}
        self.names: dict[str, QLabel] = {}
        self.tips: dict[str, QLabel] = {}
        for key, text, tip in PRESETS:
            lay.addWidget(self._preset_row(key, text, tip))
        self.preset_note = label(PRESET_NOTE, "Muted", wrap=True)
        lay.addWidget(self.preset_note)
        lay.addSpacing(SP['2'])
        # Your layouts
        lay.addWidget(label("Your layouts".upper(), "LayoutKicker"))
        self.chips_host = QWidget(self)
        self.chips = FlowLayout(self.chips_host, margin=0, spacing=SP['1_5'])
        lay.addWidget(self.chips_host)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.layout_btns: list[QPushButton] = []
        self.empty_note = label(EMPTY_NOTE, "Muted", wrap=True)
        self.chips.addWidget(self.empty_note)
        self.btn_save = QPushButton("Save as…", self.chips_host)
        self.btn_save.setObjectName("Chip")
        set_glyph_icon(self.btn_save, G['plus'], scaled(13))
        self.btn_save.clicked.connect(self.save_as)
        self.chips.addWidget(self.btn_save)
        self.full_note = label(FULL_NOTE, "Muted")
        self.full_note.setMinimumHeight(scaled(CHIP_H))
        self.chips.addWidget(self.full_note)
        self.btn_old = QPushButton("My old Home", self.chips_host)
        self.btn_old.setObjectName("Chip")
        self.btn_old.setToolTip("Your Home from before the Stage, as a layout")
        set_glyph_icon(self.btn_old, G['home'], scaled(13))
        self.btn_old.clicked.connect(self.adopt_old)
        self.chips.addWidget(self.btn_old)
        # The active layout's manage strip
        self.manage = QFrame(self)
        self.manage.setObjectName("DashEditBar")
        self.manage_lay = QVBoxLayout(self.manage)
        self.manage_lay.setContentsMargins(SP['3'], SP['2'], SP['2'], SP['2'])
        self.manage_lay.setSpacing(SP['1'])
        info = QHBoxLayout()
        info.setSpacing(SP['2'])
        self.m_name = plain_label("", "LayoutName")
        info.addWidget(self.m_name)
        self.m_rule = label("", "LayoutRule")
        self.m_rule.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info.addWidget(self.m_rule, 1)
        self.manage_lay.addLayout(info)
        btns = QHBoxLayout()
        btns.setSpacing(0)
        self.btn_time = QPushButton("Switch by time…", self.manage)
        self.btn_rename = QPushButton("Rename…", self.manage)
        self.btn_delete = QPushButton("Delete…", self.manage)
        for b, g, sig in ((self.btn_time, G['clock'], self.edit_rule),
                          (self.btn_rename, G['pencil'], self.rename),
                          (self.btn_delete, G['trash'], self.delete)):
            b.setObjectName("Ghost")
            set_glyph_icon(b, g, 15)
            b.clicked.connect(sig)
            btns.addWidget(b)
        btns.addStretch()
        self.manage_lay.addLayout(btns)
        lay.addWidget(self.manage)
        self.manage.setVisible(False)
        lay.addSpacing(SP['2'])
        # Density
        lay.addWidget(label("Density".upper(), "LayoutKicker"))
        self.density_seg = SegmentedControl('pill', scaled(34), self)
        for key in DENSITIES:
            self.density_seg.add(key, DENSITY_NAMES[key])
        self.density_seg.changed.connect(self.density)
        lay.addWidget(self.density_seg, 0, Qt.AlignmentFlag.AlignLeft)
        self.density_note = label(DENSITY_NOTE, "Muted", wrap=True)
        lay.addWidget(self.density_note)
        lay.addStretch()
        self.show_view(LayoutsView())

    def _preset_row(self, key: str, text: str, tip: str) -> QPushButton:
        b = PresetRow(self)
        b.setProperty('name', text)
        b.setAccessibleName(text)
        b.setToolTip(f"{tip}. Your current Home is saved first.")
        b.clicked.connect(lambda _=False, k=key: self.preset.emit(k))
        row = QHBoxLayout(b)
        row.setContentsMargins(SP['2'], SP['2'], SP['3'], SP['2'])
        row.setSpacing(SP['3'])
        thumb = PresetThumb(dash_preset(key), b)
        row.addWidget(thumb, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(SP['0_5'])
        name = label(text, "PresetName")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        blurb = label(tip, "PresetTip", wrap=True)
        blurb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        blurb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(name)
        col.addWidget(blurb)
        row.addLayout(col, 1)
        self.preset_btns[key], self.thumbs[key] = b, thumb
        self.names[key], self.tips[key] = name, blurb
        return b

    def set_density(self, name: str):
        """Show Home's density (no signal)."""
        if name in DENSITIES:
            self.density_seg.set_current(name, animate=False)

    # ---- state ----
    def show_view(self, view: LayoutsView):
        """Show `view`; nothing is rebuilt when it didn't change, and chip
        buttons are reused, so focus stays where it was."""
        sig = view.signature()
        if sig == self._sig:
            return
        self._sig = sig
        layouts = view.layouts
        n, full = len(layouts), len(layouts) >= MAX_LAYOUTS
        active = layouts[view.active] if view.active is not None \
            and 0 <= view.active < n else None
        self.preset_note.setText(view.note or PRESET_NOTE)
        for key, b in self.preset_btns.items():
            on = 'true' if active is None and view.preset == key else 'false'
            if b.property('on') != on:
                b.setProperty('on', on)
                b.style().unpolish(b)
                b.style().polish(b)
        # Focus first: on a chip or button about to go, it moves to a stayer.
        going = set(self.layout_btns[n:])
        if not view.offer_old:
            going.add(self.btn_old)
        if active is None:
            going |= {self.btn_time, self.btn_rename, self.btn_delete}
        f = QApplication.focusWidget()
        move_focus = f in going
        while len(self.layout_btns) < n:
            b = QPushButton(self.chips_host)
            b.setObjectName("Chip")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, b=b: self._chip(b))
            self.group.addButton(b)
            self.layout_btns.append(b)
        for b in self.layout_btns[n:]:
            self.group.removeButton(b)
            self.chips.removeWidget(b)
            b.hide()
            b.deleteLater()
        del self.layout_btns[n:]
        while self.chips.count():                  # the order again, same widgets
            self.chips.takeAt(0)
        order = ([self.empty_note] if not n else []) + self.layout_btns + [self.btn_save] \
            + [self.full_note, self.btn_old]
        for w in order:
            self.chips.addWidget(w)
        self.empty_note.setVisible(not n)
        self.full_note.setVisible(full)
        self.btn_old.setVisible(view.offer_old)
        self.btn_save.setEnabled(not full)
        self.btn_save.setToolTip(FULL_TIP if full else "Save this Home as a new layout")
        self.group.setExclusive(False)
        for i, (b, lt) in enumerate(zip(self.layout_btns, layouts)):
            self._dress(b, lt)
            b.setChecked(i == view.active)
            b.show()
        self.group.setExclusive(True)
        # Manage strip
        if active is not None:
            self.m_name.setText(self.m_name.fontMetrics().elidedText(
                active['name'], Qt.TextElideMode.ElideRight, scaled(NAME_W)))
            self.m_name.setToolTip(active['name'])
            self.m_rule.setText(rule_line(active))
            self.btn_time.setText("Change time…" if clean_rule(active.get('rule'))
                                  else "Switch by time…")
        if move_focus:
            to = self.layout_btns[view.active] if active is not None \
                else self.btn_save if not full else self.preset_btns['cinematic']
            to.setFocus(Qt.FocusReason.OtherFocusReason)
        self.manage.setVisible(active is not None)

    def _dress(self, b: QPushButton, lt: dict):
        name = lt['name']
        b.ensurePolished()
        b.setText(amp(b.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight,
                                                  scaled(CHIP_TEXT_W))))
        tip = [name]
        rule = clean_rule(lt.get('rule'))
        auto = bool(lt.get('auto'))
        if rule:
            set_glyph_icon(b, G['clock'], scaled(13), 'subtext', on_role='accent')
            tip.append(f"Switches {rule_text(rule)}")
        elif auto:
            set_glyph_icon(b, G['history'], scaled(13), 'muted', on_role='accent')
        else:
            b._glyph_spec = None
            b.setIcon(QIcon())
        if auto:
            tip.append(AUTO_TIP)
        b.setToolTip("\n".join(tip))
        if b.property('auto') != ('true' if auto else 'false'):
            b.setProperty('auto', 'true' if auto else 'false')
            b.style().unpolish(b)
            b.style().polish(b)

    def _chip(self, b: QPushButton):
        if b in self.layout_btns:
            self.switch.emit(self.layout_btns.index(b))


class TimeField(QFrame):
    """‹ HH › : ‹ MM ›, buttons only (a pad steps it with A): hours step 1,
    minutes 15, both wrap, and minutes never carry into the hour."""
    changed = pyqtSignal()

    def __init__(self, value: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TimeField")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['0_5'])
        self.h, self.m = 0, 0
        self.lh = label("", "TimeValue")
        self.lm = label("", "TimeValue")
        self.btns = []
        for part in ('h', 'm'):
            lab = self.lh if part == 'h' else self.lm
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setFixedWidth(scaled(30))
            dn = icon_button(G['chevron_left'], "Earlier" if part == 'h' else "−15 min",
                             scaled(30), "Ghost", round_=True)
            up = icon_button(G['chevron_right'], "Later" if part == 'h' else "+15 min",
                             scaled(30), "Ghost", round_=True)
            for b, step in ((dn, -1), (up, 1)):
                b.setParent(self)
                b.setAutoRepeat(True)
                b.clicked.connect(lambda _=False, p=part, s=step: self.step(p, s))
            lay.addWidget(dn)
            lay.addWidget(lab)
            lay.addWidget(up)
            self.btns += [dn, up]
            if part == 'h':
                lay.addWidget(label(":", "TimeValue"))
        self.set_value(value)

    def value(self) -> str:
        return f"{self.h:02d}:{self.m:02d}"

    def set_value(self, value: str):
        t = minutes(value)
        self.h, self.m = t // 60, t % 60
        self.lh.setText(f"{self.h:02d}")
        self.lm.setText(f"{self.m:02d}")

    def step(self, part: str, step: int):
        if part == 'h':
            self.h = (self.h + step) % 24
        else:
            self.m = (self.m + 15 * step) % 60
        self.set_value(hhmm(self.h * 60 + self.m))
        self.changed.emit()


class TimeRuleBody(QWidget):
    """The Switch by time modal's body: From and To, the explanation, and a
    warning when the window overlaps another layout's rule."""
    changed = pyqtSignal()

    def __init__(self, rule: dict | None, others, parent: QWidget | None = None):
        super().__init__(parent)
        rule = clean_rule(rule) or DEFAULT_RULE
        self.others = list(others)
        g = QVBoxLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(SP['3'])
        self.fields = []
        for text, value in (("From", rule['from']), ("To", rule['to'])):
            r = QHBoxLayout()
            r.setSpacing(SP['3'])
            k = label(text, "GlanceKey")
            k.setFixedWidth(scaled(44))
            r.addWidget(k)
            tf = TimeField(value, self)
            tf.changed.connect(self._changed)
            r.addWidget(tf)
            r.addStretch()
            g.addLayout(r)
            self.fields.append(tf)
        self.note = label("", "Muted", wrap=True)
        g.addWidget(self.note)
        self.warn_row = QWidget(self)
        wl = QHBoxLayout(self.warn_row)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(SP['2'])
        wl.addWidget(icon_label(G['warn'], 13, P['warning']), 0, Qt.AlignmentFlag.AlignTop)
        self.warn = plain_label("", "Muted", wrap=True)
        wl.addWidget(self.warn, 1)
        g.addWidget(self.warn_row)
        self._sync()

    def rule(self) -> dict:
        return {'from': self.fields[0].value(), 'to': self.fields[1].value()}

    def can_submit(self) -> bool:
        r = self.rule()
        return r['from'] != r['to']

    def first(self) -> QWidget:
        """Where focus starts: From's hour ›."""
        return self.fields[0].btns[1]

    def _sync(self):
        r = self.rule()
        self.note.setText(rule_note(r))
        warn = overlap_note(r, self.others)
        self.warn.setText(warn)
        self.warn_row.setVisible(bool(warn))

    def _changed(self):
        self._sync()
        self.changed.emit()


def time_rule_body(rule, others) -> tuple[TimeRuleBody, object]:
    """The body for Modal.open and its can_submit."""
    body = TimeRuleBody(rule, others)
    return body, body.can_submit
