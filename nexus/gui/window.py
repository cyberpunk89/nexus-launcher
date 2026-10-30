"""The main window: construction, the top bar's pages and tabs, appearance,
settings and quitting. Everything else it does lives in its mixins
(win_library, win_nav, win_controller, win_play, win_downloads, win_actions)."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QProcess, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (QApplication, QMainWindow, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget)

from nexus import __version__, paths as NX, platform
from nexus.features import has

from .. import updates
from .common import alive, CONFIG_PATH, HOME
from .fmt import fmt_size
from .theme import apply_appearance, build_stylesheet, FS, G, P, scaled, SP
from .topbar import _TabHandle, TopBar
from .win_library import LibraryMixin
from .win_nav import NavigationMixin
from .win_controller import ControllerMixin
from .win_play import PlayMixin
from .win_downloads import DownloadsMixin
from .win_actions import ActionsMixin


class MainWindow(LibraryMixin, NavigationMixin, ControllerMixin, PlayMixin, DownloadsMixin,
                 ActionsMixin, QMainWindow):
    """The one window. Its parts live in mixins, one module each: win_library,
    win_nav, win_controller, win_play, win_downloads and win_actions; what
    stays here is construction, the top bar, appearance, settings and
    quitting."""
    (HOME, LIBRARY, SYSTEM, SETTINGS, INSTALL, PROTON, NEWS, DEALS, ARTWORK) = (
        'home', 'library', 'system', 'settings', 'install', 'proton', 'news', 'deals',
        'artwork')

    DOWNLOADS = 'downloads'
    SETUP = 'setup'

    TABS = (HOME, LIBRARY, DEALS, NEWS)          # content tabs; the rest are icons
    PINS_WIDE_AT = 1280            # top-bar pins show their names from this width up

    # Sections reached by their old page keys: go(ACTIVITY) opens System on
    # Activity, go(UPCOMING) opens Deals on Upcoming.
    ACTIVITY, STORAGE, HEALTH, UPCOMING = 'activity', 'storage', 'health', 'upcoming'

    PAGE_ALIASES = {ACTIVITY: (SYSTEM, 'activity'), STORAGE: (SYSTEM, 'storage'),
                    HEALTH: (SYSTEM, 'health'), UPCOMING: (DEALS, 'upcoming')}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus")
        self.setMinimumSize(720, 480)
        self.resize(1180, 760)
        self.config = load_config()
        self.prefs = LibraryPrefs()
        self._disks = []
        self._loaded = False
        self._playing: set[str] = set()
        self._launching: dict[str, float] = {}       # slug -> monotonic start
        self._trying: dict[str, str] = {}            # slug -> Proton tried for one run
        self._busy: frozenset[str] = frozenset()     # slugs a job is changing
        self._lib_inflight = self._lib_again = self._steam_resumed = False
        self._handoff_query = self._filter_dirty = False
        self._page_focus = QTimer(self)
        self._page_focus.setSingleShot(True)
        self._page_focus.setInterval(0)
        self._page_focus.timeout.connect(self._focus_new_page)
        self._back: list[str] = []                   # page history (go_back / go_forward)
        self._forward: list[str] = []
        self._lib_retry = QTimer(self)               # the queued follow-up scan
        self._lib_retry.setSingleShot(True)
        self._lib_retry.setInterval(0)
        self._lib_retry.timeout.connect(self.reload_library)
        self._handoff: dict | None = None            # the launch whose window we focus
        self._return_to: dict[str, int] = {}         # slug -> Nexus window to come back to
        self._view = 'home'
        self.jobs = JobManager()
        self.jobs.finished.connect(self._job_done)
        self.jobs.changed.connect(self._jobs_changed)
        self._quitting = False
        self.tray = None                             # Settings → Background (tray.py)
        self._tray_quit = False
        self._tray_told = False
        self._shown_games: list = []
        self._surprise_recent: list[str] = []
        self._mirror_collections()
        self._quit_deadline = 0.0
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(100)
        self._quit_timer.timeout.connect(self._quit_poll)
        self._games: list = []
        self._disk = None
        self._load_error = ''
        self._pending_ids: tuple = ()
        self._health_stale = True
        self._health_problems = (0, 0)
        self.input = install_input_mode(self)
        self.input.changed.connect(lambda _m: self._sync_hints())
        self.input.activity.connect(self._on_activity)
        self._steam_seen = (0.0, False)
        self._install_req: dict[str, float] = {}     # slug -> when Install was pressed
        self._install_query = False
        self._running_query = False
        self._watch_finish: set[str] = set()
        self._protondb: dict[str, str] = {}             # asked once per session
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(5000)
        self._install_timer.timeout.connect(self._install_tick)

        self._build_shell()
        self._wire_details()
        self._build_pages()
        self._wire_input()
        self._start()

    def _build_shell(self):
        """The window's frame: the two shells (desk and Theatre) over one
        background, the top bar, Steam banner, page stack with the docked
        details panel, the activity strip, overlays and the hint bar."""
        central = GradientBackground()
        self.background = central
        # In the window from the start: set last, moving the finished tree of
        # ~2000 widgets in re-styled every one of them (~0.1 s at start-up).
        self.setCentralWidget(central)
        outer_root = QVBoxLayout(central)
        outer_root.setContentsMargins(0, 0, 0, 0)
        outer_root.setSpacing(0)
        # Two shells over one background: the desktop app, and Theatre mode.
        self.shell = QStackedWidget()
        outer_root.addWidget(self.shell, 1)
        self.desk = QWidget()
        self.shell.addWidget(self.desk)
        root = QVBoxLayout(self.desk)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())
        self.gate = SteamGate()
        gate_wrap = QWidget()
        gw = QVBoxLayout(gate_wrap)
        gw.setContentsMargins(SP['5'], SP['3'], SP['5'], 0)
        gw.addWidget(self.gate)
        root.addWidget(gate_wrap)
        # Pages on the left, the game's details docked on the right — shared
        # by Home and Library, hidden on the other pages.
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.split.addWidget(self.stack)
        self.details = DetailsPanel()
        self.split.addWidget(self.details)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setCollapsible(0, False)
        self.details.setVisible(False)
        root.addWidget(self.split, 1)
        # The details panel's width, as last dragged (saved once it settles).
        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.setInterval(600)
        self._split_timer.timeout.connect(self._save_details_width)
        self.split.splitterMoved.connect(self._split_moved)
        self.strip = ActivityStrip(self.jobs)
        root.addWidget(self.strip)
        self.theatre = TheatreView()
        self.attract = AttractOverlay(self.theatre)
        self.attract.open_game.connect(self.theatre.open_game)
        self._idle_timer = QTimer(self)                 # Theatre's screensaver
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._maybe_attract)
        self._apply_controls()
        self.setAcceptDrops(True)
        self.theatre.play.connect(lambda g: self.on_game_action('launch', g))
        self.theatre.surprise.connect(self.surprise)
        self.theatre.action.connect(self._theatre_action)
        self.theatre.menu.connect(self._show_menu)
        self.theatre.hero.tint.connect(self.background.set_tint)
        self.shell.addWidget(self.theatre)
        # The controller's game menu, one per shell (Theatre's is TV-sized).
        self.trailer_view = TrailerOverlay(self.desk)
        self.sheet = ActionSheet(self.desk)
        self.sheet.chosen.connect(self.on_game_action)
        self.theatre_sheet = ActionSheet(self.theatre, THEATRE_SCALE)
        self.theatre_sheet.chosen.connect(self._theatre_action)
        for sh in (self.sheet, self.theatre_sheet):
            sh.closed.connect(self._update_hints)
        # Y on the desktop without Steam's on-screen keyboard: Theatre's
        # D-pad keyboard, over the desk.
        self.palette = CommandPalette(
            self.desk, lambda: game_entries(self._games) + page_entries(self),
            lambda g: action_entries(self, g))
        self.palette.recent = list(self.prefs.palette_recent)
        self.palette.picked.connect(self._palette_picked)
        self.desk_search = TheatreSearch(self.desk)
        self.desk_search.picked.connect(self._desk_search_picked)
        self.desk_search.closed.connect(self._desk_search_closed)
        self._theatre = False
        self._was_state = Qt.WindowState.WindowNoState
        self.cursor_hider = CursorHider(self)
        self.hints = HintBar()
        outer_root.addWidget(self.hints)
        self._battery_timer = QTimer(self)          # first read soon, then every minute
        self._battery_timer.timeout.connect(self._poll_battery)
        self._battery_timer.start(0)

    def _wire_details(self):
        """The game page's requests, plus the activity strip's."""
        self.selected: list[str] = []
        self.details.request.connect(self._on_request)
        self.details.play.connect(lambda g: self.on_game_action('launch', g))
        self.details.menu.connect(self._show_menu)
        self.details.closed.connect(self.clear_selection)
        self.details.toast.connect(self.toast)
        self.details.reveal.connect(self._reveal_game)
        self.details.notes_changed.connect(self._notes_changed)
        self.details.trailer.connect(lambda t: self.trailer_view.play(t, t.get('game', '')))
        self.strip.open_activity.connect(self._open_job)

    def _build_pages(self):
        """Every page, wired to the window, in the order they're added."""
        self.homeview = HomeView('home')
        self._wire_view(self.homeview)
        self.homeview.see_all.connect(self._see_all)
        self.homeview.tint.connect(self.background.set_tint)
        self.dashboard = Dashboard(self)
        self.homeview.extra.addWidget(self.dashboard)
        self._dash_pending: set[str] = set()
        self._dash_timer = QTimer(self)
        self._dash_timer.setSingleShot(True)
        self._dash_timer.setInterval(250)
        self._dash_timer.timeout.connect(self._flush_dashboard)
        self._home_sig = None
        self._add_page(self.HOME, self.homeview)
        self._add_page(self.LIBRARY, self._build_library())
        self.activity = ActivityPage(self.jobs)
        self.activity.exe_chosen.connect(lambda slug, path: self.run(
            ['--set-exe', slug, path], f"Starting {Path(path).name} from now on", slug))
        self.storage = StoragePage()
        self.storage.request.connect(self._on_request)
        self.storage.select_game.connect(self._reveal_game)
        self.health = HealthPage()
        self.health.request.connect(self._on_request)
        self.health.counts.connect(self._health_counts)
        self.health.toast.connect(self.toast)
        self.playhistory = PlayHistoryPage()
        self.playhistory.reveal.connect(self._reveal_game)
        self.system = SystemPage({'activity': self.activity, 'play': self.playhistory,
                                  'storage': self.storage, 'health': self.health})
        self.system.section_changed.connect(self._on_system_section)
        self._add_page(self.SYSTEM, self.system)
        self.settings = SettingsPage()
        self.settings.saved.connect(self._settings_saved)
        self.settings.show_owned.setChecked(self.prefs.show_owned)
        self.settings.show_owned.toggled.connect(self._set_show_owned)
        self.settings.set_controls(self.prefs.controls)
        self.settings.controls_changed.connect(self._apply_controls)
        self.settings.request.connect(self._on_request)
        self.settings.background_changed.connect(self._background_changed)
        self.settings.import_wanted.connect(self._import_settings)
        self.settings.appearance.load(self.prefs.appearance)
        # Re-styling ~2000 widgets takes about a second: coalesce quick clicks
        # through the swatches into one apply.
        self._ap_pending = None
        self._ap_timer = QTimer(self)
        self._ap_timer.setSingleShot(True)
        self._ap_timer.setInterval(160)
        self._ap_timer.timeout.connect(self._flush_appearance)
        self.settings.appearance.changed.connect(self._queue_appearance)
        self._add_page(self.SETTINGS, self.settings)
        self.install = InstallPage()
        self.install.submit.connect(self._start_install)
        self.install.torrent.connect(self._add_torrent)
        self._torrents_started: set[str] = set()
        self._torrent_polling = self._torrent_detecting = False
        self._torrent_timer = QTimer(self)               # the torrent client
        self._torrent_timer.timeout.connect(self._poll_torrents)
        self._torrent_timer.start(5000)                  # a first look soon after start
        self.install.back.connect(lambda: self.go_back() or self.go(self.HOME))
        self.install.reindex.connect(lambda: self.run(['--reindex'],
                                                      "Looking for installed games"))
        self._add_page(self.INSTALL, self.install)
        self.protonpage = ProtonPage()
        self.protonpage.request.connect(self._on_request)
        self.protonpage.toast.connect(self.toast)
        self.protonpage.update_available.connect(
            lambda on: (self.nav[self.PROTON].set_badge('1' if on else '', 'accent'),
                        self.dash_update('system'), self._proton_update_toast(on)))
        self._add_page(self.PROTON, self.protonpage)
        self.newspage = NewsPage()
        self.newspage.toast.connect(self.toast)
        self.newspage.show_game.connect(self._reveal_game)
        self.newspage.loaded.connect(lambda: self.dash_update('news'))
        self.theatre.news_items = lambda: (getattr(self.newspage, 'data', None) or {}).get(
            'items', [])
        self.newspage.loaded.connect(self.theatre.news_changed)
        self._add_page(self.NEWS, self.newspage)
        self.dealspage = DealsPage(self.prefs)
        self.dealspage.toast.connect(self.toast)
        self.dealspage.counts.connect(self._deals_count)
        self.dealspage.loaded.connect(lambda: self.dash_update('deals', 'upcoming'))
        self.dealspage.open_settings.connect(lambda: self._open_settings('gamedb'))
        self.dealspage.repack.connect(self._deal_repack)
        self._add_page(self.DEALS, self.dealspage)
        self.downloads = DownloadsPage()
        self.downloads.toast.connect(self.toast)
        self.downloads.control.connect(self._torrent_control)
        self.downloads.add.connect(self._add_torrent)
        self.downloads.row_action.connect(self._download_action)
        self.downloads.open_settings.connect(lambda: self._open_settings('torrent'))
        self.downloads.use_client.connect(self._use_torrent_client)
        self._add_page(self.DOWNLOADS, self.downloads)
        self.artpage = ArtPickerPage()
        self.artpage.request.connect(self._on_request)
        self.artpage.back.connect(self._art_back)
        self.artpage.open_settings.connect(lambda: self._open_settings('artwork'))
        self._add_page(self.ARTWORK, self.artpage)
        self.setup = SetupPage()
        self.setup.request.connect(self._on_request)
        self.setup.preview.connect(lambda look: self.set_appearance(
            {**self.prefs.appearance, **look}, save=False))
        self.setup.finished.connect(self._setup_finished)
        self._add_page(self.SETUP, self.setup)
        self.settings.setup_wanted.connect(self.open_setup)
        self.settings.whats_new_wanted.connect(self.show_whats_new)
        self._art_from = self.HOME
        self._deals_warmed = False

    def _wire_input(self):
        """Which tabs show, toasts, shortcuts, and keyboard/controller input."""
        self.nav[self.NEWS].setVisible(self.config.get('news', True))
        self.nav[self.DEALS].setVisible(self.config.get('gamedb_enabled', False))
        self._apply_repacks()
        self.toasts = ToastHost(self.background)
        self._shortcuts()
        self.input.listen()        # before SpatialNav, which so still sees keys first
        # Arrow keys (and the controller) move focus spatially everywhere.
        self.spatial = SpatialNav(self)
        QApplication.instance().installEventFilter(self.spatial)
        self.ctrl = ControllerInput(self)
        self.ctrl.connected.connect(self._pad_connected)
        self.ctrl.disconnected.connect(self._pad_disconnected)
        self.ctrl.action.connect(self._pad_action)
        self.ctrl.used.connect(lambda: self.input.set('pad'))
        self.ctrl.used.connect(self._pad_used)
        self.focus_ring = FocusRing(self)
        self.ctrl.used.connect(self.focus_ring.nudge)
        # A bound method, not a lambda: the app outlives this window, and PyQt
        # only drops a dead receiver's connection for bound methods.
        QApplication.instance().focusChanged.connect(self._focus_moved)

    def _start(self):
        """First page, first library load, and the background timers. Runs last:
        everything the library handler touches exists by now."""
        used_before = self.prefs.PATH.exists()
        if self._first_run:
            self.open_setup()
        else:
            self.go(self.HOME)
        self.reload_library()
        self._note_version(used_before)
        QTimer.singleShot(0, lambda: alive(self) and (t := self.default_focus()) and t.setFocus())
        QTimer.singleShot(1500, self.health.refresh)    # sidebar badge, in the background
        # Repack downloads: "ready to install" toasts and repack updates.
        self._repack_updates: dict[str, str] = {}          # slug -> new repack folder
        self._session_start: dict[str, datetime] = {}      # Steam/Heroic play sessions
        self._feed_timer = QTimer(self)                     # FitGirl feed: newer repacks
        self._feed_timer.timeout.connect(self._check_repack_feed)
        self._feed_checks = 0
        self.repacks = RepackWatcher(self.prefs, self)
        self.repacks.ready.connect(self._repack_ready)
        self.repacks.scanned.connect(self._repack_scanned)
        self.repacks.enabled = self.repacks_on()
        self.repacks.start()
        QTimer.singleShot(4000, self.protonpage.refresh)  # GE update badge (6 h cache)
        QTimer.singleShot(6000, self._check_update)        # new Nexus release (daily, [updates] check)
        # "Playing now": a cheap /proc scan, only while the window is visible.
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(15000)
        self._run_timer.timeout.connect(self._poll_running)
        self._run_timer.start()
        QTimer.singleShot(800, self._poll_running)
        # While a game is starting: poll every 2 s until it's running, and
        # watch for its window to hand focus over.
        self._fast_timer = QTimer(self)
        self._fast_timer.setInterval(2000)
        self._fast_timer.timeout.connect(self._poll_running)
        self._handoff_timer = QTimer(self)
        self._handoff_timer.setInterval(800)
        self._handoff_timer.timeout.connect(self._handoff_tick)
        self._paint_cached_library()

    # ---- layout ----
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # niri and friends often give the window half a screen: fold the tab
        # labels to icons so search and the content keep their room.
        compact = self.width() < 1000
        if compact != getattr(self, '_compact', None):
            self._compact = compact
            self.topbar.set_compact(compact)
        # Pinned collections show their names only while there's room.
        self.topbar.set_pins_wide(self.width() >= self.PINS_WIDE_AT)

    def _add_page(self, key, widget):
        self.pages[key] = widget
        self.stack.addWidget(widget)

    def current(self) -> str | None:
        w = self.stack.currentWidget()
        return next((k for k, v in self.pages.items() if v is w), None)

    def _build_topbar(self):
        bar = TopBar()
        self.topbar = bar
        self.tabs = bar.tabs
        self.nav = {}
        for page, glyph, text in ((self.HOME, G['home'], "Home"),
                                  (self.LIBRARY, G['library'], "Library"),
                                  (self.DEALS, G['deals'], "Deals"),
                                  (self.NEWS, G['news'], "News")):
            self.tabs.add(page, text, glyph)
            self.nav[page] = _TabHandle(self.tabs, page)
        self.tabs.changed.connect(self.go)
        self.icons = {}
        for page, glyph, tip in ((self.DOWNLOADS, G['download'], "Downloads"),
                                 (self.SYSTEM, G['system'], "System (Ctrl+5)"),
                                 (self.PROTON, G['proton'], "Proton"),
                                 (self.SETTINGS, G['settings'], "Settings (Ctrl+,)")):
            b = bar.add_icon(glyph, tip)
            b.clicked.connect(lambda _=False, p=page: self.go(p))
            self.icons[page] = b
            self.nav[page] = b
        bar.install.clicked.connect(self.open_install)
        bar.set_pins(self.prefs.pinned_collections())
        bar.pin_clicked.connect(self._open_pinned)
        self.btn_install = bar.install
        self.search = bar.search
        # Debounced: rebuilding the grid per keystroke is wasted work. Typing
        # anywhere searches the Library.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(140)
        self._search_timer.timeout.connect(self.apply_filter)
        self.search.textChanged.connect(self._search_typed)
        return bar

    def _proton_update_toast(self, on: bool):
        """Once per new GE-Proton release: a toast that installs it in one
        click (the Proton page keeps its banner either way)."""
        ge = (self.protonpage.info or {}).get('families', {}).get('ge', {})
        tag = ge.get('latest')
        if not on or not tag or tag == getattr(self, '_proton_toasted', None) \
                or tag in (self.protonpage.info or {}).get('protongen_dismissed', []):
            return
        self._proton_toasted = tag
        self.toast(f"{tag} is out.", 'info', [
            ("Install", lambda: self.run(['--proton-install', tag], f"Installing {tag}", None,
                                         'install-proton')),
            ("Details", lambda: self.go(self.PROTON))], 12000)

    def _check_update(self):
        in_thread(updates.available, self._update_checked)

    def _update_checked(self, rel, _err):
        """A newer Nexus release (updates.available): a toast linking its page."""
        if not rel:
            return
        name = "Installer" if rel['url'].endswith('.exe') else "Release page"   # Windows: setup
        self.toast(f"Version {rel['version']} is available.", 'info', [
            (name, lambda: QDesktopServices.openUrl(QUrl(rel['url'])))], 12000)

    def _on_activity(self):
        if getattr(self, '_theatre', False) and self.prefs.controls['attract']:
            self._idle_timer.start(self.prefs.controls['attract'] * 60_000)

    def _steam_up(self) -> bool:
        """steam_running(), remembered for a few seconds (hints ask often)."""
        t, up = self._steam_seen
        if time.monotonic() - t > 10:
            up = steam_running()
            self._steam_seen = (time.monotonic(), up)
        return up

    def _load_protondb(self):
        """ProtonDB tiers for Steam games (cached a day by the CLI), painted on
        tiles when they arrive — never blocks the library."""
        known = self._protondb               # appid -> tier ('' = ProtonDB has none)
        for g in self._games:
            if known.get(str(g.get('appid'))):
                g['protondb'] = known[str(g.get('appid'))]
        ids = [str(g['appid']) for g in self._games if g.get('source') == 'steam'
               and is_installed(g) and str(g['appid']) not in known]
        if not ids:
            return

        def done(data, _err):
            if data is None:
                return
            for i in ids:
                known[i] = (data.get(i) or {}).get('tier') or ''
            for g in self._games:
                if known.get(str(g.get('appid'))):
                    g['protondb'] = known[str(g.get('appid'))]
            try:
                view = self._shown_view()
                for tiles in view.tiles.values() if hasattr(view, 'tiles') else ():
                    for t in tiles:
                        t.update()
            except RuntimeError:
                pass
        cli_json(['--protondb', *ids, '--json'], done, 60000)

    def _sync_system(self):
        """The System icon: a ring for how full the install disk is, one
        badge (running jobs, else health problems), section badges, and a
        tooltip that sums it up. Also refreshes the Home System widget."""
        d, icon = self._disk, self.icons[self.SYSTEM]
        n = len(self.jobs.active())
        problems, fails = self._health_problems
        parts = []
        if n:
            parts.append(f"{n} running")
        if not d or not d.get('total'):
            icon.usage = None
        else:
            icon.usage = d['used'] / d['total']
            parts.append(f"{fmt_size(d['free'])} free of {fmt_size(d['total'])} on {d['path']}")
        if problems:
            parts.append(f"{problems} health issue{'s' if problems != 1 else ''}")
        icon.setToolTip("System (Ctrl+5)" + (" — " + " · ".join(parts) if parts else ""))
        icon.set_badge(*system_badge(n, problems, fails))
        self.system.set_badge('activity', str(n) if n else '', 'accent')
        self.system.set_badge('health', str(problems) if problems else '',
                              'danger' if fails else 'warning')
        icon.update()
        self.dash_update('system')

    def _queue_appearance(self, ap: dict):
        self._ap_pending = ap
        self._ap_timer.start()

    def _flush_appearance(self):
        ap, self._ap_pending = self._ap_pending, None
        if ap is not None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.set_appearance(ap)
            finally:
                QApplication.restoreOverrideCursor()

    def set_appearance(self, ap: dict, save: bool = True):
        """Apply a new appearance live: palette, stylesheet, icons, painted
        widgets, then rebuild the game views so inline-styled bits follow.
        save=False: a first-run setup preview, which mustn't write library.json
        (Nexus would then not count the next start as a first run)."""
        self.prefs.appearance = apply_appearance(ap)
        if save:
            self.prefs.save()
        app = QApplication.instance()
        f = app.font()
        f.setPixelSize(scaled(FS['sm']))           # Size: the type QSS doesn't set
        app.setFont(f)
        app.setStyleSheet(build_stylesheet())
        self.theatre.setStyleSheet(build_stylesheet(THEATRE_SCALE))
        self._style_title_bar()
        if self._theatre:
            self.hints.setStyleSheet(build_stylesheet(THEATRE_SCALE))
        refresh_icons()
        for hero in (self.homeview.hero, self.theatre.hero):
            hero._frames.clear()
            if hero.games:
                hero._show(animate=False)
        self.background.set_tint(None)
        self._home_sig = None
        self.apply_filter()
        self.dashboard.refresh()
        if self.selected:
            self._sync_selection()
        for w in app.allWidgets():
            w.update()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._style_title_bar()

    def _style_title_bar(self):
        """Windows: the title bar in the page's colours instead of the system's."""
        if not platform.TITLE_BAR_COLORS:           # winId() would make a native window
            return
        platform.style_title_bar(int(self.winId()), P['bg'], P['text'], P['border'],
                                 QColor(P['bg']).lightnessF() < 0.5)

    def _health_counts(self, problems, fails):
        self._health_problems = (problems, fails)
        self._sync_system()

    def _deals_count(self, n):
        self.nav[self.DEALS].set_badge(n or '', 'accent')
        self.dash_update('deals', 'upcoming')

    def _open_settings(self, section: str | None = None):
        self.go(self.SETTINGS)
        if section:
            self.settings.show_section(section)

    def _settings_saved(self, proton_changed):
        self.config = load_config()
        self.nav[self.NEWS].setVisible(self.config.get('news', True))
        self.nav[self.DEALS].setVisible(self.config.get('gamedb_enabled', False))
        self._apply_repacks()
        if getattr(self.settings, 'gamedb_changed', False) or not self.config.get(
                'gamedb_enabled'):
            self.dealspage.res, self.dealspage._loaded = {}, False
            self.nav[self.DEALS].set_badge('', 'accent')
            if self.config.get('gamedb_enabled'):
                self.dealspage.refresh()
        self.dash_update('news', 'deals', 'upcoming')
        self._poll_torrents()                  # the torrent client may have changed
        if proton_changed and self._games:
            self.toast("Point every game at the new Proton now?", 'info',
                       ("Apply to all", lambda: self.run(
                           ['--apply-launch', 'all'], "Re-applying settings to every game",
                           steam=True)), 15000)

    @property
    def _first_run(self) -> bool:
        """Nexus has never run here: no library.json and no config.toml, and
        setup wasn't done or skipped. NEXUS_NO_SETUP=1 skips it (tests)."""
        if self.prefs.setup_done or os.environ.get('NEXUS_NO_SETUP'):
            return False
        return not self.prefs.PATH.exists() and not CONFIG_PATH.exists()

    def repacks_on(self) -> bool:
        """Settings > Repack features (config [repacks] enabled)."""
        return self.config.get('repacks', True)

    def _apply_repacks(self):
        """Show or hide what's only for repacks: Downloads, the repack tab,
        FitGirl on Deals. The background checks ask repacks_on() themselves."""
        on = self.repacks_on()
        self.icons[self.DOWNLOADS].setVisible(on)
        self.topbar.install.setToolTip("Install a repack or add a game (Ctrl+N)" if on else
                                       "Add a game (Ctrl+N)")
        self.install.set_repacks(on)
        self.dealspage.set_repacks(on)
        if hasattr(self, 'repacks'):            # the folder watcher (made in _start)
            self.repacks.set_enabled(on)

    def _note_version(self, used_before: bool):
        """Remember this version; on the first start of a newer one, show its
        "What's new" once Home is up. Not during first-run setup, nor in tests
        (NEXUS_NO_SETUP)."""
        seen = self.prefs.last_seen_version
        if seen == __version__:
            return
        show = updates.show_whats_new(seen, __version__, used_before) \
            and not self._first_run and not os.environ.get('NEXUS_NO_SETUP')
        self.prefs.last_seen_version = __version__
        if not self._first_run:                 # setup saves it when it's done (w-15)
            self.prefs.save()
        if show and updates.whats_new():
            QTimer.singleShot(1200, lambda: alive(self) and self.show_whats_new())

    def show_whats_new(self):
        """This version's notes as cards in the sheet, with the full release
        notes a click away."""
        url = updates.release_url()
        self.sheet.notes(f"What's new in Nexus {__version__}", updates.whats_new(), [
            ("Got it", G['check'], lambda: None),
            ("Full release notes", G['external'],
             lambda: QDesktopServices.openUrl(QUrl(url)))],
            empty="No notes for this version here. The release page has them all.")

    def open_setup(self):
        self.setup.open(load_config(), self.prefs.appearance)
        self.go(self.SETUP)

    def _setup_finished(self, updates: dict, opts: dict):
        if updates:
            try:
                save_config(updates)
            except (ConfigError, OSError) as e:
                self.toast(f"Couldn't save the setup — {e}", 'error', None, 0)
                return
        self.prefs.setup_done = True
        self.prefs.save()
        self.config = load_config()
        self.settings.load()
        self._apply_repacks()
        self.set_appearance({**self.prefs.appearance, **opts['appearance']})
        if opts.get('tray'):
            self._apply_controls({'close_to_tray': True})
        self.go(self.HOME)
        self.reload_library()
        if opts.get('agent'):
            self._background_changed('agent', True)          # asks first
        else:
            self.toast("All set. Settings has everything, and Ctrl+K finds anything.",
                       'success', None, 6000)

    def _import_settings(self, path: str):
        """Settings → Back up & move → Import…: what it would change (a dry
        run), a question, then the import and a restart to load it all."""
        def planned(plan, err):
            if not alive(self):
                return
            if not plan or not plan.get('ok'):
                msg = (plan or {}).get('error') or err or "it couldn't be read"
                self.toast(f"Not imported — {msg}", 'error', None, 0)
                return
            parts = [f"Replaces {len(plan['replace'])} settings file(s) (the old ones are kept "
                     "as .bak)."]
            if plan['keep_secrets']:
                parts.append("Keeps this PC's keys and tokens.")
            parts.append(f"Games: {len(plan['games_added'])} added, "
                         f"{len(plan['games_here'])} already here"
                         + (f", {len(plan['games_missing'])} not on this PC ("
                            + ", ".join(g['name'] for g in plan['games_missing'][:3])
                            + ("…" if len(plan['games_missing']) > 3 else "")
                            + ") — reinstall those, or put their folders back" if
                            plan['games_missing'] else "") + ".")
            parts.append("Nexus restarts afterwards to load everything.")
            self.confirm("Import these settings?", " ".join(parts), "Import and restart",
                         lambda: cli_json(['--import-settings', path, '--json'], imported,
                                          60000), danger=False)

        def imported(r, err):
            if not alive(self):
                return
            if not r or not r.get('ok'):
                self.toast(f"Not imported — {(r or {}).get('error') or err}", 'error', None, 0)
                return
            self.restart()
        cli_json(['--import-settings', path, '--dry-run', '--json'], planned, 60000)

    def restart(self):
        """Start Nexus again once this one is gone (the new one would hand its
        arguments to us and exit while we still listen), then quit."""
        exe, *args = platform.gui_command(NX.REPO)
        QProcess.startDetached(exe, [*args, '--restarted'])
        self.quit_after_jobs()

    def _background_changed(self, what: str, on: bool):
        """Settings → Background. The agent is a systemd user service: ask
        before installing one, then let the CLI switch it. On Windows the
        same switch is Start with Windows (a Startup-folder shortcut to
        `nexus --hidden`), which needs no question; it turns the tray on too,
        since that's where Nexus then starts."""
        if what != 'agent':
            return
        st = self.settings
        startup = not has('systemd_agent')

        def switch():
            def done(data, err):
                if not alive(self):
                    return
                data = data or {}
                st.set_agent_status(data)
                errs = data.get('errors') or ([err] if err and not data else [])
                if errs:
                    name = "Start with Windows" if startup else "the background agent"
                    self.toast(f"Couldn't switch {name} — {errs[0]}", 'error', None, 0)
                    return
                tray = startup and on and tray_available() \
                    and not self.prefs.controls.get('close_to_tray')
                if tray:
                    self._apply_controls({'close_to_tray': True})
                    st.set_controls(self.prefs.controls)
                if startup:
                    self.toast("Nexus starts in the tray when you sign in to Windows"
                               + (" — closing the window now keeps it there too." if tray
                                  else ".") if on else
                               "Nexus no longer starts with Windows.", 'success', None, 6000)
                else:
                    self.toast("Nexus keeps working in the background now." if on else
                               "The background agent is off.", 'success', None, 5000)
            cli_json(['--agent-enable' if on else '--agent-disable', '--json'], done, 60000)
        if not on or startup:
            switch()
            return
        self.confirm("Keep Nexus working in the background?",
                     "Adds a systemd user service (nexus-agent) that starts with your "
                     "session. It installs finished torrents, says when a repack is ready "
                     "and sends your alerts while this window is closed. Turn it off here "
                     "any time.", "Turn it on", switch, danger=False)
        # Until it's confirmed and done, the checkbox shows what's true.
        st.set_agent_status(st._agent_state or {})

    # ---- closing ----
    QUIT_GRACE_MS = 6000          # cancelled jobs get this long to stop before SIGKILL

    def closeEvent(self, ev):
        if self.tray is not None and not self._quitting and not self._tray_quit:
            ev.ignore()                     # to the tray: the watchers keep going
            self.hide()
            if not self._tray_told:
                self._tray_told = True
                notify_desktop("Nexus is still running",
                               "It's in the tray. Quit it from the tray menu.")
            return
        self._tray_quit = False
        active = self.jobs.active()
        waiting = [p[0] for p in self.gate.pending]
        if self._quitting or (not active and not waiting):
            ev.accept()
            return
        ev.ignore()
        parts = []
        if active:
            parts.append(f"{len(active)} job(s) are still running ({active[0].title}). "
                         f"Quitting stops them.")
        if waiting:
            parts.append(f"Waiting for Steam to close: {', '.join(waiting)}. New games are "
                         f"added to Steam next time Nexus starts; anything else is dropped.")
        self.raise_()
        self.activateWindow()
        self.confirm("Still working", "\n\n".join(parts), "Quit anyway",
                     self.quit_after_jobs)

    def show_from_tray(self, page: str | None = None):
        self.show()
        self.bring_to_front()
        if page:
            self.go(page)

    def quit_from_tray(self):
        """Quit for real; with jobs running, the window comes back to ask."""
        self._tray_quit = True
        if self.jobs.active() or self.gate.pending:
            self.show_from_tray()
        self.close()

    def sync_tray(self):
        """Create or remove the tray icon to match Settings → Background."""
        want = self.prefs.controls.get('close_to_tray') and tray_available()
        if want and self.tray is None:
            self.tray = Tray(self)
        elif not want and self.tray is not None:
            self.tray.close()
            self.tray = None
            if not self.isVisible():
                self.show_from_tray()

    def quit_after_jobs(self):
        """Cancel every job, then close once they've all stopped (or after
        QUIT_GRACE_MS, killing what's left). Closing straight away killed
        setup.exe/wine mid-write: the SIGTERM → SIGKILL grace never ran."""
        self._quitting = True
        for j in self.jobs.active():
            j.cancel()
        if self.jobs.active():
            self.toast("Stopping running jobs…", 'info', None, self.QUIT_GRACE_MS)
        self._quit_deadline = time.monotonic() + self.QUIT_GRACE_MS / 1000
        self._quit_timer.start()

    def _quit_poll(self):
        left = self.jobs.active()
        # An elevated installer can't be stopped: wait for it, however long.
        if left and (time.monotonic() < self._quit_deadline
                     or any(j.elevated for j in left)):
            return
        self._quit_timer.stop()
        for j in left:
            if j.proc is not None and j.proc.state() != QProcess.ProcessState.NotRunning:
                j.proc.kill()
                j.proc.waitForFinished(1000)
        self.close()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .actions import ActionSheet, TrailerOverlay  # noqa: E402
