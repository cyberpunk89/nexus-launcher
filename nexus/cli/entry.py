"""nexus-cli's entry point: the options (build_parser), the command table
(COMMANDS) and main()."""

import argparse
import json
import signal
import sys
from pathlib import Path

from nexus import paths as NX

from .common import _handle_sigterm, INSTALL_BASE, Logger, require_slug, use_logger


def main() -> None:
    """The CLI, with a last line of defence: an unexpected exception still
    ends with one readable line on stderr and, for --json, a JSON error on
    stdout (the GUI parses stdout — a bare traceback there reads as "bad
    JSON")."""
    try:
        _main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:                 # noqa: BLE001 — reported, then exit 1
        import traceback
        traceback.print_exc(file=sys.stderr)
        msg = f"{type(e).__name__}: {e}"
        if '--json' in sys.argv[1:]:
            print(json.dumps({'error': {'kind': 'internal', 'message': msg}}))
        print(f"nexus-cli: internal error — {msg}", file=sys.stderr)
        sys.exit(1)


# Options whose value (or first value) names a managed game by slug. They
# become paths (install dir, artwork, saves), so they are checked up front:
# `--uninstall ..` must not reach the folder above the install base.
_SLUG_OPTS = {
    'uninstall': '--uninstall', 'discard_incomplete': '--discard-incomplete',
    'restore_saves': '--restore-saves', 'check_integrity': '--check-integrity',
    'launch': '--launch', 'steam': '--steam', 'remove_steam': '--remove-steam',
    'heroic': '--heroic', 'remove_heroic': '--remove-heroic',
    'refetch_art': '--refetch-art', 'repair_launchers': '--repair-launchers',
    'show_launch': '--show-launch', 'set_launch': '--set-launch',
    'set_proton': '--proton', 'from_protongen': '--from-protongen',
    'protongen_default': '--protongen-default', 'apply_launch': '--apply-launch',
    'set_exe': '--set-exe', 'rename': '--rename', 'prefix_tool': '--prefix-tool',
    'track_session': '--track-session', 'apply_recipe': '--apply-recipe',
    'explain_log': '--explain-log',
}


_SLUG_OPTS_ALL = {'protongen_default', 'apply_launch'}   # also take 'all'


def _check_slug_args(args) -> None:
    for dest, flag in _SLUG_OPTS.items():
        v = getattr(args, dest, None)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if not v or (v == 'all' and dest in _SLUG_OPTS_ALL):
            continue
        require_slug(v, flag)


