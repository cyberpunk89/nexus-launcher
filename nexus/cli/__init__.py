"""nexus-cli — the Nexus backend: installs FitGirl repacks via umu/Proton/Wine,
manages Steam/Heroic entries, Proton builds, cleanup and the game library.

The code lives in the modules of this package, one per area (common is the
base layer; main parses arguments). This module re-exports every name, so
`nexus.cli.<name>` keeps working for bin/nexus-cli, the GUI's tooling and the
tests. To patch a name in tests, patch it where it is used: each module
imports what it needs (tests/cli_patch.py does that for every module)."""

from nexus.inno import (
    _inno_block, _INNO_MAX, _INNO_TAG, inno_header)  # noqa: F401
from .common import (
    _ACTIVE_PROC, _BACKED_UP, _BACKUP_TS_RE, _BACKUP_WINDOW, _cached_read, _CHILD_EVENT_RE,
    _FILE_CACHE, _handle_sigterm, _load_toml, _LogProxy, _OUT_LOCK, _SLUG_RE,
    _terminate_active_proc, APPLICATIONS_DIR, ARTWORK_DIR, atomic_write, backup_file,
    BACKUP_KEEP, CLI_BIN, CONFIG_PATH, DATA_DIR, default_install_base, default_scan_dir,
    dir_size_bytes, disable_cow, emit_event, existing_ancestor, glob_escape, INSTALL_BASE,
    LAUNCHERS_DIR, log, LOG_DIR, Logger, make_slug, require_slug, run, decoded, SAVES_DIR, SECRET_ENV,
    SGDB_API, STEAM_CONFIG_VDF, STEAM_ROOT, use_logger, valid_slug, VDF_ERRORS, win_path,
    WINE_ABORTS, wine_env, WINE_ENV_DROP)  # noqa: F401
from .repack import (
    _check_oom_journal, _diagnose_abnormal_exit, _INSTALL_AWAKE, _install_session,
    _memory_headroom, _parallel_md5, _PARTIAL_SUFFIXES, _REDIST_RE, _signal_name,
    _SKIPPABLE_REDIST, _swap_is_compressed, AUDIO_DRIVERS, clean_repack_name,
    clear_incomplete_marker, COMMON_DEPS, compute_md5, CONSOLE_HOST, detect_compatdata,
    detect_game_name, DISK_SPACE_WARN_MULTIPLIER, DISK_SWAP_CREDIT, exe_candidates,
    existing_install_root, find_exe_in_dir, find_game_exe, find_setup_exe, FITGIRL_PATTERNS, HARD_MIN_AVAILABLE_MB,
    HARD_MIN_EFFECTIVE_MB, incomplete_installs, INCOMPLETE_MARKER, inno_args, INNO_SILENT,
    InstallProgress, MAX_SETUP_RETRIES, parse_md5_file, preflight_hard_disk_check,
    preflight_hard_memory_check, read_incomplete_marker, read_meminfo, REDIST_EXES,
    repack_complete, repack_fingerprint, repack_info, setup_product_name, setup_redists,
    silent_setup_overrides, silent_target, SKIP_EXE_DIRS, SKIP_EXE_NAMES, verify_bins,
    VERSION_RE, write_incomplete_marker, ZRAM_SWAP_CREDIT)  # noqa: F401
from .runner import (
    _comment_safe, _elf_machine, _HOST_ELF_MACHINE, _proton_dirs_sig, _proton_rank_key,
    _PROTON_SCAN, _proton_version_tuple, _proton_wine_bins, _proton_wine_is_native,
    _scan_protons, _title_key, _umu_env, _UMU_ID_RE, _WARNED_ARCH, _WARNED_DAMAGE, BENCH_HOOK,
    browser_muted, NO_BROWSER,
    damaged_protons, find_proton, find_umu, init_prefix, install_deps, list_protons,
    proton_by_name, proton_repair_tag, PROTON_SEARCH_DIRS, proton_tool_name,
    resolve_game_proton, resolve_umu_gameid, select_runner, STEAM_SEARCH_URL, UMU_DB_URL,
    write_launch_script)  # noqa: F401
from .config import (
    load_config, resolve_install_base, resolve_scan_dirs)  # noqa: F401
from .registry import (
    _CARRIED_KEYS, _REGISTRY_LOCK_DEPTH, _REGISTRY_LOCK_FH, load_registry, register_game,
    registry_get, registry_lock, REGISTRY_PATH, save_registry, set_registry_steam_appid,
    touch_last_played, unregister_game, update_registry_entry, update_registry_size)  # noqa: F401
