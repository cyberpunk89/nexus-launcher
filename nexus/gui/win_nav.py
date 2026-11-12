"""Moving around the window: pages, back/forward history, where focus
goes, and Escape."""


from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication, QScrollBar, QWidget


class NavigationMixin:
    """Part of MainWindow (nexus/gui/window.py), which mixes it in; `self` is
    the window."""

    def _shortcuts(self):
        def sc(keys, fn, theatre=False):
            # Desktop shortcuts would act on the hidden desk behind Theatre.
            # While a modal asks (w-66) only Escape acts: it answers first.
            run = fn if theatre else (lambda fn=fn: None if self._theatre else fn())
            if fn != self._escape:
                run = (lambda run=run: None if self._open_modal() is not None else run())
            for k in keys:
                s = QShortcut(QKeySequence(k), self)
                s.activated.connect(run)
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
        # Theatre's quick menu from a keyboard or a TV remote (Guide / Start
        # on a pad).
        sc(["F10", QKeySequence(Qt.Key.Key_HomePage)], self.toggle_quick, theatre=True)
        sc(["Ctrl+N"], self.open_install)
        sc(["Ctrl+`"], lambda: (self.go(self.ACTIVITY), self.activity.show_debug(True)))
        sc(["Escape"], self._escape, theatre=True)
        sc(["Alt+Left", "Back"], lambda: not self._theatre and self.go_back())
        sc(["Alt+Right", "Forward"], lambda: not self._theatre and self.go_forward())
        for i, page in enumerate(self.TABS, start=1):
            sc([f"Ctrl+{i}"], lambda p=page: not self.nav[p].isHidden() and self.go(p))
        sc(["Ctrl+5"], lambda: self.go(self.ACTIVITY))
        sc(["Ctrl+,"], lambda: self._open_settings())
        sc(["Ctrl+K"], self.open_palette)

    def open_palette(self, game=None):
        """Ctrl+K: search everything (gui/palette.py); with a game, its actions."""
        if self._theatre or self._open_modal() is not None:
            return                                  # a question is waiting for its answer
        sheet = self._open_sheet()
        if sheet is not None:
            sheet.close_sheet()
        self.palette.open(game)

    def _palette_picked(self, key: str):
        self.prefs.palette_recent = list(self.palette.recent)
        self.prefs.save()

    # ---- navigation ----
    HISTORY = 30

    def go(self, page, _history=True):
        page, section = self.PAGE_ALIASES.get(page, (page, None))
        if page not in self.pages:
            return
        self._close_foryou()                     # collapsed on leaving Home and on every show
        if page != self.HOME:
            self._clear_stage_pick()             # Pick for me's pick lasts while Home shows
        if _history and self.current() is not None and self.current() != page:
            # Back / Forward (mouse buttons, Alt+←/→, Esc on a side page):
            # the place left, a category page included (w-98).
            self._push_place(self._place())
        if self.current() == self.SETTINGS and page != self.SETTINGS:
            self.settings.commit()               # a field still being typed in
            if self.settings.dirty:
                self.toast(f"Not saved — {self.settings.unsaved_reason()}", 'warn',
                           [("Fix it", lambda: self._open_settings(*self.settings.unsaved_at())),
                            ("Discard", self.settings.load)], 10000)
        self.stack.setCurrentWidget(self.page_widget(page))
        self.tabs.set_current(page if page in self.TABS else None)
        for p, b in self.icons.items():
            b.setChecked(p == page)
        self.topbar.show_page_tabs(page)         # the Library's tabs ride in the bar (w-118)
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
        if page == self.HOME:
            # Switch by time (w-92): a saved layout's daily window, checked
            # as Home shows (at most once a minute), before focus lands.
            dash = getattr(self, 'dashboard', None)
            if dash is not None:
                dash.time_check()
        if self.input.mode in ('keyboard', 'pad') and not self._theatre:
            self._page_focus.start()            # keys/pad: land somewhere on the new page
        self._sync_presence()                   # friends' presence only while Home shows

    def _focus_new_page(self):
        f = QApplication.focusWidget()
        here = self.stack.currentWidget()
        if f is not None and (here.isAncestorOf(f) or self.topbar.isAncestorOf(f)
                              or self.details.isAncestorOf(f) or self.nav_scope() is not self):
            return
        t = self.default_focus()
        if t is not None:
            focus_and_reveal(t)

    # History holds places: a page id, or (LIBRARY, 'cat', key) for a
    # category page in the Library (w-98) or the All categories hub (key
    # categories.HUB, w-99).
    def _place(self):
        cur = self.current()
        key = getattr(self, '_cat_key', None)
        return (self.LIBRARY, 'cat', key) if cur == self.LIBRARY and key is not None else cur

    def _push_place(self, place):
        if place is None:
            return
        self._back.append(place)
        del self._back[:-self.HISTORY]
        self._forward.clear()

    def _place_ok(self, place) -> bool:
        if isinstance(place, tuple):
            return not self._theatre and categories.is_page_key(place[2])
        return place in self.pages and self._page_shown(place)

    def _go_place(self, place):
        """Go back (or forward) to a place: a category page opens again,
        the Library itself is Browse."""
        if isinstance(place, tuple):
            if not self.open_category(place[2], _history=False):
                self.go(self.LIBRARY, _history=False)       # it's gone: Browse
                if self._category_open():
                    self.close_category(_history=False)
            return
        self.go(place, _history=False)
        if place == self.LIBRARY and self._category_open():
            self.close_category(_history=False)

    def go_back(self) -> bool:
        return self._step(self._back, self._forward)

    def go_forward(self) -> bool:
        return self._step(self._forward, self._back)

    def _step(self, src: list, dst: list) -> bool:
        cur = self._place()
        while src:
            place = src.pop()
            if place != cur and self._place_ok(place):
                if cur is not None:
                    dst.append(cur)
                self._go_place(place)
                return True
        return False

    def _page_shown(self, page) -> bool:
        """A tab that's switched off (News, Deals) isn't a place to go back to."""
        b = self.nav.get(page) if hasattr(self, 'nav') else None
        return b is None or not b.isHidden()

    def _on_system_section(self, key):
        """Each System section brings itself up to date when shown."""
        if key == 'play':
            self.playhistory.refresh(games=self._games)
        elif key == 'health' and self._health_stale:
            self._health_stale = False
            self.health.refresh()
        elif key == 'report':
            self.report.shown()
        elif key == 'storage':
            self.storage.set_library([g for g in self._games if is_installed(g)], self._disks)
            self.storage.load_orphans()

    def _showing(self, key) -> bool:
        """Whether that page (or System / Deals section) is on screen now."""
        page, section = self.PAGE_ALIASES.get(key, (key, None))
        if self.current() != page:
            return False
        return section is None or self.pages[page].section() == section

    def _focus_moved(self, _old, _new):
        self._update_hints()
        if hasattr(self, 'attract'):
            self._sync_ribbon()                       # pages over the Ribbon pause 'Moving'

    def _focus_search(self):
        self.topbar.open_search()                # the Search button opens into the field

    def _escape(self):
        """Back, one step at a time: leave the details panel (focus returns
        to the game), then close it; leave the search; leave full screen;
        from other pages, go back to the library. In Theatre the quick menu
        comes first: its question, then the menu. A modal goes before any
        sheet."""
        if self._theatre and self.quick.back():
            self.sounds.play('back')
            self._update_hints()
            return
        if self.palette.isVisible():
            self.palette.escape()
            return
        modal = self._open_modal()
        if modal is not None:
            modal.back()                      # its keyboard first, then the question
            self.sounds.play('back')
            return
        sheet = self._open_sheet()
        if sheet is not None:
            sheet.close_sheet()
            self.sounds.play('back')          # Theatre only (UiSounds checks)
            return
        if self._foryou_open():
            self.dashboard.foryou.close_list(refocus=True)   # back to the pill
            return
        focus = QApplication.focusWidget()
        in_details = focus is not None and self.details.isAncestorOf(focus)
        if self._theatre:
            if self.theatre.back():           # F11 or the quick menu leave Theatre, not B
                self.sounds.play('back')      # only when B closed something
            return
        if self.desk_search.isVisible():
            self.desk_search.close_search()
            return
        if self._filter_open():
            self.filter_pop.close_popover()
            return
        if self.current() == self.NEWS and self.newspage.reader_open():
            self.newspage.close_reader()
        elif self.current() == self.HOME and self.dashboard.editing:
            self.dashboard.set_editing(False)
        elif self.current() == self.SETUP:
            self.setup.back()                   # a step back; never out of setup
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
        elif self.current() == self.LIBRARY and self._category_open():
            # Back where the page was opened from (Browse puts its own
            # scroll and focus back), or Browse when history has nothing.
            if not self.go_back():
                self.close_category()
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
        if self._theatre and self.quick.isVisible():
            return self.quick
        modal = self._open_modal()
        if modal is not None:
            return modal.scope()
        sheet = self._open_sheet()
        if sheet is not None:
            return sheet
        if self._foryou_open():
            return self.dashboard.foryou.list
        if not self._theatre and self.desk_search.isVisible():
            return self.desk_search
        if self._filter_open():
            return self.filter_pop.card
        if self._theatre:
            if self.theatre.trailer.isVisible():
                return self.theatre.trailer
            if self.theatre.search.isVisible():
                return self.theatre.search
            return self.theatre.details if self.theatre.details.isVisible() else self.theatre
        return self

    def _filter_open(self) -> bool:
        """The Library's Filter popover is up (w-117)."""
        pop = getattr(self, 'filter_pop', None)
        return pop is not None and not self._theatre and pop.isVisible()

    def _foryou_open(self) -> bool:
        """For you now's list is open on Home's Stage (w-89)."""
        dash = getattr(self, 'dashboard', None)
        return not self._theatre and dash is not None and dash.foryou.list_open()

    def _close_foryou(self):
        dash = getattr(self, 'dashboard', None)
        if dash is not None:
            dash.foryou.close_list()

    def _clear_stage_pick(self):
        dash = getattr(self, 'dashboard', None)
        if dash is not None:
            dash.stage.clear_pick()

    def _open_sheet(self) -> 'ActionSheet | None':
        """The sheet that's up: in Theatre its settings first (w-63), then
        the options sheet."""
        if self._theatre and self.theatre_settings.isVisible():
            return self.theatre_settings
        sheet = self.theatre_sheet if self._theatre else self.sheet
        return sheet if sheet.isVisible() else None

    def _modal(self) -> 'Modal':
        """The modal for this shell: Theatre's (TV-sized) or the desk's."""
        return self.theatre_modal if self._theatre else self.modal

    def _open_modal(self) -> 'Modal | None':
        m = self._modal()
        return m if m.isVisible() else None

    def default_focus(self) -> QWidget | None:
        """Where focus lands when nothing in the window has it."""
        if self._theatre and self.quick.isVisible():
            return self.quick.default_focus()
        modal = self._open_modal()
        if modal is not None:
            return modal.default_focus()
        sheet = self._open_sheet()
        if sheet is not None:
            return sheet.default_focus()
        if self._foryou_open():
            return self.dashboard.foryou.list.default_focus()
        if not self._theatre and self.desk_search.isVisible():
            return self.desk_search.default_focus()
        if self._filter_open():
            return self.filter_pop.default_focus()
        if self._theatre:
            return self.theatre.default_focus()
        cur = self.current()
        if cur == self.HOME:
            stage = self.dashboard.stage
            shown = not stage.isHidden() and not stage.art.isHidden()
            if shown and stage.art.btn.isEnabled():
                return stage.art.btn
            return self._first_tile() or (stage.art.btn_details if shown else self.tabs.item(cur))
        if cur == self.LIBRARY:
            return self._first_tile() or self.search
        if cur == self.SETUP:
            return self.setup.default_focus()
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
        make = getattr(view, 'tile_for', None)
        if make is not None:                    # the grid builds only rows near the view
            return (make(slug) if slug else None) or make()
        tiles = getattr(view, 'tiles', {})
        if slug and tiles.get(slug):
            return tiles[slug][0]
        # isHidden, not isVisible: tiles added a moment ago aren't "visible"
        # until the event loop shows them; replaced ones were hide()-ed.
        for w in view.findChildren(GameTile):
            if not w.isHidden() and not w.pending and not _hidden_between(w, view):
                return w
        return None

    def _focus_view(self, slug=None):
        t = self._first_tile(slug)
        if t is not None:
            focus_and_reveal(t)
            if isinstance(t, ListView) and slug:
                t.reveal(slug)


def _hidden_between(w, top) -> bool:
    """A widget between `w` and `top` was hidden (a Home widget left out of
    the layout, the Stage hidden in Customise): `w` can't be seen."""
    p = w.parentWidget()
    while p is not None and p is not top:
        if p.isHidden():
            return True
        p = p.parentWidget()
    return False


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .. import categories  # noqa: E402
from .gameinfo import is_installed  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .library import ListView  # noqa: E402
from .tiles import GameTile  # noqa: E402