def build_parser() -> argparse.ArgumentParser:
    """Every nexus-cli option (the GUI drives the same flags)."""
    parser = argparse.ArgumentParser(
        prog='nexus-cli',
        description='Nexus backend: install FitGirl repacks, manage Steam/Heroic entries, Proton builds and cleanup.',
    )
    parser.add_argument('repack_dir', nargs='?', default='.',
                        help='Path to the FitGirl repack folder (default: current dir)')
    parser.add_argument('--name', help='Override auto-detected game name')
    parser.add_argument('--verify', action='store_true',
                        help='Verify MD5 checksums before installing (slow for large files)')
    parser.add_argument('--no-deps', action='store_true',
                        help='Force-skip winetricks deps even if --deps was given '
                             '(deps are already off by default)')
    parser.add_argument('--force', action='store_true',
                        help='Start from a fresh prefix: an existing install of this game is '
                             'moved aside to <slug>.old-<time> (not deleted)')
    parser.add_argument('--install-dir', default=None,
                        help='Base install directory. Overrides config [install] base; '
                             f'default {INSTALL_BASE}')
    parser.add_argument('--log', action='store_true',
                        help='Save full log to ~/.local/share/nexus/logs/<game>-<time>.log')
    parser.add_argument('--debug', action='store_true',
                        help='Enable verbose Wine/Proton debug output (implies --log)')
    parser.add_argument('--skip-mem-check', action='store_true',
                        help='Bypass the pre-install memory hard-stop (use if you know '
                             'your system can handle it; not recommended)')
    parser.add_argument('--skip-disk-check', action='store_true',
                        help='Bypass the pre-install disk-space hard-stop (use if you '
                             'know your system can handle it; not recommended)')
    parser.add_argument('--no-steam', action='store_true',
                        help='Do not add the installed game to Steam')
    parser.add_argument('--no-desktop', action='store_true',
                        help='Do not create a desktop-menu launcher entry')
    parser.add_argument('--no-heroic', action='store_true',
                        help='Do not add the installed game to Heroic (sideloaded)')
    parser.add_argument('--no-artwork', action='store_true',
                        help='Do not fetch cover/hero/logo/icon art from SteamGridDB')
    parser.add_argument('--deps', metavar='SPEC',
                        help="Install winetricks verbs into the prefix: 'all' or a "
                             "comma list (e.g. vcrun2019,d3dcompiler_47). Off by "
                             "default — Proton already ships the usual runtimes")
    parser.add_argument('-y', '--yes', action='store_true',
                        help='Auto-confirm proceed/continue prompts (non-interactive)')
    parser.add_argument('--list', action='store_true',
                        help='List managed games (with Steam/desktop status) and exit')
    parser.add_argument('--json', action='store_true',
                        help='With --list or --scan, emit machine-readable JSON')
    parser.add_argument('--sizes', action='store_true',
                        help='With --list, (re)compute and cache each managed game\'s '
                             'on-disk size (stats only, but slow for a large library)')
    parser.add_argument('--uninstall', metavar='SLUG',
                        help='Remove a game (prefix + Steam shortcut + desktop entry) and exit')
    parser.add_argument('--save-backup', action='store_true', dest='save_backup',
                        help='With --uninstall, back up detected save-game files before '
                             'removing the prefix (default: off — asks interactively unless '
                             '--yes is also given, in which case saves are NOT backed up)')
    parser.add_argument('--import-save', action='store_true', dest='import_save',
                        help='On install, restore a previous save backup into a fresh prefix '
                             'if one exists (default: off — asks interactively unless --yes '
                             'is also given, in which case no backup is imported)')
    parser.add_argument('--use-protongen-default', action='store_true',
                        dest='use_protongen_default',
                        help="On install, set the game up with protongen's default profile "
                             "(launch options + Proton) unless it already has its own")
    parser.add_argument('--interactive', action='store_true',
                        help="Show FitGirl's setup window and click through it yourself "
                             "(default: install silently into the prefix's C:\\Games\\<slug>; "
                             "config [install] unattended = false makes this the default)")
    parser.add_argument('--unattended', action='store_true',
                        help='Install silently even if config [install] unattended = false')
    parser.add_argument('--from-torrent', metavar='HASH', dest='from_torrent',
                        help=argparse.SUPPRESS)       # the download this install is from
    parser.add_argument('--defer-steam', action='store_true', dest='defer_steam',
                        help="If Steam is running when the install finishes, don't wait for "
                             "it: print a steam-pending event and leave the shortcut for a "
                             "later `--steam <slug>` (the GUI does this once Steam is closed)")
    parser.add_argument('--trash-repack', action='store_true', dest='trash_repack',
                        help='After a successful install, move the repack folder to the '
                             'Trash (config [install] trash_repack = true does it always)')
    parser.add_argument('--trash-repack-dir', metavar='PATH', dest='trash_repack_dir',
                        help='Move a repack folder (one with a setup.exe) to the Trash and exit')
    parser.add_argument('--discard-incomplete', metavar='SLUG', dest='discard_incomplete',
                        help='Move an unfinished install (cancelled or failed, not in the '
                             'library) to the Trash and exit')
    parser.add_argument('--achievements', metavar='GAME',
                        help="A Steam game's achievements (appid or steam-<appid>), or "
                             "'recent' for the newest unlocks across games; from Steam's "
                             "local cache, read-only (use --json)")
    parser.add_argument('--restore-saves', metavar='SLUG', dest='restore_saves',
                        help="Re-copy a managed game's most recent save backup into its "
                             "current prefix and exit")
    parser.add_argument('--check-integrity', metavar='SLUG', dest='check_integrity',
                        help="Hash a managed game's installed files against a saved "
                             "baseline (creating one on first run) to catch corruption, "
                             "then exit")
    parser.add_argument('--rebaseline', action='store_true',
                        help='With --check-integrity, accept the current files as the '
                             'new baseline instead of comparing against the old one')
    parser.add_argument('--clean-orphan-compatdata', metavar='APPID', dest='clean_orphan',
                        help="Move a stray compatdata prefix (see --doctor) out of "
                             "Steam's way and exit. Never deletes it outright.")
    parser.add_argument('--add-steam', metavar='PATH', dest='add_steam',
                        help='Add an already-installed game (folder or .exe) to Steam '
                             '+ desktop menu and exit (use --name to set its title)')
    parser.add_argument('--launch', metavar='SLUG',
                        help='Launch a managed game by slug and exit')
    parser.add_argument('--pause', metavar='SLUG',
                        help='Freeze a running repack (SIGSTOP everything in its prefix)')
    parser.add_argument('--resume', metavar='SLUG', help='Thaw a paused repack')
    parser.add_argument('--pause-toggle', nargs='?', const='', metavar='SLUG',
                        dest='pause_toggle',
                        help='Pause or resume (no slug: the one repack running) — '
                             'handy as a compositor key binding')
    parser.add_argument('--benchmark', metavar='SLUG',
                        help='Launch a repack with MangoHud frame-time logging')
    parser.add_argument('--bench-seconds', type=int, default=60, dest='bench_seconds',
                        help='With --benchmark: how long to log (default 60)')
    parser.add_argument('--bench-delay', type=int, default=30, dest='bench_delay',
                        help='With --benchmark: seconds after launch before logging '
                             '(default 30; Shift+F2 starts it by hand)')
    parser.add_argument('--bench-results', metavar='SLUG', dest='bench_results',
                        help="A repack's benchmark runs (avg / 1%% / 0.1%% low fps)")
    parser.add_argument('--launch-via', nargs=2, metavar=('SLUG', 'steam|nexus'),
                        dest='launch_via',
                        help='Play a repack through its Steam shortcut (Steam Input, overlay, '
                             'recording) or straight from Nexus (the default)')
    parser.add_argument('--wait', action='store_true',
                        help='With --launch: stay until the game exits (for Sunshine)')
    parser.add_argument('--sunshine', metavar='GAME',
                        help='Add or update a game in Sunshine (slug, steam-<appid> or Heroic slug)')
    parser.add_argument('--remove-sunshine', metavar='GAME', dest='remove_sunshine',
                        help="Remove a game's Sunshine entry")
    parser.add_argument('--steam', metavar='SLUG', dest='steam',
                        help='Add a Steam shortcut for a managed game (by slug) and exit')
    parser.add_argument('--remove-steam', metavar='SLUG', dest='remove_steam',
                        help='Remove just the Steam shortcut for a game and exit')
    parser.add_argument('--heroic', metavar='SLUG', dest='heroic',
                        help='Add a managed game (by slug) to Heroic as a sideloaded app and exit')
    parser.add_argument('--remove-heroic', metavar='SLUG', dest='remove_heroic',
                        help='Remove just the Heroic sideload entry for a game and exit')
    parser.add_argument('--art-variants', metavar='GAME', dest='art_variants',
                        help='SteamGridDB images to choose from for a game (any source); '
                             'use with --kind and --page')
    parser.add_argument('--kind', choices=sorted(SGDB_KINDS), default='cover',
                        help='Art kind for --art-variants / --set-art')
    parser.add_argument('--page', type=int, default=0, help='Result page for --art-variants')
    parser.add_argument('--set-art', metavar='GAME', dest='set_art',
                        help='Use a SteamGridDB image (--url) for a game, or --reset it')
    parser.add_argument('--url', help='Image URL for --set-art (SteamGridDB only)')
    parser.add_argument('--reset', action='store_true', help='With --set-art or --set-sgdb: back to the default')
    parser.add_argument('--sgdb-search', metavar='NAME', dest='sgdb_search',
                        help='Search SteamGridDB games by name (to pick the right match)')
    parser.add_argument('--set-sgdb', metavar='GAME', dest='set_sgdb',
                        help='Pin the SteamGridDB game (--id, --name) a game fetches art '
                             'from, or --reset to automatic matching')
    parser.add_argument('--id', type=int, dest='sgdb_gid', help='SteamGridDB game id for --set-sgdb')
    parser.add_argument('--sgdb-name', dest='sgdb_name', help='Its name, for display (--set-sgdb)')
    parser.add_argument('--fill-art', action='store_true', dest='fill_art',
                        help='Fetch missing hero/logo/banner art from SteamGridDB for every game')
    parser.add_argument('--if-stale', action='store_true', dest='if_stale',
                        help='With --fill-art: only if it has not run in the last day')
    parser.add_argument('--refetch-art', metavar='SLUG', dest='refetch_art',
                        help='Re-download SteamGridDB artwork for a game and exit')
    parser.add_argument('--reindex', action='store_true',
                        help='Backfill the game registry from a filesystem scan and exit')
    parser.add_argument('--scan', action='store_true',
                        help='Look for extracted repack folders (default: ~/Downloads) '
                             'and suggest them for install, then exit')
    parser.add_argument('--scan-dir', metavar='DIR', dest='scan_dir',
                        help='Folder to search with --scan. Overrides config '
                             '[install] scan_dir; default ~/Downloads')
    parser.add_argument('--detect', metavar='PATH',
                        help="Print a repack folder's detected name/slug and whether "
                             "it's already installed, as JSON, then exit (used by the GUI)")
    parser.add_argument('--steam-account', metavar='ACCOUNT', dest='steam_account',
                        help='Steam account to add shortcuts to (accountid, steamid64, login '
                             'name or persona name). Overrides config [integrations] '
                             'steam_account; see --list-steam-accounts')
    parser.add_argument('--list-steam-accounts', action='store_true',
                        dest='list_steam_accounts',
                        help='List the Steam accounts on this machine, show which one '
                             'shortcuts would go to, and exit')
    parser.add_argument('--doctor', action='store_true',
                        help='Check that wine/umu/proton/steam/deps are all present and exit')
    parser.add_argument('--prune', action='store_true',
                        help='List games deleted outside Nexus; with --yes, drop their '
                             'registry, Steam, Heroic and app-menu entries')
    parser.add_argument('--repair-launchers', metavar='SLUG', nargs='?', const='',
                        dest='repair_launchers',
                        help='Regenerate launch.sh for all managed games (or one SLUG) with '
                             'the current runner/template, then exit')
    g = parser.add_argument_group('per-game settings (launch profile)')
    g.add_argument('--show-launch', metavar='SLUG', dest='show_launch',
                   help="Show a game's Proton, launch options and protongen status "
                        "(add --json for machine-readable output)")
    g.add_argument('--set-launch', nargs=2, metavar=('SLUG', 'OPTIONS'), dest='set_launch',
                   help='Set launch options for a game and apply them to launch.sh, Steam '
                        'and Heroic. OPTIONS is a Steam launch-options string '
                        '(e.g. "PROTON_ENABLE_WAYLAND=1 mangohud %%command%%"), a umu '
                        'command line, or a protongen preset code (protongen:v1:...). '
                        'An empty string clears them.')
    g.add_argument('--proton', nargs=2, metavar=('SLUG', 'BUILD'), dest='set_proton',
                   help="Play a game with a specific installed Proton build ('default' to "
                        "unpin), applied to launch.sh, Steam and Heroic")
    g.add_argument('--from-protongen', metavar='SLUG', dest='from_protongen',
                   help="Import protongen's saved tuning for a game (read-only from "
                        "~/.config/protongen/state.toml) and apply it everywhere")
    g.add_argument('--protongen-default', metavar='SLUG', dest='protongen_default',
                   help="Apply protongen's default profile (its Settings → Save as default "
                        "profile) to a game and apply it everywhere ('all' for every game "
                        "without launch options of its own)")
    g.add_argument('--apply-launch', metavar='SLUG', dest='apply_launch',
                   help="Re-apply a game's settings to launch.sh, its desktop entry, and "
                        "its Steam shortcut / Heroic entry if it has them ('all' for "
                        "every game)")
    g.add_argument('--set-exe', nargs=2, metavar=('SLUG', 'EXE'), dest='set_exe',
                   help='Point a game at a different .exe (when auto-detection picked the '
                        'wrong one) and re-apply')
    g.add_argument('--rename', nargs=2, metavar=('SLUG', 'NAME'), dest='rename',
                   help="Change a game's display name everywhere (slug stays the same)")
    g.add_argument('--list-protons', action='store_true', dest='list_protons',
                   help='List installed Proton builds (with --json for the GUI)')
    g.add_argument('--prefix-tool', nargs=2, metavar=('SLUG', 'TOOL'), dest='prefix_tool',
                   help="Run a tool in a game's prefix: winecfg, regedit, winetricks, or "
                        "kill (stop everything running in it)")
    parser.add_argument('--track-session', nargs=2, metavar=('SLUG', 'LAUNCHER'),
                        dest='track_session', help=argparse.SUPPRESS)
    parser.add_argument('--orphans', action='store_true',
                        help='List stray Steam compatdata prefixes (with --json for the GUI)')
    g2 = parser.add_argument_group('library')
    g2.add_argument('--library', action='store_true',
                    help='List every game: Nexus installs, Steam and Heroic store games '
                         '(with --json for the GUI)')
    g2.add_argument('--owned', action='store_true',
                    help='With --library: also games you own but have not installed '
                         '(Steam via Game-db, Epic/GOG via Heroic)')
    g2.add_argument('--running', action='store_true',
                    help='Which library games are running right now')
    g2.add_argument('--open-url', metavar='URL', dest='open_url',
                    help='Launch/open a steam:// or heroic:// URL (e.g. steam://rungameid/APPID)')
    g3 = parser.add_argument_group('cleanup')
    g3.add_argument('--cleanup-scan', action='store_true', dest='cleanup_scan',
                    help='List what can be cleaned up (stray prefixes, caches, old logs, '
                         'installed repacks, ...) — add --json for the GUI')
    g3.add_argument('--cleanup', nargs='+', metavar='ID',
                    help='Clean up the given candidate ids (from --cleanup-scan): moved to '
                         'the Trash, or quarantined for stray prefixes')
    g3.add_argument('--forever', action='store_true',
                    help='With --cleanup, delete permanently instead of using the Trash')
    g3.add_argument('--quarantine-restore', metavar='NAME', dest='quarantine_restore',
                    help='Put a quarantined prefix back where it came from')
    g4 = parser.add_argument_group('Proton builds')
    g4.add_argument('--protons', action='store_true',
                    help='Installed Proton builds, who uses each, and available updates')
    g4.add_argument('--proton-releases', action='store_true', dest='proton_releases',
                    help='List recent GE-Proton releases')
    g4.add_argument('--refresh', action='store_true',
                    help='Bypass the cache for --protons, --proton-releases, --news, '
                         '--protondb and the Game-db flags')
    g4.add_argument('--proton-install', metavar='TAG', dest='proton_install',
                    help="Download, verify and install a GE-Proton release ('latest' or a tag)")
    g4.add_argument('--proton-remove', metavar='NAME', dest='proton_remove',
                    help='Move a user-installed Proton build to the Trash (refuses if in use)')
    g4.add_argument('--proton-migrate', nargs=2, metavar=('OLD', 'NEW'), dest='proton_migrate',
                    help='Point every Nexus, Steam and Heroic game using OLD at NEW')
    g5 = parser.add_argument_group('tips and protongen')
    g5.add_argument('--protondb', nargs='+', metavar='APPID',
                    help='ProtonDB tiers for Steam appids (cached a day)')
    g5.add_argument('--game-tips', metavar='GAME', dest='game_tips',
                    help='protongen tuning, ProtonDB tier, Steam Deck / anti-cheat / '
                         'PCGamingWiki facts and fitting recipes for a game '
                         '(slug, steam-<appid> or Heroic slug)')
    g5.add_argument('--perf', nargs='+', metavar=('SLUG', 'KEY=VALUE'),
                    help='Performance switches for a game: fps=60 mode=game-performance|'
                         'gamemoderun|off mangohud=on|off wayland=on|off hidraw=on|off '
                         'upscaler=off|fsr4|optiscaler gamescope=WxH[@R][,fsr][,fullscreen]'
                         '[,render=WxH]|off (just the slug: show them)')
    g5.add_argument('--repack-feed', action='store_true', dest='repack_feed',
                    help="Check FitGirl's feed for newer repacks of installed games "
                         "(fetched at most every 6 h; --refresh forces it)")
    g5.add_argument('--repack-index', action='store_true', dest='repack_index',
                    help="Sync FitGirl's whole catalogue and show which wishlist games have "
                         "a repack (incremental, at most every 6 h; --refresh forces it)")
    g5.add_argument('--repack-page', metavar='SLUG', dest='repack_page',
                    help="A repack's sizes and where a grab would download it")
    g5.add_argument('--repack-grab', metavar='SLUG', dest='repack_grab',
                    help="Send a FitGirl repack's magnet link to your torrent client "
                         "(--auto-install installs it when done; --appid tags the wishlist game)")
    g5.add_argument('--appid', metavar='APPID', help='With --repack-grab: the wishlist game')
    g5.add_argument('--repack-reject', nargs='+', metavar=('APPID', 'SLUG'), dest='repack_reject',
                    help="That repack isn't this wishlist game (no SLUG: undo)")
    g5.add_argument('--store-info', metavar='GAME', dest='store_info',
                    help='Steam store facts for a game: trailers, reviews, metacritic, '
                         'similar games (cached)')
    g5.add_argument('--torrent-add', metavar='MAGNET|FILE', dest='torrent_add',
                    help='Hand a magnet link or .torrent to your torrent client '
                         '(config [torrent]); --auto-install installs it once downloaded')
    g5.add_argument('--auto-install', action='store_true', dest='auto_install',
                    help='With --torrent-add: install the repack when the download finishes')
    g5.add_argument('--torrents', action='store_true',
                    help='Downloads added from Nexus, as your torrent client sees them')
    g5.add_argument('--all', action='store_true',
                    help='With --torrents: every torrent in the client, not just Nexus\'s')
    g5.add_argument('--torrent-pause', nargs='+', metavar='HASH', dest='torrent_pause',
                    help='Pause torrents in your client')
    g5.add_argument('--torrent-resume', nargs='+', metavar='HASH', dest='torrent_resume',
                    help='Resume torrents in your client')
    g5.add_argument('--torrent-remove', nargs='+', metavar='HASH', dest='torrent_remove',
                    help='Remove torrents from your client (--delete-data also deletes the files)')
    g5.add_argument('--delete-data', action='store_true', dest='delete_data',
                    help='With --torrent-remove: delete the downloaded files too')
    g5.add_argument('--torrent-test', action='store_true', dest='torrent_test',
                    help='Check that Nexus can talk to your torrent client')
    g5.add_argument('--torrent-detect', action='store_true', dest='torrent_detect',
                    help=argparse.SUPPRESS)
    g5.add_argument('--torrent-done', metavar='HASH', dest='torrent_done',
                    help=argparse.SUPPRESS)
    g5.add_argument('--torrent-forget', metavar='HASH', dest='torrent_forget',
                    help="Stop following a download (the client keeps it)")
    g5.add_argument('--check-repack', metavar='DIR', dest='check_repack',
                    help="Look a repack folder over before installing it (extra programs, "
                         "disguised names, what the installer runs); --clamav also scans it")
    g5.add_argument('--ignore-safety', action='store_true', dest='ignore_safety',
                    help="Install a repack even when --check-repack's look says it's tampered")
    g5.add_argument('--clamav', action='store_true',
                    help='With --check-repack: scan the small files with ClamAV')
    g5.add_argument('--history', action='store_true',
                    help='Play sessions: the last 12 weeks and this year in review')
    g5.add_argument('--notify-test', action='store_true', dest='notify_test',
                    help='Send a test push to the phone targets in config [notify]')
    g5.add_argument('--apply-recipe', nargs=2, metavar=('SLUG', 'RECIPE'), dest='apply_recipe',
                    help="Merge one of protongen's recipes into a game's launch options")
    g5.add_argument('--explain-log', metavar='SLUG', dest='explain_log',
                    help="Explain a game's latest log with protongen's local AI (if enabled)")
    g6 = parser.add_argument_group('news (experimental)')
    g6.add_argument('--news', action='store_true',
                    help='Gaming news: Linux gaming feeds, Proton releases, Steam news for '
                         'your installed games (cached an hour; --refresh to bypass)')
    g6.add_argument('--news-add-feed', nargs='+', metavar=('URL', 'NAME'), dest='news_add',
                    help='Add an RSS/Atom feed to the News page')
    g6.add_argument('--news-remove-feed', metavar='URL', dest='news_remove',
                    help='Remove a feed you added')
    g7 = parser.add_argument_group('Game-db (wishlist deals, releases, backlog)')
    g7.add_argument('--gamedb', action='store_true',
                    help='Everything from Game-db: deals, upcoming releases, backlog '
                         '(cached 45 min; --refresh to bypass)')
    g7.add_argument('--deals', action='store_true',
                    help='Wishlist games on sale, closest to their all-time low first')
    g7.add_argument('--upcoming', action='store_true',
                    help='Unreleased wishlist games with a release countdown')
    g7.add_argument('--backlog', action='store_true',
                    help="Backlog progress and tonight's picks from your owned games")
    g7.add_argument('--time', choices=('lt1h', '1to3', '3plus', 'long'), default='1to3',
                    help='With --backlog: how long you have to play (default 1to3)')
    g7.add_argument('--gamedb-lookup', nargs='+', metavar='APPID', dest='gamedb_lookup',
                    help='Cached Game-db info (price, HLTB, release) for Steam appids; '
                         'never hits the network')
    return parser