from .activity import ActivityPage, ActivityStrip  # noqa: E402
from .artpicker import ArtPickerPage  # noqa: E402
from .config import ConfigError, load_config, save_config, steam_running  # noqa: E402
from .dashboard import Dashboard, system_badge  # noqa: E402
from .deals import DealsPage  # noqa: E402
from .details import DetailsPanel  # noqa: E402
from .downloads import DownloadsPage  # noqa: E402
from .gameinfo import is_installed  # noqa: E402
from .input import (ControllerInput, FocusRing, HintBar, install_input_mode, SpatialNav)  # noqa: E402
from .install import InstallPage, RepackWatcher  # noqa: E402
from .jobs import cli_json, in_thread, JobManager  # noqa: E402
from .setup import SetupPage  # noqa: E402
from .palette import action_entries, CommandPalette, game_entries, page_entries  # noqa: E402
from .tray import Tray, tray_available  # noqa: E402
from .library import HomeView  # noqa: E402
from .newspage import NewsPage  # noqa: E402
from .prefs import LibraryPrefs  # noqa: E402
from .protonpage import ProtonPage  # noqa: E402
from .settings import SettingsPage  # noqa: E402
from .storage import StoragePage  # noqa: E402
from .system import HealthPage, PlayHistoryPage, SystemPage  # noqa: E402
from .theatre import (AttractOverlay, CursorHider, THEATRE_SCALE, TheatreSearch, TheatreView)  # noqa: E402
from .widgets import (GradientBackground, notify_desktop, refresh_icons, SteamGate,
    ToastHost)  # noqa: E402
