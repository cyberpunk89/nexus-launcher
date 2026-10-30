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
        sc(["Ctrl+,"], lambda: self.go(self.SETTINGS))
        sc(["Ctrl+K"], self.open_palette)

    def open_palette(self, game=None):
        """Ctrl+K: search everything (gui/palette.py); with a game, its actions."""
        if self._theatre:
            return
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
        cur = self.current()
        if _history and cur is not None and cur != page:
            # Back / Forward (mouse buttons, Alt+←/→, Esc on a side page).
            self._back.append(cur)
            del self._back[:-self.HISTORY]
            self._forward.clear()
        if self.current() == self.SETTINGS and page != self.SETTINGS:
            self.settings.commit()               # a field still being typed in
            if self.settings.dirty:
                self.toast(f"Not saved — {self.settings.unsaved_reason()}", 'warn',
                           [("Fix it", lambda: self.go(self.SETTINGS)),
                            ("Discard", self.settings.load)], 10000)
        self.stack.setCurrentWidget(self.page_widget(page))
        self.tabs.set_current(page if page in self.TABS else None)
        for p, b in self.icons.items():
            b.setChecked(p == page)
        self._sync_pins()
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

    def _focus_moved(self, _old, _new):
        self._update_hints()

    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _escape(self):
        """Back, one step at a time: leave the details panel (focus returns
        to the game), then close it; leave the search; leave full screen;
        from other pages, go back to the library. In Theatre the quick menu
        comes first: its question, then the menu."""
        if self._theatre and self.quick.back():
            self.sounds.play('back')
            self._update_hints()
            return
        if self.palette.isVisible():
            self.palette.escape()
            return
        sheet = self._open_sheet()
        if sheet is not None:
            sheet.close_sheet()
            self.sounds.play('back')          # Theatre only (UiSounds checks)
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
        elif self.current() == self.LIBRARY and not self.shelf_editor.isHidden():
            self._shelf_done()                  # the shelf builder (w-51): done, saved
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
        if self._theatre and self.quick.isVisible():
            return self.quick
        sheet = self._open_sheet()
        if sheet is not None:
            return sheet
        if not self._theatre and self.desk_search.isVisible():
            return self.desk_search
        if self._theatre:
            if self.theatre.trailer.isVisible():
                return self.theatre.trailer
            if self.theatre.search.isVisible():
                return self.theatre.search
            return self.theatre.details if self.theatre.details.isVisible() else self.theatre
        return self

    def _open_sheet(self) -> 'ActionSheet | None':
        """The sheet that's up: in Theatre its settings first (w-63), then
        the options sheet."""
        if self._theatre and self.theatre_settings.isVisible():
            return self.theatre_settings
        sheet = self.theatre_sheet if self._theatre else self.sheet
        return sheet if sheet.isVisible() else None

    def default_focus(self) -> QWidget | None:
        """Where focus lands when nothing in the window has it."""
        if self._theatre and self.quick.isVisible():
            return self.quick.default_focus()
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
        make = getattr(view, 'tile_for', None)
        if make is not None:                    # the grid builds only rows near the view
            return (make(slug) if slug else None) or make()
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


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .gameinfo import is_installed  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .library import ListView  # noqa: E402
from .tiles import GameTile  # noqa: E402