def _cmd_restore_saves(args, install_base):
    entry = registry_get(args.restore_saves)
    if not entry or not entry.get('wine_prefix'):
        print(f"'{args.restore_saves}' is not a managed game with a prefix",
              file=sys.stderr)
        sys.exit(1)
    latest = latest_save_backup(args.restore_saves)
    if latest is None:
        print(f"No save backup found for '{args.restore_saves}'")
        sys.exit(1)
    # What's there now is kept first: a restore never loses progress.
    before = backup_prefix_saves(args.restore_saves, Path(entry['wine_prefix']),
                                 suffix=PRE_RESTORE)
    if before and before[2]:
        _report_failed_copies(before[2], "current save file(s)")
        print("Nothing was restored, so nothing was overwritten.", file=sys.stderr)
        sys.exit(1)
    if before:
        print(f"Kept the current saves ({before[1]} file(s)) in {before[0]}")
    n, failed = restore_prefix_saves(args.restore_saves, Path(entry['wine_prefix']))
    print(f"Restored {n} save file(s) from {latest.name} into {entry['wine_prefix']}")
    if failed:
        _report_failed_copies(failed, "save file(s)")
        sys.exit(1)
    sys.exit(0)


def _cmd_launch_via(args, install_base):
    slug, via = args.launch_via
    if via not in ('steam', 'nexus'):
        print("--launch-via takes steam or nexus", file=sys.stderr)
        sys.exit(2)
    cmd_launch_via(require_slug(slug, '--launch-via'), via)
    sys.exit(0)


