"""The shelf builder (w-51, a modal since w-67): the body of the New shelf
and Edit shelf modals for a shelf you make from rules (nexus/smart.py
"shelves you build"). It edits a draft and never touches prefs: the window
saves the draft once, on Save; Cancel, Esc or B leave nothing behind. A
match count and the first covers show what the shelf will hold. Numbers
are picked from lists, not typed, so a controller can drive all of it."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QWidget)

from nexus import smart

from .art import art_lookup, ART, COVER_KINDS, placeholder_pixmap
from .common import alive
from .theme import G, P, R, SP
from .widgets import (clear_layout, FlowLayout, icon_button, icon_label, label, NoWheelCombo,
    SegmentedControl, set_glyph_icon)


class RuleRow(QWidget):
    """One rule: field, op, value (when the op needs one) and ✕."""
    changed = pyqtSignal()
    remove = pyqtSignal(object)

    def __init__(self, rule: dict, fields: list[str], games: list[dict], facts: dict,
                 scale: float = 1.0, names: dict | None = None):
        super().__init__()
        self.rule = dict(rule)
        self._games, self._facts = games, facts
        self._names = names or {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(int(SP['2'] * scale))
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
        self.btn_remove = icon_button(G['close'], "Remove this rule", int(30 * scale), "Ghost",
                                      round_=True)
        self.btn_remove.clicked.connect(lambda: self.remove.emit(self))
        for w, stretch in ((self.field, 3), (self.op, 3), (self.value, 3)):
            w.setMinimumHeight(int(34 * scale))
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
        if kind == 'people':
            # Play together's friends (w-83): shown, not edited here (the
            # picker made them); a list can't be found with findData.
            ids = self.rule['value'] or []
            self.value.addItem(smart.people_text(ids, self._names), None)
            self.value.setToolTip(", ".join(self._names.get(i, i) for i in ids))
            self.value.setCurrentIndex(0)
            self.value.setVisible(True)
            self.value.setEnabled(False)
            self.value.blockSignals(False)
            return
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


class ShelfForm(QWidget):
    """The modal's body: name, "Start from" templates (a new shelf only),
    All / Any, the rule rows, "Add a rule", and a well with the match count
    and the first covers. `draft()` is the shelf as it stands. `games` and
    `fields` decide what the rows offer; the count is taken over `pool` (what
    the Shelves view shows). `touched` (a pick that isn't typing) and
    `relayout` (rows added or removed) are for the modal."""
    touched = pyqtSignal()
    relayout = pyqtSignal()
    COVERS = 6

    def __init__(self, shelf: dict, games: list[dict], pool: list[dict], facts: dict,
                 fields: list[str], new: bool = False, scale: float = 1.0,
                 friends: set | None = None, names: dict | None = None):
        super().__init__()
        self._friends, self._names = friends, names or {}
        sc = self._sc = lambda v: int(v * scale)        # noqa: E731
        self.scale = scale
        self.shelf = {**shelf, 'rules': [dict(r) for r in shelf.get('rules') or ()]}
        self._games, self._pool, self._facts = games, pool, facts or {}
        self._fields = fields
        self._auto = self.shelf.get('name') or ''        # a template's name, until you type one
        self._strip_slugs: list[str] | None = None
        self._more: QLabel | None = None          # "+N" after the covers
        self.rows: list[RuleRow] = []
        self.templates: dict[str, QPushButton] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(sc(SP['3']))

        self.name = QLineEdit(self.shelf.get('name') or '')
        self.name.setPlaceholderText("Shelf name, e.g. “Quick RPGs”")
        self.name.setMaxLength(smart.MAX_NAME)
        self.name.setAccessibleName("Shelf name")
        self.name.setMinimumHeight(sc(40))
        lay.addWidget(self.name)

        if new:
            lay.addWidget(label("START FROM", "SheetSection"))
            host = QWidget()
            flow = FlowLayout(host, 0, sc(SP['1_5']))
            for key, title in smart.templates(fields):
                b = QPushButton(title)
                b.setObjectName("Chip")
                b.setCheckable(True)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, k=key: self._template(k))
                flow.addWidget(b)
                self.templates[key] = b
            self._mark_template(smart.BLANK)
            lay.addWidget(host)

        lay.addWidget(label("RULES", "SheetSection"))
        self.match_row = QWidget()
        ml = QHBoxLayout(self.match_row)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(sc(SP['2']))
        ml.addWidget(label("Show games that match", "Muted"))
        self.match = SegmentedControl('pill', sc(30))
        self.match.add('all', "All", tip="Every rule must match")
        self.match.add('any', "Any", tip="One matching rule is enough")
        self.match.set_current(self.shelf.get('match', 'all'), animate=False)
        self.match.changed.connect(self._match_changed)
        ml.addWidget(self.match)
        ml.addWidget(label("of these rules", "Muted"))
        ml.addStretch()
        lay.addWidget(self.match_row)

        rules = QWidget()                        # the rows, in order (Tab follows them)
        self.rules_box = QVBoxLayout(rules)
        self.rules_box.setContentsMargins(0, 0, 0, 0)
        self.rules_box.setSpacing(sc(SP['2']))
        lay.addWidget(rules)

        add = QHBoxLayout()
        self.btn_add = QPushButton("Add a rule")
        self.btn_add.setObjectName("Ghost")
        set_glyph_icon(self.btn_add, G['plus'], 15, 'subtext', hover_role='text')
        self.btn_add.clicked.connect(self._add_rule)
        add.addWidget(self.btn_add)
        add.addStretch()
        lay.addLayout(add)

        # What the shelf will hold: the count and up to COVERS covers.
        well = QFrame()
        well.setObjectName("ModalWell")
        wl = QVBoxLayout(well)
        m = sc(SP['3'])
        wl.setContentsMargins(m, m, m, m)
        wl.setSpacing(sc(SP['2']))
        top = QHBoxLayout()
        top.setSpacing(sc(SP['2']))
        top.addWidget(icon_label(G['sparkles'], sc(16), P['accent']), 0,
                      Qt.AlignmentFlag.AlignVCenter)
        self.count = label("", "SmartHead")
        top.addWidget(self.count, 1)
        wl.addLayout(top)
        self.strip_host = QWidget()
        self.strip = QHBoxLayout(self.strip_host)
        self.strip.setContentsMargins(0, 0, 0, 0)
        self.strip.setSpacing(sc(SP['2']))
        wl.addWidget(self.strip_host)
        lay.addWidget(well)

        self.name.textChanged.connect(self._update)
        for rule in self.shelf['rules']:
            self._append(rule)
        self._sync()
        self._update()

    # ---- the draft ----
    def draft(self) -> dict:
        """The shelf as edited: what Save keeps."""
        return {'id': self.shelf.get('id') or '', 'name': smart.clean_name(self.name.text()),
                'match': self.match.current() or 'all',
                'rules': [dict(r.rule) for r in self.rows], 'removed': False}

    def matches(self) -> list[dict]:
        return smart.user_games(self.draft(), self._pool, self._facts, friends=self._friends)

    # ---- rows ----
    def _append(self, rule: dict) -> 'RuleRow':
        row = RuleRow(rule, self._fields, self._games, self._facts, self.scale, self._names)
        row.changed.connect(self._rules_changed)
        row.remove.connect(self._remove_rule)
        self.rules_box.addWidget(row)
        row.show()                               # now: focus may go there at once
        self.rows.append(row)
        return row

    def _sync(self):
        n = len(self.rows)
        self.match_row.setVisible(n > 1)
        self.btn_add.setEnabled(n < smart.MAX_RULES)
        for r in self.rows:
            r.btn_remove.setVisible(n > 1)

    def _set_rules(self, rules: list[dict]):
        for r in self.rows:
            self.rules_box.removeWidget(r)
            r.hide()
            r.deleteLater()
        self.rows = []
        for rule in rules:
            self._append(rule)
        self._sync()

    def _add_rule(self):
        if len(self.rows) >= smart.MAX_RULES:
            return
        used = {r.rule['field'] for r in self.rows}
        field = next((f for f in self._fields if f not in used), self._fields[0]
                     if self._fields else 'playtime')
        row = self._append(smart.default_rule(field))
        self._sync()
        self._edited()
        self.relayout.emit()
        row.field.setFocus(Qt.FocusReason.OtherFocusReason)

    def _remove_rule(self, row: 'RuleRow'):
        if len(self.rows) <= 1:
            return
        i = self.rows.index(row)
        self.rows.remove(row)
        # Focus moves first: hiding the focused ✕ would throw it anywhere (w-60).
        nxt = self.rows[min(i, len(self.rows) - 1)]
        nxt.field.setFocus(Qt.FocusReason.OtherFocusReason)
        self.rules_box.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._sync()
        self._edited()
        self.relayout.emit()

    # ---- templates ----
    def _template(self, key: str):
        """Start from a template: its rules (and its name, unless you named
        the shelf yourself)."""
        t = smart.from_template(key, self._fields)
        name = self.name.text().strip()
        if not name or name == self._auto:
            self.name.setText(t['name'])
            self._auto = t['name']
        self.match.set_current(t['match'], animate=False)
        self._set_rules(t['rules'])
        self._mark_template(key)
        self.touched.emit()
        self._update()
        self.relayout.emit()

    def _mark_template(self, key: str | None):
        for k, b in self.templates.items():
            b.setChecked(k == key)

    # ---- edits ----
    def _edited(self):
        """A rule or the match changed by hand: no template is it any more."""
        self._mark_template(None)
        self.touched.emit()
        self._update()

    def _match_changed(self, _key: str):
        self._edited()

    def _rules_changed(self):
        self._edited()

    # ---- the count and the covers ----
    def _update(self, *_):
        hit = self.matches()
        n = len(hit)
        self.count.setText("No games match yet" if not n else
                           f"{n} game{'s' if n != 1 else ''} match{'es' if n == 1 else ''}")
        slugs = [g['slug'] for g in hit[:self.COVERS]]
        more = f"+{n - len(slugs)}" if n > len(slugs) else ""
        if slugs == self._strip_slugs:           # the same covers: only "+N" moves
            self._more.setText(more)
            return
        self._strip_slugs = slugs
        clear_layout(self.strip)
        sc = self._sc
        made = [cover_thumb(g, sc(56), sc(84)) for g in hit[:self.COVERS]]
        if not hit:
            made.append(label("Pick rules that some of your games meet.", "Muted"))
        self._more = label(more, "Muted")
        made.append(self._more)
        for w in made:
            self.strip.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
            w.show()                             # it has its parent now: never a window
        self.strip.addStretch()


def cover_thumb(g: dict, w: int, h: int) -> QLabel:
    """One cover, from the disk only (never fetched here); a stand-in
    until it is decoded, or when the game has none. The shelf modal's well
    and Play together's (w-83)."""
    lab = QLabel()
    lab.setFixedSize(w, h)
    name = g.get('display_name') or g.get('slug') or ''
    lab.setToolTip(name)
    # Two initials, not three: a third doesn't fit this narrow.
    lab.setPixmap(placeholder_pixmap(' '.join(name.split()[:2]), w, h))
    path, _url, _kind = art_lookup(g, COVER_KINDS)
    if path:
        ART.pixmap_async(path, w, h, R['sm'],
                         lambda pm, lab=lab: alive(lab) and lab.setPixmap(pm))
    return lab
