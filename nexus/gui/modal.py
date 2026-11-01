"""The in-window modal (w-66): a centred card over a dimmed page for
anything that ends in one yes/no or one commit — every confirm(), a name
for a collection, a game's name in the library. A child overlay like
ActionSheet, never a dialog (niri floats those). The header, the
scrolling body and the footer are built once; each open refills the body.

Mouse, keyboard and pad alike: Enter runs the focused button (from a text
field, the primary when allowed), Esc / B / ✕ cancel, Tab stays inside,
a click on the scrim cancels unless something was typed. A danger
question starts on Cancel, so a habitual A or Enter never confirms it."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QFrame, QHBoxLayout, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .common import alive, MAX_COLLECTION_NAME
from .overlay import Overlay
from .theme import G, SP


class Modal(Overlay):
    """One per shell, like the sheets: the desk's and Theatre's (at
    THEATRE_SCALE). A new question while one is up replaces it; the first
    counts as cancelled."""
    closed = pyqtSignal()

    def __init__(self, parent: QWidget, scale: float = 1.0):
        super().__init__(parent)
        self.setObjectName("ModalScrim")
        self.scale = scale
        sc = self._sc
        self._width = 440
        self._on_primary = None
        self._can_submit = None
        self._typed = False                      # a field was typed in: the scrim keeps it
        self._start = None                       # where focus started (default_focus)
        self._kb = None                          # the on-screen keyboard typing for the field
        self.field: QLineEdit | None = None
        self.card = QFrame(self)
        self.card.setObjectName("Modal")
        lay = QVBoxLayout(self.card)
        m = sc(SP['5'])
        lay.setContentsMargins(m, m, m, m)
        lay.setSpacing(sc(SP['4']))
        head = QHBoxLayout()
        head.setSpacing(sc(SP['3']))
        self.badge = GlyphBadge(G['info'], 'dusk', sc(36))
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(sc(SP['0_5']))
        self.title = plain_label("", "SheetTitle", wrap=True)
        col.addWidget(self.title)
        self.sub = plain_label("", "ModalSub", wrap=True)
        col.addWidget(self.sub)
        head.addLayout(col, 1)
        self.btn_close = icon_button(G['close'], "Cancel (Esc)", sc(32), "Ghost")
        self.btn_close.clicked.connect(self._close_clicked)
        head.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.body = QVBoxLayout(host)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(sc(SP['3']))
        self.scroll.setWidget(host)
        lay.addWidget(self.scroll, 1)
        foot = QHBoxLayout()
        foot.setSpacing(sc(SP['2']))
        foot.addStretch()
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("Ghost")
        self.btn_cancel.clicked.connect(self._close_clicked)
        self.primary = QPushButton()
        self.primary.setObjectName("Primary")
        self.primary.clicked.connect(self._submit)
        for b in (self.btn_cancel, self.primary):
            b.setMinimumSize(sc(96), sc(36))
            foot.addWidget(b)
        lay.addLayout(foot)

    def _sc(self, v) -> int:
        return int(v * self.scale)

    # ---- open ----
    def open(self, title: str, body: QWidget | None, primary: str, on_primary, *,
             glyph: str = '', sub: str = '', danger: bool = False, can_submit=None,
             width: int = 440, text: str = ''):
        """Show the modal. `body` (any widget, or None) becomes the modal's
        and is deleted when it closes; `text`, plain, explains above it.
        `on_primary()` runs once the modal has closed, so it may open
        another overlay. `can_submit()` says whether the primary is allowed
        (refresh() asks again; any QLineEdit in the body does on each edit).
        Focus starts on Cancel when `danger`, else on the first text field,
        else on the primary."""
        if self.isVisible():
            self._drop_keyboard()                # replaced: the first one is cancelled
        else:
            self._remember_focus()
        self._on_primary, self._can_submit = on_primary, can_submit
        self._typed, self._width = False, width
        clear_layout(self.body)
        self.title.setText(title)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        self.badge.glyph = glyph or G['warn' if danger else 'info']
        self.badge.grad = 'ember' if danger else 'dusk'
        self.badge.update()
        parts = ([plain_label(text, "ModalText", wrap=True)] if text else []) \
            + ([body] if body is not None else [])
        for w in parts:
            self.body.addWidget(w)
            w.show()                             # now, not a pass later: focus may go there
        self.body.addStretch()
        self.scroll.setVisible(bool(parts))
        edits = [w for p in parts for w in ([p] if isinstance(p, QLineEdit) else [])
                 + p.findChildren(QLineEdit)]
        for ed in edits:
            ed.textChanged.connect(self.refresh)
            ed.textEdited.connect(self._edited)
        self.field = edits[0] if edits else None
        self.primary.setText(amp(primary))
        self.primary.setObjectName("DangerSolid" if danger else "Primary")
        self.primary.style().unpolish(self.primary)
        self.primary.style().polish(self.primary)
        self.refresh()
        self._start = self.btn_cancel if danger else self.field or self.primary
        self.setGeometry(self.parentWidget().rect())
        self.ensurePolished()                    # the theme's sizes, before measuring
        self._place()
        self._cover()
        if self._start is self.field:
            self.field.selectAll()
        self._start.setFocus(Qt.FocusReason.TabFocusReason)

    def ask(self, title: str, text: str, yes: str, on_yes, danger: bool = True):
        """A yes/no question (MainWindow.confirm): `text` under the title,
        Cancel and `yes`."""
        self.open(title, None, yes, on_yes, text=text, danger=danger)

    def prompt(self, title: str, initial: str, ok: str, on_ok, text: str = '',
               max_len: int = MAX_COLLECTION_NAME):
        """A name: one text field, `ok` saves it (Enter too), Cancel drops
        it. `on_ok(name)` gets it stripped; an empty name can't be saved."""
        ed = QLineEdit(initial)
        ed.setMaxLength(max_len)
        ed.setMinimumHeight(self._sc(40))
        self.open(title, ed, ok, lambda: on_ok(ed.text().strip()), text=text,
                  glyph=G['pencil'], can_submit=lambda: bool(ed.text().strip()))

    def refresh(self, *_):
        self.primary.setEnabled(self._allowed())

    def _allowed(self) -> bool:
        return self._can_submit is None or bool(self._can_submit())

    def _edited(self, *_):
        self._typed = True

    def _place(self):
        """The card: centred, `width` (scaled) or narrower on a small window,
        as tall as its content up to 85 % of the window, the body scrolling
        beyond that."""
        pw, ph = self.width(), self.height()
        w = max(min(self._sc(self._width), pw - 2 * self._sc(SP['6'])), self._sc(240))
        self.card.setFixedWidth(w)
        lay = self.card.layout()
        m = lay.contentsMargins()
        inner = w - m.left() - m.right()
        bl = self.body
        body_h = bl.heightForWidth(inner) if bl.hasHeightForWidth() else bl.sizeHint().height()
        self.scroll.setFixedHeight(max(body_h, 0))
        h = lay.heightForWidth(w) if lay.hasHeightForWidth() else lay.sizeHint().height()
        top = int(ph * 0.85)
        if h > top:
            self.scroll.setFixedHeight(max(body_h - (h - top), self._sc(48)))
            h = top
        self.card.setGeometry((pw - w) // 2, max((ph - h) // 2, 0), w, h)

    def resizeEvent(self, ev):
        if self.isVisible():
            self._place()
        super().resizeEvent(ev)

    # ---- close ----
    def _submit(self):
        if self.isHidden() or not self._allowed():
            return
        fn = self._on_primary
        self._close(restore=True)
        if fn is not None:
            fn()

    def _close_clicked(self):
        self.cancel()

    def cancel(self, restore: bool = True):
        """Close without the primary: ✕, Cancel, Esc / B, the scrim, Theatre
        switched on or off."""
        if self.isHidden():
            return
        self._drop_keyboard()
        self._close(restore)

    def _close(self, restore: bool):
        self._on_primary = self._can_submit = None
        # Focus goes back before the hide: hiding the focused button would
        # throw it to whatever is next (Theatre's drawer, w-60).
        self._restore_focus(restore)
        self.hide()
        clear_layout(self.body)                  # deleteLater: on_primary can still read it
        self.field = self._start = None
        self.closed.emit()

    def back(self) -> bool:
        """Esc / B: the on-screen keyboard first, when it's typing for the
        field, then the modal. False when nothing was open."""
        if self.isHidden():
            return False
        if self._kb is not None and self._kb.isVisible():
            self._kb.close_search()
            return True
        self.cancel()
        return True

    # ---- focus and the pad ----
    def scope(self) -> QWidget:
        """Where the D-pad may go: the keyboard while it types, else here."""
        return self._kb if self._kb is not None and self._kb.isVisible() else self

    def default_focus(self) -> QWidget | None:
        """The widget it opened on (w-63: a deferred refocus lands there)."""
        if self._kb is not None and self._kb.isVisible():
            return self._kb.default_focus()
        if alive(self._start) and not self._start.isHidden() and self._start.isEnabled():
            return self._start
        chain = self._focusables()
        return chain[0] if chain else None

    def type_with(self, kb):
        """A on the field with a pad: `kb` (a TheatreSearch, over this
        modal) types the name; Done puts it in the field and focuses the
        primary, B drops it."""
        if self.field is None or self.isHidden():
            return
        self._kb = kb
        kb.open_entry(self.title.text(), self.field.text(), self._typed_in,
                      max_len=self.field.maxLength())

    def _typed_in(self, text: str):
        if self.field is None or self.isHidden():
            return
        self.field.setText(text)
        self._typed = True
        focus_and_reveal(self.primary if self.primary.isEnabled() else self.field)

    def keyboard_closed(self):
        """The keyboard went away (Done or B): focus back in the modal."""
        if self._kb is None:
            return
        self._kb = None
        if self.isVisible() and self.field is not None:
            focus_and_reveal(self.field)

    def _drop_keyboard(self):
        kb, self._kb = self._kb, None
        if kb is not None and kb.isVisible():
            kb.close_search()

    def _focusables(self) -> list[QWidget]:
        body = [w for w in self.scroll.widget().findChildren(QWidget)
                if not w.isHidden() and w.isEnabled()
                and w.focusPolicy() & Qt.FocusPolicy.TabFocus]
        return body + [b for b in (self.btn_cancel, self.primary, self.btn_close)
                       if b.isEnabled()]

    def focusNextPrevChild(self, next_: bool) -> bool:
        """Tab and Shift+Tab go round inside the card, never behind it."""
        chain = self._focusables()
        if chain:
            f = QApplication.focusWidget()
            i = chain.index(f) if f in chain else -1
            focus_and_reveal(chain[(i + (1 if next_ else -1)) % len(chain)])
        return True

    def keyPressEvent(self, ev):
        k = ev.key()
        f = QApplication.focusWidget()
        if k in (Qt.Key.Key_Escape, Qt.Key.Key_Back):
            self.back()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if isinstance(f, QLineEdit):
                self._submit()
            elif isinstance(f, QAbstractButton) and self.isAncestorOf(f) and f.isEnabled():
                f.click()
        # Every other key stops here: nothing reaches the page behind.
        ev.accept()

    def mousePressEvent(self, ev):
        if not self.card.geometry().contains(ev.position().toPoint()) and not self._typed:
            self.cancel()
        ev.accept()


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .dashboard import GlyphBadge  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .widgets import amp, clear_layout, icon_button, plain_label  # noqa: E402