def _cmd_pause(args, install_base):
    target = args.pause or args.resume or args.pause_toggle or None
    cmd_pause(require_slug(target, '--pause') if target else None,
              'pause' if args.pause else 'resume' if args.resume else 'toggle')
    sys.exit(0)


def _cmd_remove_heroic(args, install_base):
    use_logger(Logger())
    remove_heroic_entry(args.remove_heroic,
                        exe=(registry_get(args.remove_heroic) or {}).get('exe'))
    sys.exit(0)


def _cmd_gamedb_lookup(args, install_base):
    from nexus import gamedb
    ids = [int(a) for a in args.gamedb_lookup if a.isdigit()]
    print(json.dumps({str(k): v for k, v in gamedb.lookup(ids).items()}))
    sys.exit(0)


def _cmd_notify_test(args, install_base):
    from nexus import notify
    if not notify.configured():
        print("Nothing to send to: set [notify] ntfy = \"https://…\" and/or "
              "kdeconnect = true in config.toml.", file=sys.stderr)
        sys.exit(1)
    errs = notify.push("Nexus", "Test notification — pushes work.")
    for e in errs:
        print(f"  [!] {e}", file=sys.stderr)
    print("Sent." if not errs else "Some targets failed.")
    sys.exit(1 if errs else 0)