from .sgdb import (
    _art_file, _cache_json, _cache_put, _cache_set, _download, _SGDB_CDN, _sgdb_get,
    cached_sgdb_id,
    _sgdb_json, _SGDB_LOCK, _variant, art_dir_for, ART_MAX_BYTES, art_variants,
    cmd_art_variants, cmd_fill_art, cmd_set_art, cmd_set_sgdb, cmd_sgdb_search, fetch_artwork,
    MISS_TTL, overlay_art, read_picks, read_sgdb_match, save_art, SGDB_CACHE, sgdb_game_id,
    SGDB_KINDS, sgdb_search, VARIANTS_TTL)  # noqa: F401
from .launchprofile import (
    _check_shared_config, _DEFAULT_WRAPPER_RANK, _env_get, _ENV_KEY_RE, _env_set,
    _GAMESCOPE_RE, _is_env_token, _lsfg_name, _rank, _RESERVED_ENV, _toml_chunks,
    _UNSAFE_ENV_RE, _UPSCALER_KEYS, apply_perf, decode_protongen_code, empty_profile,
    format_launch_options, FPS_ENV, gamescope_argv, heroic_launch_fields, LaunchParseError,
    LSFG_CONF, lsfg_installed, LSFG_LAYER, LSFG_PREFIX, lsfg_set, lsfg_state, parse_gamescope,
    parse_launch_options, parse_perf_args, PERF_MODES, perf_state,
    profile_from_protongen_config, profile_is_empty, protongen_bin, PROTONGEN_CODE_PREFIX,
    protongen_default_config, protongen_default_profile, protongen_heroic_id,
    protongen_memory_for, PROTONGEN_STATE, split_wrappers, UPSCALERS, WRAPPER_RANK)  # noqa: F401
from .steam import (
    _appid_signed, _ci_key, _copy_grid_art, _count_shortcuts, _ensure_tracked_files,
    _integrity_files, _INTEGRITY_SKIP_TOPLEVEL, _is_save_noise, _iter_savedata,
    _load_vdf_text, _read_compat_mapping, _redirect_compatdata, _report_failed_copies,
    _SAVE_CONTAINERS, _SAVE_DIRS, _SAVE_NOISE, _SAVE_NOISE_ANY, _SAVE_NOISE_EXT,
    _shortcut_uid, _STEAM_ANNOUNCED, _steam_last_logged_in, _steam_login_users,
    _STEAM_PROFILE, _stored_appid, _unique_dir, _wait_for_steam_closed, _write_compat_mapping,
    add_steam_shortcut_native, backup_prefix_saves, cmd_check_integrity, find_prefix_savedata,
    find_steam_userdata, INTEGRITY_DIR, integrity_manifest_path, latest_save_backup,
    load_integrity_manifest, load_shortcuts, match_steam_account, PRE_RESTORE,
    resolve_steam_account, restore_prefix_saves, save_integrity_manifest, save_shortcuts,
    savedata_owners, set_steam_account_pin, set_steam_compat_tool, shortcut_appid,
    split_game_shortcuts, steam_account_label, STEAM_ACCOUNT_PIN, steam_accounts,
    steam_is_running)  # noqa: F401
from .desktop import (
    _desktop_path, write_desktop_entry)  # noqa: F401
from .heroic import (
    _heroic_app_name, _heroic_exe, _heroic_game_config, _heroic_lib_path,
    _heroic_wine_version, _load_heroic_library, _NANOID_ALPHABET, add_heroic_entry,
    find_heroic_config, heroic_is_running, remove_heroic_entry)  # noqa: F401
from .manage import (
    _add_achievement_counts, _cover_path, _disk_info, _game_by_id, _heroic_execs,
    _installed_games, _iso_to_ts, _parse_size, _protongen_id, _read_shortcuts,
    _registry_or_scan, _steam_appnames, cmd_library, cmd_list, cmd_running, heroic_play_stats,
    managed_entries, owned_games, steam_shortcut_last_played)  # noqa: F401
from .running import (
    _OPEN_URL_RE, _proc_state, BENCH_DIR, cmd_bench_results, cmd_benchmark, cmd_open_url,
    cmd_pause, parse_mangohud_csv, running_repacks)  # noqa: F401
