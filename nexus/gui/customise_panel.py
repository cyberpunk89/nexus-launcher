"""Customise Home's docked panel (w-157): a 328 px column at the right of Home
with a Widgets tab and a Layouts tab, and Undo, Reset and Done under them. It
folds to a 56 px rail. Home changes as you go, so there is no Cancel.

The panel only shows controls and emits what was pressed; the Dashboard is the
controller (like LayoutsBar). `dock_width()` is a constant (w-101): the grid
chooses its columns from it, so it must never be read from the geometry the
layout is sizing.

`HomePage` is the Home page of the window: the Home scroll area and the panel
side by side. The panel is a sibling of the scroll area, never a window."""

from PyQt6.QtCore import pyqtSignal, QEvent, Qt
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from .layouts_bar import LayoutsBar
from .theme import G, scaled, SP
from .widgets import FlowLayout, icon_button, label, plain_label, SegmentedControl

DOCK_W = 328                     # the panel, scaled
RAIL_W = 56                      # folded, scaled
TABS = (('widgets', "Widgets"), ('layouts', "Layouts"))
SUB_TAIL = "Home updates as you go"
UNDO_TIP = "Take back the last change"
RESET_TIP = "Back to the preset Home started from"


class CustomisePanel(QFrame):
    undo = pyqtSignal()
    reset = pyqtSignal()
    done = pyqtSignal()
    tab = pyqtSignal(str)
    folded = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CustomiseDock")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._folded = False
        self._tab = 'widgets'
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.pages = QStackedWidget(self)
        outer.addWidget(self.pages)
        self.pages.addWidget(self._build_full())
        self.pages.addWidget(self._build_rail())
        self.setFixedWidth(self.dock_width())
        self.set_can_undo(False)

    # ---- the full panel ----
    def _build_full(self) -> QWidget:
        page = QWidget(self.pages)
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        # header: title, subline, fold
        head = QHBoxLayout()
        head.setContentsMargins(SP['5'], SP['4'], SP['3'], SP['2'])
        head.setSpacing(SP['2'])
        titles = QVBoxLayout()
        titles.setSpacing(SP['0_5'])
        self.title = label("Customise Home", "CustomiseTitle")
        self.sub = plain_label("", "CustomiseSub")
        self.sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        titles.addWidget(self.title)
        titles.addWidget(self.sub)
        head.addLayout(titles, 1)
        self.btn_fold = icon_button(G['chevron_right'], "Fold to a rail", scaled(32), "Ghost",
                                    round_=True)
        self.btn_fold.setParent(page)
        self.btn_fold.clicked.connect(lambda: self.fold(True))
        head.addWidget(self.btn_fold, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(head)
        # tabs
        tabrow = QHBoxLayout()
        tabrow.setContentsMargins(SP['5'], 0, SP['5'], 0)
        self.tabs = SegmentedControl('underline', scaled(36), page)
        for key, text in TABS:
            self.tabs.add(key, text)
        self.tabs.changed.connect(self._tab_changed)
        tabrow.addWidget(self.tabs)
        tabrow.addStretch()
        col.addLayout(tabrow)
        line = QFrame(page)
        line.setObjectName("CustomiseLine")
        line.setFixedHeight(1)
        col.addWidget(line)
        # bodies, each in its own scroll area
        self.stack = QStackedWidget(page)
        self.widgets_tab = self._body(label_text="Add widgets")
        self.layouts_tab = self._body()
        self.layouts_bar = LayoutsBar(self.layouts_tab.parentWidget())
        self.layouts_tab.addWidget(self.layouts_bar)
        self.add_host = QWidget(self.widgets_tab.parentWidget())
        self.add_row = FlowLayout(self.add_host, margin=0, spacing=SP['1_5'])
        self.widgets_tab.addWidget(self.add_host)
        self.widgets_tab.addStretch()
        self.layouts_tab.addStretch()
        col.addWidget(self.stack, 1)
        line = QFrame(page)
        line.setObjectName("CustomiseLine")
        line.setFixedHeight(1)
        col.addWidget(line)
        # footer: Undo, Reset, Done
        foot = QHBoxLayout()
        foot.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        foot.setSpacing(SP['1'])
        self.btn_undo = QPushButton("Undo", page)
        self.btn_undo.setObjectName("Ghost")
        self.btn_undo.setToolTip(UNDO_TIP)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_reset = QPushButton("Reset", page)
        self.btn_reset.setObjectName("Ghost")
        self.btn_reset.setToolTip(RESET_TIP)
        self.btn_reset.clicked.connect(self.reset)
        self.btn_done = QPushButton("Done", page)
        self.btn_done.setObjectName("Primary")
        self.btn_done.clicked.connect(self.done)
        foot.addWidget(self.btn_undo)
        foot.addWidget(self.btn_reset)
        foot.addStretch()
        foot.addWidget(self.btn_done)
        col.addLayout(foot)
        return page

    def _body(self, label_text: str = '') -> QVBoxLayout:
        """A tab's scroll area (added to the stack); returns the layout its
        contents go in."""
        scroll = QScrollArea(self.stack)
        scroll.setObjectName("CustomiseScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("CustomiseBody")
        lay = QVBoxLayout(host)
        lay.setContentsMargins(SP['5'], SP['3'], SP['5'], SP['3'])
        lay.setSpacing(SP['2'])
        if label_text:
            lay.addWidget(label(label_text.upper(), "LayoutKicker"))
        scroll.setWidget(host)
        self.stack.addWidget(scroll)
        return lay

    # ---- the rail ----
    def _build_rail(self) -> QWidget:
        page = QWidget(self.pages)
        col = QVBoxLayout(page)
        col.setContentsMargins(SP['2'], SP['3'], SP['2'], SP['3'])
        col.setSpacing(SP['2'])
        top = Qt.AlignmentFlag.AlignHCenter
        size = scaled(40)
        self.rail_unfold = icon_button(G['chevron_left'], "Unfold Customise", size, "Ghost",
                                       round_=True)
        self.rail_widgets = icon_button(G['grid'], "Widgets", size, "RailButton", round_=True)
        self.rail_layouts = icon_button(G['shelves'], "Layouts", size, "RailButton", round_=True)
        self.rail_undo = icon_button(G['history'], UNDO_TIP, size, "Ghost", round_=True)
        self.rail_done = icon_button(G['check'], "Done", size, "RailDone", round_=True)
        for b in (self.rail_unfold, self.rail_widgets, self.rail_layouts, self.rail_undo,
                  self.rail_done):
            b.setParent(page)
        for b in (self.rail_widgets, self.rail_layouts):
            b.setCheckable(True)
        self.rail_unfold.clicked.connect(lambda: self.fold(False))
        self.rail_widgets.clicked.connect(lambda: self._rail_tab('widgets'))
        self.rail_layouts.clicked.connect(lambda: self._rail_tab('layouts'))
        self.rail_undo.clicked.connect(self.undo)
        self.rail_done.clicked.connect(self.done)
        col.addWidget(self.rail_unfold, 0, top)
        col.addSpacing(SP['2'])
        col.addWidget(self.rail_widgets, 0, top)
        col.addWidget(self.rail_layouts, 0, top)
        col.addStretch()
        col.addWidget(self.rail_undo, 0, top)
        col.addWidget(self.rail_done, 0, top)
        return page

    # ---- state ----
    def dock_width(self) -> int:
        """The width the panel takes from Home: a constant by state, never a
        measured geometry (w-101)."""
        return scaled(RAIL_W if self._folded else DOCK_W)

    def is_folded(self) -> bool:
        return self._folded

    def current_tab(self) -> str:
        return self._tab

    def set_tab(self, key: str, emit: bool = False):
        if key not in dict(TABS):
            return
        self._tab = key
        self.tabs.set_current(key, animate=False)
        self.stack.setCurrentIndex([k for k, _t in TABS].index(key))
        for k, b in (('widgets', self.rail_widgets), ('layouts', self.rail_layouts)):
            b.setChecked(k == key)
        if emit:
            self.tab.emit(key)

    def _tab_changed(self, key: str):
        self.set_tab(key, emit=True)

    def _rail_tab(self, key: str):
        self.set_tab(key, emit=True)
        self.fold(False, focus=self.tabs.item(key))

    def set_can_undo(self, on: bool):
        """Undo is only on while there is something to take back. A disabled
        button that held the focus hands it to Reset / Done."""
        for b, other in ((self.btn_undo, self.btn_reset), (self.rail_undo, self.rail_done)):
            if not on and b is QApplication.focusWidget():
                other.setFocus(Qt.FocusReason.OtherFocusReason)
            b.setEnabled(on)

    def set_sub(self, text: str):
        """The subline: where Home started from, the spacing, and a promise."""
        self._sub_full = f"{text} · {SUB_TAIL}" if text else SUB_TAIL
        self._elide_sub()

    def _elide_sub(self):
        full = getattr(self, '_sub_full', '')
        self.sub.setText(self.sub.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideRight, self.dock_width() - scaled(96)))
        self.sub.setToolTip(full)

    def changeEvent(self, ev):
        """A live Appearance change (Size) moves what scaled() says: the dock
        width and the subline follow it."""
        if ev.type() == QEvent.Type.StyleChange and self.width() != self.dock_width():
            self.setFixedWidth(self.dock_width())
            self._elide_sub()
        super().changeEvent(ev)

    def fold(self, on: bool, focus: QWidget | None = None):
        """Fold to the rail or open again. Focus that was in the panel goes
        to the matching control on the other side (the rail button of the
        tab, or the tab) before the old one hides."""
        if on == self._folded:
            return
        had = self.isAncestorOf(QApplication.focusWidget())
        self._folded = on
        self.setFixedWidth(self.dock_width())
        if had or focus is not None:
            rail = self.rail_widgets if self._tab == 'widgets' else self.rail_layouts
            to = focus or (rail if on else self.tabs.item(self._tab))
            self.pages.setCurrentIndex(1 if on else 0)
            if to is not None:
                to.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.pages.setCurrentIndex(1 if on else 0)
        self.folded.emit(on)

    def default_focus(self) -> QWidget:
        """Where pad focus starts in the panel: the current tab (the rail's
        button while folded)."""
        if self._folded:
            return self.rail_widgets if self._tab == 'widgets' else self.rail_layouts
        return self.tabs.item(self._tab)

    def open(self):
        """Customise starts: the Widgets tab, unfolded."""
        self.pages.setCurrentIndex(0)
        self._folded = False
        self.setFixedWidth(self.dock_width())
        self.set_tab('widgets')


class HomePage(QWidget):
    """The window's Home page: Home's scroll area, and the Customise panel
    docked at its right."""

    def __init__(self, view: QWidget, panel: CustomisePanel, parent: QWidget | None = None):
        super().__init__(parent)
        self.view, self.panel = view, panel
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(view, 1)
        lay.addWidget(panel)