def _cmd_orphans(args, install_base):
    orphans = find_orphan_compatdata(install_base)
    rows = [{**o, 'path': str(o['path'])} for o in orphans]
    if args.json:
        print(json.dumps({'orphans': rows}))
    else:
        for o in rows:
            print(f"  {o['path']}  {o['size_bytes'] / 1e9:.2f} GB  ({o['reason']})"
                  + ("  SAVE DATA?" if o['has_savedata'] else ''))
    sys.exit(0)


def _cmd_apply_launch(args, install_base):
    config = load_config()
    slugs = (sorted(load_registry()['games']) if args.apply_launch == 'all'
             else [args.apply_launch])
    for slug in slugs:
        cmd_apply(slug, config)
    sys.exit(0)


# Every command flag and what runs for it, checked in this order: the first
# flag given wins, so the order is part of the behaviour. A row is (dest, or a
# test of the parsed args; handler(args, install_base)). nexus-cli exits 0
# after a handler unless the handler exits itself (a few pass on their own
# status with sys.exit). With no command flag, the args name a repack to
# install (cmd_install).
COMMANDS = (
    ('list_steam_accounts', lambda a, base: cmd_list_steam_accounts()),
    ('doctor', lambda a, base: cmd_doctor(base, as_json=a.json)),
    ('prune', lambda a, base: sys.exit(cmd_prune(yes=a.yes, as_json=a.json))),
    (lambda a: a.repair_launchers is not None,
     lambda a, base: cmd_repair_launchers(base, load_config(), a.repair_launchers or None)),
    ('list', lambda a, base: cmd_list(base, as_json=a.json, compute_sizes=a.sizes)),
    ('reindex', lambda a, base: cmd_reindex(base)),
    ('scan', lambda a, base: cmd_scan(resolve_scan_dirs(a.scan_dir), as_json=a.json)),
    ('detect', lambda a, base: cmd_detect(Path(a.detect).expanduser(), a.name)),
    ('achievements', lambda a, base: sys.exit(cmd_achievements(a.achievements, a.json))),
    ('trash_repack_dir',
     lambda a, base: sys.exit(cmd_trash_repack(Path(a.trash_repack_dir).expanduser()))),
    ('discard_incomplete',
     lambda a, base: sys.exit(cmd_discard_incomplete(base, a.discard_incomplete))),
    ('uninstall',
     lambda a, base: cmd_uninstall(base, a.uninstall, yes=a.yes, save_backup=a.save_backup)),
    ('restore_saves', _cmd_restore_saves),
    ('check_integrity',
     lambda a, base: cmd_check_integrity(a.check_integrity, rebaseline=a.rebaseline)),
    ('clean_orphan', lambda a, base: cmd_clean_orphan_compatdata(base, a.clean_orphan, yes=a.yes)),
    ('add_steam',
     lambda a, base: add_existing_to_steam(Path(a.add_steam), a.name, a, load_config())),
    ('launch_via', _cmd_launch_via),
    (lambda a: a.pause or a.resume or a.pause_toggle is not None, _cmd_pause),
    ('benchmark',
     lambda a, base: cmd_benchmark(base, require_slug(a.benchmark, '--benchmark'), a.bench_seconds, a.bench_delay)),
    ('bench_results',
     lambda a, base: cmd_bench_results(require_slug(a.bench_results, '--bench-results'), a.json)),
    (lambda a: a.sunshine or a.remove_sunshine,
     lambda a, base: cmd_sunshine(a.sunshine or a.remove_sunshine, remove=bool(a.remove_sunshine))),
    ('launch', lambda a, base: cmd_launch(base, a.launch, force=a.force, wait=a.wait)),
    ('steam', lambda a, base: cmd_add_steam(a.steam, load_config())),
    ('remove_steam', lambda a, base: remove_steam_shortcut(a.remove_steam)),
    ('heroic', lambda a, base: cmd_add_heroic(a.heroic, load_config())),
    ('remove_heroic', _cmd_remove_heroic),
    ('refetch_art', lambda a, base: cmd_refetch_art(a.refetch_art, load_config())),
    ('art_variants',
     lambda a, base: cmd_art_variants(a.art_variants, a.kind, max(0, a.page), a.json)),
    ('sgdb_search', lambda a, base: cmd_sgdb_search(a.sgdb_search, a.json)),
    ('set_sgdb', lambda a, base: cmd_set_sgdb(a.set_sgdb, a.sgdb_gid, a.sgdb_name, a.reset)),
    ('set_art', lambda a, base: cmd_set_art(a.set_art, a.kind, a.url, a.reset)),
    ('fill_art', lambda a, base: cmd_fill_art(base, a.json, a.if_stale)),
    ('news', lambda a, base: cmd_news(a.refresh, a.json)),
    ('news_add',
     lambda a, base: cmd_news_feed('add', a.news_add[0], ' '.join(a.news_add[1:]) or None)),
    ('news_remove', lambda a, base: cmd_news_feed('remove', a.news_remove, None)),
    ('gamedb', lambda a, base: cmd_gamedb('all', a.refresh, a.json, a.time)),
    ('deals', lambda a, base: cmd_gamedb('deals', a.refresh, a.json, a.time)),
    ('upcoming', lambda a, base: cmd_gamedb('upcoming', a.refresh, a.json, a.time)),
    ('backlog', lambda a, base: cmd_gamedb('backlog', a.refresh, a.json, a.time)),
    ('gamedb_lookup', _cmd_gamedb_lookup),
    ('protondb', lambda a, base: cmd_protondb(a.protondb, a.refresh, a.json)),
    ('game_tips', lambda a, base: cmd_game_tips(a.game_tips, a.json)),
    ('perf',
     lambda a, base: cmd_perf(require_slug(a.perf[0], '--perf'), a.perf[1:], load_config())),
    ('repack_feed', lambda a, base: cmd_repack_feed(a.refresh, a.json)),
    ('repack_index', lambda a, base: cmd_repack_index(a.refresh, a.json)),
    ('repack_page', lambda a, base: cmd_repack_page(a.repack_page, a.json)),
    ('repack_grab',
     lambda a, base: cmd_repack_grab(a.repack_grab, a.appid, a.auto_install, a.json)),
    ('repack_reject', lambda a, base: cmd_repack_reject(a.repack_reject)),
    (lambda a: a.torrent_add or a.torrents or a.torrent_done or
                a.torrent_forget or a.torrent_pause or a.torrent_resume or
                a.torrent_remove or a.torrent_test or a.torrent_detect,
     lambda a, base: cmd_torrent(a)),
    ('check_repack', lambda a, base: cmd_check_repack(a.check_repack, a.json, a.clamav)),
    ('history', lambda a, base: cmd_history(a.json)),
    ('store_info', lambda a, base: cmd_store_info(a.store_info, a.json, a.refresh)),
    ('notify_test', _cmd_notify_test),
    ('apply_recipe', lambda a, base: cmd_apply_recipe(*a.apply_recipe, load_config())),
    ('explain_log', lambda a, base: cmd_explain_log(a.explain_log)),
    ('protons', lambda a, base: cmd_protons(load_config(), a.json, a.refresh)),
    ('proton_releases', lambda a, base: cmd_proton_releases(a.refresh, a.json)),
    ('proton_install', lambda a, base: cmd_proton_install(a.proton_install, a.force)),
    ('proton_remove', lambda a, base: cmd_proton_remove(a.proton_remove, a.force, load_config())),
    ('proton_migrate', lambda a, base: cmd_proton_migrate(*a.proton_migrate, load_config())),
    ('cleanup_scan', lambda a, base: cmd_cleanup_scan(base, a.json)),
    ('cleanup', lambda a, base: cmd_cleanup(base, a.cleanup, a.forever, a.yes)),
    ('quarantine_restore', lambda a, base: cmd_quarantine_restore(a.quarantine_restore)),
    ('library', lambda a, base: cmd_library(base, as_json=a.json, owned=a.owned)),
    ('running', lambda a, base: cmd_running(base, a.json)),
    ('open_url', lambda a, base: cmd_open_url(a.open_url)),
    ('track_session', lambda a, base: cmd_track_session(*a.track_session)),
    ('orphans', _cmd_orphans),
    ('list_protons', lambda a, base: cmd_list_protons(load_config(), as_json=a.json)),
    ('prefix_tool',
     lambda a, base: cmd_prefix_tool(a.prefix_tool[0], a.prefix_tool[1], load_config())),
    ('show_launch', lambda a, base: cmd_show_launch(a.show_launch, load_config(), as_json=a.json)),
    ('set_launch',
     lambda a, base: cmd_set_launch(a.set_launch[0], a.set_launch[1], load_config())),
    ('set_proton',
     lambda a, base: cmd_set_proton(a.set_proton[0], a.set_proton[1], load_config())),
    ('from_protongen', lambda a, base: cmd_from_protongen(a.from_protongen, load_config())),
    ('protongen_default',
     lambda a, base: cmd_protongen_default(a.protongen_default, load_config())),
    ('apply_launch', _cmd_apply_launch),
    ('set_exe', lambda a, base: cmd_set_exe(a.set_exe[0], a.set_exe[1], load_config())),
    ('rename', lambda a, base: cmd_rename(a.rename[0], a.rename[1], load_config())),
)