from .sunshine import (
    _achievement_watcher, _cli_command, _has_option, _launch_through_steam,
    _owned_artwork_dir, _owned_game_dir, _prefix_markers, _rmtree_collect, _run_session,
    _sunshine_find, _sunshine_marker, _sunshine_png, cmd_launch, cmd_launch_via, cmd_sunshine,
    cmd_track_session, cmd_uninstall, game_has_steam_shortcut, launch_log_path,
    prefix_processes, remove_steam_shortcut, steam_shortcut_url, SUNSHINE_APPS, SUNSHINE_ART,
    sunshine_entry, sunshine_has)  # noqa: F401
from .apply import (
    _entry_paths, _proton_is_explicit, _require_entry, _save_profile, cmd_apply,
    cmd_from_protongen, cmd_perf, cmd_protongen_default, cmd_set_launch, cmd_set_proton,
    cmd_show_launch, latest_install_log, profile_owns_options, regen_launch_sh, sync_heroic,
    sync_steam)  # noqa: F401
from .protoncmds import (
    _heroic_proton_bins, _pacman_version, _valve_protons, cmd_list_protons,
    cmd_proton_install, cmd_proton_migrate, cmd_proton_releases, cmd_proton_remove,
    cmd_protondb, cmd_protons, ge_is_newer, ge_update, install_hint, proton_usage)  # noqa: F401
from .downloads import (
    _cached_wishlist, _owned_store_ids, _size_bytes, _torrent_dest, _torrent_mark,
    cmd_check_repack, cmd_repack_feed, cmd_repack_grab, cmd_repack_index, cmd_repack_page,
    cmd_repack_reject, cmd_torrent, send_alerts)  # noqa: F401
from .info import (
    _ago, _llm_config, cmd_apply_recipe, cmd_explain_log, cmd_game_tips, cmd_gamedb,
    cmd_history, cmd_news, cmd_news_feed, cmd_news_source, cmd_store_info, compat_lines, CONFIG_SECRETS,
    config_secrets_doctor, gamedb_doctor)  # noqa: F401
from .gamecmds import (
    _launcher_display_name, _local_artwork, ACHIEVEMENT_COUNTS, add_existing_to_steam,
    cmd_achievements, cmd_add_heroic, cmd_add_steam, cmd_detect, cmd_discard_incomplete,
    cmd_prefix_tool, cmd_refetch_art, cmd_reindex, cmd_rename, cmd_repair_launchers, cmd_scan,
    cmd_set_exe, cmd_trash_repack, PREFIX_TOOLS, scan_repacks, steam_account_id)  # noqa: F401
from .doctor import (
    _all_shortcut_appids, _all_shortcut_names, _launch_sh_proton_ok, _looks_unmounted,
    _ORPHAN_BAK_RE, _quarantine_dir, _steam_libraries, cleanup_candidates,
    cmd_clean_orphan_compatdata, cmd_cleanup, cmd_cleanup_scan, cmd_doctor,
    cmd_list_steam_accounts, cmd_prune, cmd_quarantine_restore, doctor_checks,
    find_orphan_compatdata, ORPHAN_HOLDING_DIR, quarantine_path, quarantined,
    stale_registry_entries)  # noqa: F401
from .entry import (
    _check_slug_args, _cmd_apply_launch, _cmd_gamedb_lookup, _cmd_launch_via,
    _cmd_notify_test, _cmd_orphans, _cmd_pause, _cmd_remove_heroic, _cmd_restore_saves, _main,
    _SLUG_OPTS, _SLUG_OPTS_ALL, build_parser, COMMANDS, main)  # noqa: F401
from .install import (
    _best_effort, cmd_install)  # noqa: F401
from .agent import (
    _take_lock, Agent, AGENT_LOCK, AGENT_STATE, agent_status, cmd_agent,
    cmd_agent_enable, cmd_agent_status, cmd_check_alerts, install_cmd, playing, UNIT_DIR,
    UNIT_NAME, unit_text)  # noqa: F401
from .move import (
    cmd_move, file_map, fix_heroic_prefix, move_targets, rebase)  # noqa: F401
from .snapshots import (
    auto_snapshot, btrfs_ready, cmd_snapshots, convert, fs_type, is_subvolume,
    list_snapshots, make_game_dir, remove_game_dir, rollback, snap_root, snapshot_dirs,
    snapshot_doctor, SnapshotError, status as snapshot_status, take)  # noqa: F401
from .importgame import cmd_add_exe, cmd_import, guess_name, run_windows_exe, wine_prefix_of  # noqa: F401
from .backup import (
    BackupError, cmd_export_settings, cmd_import_settings, export_settings, import_settings,
    keep_secrets, strip_secrets)  # noqa: F401
