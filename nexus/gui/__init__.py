"""Nexus — game launcher, FitGirl repack installer and Proton manager (PyQt6).

Drives the `nexus-cli` backend via QProcess (every read is a JSON command,
every change a queued job) and reads artwork from disk for display.

Layout: a sidebar (Library / Activity / Storage / Health / Settings) and a
stacked page area. Everything happens inside the one window — tiling and
scrolling compositors (niri) float every dialog, so dialogs are kept to file
pickers and the quit-while-busy question. Jobs report as Activity cards, a
strip under the library and toasts; the raw console is a debug view.

The visual language follows the protongen design system (Rosé Pine dark):
token-driven color/space/radius/type, a two-radial-gradient window background,
translucent cards, one accent with semantic status colors, and pill badges.
Two things in that spec have no Qt equivalent and are approximated here:
`backdrop-filter` blur (Qt cannot blur what is behind a widget — translucent
fills are composited over the gradient instead) and `outline-offset` focus rings
(drawn as a 2px accent border on the element itself).

The code lives in the modules of this package, one per area (common, theme
and fmt are the base layers; app has main()). This module re-exports every
name, so `nexus.gui.<name>` keeps working; to patch a name in tests, use
tests/patching.py (each module imports what it uses).
"""

from .common import (
    _REPO, alive, APPLICATIONS_DIR, ATTRACT_MINUTES, CLI, cli_args, CLI_BIN, COLLECTION_NAMES,
    CONFIG_PATH, DATA_DIR, DESKTOP_PATH, HOME, ICON_PATH, INSTALL_BASE_DEFAULT, LEGACY_DESKTOP,
    LOGO_SVG,
    MAX_COLLECTION_NAME, MAX_NOTE, MAX_TITLE)  # noqa: F401
from .theme import (
    _check_icon_path, _chevron_icon_path, _first_installed, _hex_ok, _ICON_CACHE, _mix, _rgba,
    _shade, _stroke_icon, accent_hex, ACCENTS, APPEARANCE_DEFAULT, apply_appearance,
    build_stylesheet, CHIP_H, CORNERS, FONTS, FS, G, GLOW, GLYPH_H, GLYPHS, GRAD,
    ICON_FALLBACK_STACK, ICON_TTF, LOOK, MONO_STACK, normalize_appearance, P, PAL, pill,
    PILL_H, pill_qss, qss_grad, R, R_BASE, resolve_fonts, SANS_STACK, SP, SRC_HUE, THEMES,
    TILE_LIFT, TILE_W, DARK_STATUS, DAWN_STATUS, is_light, SCALES, scaled, ui_scale)  # noqa: F401
from .fmt import (
    fmt_ago, fmt_bytes, fmt_elapsed, fmt_eta, fmt_hours, fmt_relative, fmt_size)  # noqa: F401
from .widgets import (
    _role_of, _SOFT_BREAK, _style_icon_label, ask, clear_layout, empty_state_text, EmptyState,
    FlowLayout, glyph_pixmap, GradientBackground, icon_button, icon_label, label,
    notify_desktop, NoWheelCombo, plain_label, PopupRounder, protongen_code_preview,
    refresh_icons, SegmentedControl, set_glyph_icon, short_path, soft_wrap, start_steam,
    SteamGate, Toast, ToastHost)  # noqa: F401
from .art import (
    _paint_placeholder, _PLACEHOLDERS, _TINTS, anim_ms, ART, art_lookup, art_source, ArtCache,
    cover_image, COVER_KINDS, cover_pixmap, dominant_color, fit_image, HERO_KINDS, LOGO_KINDS,
    MOTION, PLACEHOLDER_BYTES, placeholder_pixmap, pixmap_bytes, rounded_pixmap, screen_pixmap,
    THUMB_DIR, thumb_image, THUMB_MIN_BYTES, WIDE_KINDS)  # noqa: F401
from .jobs import (
    _cli_env, _CLI_MEMO, _CLI_WAITING, _DISPATCH, _Dispatch, _EVENT_RE, _FILE_RE,
    _INSTALL_STEPS, _SILENT_STEP, _STEP_RE, _Task, cli_cache_clear, cli_json, CliQuery,
    in_thread, Job, JobManager, match_running)  # noqa: F401
from .activity import (
    ActivityCard, ActivityPage, ActivityStrip)  # noqa: F401
from .tiles import (
    GameTile)  # noqa: F401
from .prefs import (
    clean_collection_name, LibraryPrefs)  # noqa: F401