def _main() -> None:
    args = build_parser().parse_args()
    _check_slug_args(args)

    # A GUI front-end's Cancel button sends SIGTERM to this process; make sure
    # that also stops whatever subprocess run() is currently waiting on
    # (umu-run/wine/winetricks), instead of just orphaning it.
    signal.signal(signal.SIGTERM, _handle_sigterm)
    for line in NX.migrate():
        print(f"  Moved {line} (old path kept as a symlink).", file=sys.stderr)
    # Under the GUI stdout is a pipe (block-buffered by default), which held
    # status lines like "[3/4] Running setup.exe" back for the whole install.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # Which Steam account gets the shortcuts. CLI beats config; both beat
    # auto-detection. Must be set before any find_steam_userdata() call.
    set_steam_account_pin(args.steam_account or load_config().get('steam_account'))

    install_base = resolve_install_base(args.install_dir)

    for when, handler in COMMANDS:
        if when(args) if callable(when) else getattr(args, when):
            handler(args, install_base)
            sys.exit(0)
    cmd_install(args, install_base)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import (cmd_apply, cmd_from_protongen, cmd_perf, cmd_protongen_default,
    cmd_set_launch, cmd_set_proton, cmd_show_launch)  # noqa: E402
from .config import load_config, resolve_install_base, resolve_scan_dirs  # noqa: E402
from .doctor import (cmd_clean_orphan_compatdata, cmd_cleanup, cmd_cleanup_scan, cmd_doctor,
    cmd_list_steam_accounts, cmd_prune, cmd_quarantine_restore, find_orphan_compatdata)  # noqa: E402
