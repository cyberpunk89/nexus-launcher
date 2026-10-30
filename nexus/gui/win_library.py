"""The library side of the window: the grid/list/shelves, search and
filters, selection, collections, and loading the library from the CLI."""

import json
import random

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QMenu, QPushButton, QSlider,
    QStackedWidget, QVBoxLayout, QWidget)

from nexus import facts, net, paths as NX, smart

from .common import alive, COLLECTION_NAMES
from .theme import G, SP


# Chips for the stores in nexus/sources: hidden until the library has a
# game from one.
STORE_CHIPS = ('epic',)


class LibraryMixin:
    """Part of MainWindow (nexus/gui/window.py), which mixes it in; `self` is
    the window."""

    def _search_typed(self, text):
        if text and self.current() != self.LIBRARY:
            self.go(self.LIBRARY)
            self.search.setFocus()
        self._search_timer.start()

    def _wire_view(self, v):
        v.clicked.connect(self._tile_clicked)
        v.play.connect(lambda g: self.on_game_action('launch', g))
        v.menu.connect(self._show_menu)
        v.pending_clicked.connect(self._open_job)
        v.activated.connect(self._tile_activated)
        v.double.connect(self._tile_double)
        if isinstance(v, HomeView):                 # smart shelves: Library and Home
            v.show_filter.connect(self._show_filter)
            v.shelf_menu.connect(self._smart_menu)
            v.hidden_menu.connect(self._smart_hidden_menu)
            v.new_shelf.connect(self._new_shelf)

    def _see_all(self, chip, sort):
        self.go(self.LIBRARY)
        self.search.clear()
        self.filter.setCurrentIndex(0)
        i = self.sort.findData(sort)
        if i >= 0:
            self.sort.setCurrentIndex(i)
        if self._view == 'home':
            self.set_view('grid', save=False)
        own = 'owned' if chip == 'owned' else 'installed'
        self.own_seg.set_current(own, animate=False)
        self.prefs.owned_view = own
        self._set_chip(chip if chip in self.chips else 'all')

    def _build_library(self):
        self._facts_stamp = None              # (mtime, size) of facts.json last read
        self._facts_idx = facts.read_index()
        self._smart_sig = None                # what the Smart group was built from
        self._smart_temp = None               # a key shown only while in use (a genre)
        self._smart_want = None               # a restored key waiting for its entry
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        bl.setSpacing(SP['2'])
        bl.addWidget(label("Library", "PageTitle"))
        self.count_label = label("", "ShelfCount")
        bl.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        bl.addStretch()
        self.filter = QComboBox()
        for text, key in (("Any status", 'all'), ("Update pending", 'update'),
                          ("With launch options", 'options'), ("Not in Steam", 'no-steam'),
                          *((("Not in Heroic", 'no-heroic'),) if has('heroic') else ()),
                          ("Hidden games", 'hidden')):
            self.filter.addItem(text, key)
        self.filter.currentIndexChanged.connect(self.apply_filter)
        self._smart_from = self.filter.count()    # the Smart group starts here
        self.btn_clear_filter = icon_button(G['close'], "Clear filter", 34, "Ghost", round_=True)
        self.btn_clear_filter.clicked.connect(self._clear_filter)
        self.btn_clear_filter.setVisible(False)
        # A shelf you built, shown as the filter: edit its rules (w-51).
        self.btn_edit_shelf = icon_button(G['pencil'], "Edit this shelf", 34, "Ghost",
                                          round_=True)
        self.btn_edit_shelf.clicked.connect(
            lambda: self._edit_shelf(str(self.filter.currentData())[len(smart.USER):]))
        self.btn_edit_shelf.setVisible(False)
        self.sort = QComboBox()
        for text, key in (("Recently played", 'played'), ("Name", 'name'),
                          ("Most played", 'most'), ("Recently added", 'added'),
                          ("Size on disk", 'size')):
            self.sort.addItem(text, key)
        self.sort.currentIndexChanged.connect(self._sort_changed)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(140, 260)
        self.size_slider.setFixedWidth(90)
        self.size_slider.setToolTip("Tile size")
        self.size_slider.setValue(self.prefs.tile)
        # Applied a moment after it stops moving: drag, arrows and D-pad alike.
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.setInterval(250)
        self._tile_timer.timeout.connect(self._tile_size_changed)
        self.size_slider.valueChanged.connect(self._tile_timer.start)
        self.view_seg = SegmentedControl('pill', 34)
        for key, glyph, tip in (('home', G['shelves'], "Shelves — one row per source"),
                                ('grid', G['grid'], "Grid"),
                                ('list', G['list'], "List")):
            self.view_seg.add(key, glyph=glyph, tip=tip)
        self.view_seg.changed.connect(self.set_view)
        refresh = icon_button(G['refresh'], "Refresh (Ctrl+R)", 34, "Ghost", round_=True)
        refresh.clicked.connect(self.refresh_library)
        self.btn_refresh = refresh
        dice = icon_button(G['dice'], "Surprise me — pick from what's shown (Ctrl+D)", 34,
                           "Ghost", round_=True)
        dice.clicked.connect(lambda: self.surprise(self._shown_games))
        self.btn_surprise = dice
        # Right-anchored, with the view switch last before refresh: controls
        # that hide per view are on its left, so the switch itself never moves.
        for w in (self.btn_edit_shelf, self.btn_clear_filter, self.filter, self.sort,
                  self.size_slider,
                  self.view_seg, dice, refresh):
            bl.addWidget(w)
        lay.addWidget(bar)

        chips = WrapRow(SP['1_5'])              # own_seg drops under the chips if narrow
        chips.layout().setContentsMargins(SP['5'], SP['3'], SP['5'], 0)
        cl = chips.left
        cl.setSpacing(SP['1_5'])
        self.chips = {}
        # One chip per store. Heroic is Linux-only; a store source's chip
        # (Epic) shows once the library has a game from it (_store_chips).
        for key, text in (('all', "All"), ('nexus', "Repacks"), ('steam', "Steam"),
                          *((('heroic', "Heroic"),) if has('heroic') else ()),
                          ('epic', "Epic"), ('favorite', "★ Favourites")):
            c = QPushButton(text)
            c.setObjectName("Chip")
            c.setCheckable(True)
            c.clicked.connect(lambda _=False, k=key: self._set_chip(k))
            cl.addWidget(c)
            self.chips[key] = c
        self.chips['all'].setChecked(True)
        self._chip = 'all'
        self._store_chips()
        self._col_chips = QHBoxLayout()            # one per collection, rebuilt as they change
        self._col_chips.setContentsMargins(0, 0, 0, 0)
        self._col_chips.setSpacing(SP['1_5'])
        cl.addLayout(self._col_chips)
        self._rebuild_collection_chips()
        cl.addStretch()
        # Installed / not installed (owned on Steam, Epic, GOG) / everything.
        self.own_seg = SegmentedControl('pill', 30)
        for key, text in (('installed', "Installed"), ('owned', "Not installed"),
                          ('all', "Everything")):
            self.own_seg.add(key, text)
        self.own_seg.set_current(self.prefs.owned_view, animate=False)
        self.own_seg.changed.connect(self._set_owned_view)
        chips.set_right(self.own_seg)
        self.own_seg.setVisible(self.prefs.show_owned)
        lay.addWidget(chips)
        # The shelf builder (w-51): under the chips while open; the grid
        # below is its live preview.
        self.shelf_editor = ShelfEditor()
        self.shelf_editor.setVisible(False)
        self.shelf_editor.changed.connect(self._shelf_edited)
        self.shelf_editor.done.connect(self._shelf_done)
        self.shelf_editor.hide_asked.connect(lambda sid: self._hide_smart(smart.USER + sid))
        self.shelf_editor.remove_asked.connect(self._remove_shelf)
        ed = QHBoxLayout()
        ed.setContentsMargins(SP['5'], SP['3'], SP['5'], 0)
        ed.addWidget(self.shelf_editor, 10)       # up to its MAX_W, then the stretch
        ed.addStretch(1)
        lay.addLayout(ed)
        self._shelf_save = QTimer(self)        # a burst of edits saves library.json once
        self._shelf_save.setSingleShot(True)
        self._shelf_save.setInterval(600)
        self._shelf_save.timeout.connect(self.prefs.save)
        self._restore_library_state()
        self.filter.currentIndexChanged.connect(self._save_library_state)

        self.views = QStackedWidget()
        self.library = LibraryView()
        self.library.tile_width = self.prefs.tile
        self.home = HomeView('shelves')
        self.listview = ListView()
        self._views = {'grid': self.library, 'home': self.home, 'list': self.listview}
        for v in (self.home, self.library, self.listview):
            self._wire_view(v)
            self.views.addWidget(v)
        self.home.see_all.connect(self._see_all)
        lay.addWidget(self.views, 1)
        # A game page's genre or Co-op chip (DetailsPanel.filter_library,
        # when that panel has it) filters the Library the same way.
        sig = getattr(self.details, 'filter_library', None) if hasattr(self, 'details') else None
        if sig is not None:
            sig.connect(lambda key: self._show_filter(key, from_page=True))
        # The agent (or, on Windows, this window) fills the facts index in
        # the background: look for a new one now and then, cheaply (a stat).
        self._facts_timer = QTimer(self)
        self._facts_timer.setInterval(60000)
        self._facts_timer.timeout.connect(self._facts_tick)
        self._facts_timer.start()
        self.set_view(self.prefs.view, save=False)
        return page

    # ---- views / selection ----
    @property
    def view(self):
        return self._views[self._view]

    def _shown_view(self):
        """The view on screen: Shelves give way to the grid while a search
        or filter is on, so this isn't always `view`."""
        return self.views.currentWidget()

    def set_view(self, key, save=True):
        self._view = key if key in self._views else 'home'
        self.view_seg.set_current(self._view)
        self.views.setCurrentWidget(self.view)
        self.size_slider.setVisible(self._view == 'grid')
        self.sort.setVisible(self._view != 'home')
        if save:
            self.prefs.view = self._view
            self.prefs.save()
        if self._games or save:
            self.apply_filter()

    def _sort_changed(self):
        self.listview.reset_sort()
        self._save_library_state()
        self.apply_filter()

    def _tile_size_changed(self):
        if self.size_slider.value() == self.prefs.tile:
            return
        self.prefs.tile = self.size_slider.value()
        self.prefs.save()
        self.library.tile_width = self.prefs.tile
        self.apply_filter()

    def _set_show_owned(self, on: bool):
        self.prefs.show_owned = on
        self.prefs.save()
        self.own_seg.setVisible(on)
        self.reload_library()

    def _set_owned_view(self, key):
        if self.own_seg.current() != key:
            self.own_seg.set_current(key, animate=False)
        self.prefs.owned_view = key
        self.prefs.save()
        self.apply_filter()

    def _rebuild_collection_chips(self):
        for key in [k for k in self.chips if k.startswith('col:')]:
            c = self.chips.pop(key)
            self._col_chips.removeWidget(c)
            c.hide()
            c.deleteLater()
        for name in self.prefs.collections:
            c = CollectionChip(name)
            c.clicked.connect(lambda _=False, k=f'col:{name}': self._set_chip(k))
            c.manage.connect(self._manage_collection)
            c.setChecked(self._chip == f'col:{name}')
            self._col_chips.addWidget(c)
            self.chips[f'col:{name}'] = c

    def _store_chips(self):
        """Show a store source's chip only when the library has its games
        (or it's the chip in use, so a restored filter stays reachable)."""
        have = {g.get('source') for g in getattr(self, '_games', None) or ()}
        if 'steam' in self.chips:           # Windows without Steam: no chip, unless in use
            self.chips['steam'].setVisible(
                has('proton') or 'steam' in have or self._chip == 'steam'
                or NX.STEAM_ROOT.is_dir())
        for key in STORE_CHIPS:
            if key in self.chips:
                self.chips[key].setVisible(key in have or key == self._chip)

    def _set_chip(self, key):
        key = key if key in self.chips else 'all'
        self._chip = key
        for k, c in self.chips.items():
            c.setChecked(k == key)
        self._store_chips()
        self._save_library_state()
        self.apply_filter()

    def _restore_library_state(self):
        st = self.prefs.library_state
        status = st.get('status')
        if isinstance(status, str) and status.startswith(smart.KEY_PREFIXES):
            self._smart_want = status             # its entry comes with the library
        for box, key in ((self.sort, 'sort'), (self.filter, 'status')):
            i = box.findData(st.get(key))
            if i >= 0:
                box.blockSignals(True)
                box.setCurrentIndex(i)
                box.blockSignals(False)
        chip = st.get('chip')
        if chip in self.chips:
            self._chip = chip
            for k, c in self.chips.items():
                c.setChecked(k == chip)
            self._store_chips()

    def _save_library_state(self, *_):
        status = self.filter.currentData()
        if status == 'all' and self._smart_want:      # not restored yet: keep it
            status = self._smart_want
        st = {'sort': self.sort.currentData(), 'status': status,
              'chip': self._chip, 'details_w': self.prefs.library_state.get('details_w')}
        if st != self.prefs.library_state:
            self.prefs.library_state = st
            self.prefs.save()

    def _split_moved(self, *_):
        if self.details.isVisible():
            self._split_timer.start()

    def _save_details_width(self):
        if self.details.isVisible() and self.details.width() > 0:
            self.prefs.library_state = {**self.prefs.library_state,
                                        'details_w': self.details.width()}
            self.prefs.save()

    def _tile_clicked(self, game, mods):
        slug = game.get('slug')
        if not slug:
            return
        order = [g['slug'] for g in self._shown_games] if self._showing(self.LIBRARY) else []
        anchor = getattr(self, '_sel_anchor', None)
        if mods & Qt.KeyboardModifier.ShiftModifier and anchor in order and slug in order:
            a, b = sorted((order.index(anchor), order.index(slug)))
            rng = order[a:b + 1]                       # the games in between, as shown
            keep = self.selected if mods & Qt.KeyboardModifier.ControlModifier else []
            self.selected = [*[s for s in keep if s not in rng], *rng]
        elif mods & Qt.KeyboardModifier.ControlModifier:
            self.selected = [s for s in self.selected if s != slug] if slug in self.selected \
                else [*self.selected, slug]
            self._sel_anchor = slug
        else:
            self.selected = [slug]
            self._sel_anchor = slug
        self._sync_selection()

    def _sync_selection(self):
        by = {g['slug']: g for g in self._games}
        self.selected = [s for s in self.selected if s in by]
        self._shown_view().mark_selected(set(self.selected))
        self.homeview.mark_selected(set(self.selected))
        self._selection_changed([by[s] for s in self.selected])

    def clear_selection(self):
        self.selected = []
        self._sync_selection()

    def _selected_repack(self) -> dict | None:
        g = self.game(self.selected[0]) if len(self.selected) == 1 else None
        return g if g is not None and g.get('source', 'nexus') == 'nexus' \
            and self.current() in (self.HOME, self.LIBRARY) else None

    def _rename_selected(self):
        g = self._selected_repack()
        if g is not None:
            self.details.open_section('files')
            self.details.ed_name.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.details.ed_name.selectAll()

    def _uninstall_selected(self):
        # Line edits keep Delete for themselves (Qt gives them the key first).
        g = self._selected_repack()
        if g is not None:
            self.on_game_action('uninstall', g)

    def _notes_changed(self, slug: str, text: str):
        if text.strip():
            self.prefs.notes[slug] = text
        else:
            self.prefs.notes.pop(slug, None)
        self.prefs.save()
        g = self.game(slug)
        if g is not None:
            g['notes'] = text

    def _tile_activated(self, game):
        """Enter / A on a game: open its page and put focus on Play, the way a
        console launcher does; a second press plays. (Settings → Controls can
        make it play straight away.)"""
        if self.prefs.controls['pad_a'] == 'play' and can_quick_play(game):
            self.on_game_action('launch', game)
            return
        self._open_on_play(game)

    def _tile_double(self, game):
        """A double click: plays (Settings → Controls can make it open the
        game's page instead). Something that can't play right now — running,
        busy, not installed — opens the page."""
        if self.prefs.controls['double_click'] == 'play' and can_quick_play(game):
            self.on_game_action('launch', game)
            return
        self._open_on_play(game)

    def _reveal_game(self, slug):
        """Show one game in the Library, selected. The search and filters
        stay as they are when the game is already in what they show."""
        self.go(self.LIBRARY)
        view = self._shown_view()
        if slug not in getattr(view, 'tiles', {}) and slug not in getattr(view, 'games', {}):
            self.search.clear()
            self.filter.setCurrentIndex(0)
            g = self.game(slug)
            if g is not None and self.prefs.show_owned and not is_installed(g) \
                    and self.prefs.owned_view == 'installed':
                self._set_owned_view('all')
            self._set_chip('all')
        self.selected = [slug]
        self._sync_selection()
        self._shown_view().reveal(slug)

    # ---- library ----
    # ---- collections ----
    def _collections_changed(self):
        """After a collection was made, renamed, deleted or a game moved in
        or out: game dicts, chips, Home, Theatre and the details panel."""
        COLLECTION_NAMES[:] = list(self.prefs.collections)
        for g in self._games:
            g['collections'] = self.prefs.collections_of(g['slug'])
        self._rebuild_collection_chips()
        if not self._chip.startswith('col:') or self._chip in self.chips:
            self.apply_filter()
        else:
            self._set_chip('all')               # its collection is gone
        if self._theatre:
            self.theatre.set_games([g for g in self._games if not g.get('hidden')])
        self._sync_selection()

    def ask_collection_name(self, title: str, initial: str, on_done):
        """A name: the on-screen keyboard for a controller (and in Theatre),
        else a text field in the sheet."""
        if self._theatre:
            self.theatre.search.open_entry(title, initial, on_done)
        elif self.input.mode == 'pad':
            self.desk_search.open_entry(title, initial, on_done)
        else:
            self.sheet.prompt(title, initial, "Save", on_done)
        self._update_hints()

    def _create_collection(self, cname, slug=None):
        made = self.prefs.add_collection(cname)
        if made is None:
            return
        if slug and slug not in self.prefs.collections[made]:
            self.prefs.toggle_member(made, slug)
        self._collections_changed()
        g = self.game(slug) if slug else None
        self.toast(f"Added {g['display_name']} to “{made}”." if g else
                   f"Made the collection “{made}”.", 'success', None, 2500)

    def _manage_collection(self, cname):
        sheet = self.theatre_sheet if self._theatre else self.sheet
        sheet.choose(cname, [
            ("Rename…", G['pencil'], lambda: self.ask_collection_name(
                "Rename collection", cname, lambda new: self._rename_collection(cname, new))),
            ("Delete collection", G['trash'], lambda: self.confirm(
                f"Delete “{cname}”?", "Its games stay in your library — only the "
                "collection goes.", "Delete collection",
                lambda: self._delete_collection(cname)))],
            sub=f"{len(self.prefs.collections.get(cname, []))} games")
        self._update_hints()

    def _rename_collection(self, old, new):
        made = self.prefs.rename_collection(old, new)
        if made is None:
            self.toast(f"There's already a collection called “{new}”.", 'warn', None, 3500)
            return
        if self._chip == f'col:{old}':
            self._chip = f'col:{made}'
        self._collections_changed()
        self._save_library_state()

    def _delete_collection(self, cname):
        self.prefs.delete_collection(cname)
        self._collections_changed()
        self.toast(f"Deleted the collection “{cname}”.", 'info', None, 2500)

    # ---- surprise me ----
    def surprise(self, pool=None):
        """Pick a game (from `pool`, else everything installed) and open its
        page with Play focused; the toast offers another pick."""
        games = pool if pool is not None else self._games
        g = surprise_pick(games, random, tuple(self._surprise_recent))
        if g is None:
            self.toast("Nothing to pick from here — every game shown is running, hidden "
                       "or not installed." if games else "Install a game first.", 'info',
                       None, 3500)
            return
        self._surprise_recent = [*self._surprise_recent, g['slug']][-3:]
        if self._theatre:
            self.theatre.open_game(g)
        else:
            if not (self._showing(self.HOME) or self._showing(self.LIBRARY)):
                self.go(self.HOME)
            self._open_on_play(g)
        self.toast(f"How about {g.get('display_name') or g['slug']}?", 'info',
                   ("Pick again", lambda p=pool: self.surprise(p)), 6000)

    def refresh_library(self):
        """The Refresh button / Ctrl+R: rescan, and retry art that failed."""
        ART.forget_failures()
        self.reload_library()

    def reload_library(self):
        """Fetch the library from the CLI in the background. One scan at a
        time: asking again while one is out queues exactly one more, so a
        late answer can't overwrite a newer one."""
        if self._lib_inflight:
            self._lib_again = True
            return
        self._lib_inflight = True
        self.btn_refresh.setEnabled(False)
        cli_json(self._library_args(), self._on_library)

    def _library_args(self):
        return ['--library', '--json'] + (['--owned'] if self.prefs.show_owned else [])

    # The last scan, painted at start-up while the real one runs: the window
    # shows the library at once instead of "Loading…". Games from it can't
    # be played until the fresh scan confirms them (g['stale']).
    LIB_CACHE = NX.CACHE_DIR / 'library.json'

    def _paint_cached_library(self):
        try:
            blob = json.loads(self.LIB_CACHE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if not isinstance(blob, dict) or blob.get('args') != self._library_args() \
                or not isinstance((blob.get('data') or {}).get('games'), list):
            return
        self._on_library(blob['data'], '', cached=True)

    def _save_library_cache(self, data):
        net.save_json(self.LIB_CACHE, {'args': self._library_args(), 'data': data})

    def _on_library(self, data, err, cached=False):
        if cached:
            if self._loaded:                 # the real scan already answered
                return
        else:
            self._lib_inflight = False
            if self._lib_again:
                self._lib_again = False
                self._lib_retry.start()
            else:
                self.btn_refresh.setEnabled(True)
            if data is not None:
                self._save_library_cache(data)
        if data is None:
            # Keep showing what we had: an empty library would also read as
            # "every game stopped" to the running-state poll.
            self._load_error = err or 'unknown error'
            if self._loaded:
                self.toast(f"Couldn't refresh the library — {self._load_error}", 'error',
                           ("Retry", self.reload_library), 10000)
        else:
            self._games, self._load_error = data.get('games', []), ''
            for g in self._games:
                g['stale'] = cached
            self._disk = data.get('disk')
            self._disks = data.get('disks') or ([self._disk] if self._disk else [])
            self._loaded = True
            self.newspage.set_games(self._games)
            busy = self.jobs.busy_slugs()
            self._busy = frozenset(g['slug'] for g in self._games if g['slug'] in busy)
            self._mark_states()
            for g in self._games:
                g['install_requested'] = g['slug'] in self._install_req and not is_installed(g) \
                    and not g.get('installing')
                g['display_name'] = ' '.join((g.get('display_name') or g['slug']).split())
                ov = self.prefs.overrides.get(g['slug'])
                if ov:
                    g['store_name'] = g['display_name']
                    g['display_name'] = ov['name']
                g['favorite'] = g['slug'] in self.prefs.favorites
                g['notes'] = self.prefs.notes.get(g['slug'], '')
                g['hidden'] = g['slug'] in self.prefs.hidden
                g['collections'] = self.prefs.collections_of(g['slug'])
                # ProtonDB tiers already known (_load_protondb), here rather
                # than after the views are filled: Home compares the tiers to
                # decide whether to rebuild, and rebuilt on every refresh.
                tier = self._protondb.get(str(g.get('appid')))
                if tier:
                    g['protondb'] = tier
                # Repacks record when Nexus installed them; Steam games when
                # their manifest was written — one "added" date for both.
                g['added_at'] = g.get('added_at') or g.get('installed_at')
                if g['slug'] in self._repack_updates and g.get('source', 'nexus') == 'nexus':
                    g['repack_update'] = self._repack_updates[g['slug']]
                    g['update_pending'] = True
                if g.get('repack_online'):             # FitGirl posted a newer one
                    g['update_pending'] = True
            mark_duplicates(self._games, self.prefs.controls['dedupe'])
            self._store_chips()
            for g in [] if cached else self._games:
                if g['slug'] in self._watch_finish and is_installed(g):
                    self._install_req.pop(g['slug'], None)
                    self.toast(f"{g['display_name']} is installed — ready to play.", 'success',
                               ("Play", lambda g=g: self.on_game_action('launch', g)), 8000)
            if not cached:
                self._watch_finish = set()
            if not self._steam_resumed and not cached:
                self._steam_resumed = True
                self._resume_steam_pending()
        self._sync_system()
        self.apply_filter()
        self._sync_selection()
        if cached:
            return
        self._load_protondb()
        self._watch_installs()
        if data is not None and not self._feed_timer.isActive():
            self._feed_timer.start(8000)            # first check soon, then every 6 h
        if data is not None and not getattr(self, '_art_filled', False) \
                and self.config.get('api_key'):
            # Once a day, quietly fill missing hero/logo/banner art (the CLI
            # skips it when it already ran today); reload only if it found any.
            self._art_filled = True
            QTimer.singleShot(6000, lambda: alive(self) and cli_json(
                ['--fill-art', '--if-stale', '--json'], self._art_fill_done, 600000))
        if not self._deals_warmed and self.config.get('gamedb_enabled'):
            # One cached call (45 min): keeps the sidebar badge and the details
            # panel's deal line warm without opening the Deals page.
            self._deals_warmed = True
            QTimer.singleShot(2500, self.dealspage.refresh)
        if self._showing(self.STORAGE):
            self.storage.set_library([g for g in self._games if is_installed(g)], self._disks)

    def apply_filter(self):
        if self._loaded and not self._showing(self.LIBRARY) and not self._theatre:
            # Off screen, the Library's views wait: a game starting or a job
            # finishing only rebuilds what's visible (Home); go(LIBRARY)
            # catches up.
            self._filter_dirty = True
            pending = [j for j in self.jobs.jobs if j.kind == 'install' and j.active]
            self._pending_ids = tuple(id(j) for j in pending)
            empty = (f"Couldn't load the library — {self._load_error}"
                     if self._load_error and not self._games else None)
            self._refresh_home(empty, pending)
            return
        self._filter_dirty = False
        self._sync_facts()
        self._rebuild_smart_filter()
        q = self.search.text().strip().lower()
        status = self.filter.currentData() or 'all'
        self.btn_clear_filter.setVisible(status != 'all')
        self.btn_edit_shelf.setVisible(smart.is_user(status) and self.shelf_editor.isHidden())
        if status.startswith(smart.KEY_PREFIXES):
            hit = {g['slug'] for g in self._smart_games(
                status, [g for g in self._games if not g.get('hidden')])}
            smart_keep = lambda g: g['slug'] in hit
        else:
            smart_keep = None
        keep = smart_keep or {
            'all': lambda g: not g.get('hidden'),
            'hidden': lambda g: g.get('hidden'),
            'update': lambda g: g.get('update_pending') and not g.get('hidden'),
            'options': lambda g: g.get('launch_options') and not g.get('hidden'),
            'no-steam': lambda g: g.get('source', 'nexus') == 'nexus' and not g.get('steam')
            and not g.get('hidden'),
            'no-heroic': lambda g: g.get('source', 'nexus') == 'nexus' and not g.get('heroic')
            and not g.get('hidden'),
        }[status]
        own = self.prefs.owned_view if self.prefs.show_owned else 'installed'
        in_own = {'installed': is_installed, 'owned': lambda g: not is_installed(g),
                  'all': lambda g: True}[own]
        chip = self._chip
        in_chip = (lambda g: True) if chip == 'all' else \
            (lambda g: g.get('favorite')) if chip == 'favorite' else \
            (lambda g, n=chip[4:]: n in (g.get('collections') or ())) \
            if chip.startswith('col:') else \
            (lambda g: g.get('source', 'nexus') == chip)
        # Every word of the query, punctuation aside: "witcher 3" finds
        # "The Witcher® 3: Wild Hunt" (Theatre's search matches the same way).
        words = _search_norm(q).split()

        def found(g):
            name = _search_norm(g.get('display_name') or '') + ' ' + g.get('slug', '')
            return all(w in name for w in words)
        games = [g for g in self._games if keep(g) and in_chip(g) and in_own(g)
                 and (not words or found(g)) and not (g.get('dup_of') and status != 'hidden')]
        self._shown_games = games                  # what "Surprise me" picks from here
        # A search or status filter reads better as a grid than as shelves.
        if self._view == 'home' and (q or status != 'all' or own != 'installed'):
            self.views.setCurrentWidget(self.library)
            target = self.library
        else:
            self.views.setCurrentWidget(self.view)
            target = self.view
        self.sort.setVisible(target is not self._views['home'])
        key = self.sort.currentData()
        name = lambda g: (g.get('display_name') or g.get('slug', '')).lower()
        games.sort(key=name)
        if key == 'played':
            games.sort(key=lambda g: g.get('last_played') or '', reverse=True)
        elif key == 'most':
            games.sort(key=lambda g: g.get('playtime_minutes') or 0, reverse=True)
        elif key == 'added':
            games.sort(key=lambda g: g.get('added_at') or '', reverse=True)
        elif key == 'size':
            games.sort(key=lambda g: g.get('disk_bytes') or 0, reverse=True)
        if self._load_error and not self._games:
            empty = f"Couldn't load the library — {self._load_error}"
        elif not self._loaded:
            empty = "Loading your library…"
        elif self._games and not games:
            empty = "No games match the search or filter."
        else:
            empty = None
        pending = [j for j in self.jobs.jobs if j.kind == 'install' and j.active]
        self._pending_ids = tuple(id(j) for j in pending)
        hidden = status == 'hidden'
        n, total = len(games), len([g for g in self._games if bool(g.get('hidden')) == hidden
                                    and in_own(g)])
        self.count_label.setText(f"{n} of {total}" if n != total else
                                 f"{n} game{'s' if n != 1 else ''}")
        if isinstance(target, HomeView):            # shelves are rebuilt: keep the place
            with KeepPlace(target):
                target.set_games(games, empty, pending)
                if games:
                    target.set_smart(*self._smart_shelves(games))
        else:
            target.set_games(games, empty, pending)
        target.set_jobs(self.jobs)
        target.mark_selected(set(self.selected))
        if not self.shelf_editor.isHidden():
            self.shelf_editor.set_count(n)
        self._refresh_home(empty, pending)

    # ---- smart shelves (nexus/smart.py) ----
    def _sync_facts(self) -> bool:
        """Read the facts index again when its file changed; True if it did."""
        try:
            st = facts.index_path().stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._facts_stamp:
            return False
        self._facts_stamp = stamp
        self._facts_idx = facts.read_index()
        return True

    def _smart_facts(self) -> dict:
        return self._facts_idx.get('games') or {}

    def _facts_tick(self):
        if self._loaded and self._sync_facts():
            self.apply_filter()

    def _smart_note(self, games) -> str:
        """The divider's note: what the shelves come from, or, while the
        index is still filling, how far it got."""
        idx = self._facts_idx
        missing = (idx.get('missing') or {}).get('steam')
        if idx.get('built') and not missing:
            return "Sorted from your play time and store info"
        known = self._smart_facts()
        could = [g for g in games if _game_store_id(g)]
        have = sum(1 for g in could if g['slug'] in known)
        return "More appear as Nexus learns about your games" + (
            f" · {have} of {len(could)}" if could else '')

    def _smart_shelves(self, games) -> tuple[list, str, int, bool]:
        """What HomeView.set_smart needs for the Shelves view: your own
        shelves first (shown even when empty), then the automatic ones."""
        hidden = set(self.prefs.smart_hidden)
        facts = self._smart_facts()
        mine = [{'key': smart.USER + s['id'], 'title': smart.title_of(s),
                 'tip': smart.describe(s), 'games': smart.user_games(s, games, facts)}
                for s in self.prefs.user_shelves() if smart.USER + s['id'] not in hidden]
        auto = smart.shelves(games, facts, hidden=hidden, linux=has('proton'))
        return mine + auto, self._smart_note(games), len(self._hidden_keys()), True

    def _hidden_keys(self) -> list[str]:
        """What the divider's "N hidden" brings back: hidden shelves (yours
        only while not removed), then the ones you removed."""
        live = {smart.USER + s['id'] for s in self.prefs.user_shelves()}
        hid = [k for k in self.prefs.smart_hidden if not smart.is_user(k) or k in live]
        return hid + [smart.USER + s['id'] for s in self.prefs.smart_shelves if s['removed']]

    def _smart_games(self, key: str, games: list[dict]) -> list[dict]:
        """The games under any smart key, yours included."""
        if smart.is_user(key):
            shelf = smart.find_user(self.prefs.smart_shelves, key)
            return smart.user_games(shelf, games, self._smart_facts()) if shelf else []
        return smart.games_for(key, games, self._smart_facts(), linux=has('proton'))

    def _smart_label(self, key: str) -> str:
        shelf = smart.find_user(self.prefs.smart_shelves, key)
        return smart.title_of(shelf) if shelf else smart.label(key)

    def _smart_tip(self, key: str) -> str:
        shelf = smart.find_user(self.prefs.smart_shelves, key)
        return smart.describe(shelf) if shelf else smart.tip(key)

    def _rebuild_smart_filter(self, want: str | None = None):
        """The status filter's Smart group: a separator, a "Smart" header,
        then every rule that matches a game and the top genres (hidden
        shelves too). A key in use that isn't among them (a genre chosen on
        a game page) gets a temporary entry. Rebuilt only when that list
        changes, without touching the current choice (or choosing `want`)."""
        cur = want or self.filter.currentData()
        if not want and self._smart_want and self._loaded:
            cur, self._smart_want = self._smart_want, None
        pool = [g for g in self._games if not g.get('hidden') and not g.get('dup_of')]
        keys = [smart.USER + s['id'] for s in self.prefs.user_shelves()] + \
            smart.filter_keys(pool, self._smart_facts(), linux=has('proton'))
        if self._smart_temp and self._smart_temp != cur:
            self._smart_temp = None
        if cur and cur.startswith(('smart:', 'genre:')) and cur not in keys:
            self._smart_temp = cur
        if self._smart_temp:
            keys.append(self._smart_temp)
        sig = (tuple((k, self._smart_label(k)) for k in keys), cur)
        if sig == self._smart_sig:
            return
        self._smart_sig = sig
        box = self.filter
        box.blockSignals(True)
        while box.count() > self._smart_from:
            box.removeItem(box.count() - 1)
        if keys:
            box.insertSeparator(box.count())
            box.addItem("Smart")
            box.model().item(box.count() - 1).setEnabled(False)
            for key in keys:
                box.addItem(f"  {self._smart_label(key)}", key)
                box.setItemData(box.count() - 1, self._smart_tip(key),
                                Qt.ItemDataRole.ToolTipRole)
        i = box.findData(cur)
        box.setCurrentIndex(i if i >= 0 else 0)
        box.blockSignals(False)
        self.btn_clear_filter.setVisible(box.currentData() != 'all')

    def _show_filter(self, key: str, from_page: bool = False):
        """Show the Library filtered by a smart key: a smart shelf's "See
        all", or a game page's genre / Co-op chip (`from_page`, which also
        drops the source chip so the whole library is searched). The grid
        shows because a filter is on; ✕ or "Any status" goes back."""
        if not isinstance(key, str) or not key.startswith(smart.KEY_PREFIXES):
            return
        if self.current() != self.LIBRARY:
            self.go(self.LIBRARY)
        self.search.clear()
        if from_page and self._chip != 'all':
            self._chip = 'all'
            for k, c in self.chips.items():
                c.setChecked(k == 'all')
            self._store_chips()
        self._smart_want = None
        if self.filter.findData(key) < 0:
            self._rebuild_smart_filter(want=key)      # a temporary entry, chosen
        i = self.filter.findData(key)
        if i >= 0 and i != self.filter.currentIndex():
            self.filter.setCurrentIndex(i)            # apply_filter and save
        else:
            self._save_library_state()
            self.apply_filter()

    def _clear_filter(self):
        """✕ next to the filter: back to "Any status" (Shelves come back in
        the Shelves view)."""
        self._smart_want = None
        self.filter.setCurrentIndex(0)

    def _smart_menu(self, key: str, pos: QPoint):
        """A smart shelf's ⋮: See all, Save as collection, Hide this shelf;
        a shelf you built also has Edit and Remove. The desktop menu, or the
        ActionSheet when a pad is driving."""
        name = self._smart_label(key)
        items = [("See all", G['grid'], lambda: self._show_filter(key)),
                 ("Save as collection", G['collection'], lambda: self._save_smart(key)),
                 ("Hide this shelf", G['eye_off'], lambda: self._hide_smart(key))]
        if smart.is_user(key):
            sid = key[len(smart.USER):]
            items[1:1] = [("Edit shelf", G['pencil'], lambda: self._edit_shelf(sid))]
            items.append(("Remove shelf", G['trash'], lambda: self._remove_shelf(sid)))
        if self.input.mode == 'pad':
            self.sheet.choose(name, items, sub=self._smart_tip(key))
            self._update_hints()
            return
        m = QMenu(self)
        for text, glyph, fn in items:
            m.addAction(amp(text)).triggered.connect(lambda _=False, fn=fn: fn())
        m.exec(pos if not pos.isNull() else QCursor.pos())
        m.deleteLater()

    def _smart_hidden_menu(self, pos: QPoint):
        """The divider's "N hidden": bring a hidden or removed shelf back."""
        removed = {smart.USER + s['id'] for s in self.prefs.smart_shelves if s['removed']}
        items = [(f"Bring back “{self._smart_label(k)}”", G['reset'],
                  lambda k=k: self._restore_shelf(k[len(smart.USER):])) if k in removed else
                 (f"Show “{self._smart_label(k)}” again", G['eye'],
                  lambda k=k: self._unhide_smart(k)) for k in self._hidden_keys()]
        if not items:
            return
        if self.input.mode == 'pad':
            self.sheet.choose("Hidden shelves", items)
            self._update_hints()
            return
        m = QMenu(self)
        for text, _glyph, fn in items:
            m.addAction(amp(text)).triggered.connect(lambda _=False, fn=fn: fn())
        m.exec(pos if not pos.isNull() else QCursor.pos())
        m.deleteLater()

    def _smart_pool(self) -> list[dict]:
        """The games the Shelves view makes its smart shelves from."""
        return [g for g in self._games if not g.get('hidden') and is_installed(g)
                and not g.get('dup_of')]

    def _save_smart(self, key: str):
        """"Save as collection": an ordinary collection named after the
        shelf, holding today's games (added to it if it exists)."""
        games = self._smart_games(key, self._smart_pool())
        cname = self.prefs.add_collection(self._smart_label(key))
        if not cname or not games:
            return
        slugs = self.prefs.collections[cname]
        added = [g['slug'] for g in games if g['slug'] not in slugs]
        slugs.extend(added)
        self.prefs.save()
        self._collections_changed()
        self.toast(f"Saved {len(games)} game{'s' if len(games) != 1 else ''} to the "
                   f"collection “{cname}”.", 'success', None, 4000)

    def _hide_smart(self, key: str):
        if key not in self.prefs.smart_hidden:
            self.prefs.smart_hidden.append(key)
            self.prefs.save()
        if smart.is_user(key) and not self.shelf_editor.isHidden():
            self._shelf_done()
        self.apply_filter()
        self.dash_update('smart')
        self.toast(f"Hid “{self._smart_label(key)}”.", 'info',
                   ("Undo", lambda: self._unhide_smart(key)), 6000)

    def _unhide_smart(self, key: str):
        if key in self.prefs.smart_hidden:
            self.prefs.smart_hidden.remove(key)
            self.prefs.save()
        self.apply_filter()

    # ---- shelves you build (w-51, gui/shelfedit.py) ----
    def _shelf_pool(self) -> list[dict]:
        """What the builder offers fields and values from: the library."""
        return [g for g in self._games if not g.get('hidden') and not g.get('dup_of')]

    def _new_shelf(self):
        shelf = self.prefs.new_shelf(smart.fields_offered(self._shelf_pool(),
                                                          self._smart_facts()))
        if shelf is None:
            self.toast(f"That's {smart.MAX_USER_SHELVES} shelves, the most Nexus keeps. "
                       "Edit one instead.", 'warn', None, 5000)
            return
        self._edit_shelf(shelf['id'], new=True)

    def _edit_shelf(self, sid: str, new: bool = False):
        """Open the builder on a shelf: the Library shows it as the filter,
        so the grid below is the live preview."""
        shelf = self.prefs.find_shelf(sid)
        if shelf is None or shelf['removed']:
            return
        ed = self.shelf_editor
        ed.open(shelf, self._shelf_pool(), self._smart_facts(), new=new)
        ed.setVisible(True)
        self._smart_sig = None
        self._show_filter(smart.USER + sid, from_page=True)
        target = ed.name if new and self.input.mode != 'pad' else \
            (ed.rows[0].field if ed.rows else ed.btn_done)
        QTimer.singleShot(0, lambda: alive(target) and target.setFocus(
            Qt.FocusReason.OtherFocusReason))

    def _shelf_edited(self, shelf: dict):
        """Every edit: keep it (library.json a moment later), re-filter."""
        if self.prefs.update_shelf(shelf, save=False) is None:
            return
        self._shelf_save.start()
        self.apply_filter()

    def _shelf_done(self):
        """Close the builder and go back to the shelves."""
        if self._shelf_save.isActive():
            self._shelf_save.stop()
            self.prefs.save()
        key = smart.USER + (self.shelf_editor.shelf or {}).get('id', '')
        self.shelf_editor.setVisible(False)
        if smart.is_user(self.filter.currentData()):
            self._clear_filter()
        else:
            self.apply_filter()
        # Its row in the Shelves view, scrolled into sight (the focus goes there too).
        row = next((x for x in self.home._smart if isinstance(x, ShelfRow)
                    and x.title == self._smart_label(key)), None)
        if row is not None and self.views.currentWidget() is self.home:
            QTimer.singleShot(0, lambda: alive(row) and focus_and_reveal(row.btn_menu))
        self.dash_update('smart')

    def _remove_shelf(self, sid: str):
        """Remove: off the Library, the filter menu and Home; kept in
        library.json, so Undo (or "N hidden") brings it back."""
        shelf = self.prefs.find_shelf(sid)
        if shelf is None:
            return
        if not self.shelf_editor.isHidden() and \
                (self.shelf_editor.shelf or {}).get('id') == sid:
            self._shelf_done()
        self.prefs.set_shelf_removed(sid, True)
        if self.filter.currentData() == smart.USER + sid:
            self._clear_filter()
        self.apply_filter()
        self.dash_update('smart')
        self.toast(f"Removed “{smart.title_of(shelf)}”. Your games stay.", 'info',
                   ("Undo", lambda: self._restore_shelf(sid)), 6000)

    def _restore_shelf(self, sid: str):
        self.prefs.set_shelf_removed(sid, False)
        self.apply_filter()
        self.dash_update('smart')

    def _refresh_home(self, empty=None, pending=()):
        """Home shows the whole library, so it only rebuilds when the library
        (or a playing/favourite/job state) changes — not on every search key."""
        everything = [g for g in self._games if not g.get('hidden')]
        visible = [g for g in everything if is_installed(g)]
        self._sync_facts()
        struct = (self._loaded, self._load_error, tuple(id(j) for j in pending),
                  tuple(COLLECTION_NAMES), self._facts_stamp, repr(self.prefs.smart_shelves),
                  tuple((g['slug'], g.get('favorite'), tuple(g.get('collections') or ()),
                         g.get('last_played'), is_installed(g),
                         bool(g.get('installing')), g.get('install_requested'),
                         g.get('update_pending'), g.get('protondb')) for g in everything))
        live = tuple((g['slug'], g.get('playing'), g.get('launching')) for g in everything)
        sig = (struct, live)
        if sig == self._home_sig:
            return
        if self._home_sig is not None and self._home_sig[0] == struct:
            # Only "playing" / "starting" moved (a game launched or quit): the
            # tiles show it on their next paint and the hero re-picks; nothing
            # is rebuilt (Theatre included — its rows don't depend on it).
            self._home_sig = sig
            views = (self.homeview, self.theatre) if self._theatre else (self.homeview,)
            for view in views:
                for t in view.findChildren(GameTile):
                    t._sync_buttons()
                    t.update()
                view.hero.set_games(featured_games(visible))
            return
        self._home_sig = sig
        if self._theatre:
            with KeepPlace(self.theatre):
                self.theatre.set_games(everything)
        with KeepPlace(self.homeview):
            self.homeview.set_games(visible, empty if not visible else None, pending)
            self.dashboard.refresh()
        self.homeview.set_jobs(self.jobs)
        self.homeview.mark_selected(set(self.selected))

    def dash_update(self, *kinds):
        """Coalesce dashboard refreshes (job output can arrive many times a
        second)."""
        self._dash_pending.update(kinds)
        self._dash_timer.start()

    def _flush_dashboard(self):
        kinds, self._dash_pending = self._dash_pending, set()
        try:
            self.dashboard.refresh(kinds)
        except RuntimeError:
            pass

    def _selection_changed(self, games):
        if not games or self.current() not in (self.HOME, self.LIBRARY):
            self.details.setVisible(False)
            return
        self.details.show_games(games)
        if not self.details.isVisible():
            self.details.setVisible(True)
            total = self.split.width()
            want = self.prefs.library_state.get('details_w') or 380
            w = max(320, min(int(want), total - 300))
            self.split.setSizes([max(total - w, 300), w])

    def _show_menu(self, game, pos):
        """The game menu: the desktop QMenu for mouse and keyboard; for a
        controller (and anything in Theatre) the ActionSheet instead."""
        if self._theatre:
            self.theatre_sheet.open(game, game_actions(game, theatre=True), theatre=True)
        elif self.input.mode == 'pad':
            self.sheet.open(game, game_actions(game))
        else:
            m = build_game_menu(self, game, self.on_game_action)
            m.exec(pos if not pos.isNull() else QCursor.pos())
            m.deleteLater()                      # its closures hold the game dict
            return
        self._update_hints()


# Siblings, imported last: they import this module too, and every use
# below is inside a method, so the cycle resolves at call time.
from .actions import build_game_menu, game_actions  # noqa: E402
from .art import ART  # noqa: E402
from .dashboard import _game_store_id, mark_duplicates  # noqa: E402
from ..features import has  # noqa: E402
from .gameinfo import can_quick_play, is_installed  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .library import (featured_games, HomeView, KeepPlace, LibraryView, ListView,
    ShelfRow, surprise_pick)  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .shelfedit import ShelfEditor  # noqa: E402
from .theatre import _search_norm  # noqa: E402
from .tiles import GameTile  # noqa: E402
from .widgets import amp, icon_button, label, SegmentedControl, WrapRow  # noqa: E402
from .topbar import CollectionChip  # noqa: E402