from .gameinfo import (
    anticheat_warning, can_quick_play, compat_facts, install_fraction, is_installed,
    PCGW_COLORS, PCGW_WORDS, play_enabled, play_label, SOURCE_LABEL, TIER_COLORS)  # noqa: F401
from .actions import (
    _gate_actions, about_html, action_glyph, ACTION_GLYPHS, ActionSheet, build_game_menu,
    COUCH_SKIP, game_actions, GameAction, NOT_WHILE_BUSY, NOT_WHILE_PLAYING,
    TrailerOverlay)  # noqa: F401
from .library import (
    _EdgeFade, _ShelfScroll, _TileSignals, featured_games, HeroCarousel, HomeView, KeepPlace,
    LibraryView, ListView, shelf_page_target, ShelfRow, surprise_pick)  # noqa: F401
from .dashboard import (
    _game_store_id, _visible_games, AchievementsWidget, ClickRow, CollectionsWidget,
    dash_cols, dash_default, DASH_DEFAULT_SIZE, DASH_KINDS, DASH_MERGED, dash_move,
    dash_normalize, dash_pack, DASH_PLACE_AFTER, DASH_SIZES, DASH_SPAN, DASH_WIDGETS,
    Dashboard, DashWidget, DealsWidget, DUP_ORDER, FavouritesWidget, game_row, GlyphBadge,
    InstalledWidget, job_facts, JumpWidget, mark_duplicates, NewsWidget, OwnedWidget,
    PlaytimeWidget, ShelfWidget, SurpriseWidget, system_badge, system_status, SystemWidget,
    SysTile, UpcomingWidget, UpdatesWidget)  # noqa: F401
from .details import (
    _hero_art, AchievementRow, DetailsPanel, Disclosure, ElideLabel, HeroBanner, PerfPanel,
    StatChip, StatTile, BATCH_ACTIONS)  # noqa: F401
from .storage import (
    _SEG_COLORS, SortItem, StorageBar, StoragePage)  # noqa: F401
from .protonpage import (
    ProtonPage)  # noqa: F401
from .newspage import (
    elide_lines, NEWS_FEED_STYLE, news_group, news_image, NEWS_STYLE, news_style, NewsArt,
    NewsFeature, NewsPage, NewsReader, NewsTile)  # noqa: F401
from .deals import (
    _pill, deal_links, deal_meta, deal_sort_key, deal_was, DealArt, DealCard, DealsPage,
    DealSpotlight, meta_html, steam_hero_url)  # noqa: F401
from .downloads import (
    DownloadRow, DownloadsPage, torrent_state)  # noqa: F401
from .artpicker import (
    ART_KIND_LABEL, ART_THUMB, ArtPickerPage, ArtThumb)  # noqa: F401
from .system import (
    HealthPage, PlayHistoryPage, SystemPage, WeekBars)  # noqa: F401
from .settings import (
    AppearanceCard, SettingsPage, Swatch)  # noqa: F401
from .install import (
    InstallPage, protongen_has_default, RepackWatcher)  # noqa: F401
from .config import (
    _read_config_toml, _toml_literal, ConfigError, load_config, save_config, steam_running,
    toml_update)  # noqa: F401
from .input import (
    _native_arrows, ControllerInput, focus_and_reveal, FocusRing, HintBar, INPUT, input_mode,
    InputMode, install_input_mode, NAV_KEYS, SpatialNav)  # noqa: F401
from .theatre import (
    _news_html, _SEARCH_JUNK, _search_norm, AttractOverlay, CursorHider, search_games,
    THEATRE_SCALE, TheatreCard, TheatreDetails, TheatreSearch, TheatreView)  # noqa: F401
from .topbar import _TabHandle, CollectionChip, TopBar, TopIcon, Wordmark  # noqa: F401
from .win_library import LibraryMixin  # noqa: F401
from .win_nav import NavigationMixin  # noqa: F401
from .win_controller import ControllerMixin  # noqa: F401
from .win_play import last_exit_code, PlayMixin  # noqa: F401
from .win_downloads import DownloadsMixin  # noqa: F401
from .win_actions import ActionsMixin  # noqa: F401
from .palette import (action_entries, CommandPalette, Entry, fuzzy_score, game_entries,
    page_entries, rank)  # noqa: F401
from .tray import recent_games, Tray, tray_available  # noqa: F401
from .setup import SetupPage, STEPS  # noqa: F401
from .window import MainWindow  # noqa: F401
from .app import (
    ensure_icon, forward_to_running, install_desktop_entry, install_excepthook, INSTANCE,
    INSTANCE_MAX, InstanceServer, main, manager_exec)  # noqa: F401