from .downloads import (cmd_check_repack, cmd_repack_feed, cmd_repack_grab, cmd_repack_index,
    cmd_repack_page, cmd_repack_reject, cmd_torrent)  # noqa: E402
from .gamecmds import (add_existing_to_steam, cmd_achievements, cmd_add_heroic, cmd_add_steam,
    cmd_detect, cmd_discard_incomplete, cmd_prefix_tool, cmd_refetch_art, cmd_reindex,
    cmd_rename, cmd_repair_launchers, cmd_scan, cmd_set_exe, cmd_trash_repack)  # noqa: E402
from .heroic import remove_heroic_entry  # noqa: E402
from .info import (cmd_apply_recipe, cmd_explain_log, cmd_game_tips, cmd_gamedb, cmd_history,
    cmd_news, cmd_news_feed, cmd_store_info)  # noqa: E402
from .manage import cmd_library, cmd_list, cmd_running  # noqa: E402
from .protoncmds import (cmd_list_protons, cmd_proton_install, cmd_proton_migrate,
    cmd_proton_releases, cmd_proton_remove, cmd_protondb, cmd_protons)  # noqa: E402
from .registry import load_registry, registry_get  # noqa: E402
from .running import cmd_bench_results, cmd_benchmark, cmd_open_url, cmd_pause  # noqa: E402
from .sgdb import (cmd_art_variants, cmd_fill_art, cmd_set_art, cmd_set_sgdb, cmd_sgdb_search,
    SGDB_KINDS)  # noqa: E402
from .steam import (_report_failed_copies, backup_prefix_saves, cmd_check_integrity,
    latest_save_backup, PRE_RESTORE, restore_prefix_saves, set_steam_account_pin)  # noqa: E402
from .sunshine import (cmd_launch, cmd_launch_via, cmd_sunshine, cmd_track_session,
    cmd_uninstall, remove_steam_shortcut)  # noqa: E402
from .install import cmd_install  # noqa: E402
