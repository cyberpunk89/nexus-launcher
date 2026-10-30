"""The shelf builder (w-51): an inline panel on the Library page for a shelf
you make from rules (nexus/smart.py "shelves you build"). No dialog: it
opens under the chips, the grid below shows the games that match as you
edit, and every change is saved as you go. Numbers are picked from lists,
not typed, so a controller can drive all of it."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from nexus import smart

from .theme import G, P, SP
from .widgets import (icon_button, icon_label, label, NoWheelCombo, SegmentedControl,
    set_glyph_icon)


class RuleRow(QWidget):
    """One rule: field, op, value (when the op needs one) and ✕."""
    changed = pyqtSignal()
    remove = pyqtSignal(object)

    def __init__(self, rule: dict, fields: list[str], games: list[dict], facts: dict):
        super().__init__()
        self.rule = dict(rule)
        self._games, self._facts = games, facts
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        self.field = NoWheelCombo()
        self.field.setAccessibleName("Rule")
        for f in fields if rule['field'] in fields else [*fields, rule['field']]:
            self.field.addItem(smart.FIELDS[f][0], f)
        self.field.setCurrentIndex(self.field.findData(rule['field']))
        self.field.currentIndexChanged.connect(self._field_changed)
        self.op = NoWheelCombo()
        self.op.setAccessibleName("Condition")
        self.op.currentIndexChanged.connect(self._op_changed)
        self.value = NoWheelCombo()
        self.value.setAccessibleName("Value")
        self.value.currentIndexChanged.connect(self._value_changed)
        keep = self.value.sizePolicy()           # columns stay put when an op needs no value
        keep.setRetainSizeWhenHidden(True)
        self.value.setSizePolicy(keep)
        self.btn_remove = icon_button(G['close'], "Remove this rule", 30, "Ghost", round_=True)
        self.btn_remove.clicked.connect(lambda: self.remove.emit(self))
        for w, stretch in ((self.field, 3), (self.op, 3), (self.value, 3)):
            lay.addWidget(w, stretch)
        lay.addWidget(self.btn_remove)
        self._fill_ops()

    def _fill_ops(self):
        self.op.blockSignals(True)
        self.op.clear()
        for op, text, _kind in smart.ops(self.rule['field']):
            self.op.addItem(text, op)
        self.op.setCurrentIndex(max(0, self.op.findData(self.rule['op'])))
        self.op.blockSignals(False)
        self._fill_values()

    def _fill_values(self):
        kind = smart.value_kind(self.rule['field'], self.rule['op'])
        self.value.blockSignals(True)
        self.value.clear()
        for v, text in smart.values_for(kind, self._games, self._facts, self.rule['value']) \
                if kind else ():
            self.value.addItem(text, v)
        i = self.value.findData(self.rule['value'])
        if kind and i < 0 and self.value.count():
            i = 0
            self.rule['value'] = self.value.itemData(0)
        self.value.setCurrentIndex(i)
        self.value.setVisible(kind is not None)
        self.value.setEnabled(self.value.count() > 0)
        self.value.blockSignals(False)

    def _field_changed(self):
        self.rule = smart.default_rule(self.field.currentData())
        self._fill_ops()
        self.changed.emit()

    def _op_changed(self):
        before = smart.value_kind(self.rule['field'], self.rule['op'])
        self.rule['op'] = self.op.currentData()
        kind = smart.value_kind(self.rule['field'], self.rule['op'])
        if kind != before:
            self.rule['value'] = smart.default_value(kind)
        self._fill_values()
        self.changed.emit()

    def _value_changed(self):
        self.rule['value'] = self.value.currentData()
        self.changed.emit()


class ShelfEditor(QFrame):
    """The builder panel. The window owns the shelf list: this edits one
    shelf dict and emits `changed(shelf)` on every edit (the window saves
    it and re-filters the grid, then calls set_count)."""
    changed = pyqtSignal(dict)
    done = pyqtSignal()
    hide_asked = pyqtSignal(str)          # the shelf id
    remove_asked = pyqtSignal(str)
    MAX_W = 820                           # rules read as sentences, not across a wide window

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.setMaximumWidth(self.MAX_W)
        self.shelf: dict | None = None
        self._fields: list[str] = []
        self._games: list[dict] = []
        self._facts: dict = {}
        self.rows: list[RuleRow] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        lay.setSpacing(SP['3'])

        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        head.addWidget(icon_label(G['sparkles'], 16, P['accent']), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        self.kicker = label("Your shelf", "SmartHead")
        head.addWidget(self.kicker)
        head.addStretch()
        self.count = label("", "ShelfCount")
        head.addWidget(self.count)
        lay.addLayout(head)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Shelf name, e.g. “Quick RPGs”")
        self.name.setMaxLength(smart.MAX_NAME)
        self.name.setAccessibleName("Shelf name")
        self.name.textEdited.connect(self._name_edited)
        lay.addWidget(self.name)

        self.match_row = QWidget()
        ml = QHBoxLayout(self.match_row)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(SP['2'])
        ml.addWidget(label("Show games that match", "Muted"))
        self.match = SegmentedControl('pill', 30)
        self.match.add('all', "All", tip="Every rule must match")
        self.match.add('any', "Any", tip="One matching rule is enough")
        self.match.changed.connect(self._match_changed)
        ml.addWidget(self.match)
        ml.addWidget(label("of these rules", "Muted"))
        ml.addStretch()
        lay.addWidget(self.match_row)

        self.rules_box = QVBoxLayout()
        self.rules_box.setSpacing(SP['2'])
        lay.addLayout(self.rules_box)

        add = QHBoxLayout()
        self.btn_add = QPushButton("Add a rule")
        self.btn_add.setObjectName("Ghost")
        set_glyph_icon(self.btn_add, G['plus'], 15, 'subtext', hover_role='text')
        self.btn_add.clicked.connect(self._add_rule)
        add.addWidget(self.btn_add)
        add.addStretch()
        lay.addLayout(add)

        rule = QFrame()
        rule.setObjectName("SmartRule")
        rule.setFixedHeight(1)
        lay.addWidget(rule)

        foot = QHBoxLayout()
        foot.setSpacing(SP['2'])
        self.note = label("Saved as you go", "SavedNote")
        foot.addWidget(self.note)
        foot.addStretch()
        self.btn_hide = QPushButton("Hide shelf")
        self.btn_hide.setObjectName("Ghost")
        self.btn_hide.setToolTip("Take it off the Shelves view; it stays in the filter menu")
        set_glyph_icon(self.btn_hide, G['eye_off'], 15, 'subtext', hover_role='text')
        self.btn_hide.clicked.connect(lambda: self.shelf and self.hide_asked.emit(self.shelf['id']))
        self.btn_remove = QPushButton("Remove shelf")
        self.btn_remove.setObjectName("Danger")
        self.btn_remove.setToolTip("Your games stay; the shelf can be brought back")
        set_glyph_icon(self.btn_remove, G['trash'], 15, 'danger', hover_role='on_accent')
        self.btn_remove.clicked.connect(
            lambda: self.shelf and self.remove_asked.emit(self.shelf['id']))
        self.btn_done = QPushButton("Done")
        self.btn_done.setObjectName("Primary")
        set_glyph_icon(self.btn_done, G['check'], 15, 'on_accent', hover_role=None)
        self.btn_done.clicked.connect(self.done)
        for b in (self.btn_hide, self.btn_remove, self.btn_done):
            foot.addWidget(b)
        lay.addLayout(foot)

    # ---- filling ----
    def open(self, shelf: dict, games: list[dict], facts: dict, new: bool = False):
        """Edit `shelf` (a copy is kept); `games` and `facts` decide which
        fields and values are offered."""
        self.shelf = {**shelf, 'rules': [dict(r) for r in shelf.get('rules') or ()]}
        self._games, self._facts = games, facts or {}
        self._fields = smart.fields_offered(games, self._facts)
        self.kicker.setText("New shelf" if new else "Edit shelf")
        self.name.setText(self.shelf.get('name') or '')
        self.match.set_current(self.shelf.get('match', 'all'), animate=False)
        self._build_rows()

    def _build_rows(self):
        for r in self.rows:
            self.rules_box.removeWidget(r)
            r.hide()
            r.deleteLater()
        self.rows = []
        for rule in self.shelf['rules']:
            self._append(rule)
        self._sync()

    def _append(self, rule: dict) -> RuleRow:
        row = RuleRow(rule, self._fields, self._games, self._facts)
        row.changed.connect(self._rules_changed)
        row.remove.connect(self._remove_rule)
        self.rules_box.addWidget(row)
        self.rows.append(row)
        return row

    def _sync(self):
        n = len(self.rows)
        self.match_row.setVisible(n > 1)
        self.btn_add.setEnabled(n < smart.MAX_RULES)
        for r in self.rows:
            r.btn_remove.setVisible(n > 1)

    def set_count(self, n: int):
        self.count.setText("No games match yet" if not n else
                           f"{n} game{'s' if n != 1 else ''} match{'es' if n == 1 else ''}")

    # ---- edits ----
    def _emit(self):
        self.shelf['rules'] = [dict(r.rule) for r in self.rows]
        self.changed.emit({**self.shelf, 'rules': [dict(r) for r in self.shelf['rules']]})

    def _name_edited(self, text: str):
        self.shelf['name'] = smart.clean_name(text)
        self._emit()

    def _match_changed(self, key: str):
        self.shelf['match'] = key
        self._emit()

    def _rules_changed(self):
        self._emit()

    def _add_rule(self):
        if len(self.rows) >= smart.MAX_RULES:
            return
        used = {r.rule['field'] for r in self.rows}
        field = next((f for f in self._fields if f not in used), self._fields[0]
                     if self._fields else 'playtime')
        row = self._append(smart.default_rule(field))
        self._sync()
        self._emit()
        row.field.setFocus(Qt.FocusReason.OtherFocusReason)

    def _remove_rule(self, row: RuleRow):
        if len(self.rows) <= 1:
            return
        i = self.rows.index(row)
        self.rows.remove(row)
        self.rules_box.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._sync()
        self._emit()
        nxt = self.rows[min(i, len(self.rows) - 1)]
        nxt.field.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.done.emit()
            return
        super().keyPressEvent(ev)
