"""Per-game maintenance commands: prefix tools, renames, re-adding to
Steam/Heroic, reindexing, scanning for repacks."""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from nexus import paths as NX

from .common import (ARTWORK_DIR, dir_size_bytes, LAUNCHERS_DIR, log, Logger, make_slug,
    STEAM_ROOT, use_logger, valid_slug, wine_env)


PREFIX_TOOLS = ('winecfg', 'regedit', 'winetricks', 'kill')


def cmd_prefix_tool(slug: str, tool: str, config: dict) -> None:
    """Run a Wine tool against a game's prefix with the same runner + Proton the
    game plays with: winecfg, regedit, winetricks (GUI), or kill (stop every
    process in the prefix via wineserver -k)."""
    entry = _require_entry(slug)
    if tool not in PREFIX_TOOLS:
        print(f"Unknown tool '{tool}' (choose: {', '.join(PREFIX_TOOLS)})", file=sys.stderr)
        sys.exit(1)
    exe, game_dir, wine_prefix = _entry_paths(entry)
    if not wine_prefix or not wine_prefix.is_dir():
        print(f"'{slug}' has no prefix on disk", file=sys.stderr)
        sys.exit(1)
    use_logger(Logger())
    proton = resolve_game_proton(entry, config)
    runner = select_runner(config.get('runner', 'auto'), proton)
    if runner != 'wine' and not game_dir:
        runner = 'wine'
    env = {**wine_env(), 'WINEPREFIX': str(wine_prefix), 'WINEDEBUG': '-all'}
    if tool == 'kill':
        bins = _proton_wine_bins(proton) if proton and runner != 'wine' else {}
        server = bins.get('WINESERVER') or shutil.which('wineserver') or 'wineserver'
        try:
            rc = subprocess.run([server, '-k'], env=env, timeout=30).returncode
        except subprocess.TimeoutExpired:
            rc = -1
        print(f"Stopped processes in {wine_prefix}" if rc == 0
              else f"wineserver -k exited {rc} (nothing running?)")
        return
    if runner == 'umu':
        env.update(_umu_env({}, wine_prefix, proton))
        cmd = ['umu-run', tool]
    elif runner == 'proton':
        env.update({'STEAM_COMPAT_DATA_PATH': str(game_dir),
                    'STEAM_COMPAT_CLIENT_INSTALL_PATH': str(STEAM_ROOT)})
        if tool == 'winetricks':
            env.update(_proton_wine_bins(proton))
            cmd = ['winetricks']
        else:
            cmd = [str(proton), 'run', tool]
    else:
        cmd = [tool] if tool == 'winetricks' else ['wine', tool]
    print(f"Running {tool} for '{slug}' ({' '.join(cmd)})...")
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_set_exe(slug: str, exe_path: str, config: dict) -> None:
    """Point a game at a different executable (the largest-exe guess was
    wrong), then re-apply. The Steam appid is kept, so saves/art survive."""
    entry = _require_entry(slug)
    exe = Path(exe_path).expanduser().resolve()
    if not exe.is_file() or exe.suffix.lower() != '.exe':
        print(f"Error: '{exe}' is not an .exe file", file=sys.stderr)
        sys.exit(1)
    update_registry_entry(slug, exe=str(exe))
    print(f"'{slug}' now launches {exe}")
    cmd_apply(slug, config)


def cmd_rename(slug: str, name: str, config: dict) -> None:
    """Change a game's display name everywhere (the slug, and so the install
    dir and Steam appid, stay the same)."""
    _require_entry(slug)
    name = ' '.join(name.split())
    if not name:
        print("Error: empty name", file=sys.stderr)
        sys.exit(1)
    update_registry_entry(slug, display_name=name)
    print(f"'{slug}' renamed to {name!r}")
    cmd_apply(slug, config)


def _local_artwork(slug: str) -> dict:
    """Build an artwork dict from already-downloaded files in ARTWORK_DIR/<slug>."""
    out: dict[str, Path] = {}
    d = ARTWORK_DIR / slug
    if d.is_dir():
        for kind in ('cover', 'hero', 'logo', 'icon'):
            if hit := _art_file(d, kind):
                out[kind] = hit
    return out


def cmd_add_steam(slug: str, config: dict) -> None:
    """Add a Steam shortcut for an already-managed game (by slug). Steam runs
    the game .exe through a forced Proton, with the prefix redirected to the
    install."""
    entry = registry_get(slug)
    if not entry or not entry.get('launcher'):
        print(f"'{slug}' is not a managed game with a launcher", file=sys.stderr)
        sys.exit(1)
    use_logger(Logger())
    artwork = _local_artwork(slug)
    if not artwork and config.get('api_key'):
        artwork = fetch_artwork(entry['display_name'], slug, config) or {}
    appid = sync_steam(entry, config, resolve_game_proton(entry, config), artwork or None)
    if not appid:
        sys.exit(1)
    set_registry_steam_appid(slug, appid)


def cmd_add_heroic(slug: str, config: dict) -> None:
    """Add an already-managed game (by slug) to Heroic as a sideloaded app,
    using its registered exe + prefix + cached artwork."""
    entry = registry_get(slug)
    if not entry:
        print(f"'{slug}' is not a managed game", file=sys.stderr)
        sys.exit(1)
    use_logger(Logger())
    artwork = _local_artwork(slug)
    if not artwork and config.get('api_key'):
        artwork = fetch_artwork(entry['display_name'], slug, config) or {}
    app_name = sync_heroic(entry, config, resolve_game_proton(entry, config), artwork or None)
    if app_name:
        update_registry_entry(slug, heroic_app_name=app_name)


def cmd_refetch_art(slug: str, config: dict) -> None:
    entry = registry_get(slug)
    if not entry:
        print(f"'{slug}' is not a managed game", file=sys.stderr)
        sys.exit(1)
    log_local = Logger()
    use_logger(log_local)
    art = fetch_artwork(entry['display_name'], slug, config)
    if not art:
        print("No artwork (missing API key, no match, or offline).")
        return
    print(f"Fetched: {', '.join(sorted(k for k in art if not k.endswith('_url')))}")
    appid = entry.get('steam_appid')
    if appid:
        cfg_dir = find_steam_userdata(announce=True)
        if cfg_dir:
            _copy_grid_art(cfg_dir / 'grid', appid, art)
            print("Updated Steam grid artwork.")


def _launcher_display_name(launch_path: Path, slug: str,
                           steam_names: set[str] | None = None) -> str:
    """Best display name for a game: prefer the launcher's '# Game: <name>'
    comment, then a matching Steam shortcut AppName, then a prettified slug."""
    try:
        for line in launch_path.read_text().splitlines():
            if line.startswith('# Game: '):
                return line[len('# Game: '):].strip() or slug
    except OSError:
        pass
    for n in (steam_names or ()):
        if make_slug(n) == slug:
            return n
    return slug.replace('-', ' ').title()


def cmd_reindex(install_base: Path) -> None:
    """Backfill the registry from a filesystem scan of managed launchers."""
    steam_names = _steam_appnames()
    added = 0
    for slug, game_dir in _installed_games(install_base):
        if registry_get(slug):
            continue
        launch = game_dir / 'launch.sh'
        exe = find_game_exe(game_dir / 'pfx') or find_game_exe(game_dir)
        register_game(
            slug=slug, display_name=_launcher_display_name(launch, slug, steam_names),
            exe=exe, compatdata=game_dir, wine_prefix=game_dir / 'pfx', launcher=launch,
            source='install', install_base=install_base, steam_appid=None,
            has_desktop=_desktop_path(slug).exists(), artwork_dir=ARTWORK_DIR / slug)
        added += 1
    # Also register any external launchers (from --add-steam) not under install_base
    for sh in sorted(LAUNCHERS_DIR.glob('*.sh')) if LAUNCHERS_DIR.is_dir() else []:
        slug = sh.stem
        if registry_get(slug):
            continue
        register_game(
            slug=slug, display_name=_launcher_display_name(sh, slug, steam_names),
            exe=None, compatdata=None, wine_prefix=None, launcher=sh, source='add-steam',
            install_base=install_base, steam_appid=None,
            has_desktop=_desktop_path(slug).exists(), artwork_dir=ARTWORK_DIR / slug)
        added += 1
    print(f"Reindexed: {added} game(s) added to the registry.")


def scan_repacks(scan_dir: Path) -> list[dict]:
    """Look for extracted FitGirl repack folders directly under scan_dir (e.g.
    ~/Downloads) — a folder counts as a candidate if it has a setup.exe, the
    same signal find_setup_exe uses to run the installer. Cheap: one dir
    listing, plus a size scan only for folders that pass that check.

    `complete` is False while a download is still running; `update_for`
    names the installed game this is a different release of (its
    fingerprint differs from the one recorded at install)."""
    if not scan_dir.is_dir():
        return []
    known = load_registry()['games']
    candidates = []
    try:
        entries = sorted(scan_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or not find_setup_exe(entry):
            continue
        info = repack_info(entry)
        slug = info['slug']
        fp = repack_fingerprint(entry)
        recorded = (known.get(slug) or {}).get('repack_fingerprint')
        try:
            mtime = int(entry.stat().st_mtime)
        except OSError:
            mtime = None
        candidates.append({
            'path': str(entry),
            'name': info['name'],
            'slug': slug,
            'version': info['version'],
            'size_bytes': dir_size_bytes(entry),
            'installed': slug in known,
            'fingerprint': fp,
            'mtime': mtime,
            'complete': repack_complete(entry),
            'update_for': slug if (slug in known and fp and recorded and fp != recorded)
                          else None,
            'same_release': bool(fp and recorded and fp == recorded),
        })
    return candidates


def cmd_scan(scan_dirs: list[Path], as_json: bool = False) -> None:
    candidates = [c for d in scan_dirs for c in scan_repacks(d)]
    if as_json:
        print(json.dumps({'scan_dir': str(scan_dirs[0]),
                          'scan_dirs': [str(d) for d in scan_dirs],
                          'candidates': candidates}))
        return
    where = ', '.join(str(d) for d in scan_dirs)
    if not candidates:
        print(f"No repack folders found in {where}")
        return
    print(f"Repack folders found in {where}:\n")
    for c in candidates:
        tag = ("  (update for the installed game)" if c['update_for'] else
               "  (already installed)" if c['installed'] else "")
        if not c['complete']:
            tag += "  (still downloading)"
        print(f"  {c['name']}  —  {c['size_bytes'] / 1e9:.1f} GB{tag}")
        print(f"    {c['path']}")
    print(f"\nInstall one with:  nexus-cli \"<path>\"")


def steam_account_id() -> int | None:
    """The Steam account Nexus works with (see resolve_steam_account)."""
    cfg = find_steam_userdata()
    try:
        return int(cfg.parent.name) if cfg else None
    except ValueError:
        return None


ACHIEVEMENT_COUNTS = NX.CACHE_DIR / 'achievements.json'


def cmd_achievements(ident: str, as_json: bool) -> int:
    """Achievements from Steam's own stats cache: one game's list, or the
    newest unlocks across every game the account has run."""
    from nexus import achievements as A
    entry = registry_get(ident) if valid_slug(ident) else None
    if entry is not None:                      # a repack: its Steam emulator's files
        from nexus import emuach, compat
        r = (emuach.status(Path(entry['wine_prefix']), STEAM_ROOT,
                           appid=compat.store_appid(entry)) if entry.get('wine_prefix') else None) \
            or {'appid': None, 'total': 0, 'unlocked': 0, 'items': []}
        if as_json:
            print(json.dumps(r))
            return 0
        if not r['total']:
            print("No achievement file from the game's Steam emulator yet.")
            return 0
        print(f"{r['unlocked']} of {r['total']} achievements ({r.get('emulator')})")
        for a in r['items']:
            mark = '✓' if a['unlocked'] else ' '
            print(f"  [{mark}] {a['title'] if a['unlocked'] or not a['hidden'] else 'Hidden'}")
        return 0
    account = steam_account_id()
    if account is None:
        print("No Steam account found.", file=sys.stderr)
        return 1
    if ident == 'recent':
        stats = STEAM_ROOT / 'appcache' / 'stats'
        appids = [int(p.stem.split('_')[1]) for p in stats.glob('UserGameStatsSchema_*.bin')
                  if p.stem.split('_')[1].isdigit()]
        out = {'recent': A.recent(STEAM_ROOT, account, appids, n=12)}
        if as_json:
            print(json.dumps(out))
        else:
            for a in out['recent']:
                when = datetime.fromtimestamp(a['time']).strftime('%Y-%m-%d')
                print(f"  {when}  {a['title']}  ({a['appid']})")
        return 0
    appid = ident.removeprefix('steam-')
    if not appid.isdigit():
        print(f"Not a Steam game: {ident!r} (achievements come from Steam)", file=sys.stderr)
        return 1
    r = A.achievements(STEAM_ROOT, account, int(appid)) or \
        {'appid': int(appid), 'total': 0, 'unlocked': 0, 'items': []}
    if as_json:
        print(json.dumps(r))
        return 0
    print(f"{r['unlocked']} of {r['total']} achievements")
    for a in r['items']:
        mark = '✓' if a['unlocked'] else ' '
        title = a['title'] if a['unlocked'] or not a['hidden'] else "Hidden achievement"
        print(f"  [{mark}] {title}")
    return 0


def cmd_trash_repack(path: Path) -> int:
    """Move a repack folder to the Trash. Only a folder that looks like one
    (it has a setup.exe) — this is reachable from a GUI button."""
    from nexus import cleanup as C
    if not path.is_dir() or not find_setup_exe(path):
        print(f"Not a repack folder (no setup.exe): {path}", file=sys.stderr)
        return 1
    try:
        C.trash(path)
    except OSError as e:
        print(f"Could not move {path} to the Trash: {e}", file=sys.stderr)
        return 1
    print(f"Moved {path.name} to the Trash.")
    return 0


def cmd_discard_incomplete(install_base: Path, slug: str) -> int:
    """Trash an unfinished install's folder — only one that has the marker and
    isn't a library game, so a working install can never go this way."""
    from nexus import cleanup as C
    game_dir = install_base / slug
    if slug in load_registry()['games']:
        print(f"'{slug}' is in the library; uninstall it instead.", file=sys.stderr)
        return 1
    if read_incomplete_marker(game_dir) is None:
        print(f"No unfinished install at {game_dir}", file=sys.stderr)
        return 1
    try:
        C.trash(game_dir)
    except OSError as e:
        print(f"Could not move {game_dir} to the Trash: {e}", file=sys.stderr)
        return 1
    print(f"Moved the unfinished install of {slug} to the Trash.")
    return 0


def cmd_detect(path: Path, name_override: str | None) -> None:
    """Print a repack folder's detected name/slug and whether it's already a
    managed install, as JSON. Read-only; used by the GUI to warn before a
    duplicate install starts."""
    if name_override:
        name, slug = name_override, make_slug(name_override)
    elif path.is_dir():
        name, slug = detect_game_name(path)
    else:
        name, slug = path.name, make_slug(path.name)
    entry = load_registry()['games'].get(slug)
    fp = repack_fingerprint(path) if path.is_dir() else None
    recorded = (entry or {}).get('repack_fingerprint')
    print(json.dumps({'path': str(path), 'name': name, 'slug': slug,
                      'installed': entry is not None,
                      'update': bool(fp and recorded and fp != recorded)}))


def cmd_repair_launchers(install_base: Path, config: dict, slug: str | None = None) -> None:
    """Regenerate launch.sh for managed games from their registry entry, launch
    profile and Proton (resolve_game_proton — the same build Steam and Heroic
    use, so a repair never silently moves a game to a different Proton). Does
    not touch Steam/Heroic — use --apply-launch <slug> for that."""
    use_logger(Logger())
    reg = load_registry()
    if slug and slug not in reg['games']:
        print(f"'{slug}' is not a managed game", file=sys.stderr)
        sys.exit(1)
    entries = [reg['games'][slug]] if slug else list(reg['games'].values())
    n = 0
    for e in entries:
        proton = resolve_game_proton(e, config)
        if proton and not e.get('proton'):
            update_registry_entry(e['slug'], proton=proton.parent.name)
        path = regen_launch_sh(e, config, proton)
        if path:
            print(f"  regenerated {path}  (Proton: {proton.parent.name if proton else 'none'})")
            n += 1
    print(f"Repaired {n} launcher(s).")


def add_existing_to_steam(path: Path, name: str | None, args, config: dict) -> None:
    """Register an already-installed game (by dir or exe) into Steam + desktop
    menu, generating a launcher for its current prefix. No files are moved."""
    use_logger(Logger())  # plain console logger for this standalone command

    path = path.expanduser().resolve()
    if path.is_dir():
        exe = find_exe_in_dir(path)
        if not exe:
            print(f"Error: no game .exe found under {path}", file=sys.stderr)
            sys.exit(1)
    elif path.suffix.lower() == '.exe' and path.exists():
        exe = path
    else:
        print(f"Error: '{path}' is not a game folder or .exe", file=sys.stderr)
        sys.exit(1)

    display_name = name or exe.stem
    slug = make_slug(display_name)
    clash = registry_get(slug)
    if clash and clash.get('exe') and Path(clash['exe']) != exe:
        print(f"Error: '{slug}' is already a managed game ({clash['exe']}). "
              f"Pick a different --name.", file=sys.stderr)
        sys.exit(1)
    compatdata, wine_prefix = detect_compatdata(exe)
    proton = resolve_game_proton(clash, config)
    runner = select_runner(config.get('runner', 'auto'), proton)
    # umu/proton launchers only make sense for an exe inside a Proton-style
    # prefix (pfx/ ancestor); a raw-wine prefix falls back to plain wine.
    if runner in ('umu', 'proton') and not compatdata:
        runner = 'wine'
    runner_label = (f"umu-run + {proton.parent.name}" if runner == 'umu'
                    else proton.parent.name if runner == 'proton'
                    else 'Wine (system)')

    log.section("Add to Steam")
    log.print(f"  Game:        {display_name}")
    log.print(f"  Slug:        {slug}")
    log.print(f"  Exe:         {exe}")
    log.print(f"  Runner:      {runner_label}")
    if compatdata:
        log.print(f"  Compat data: {compatdata}")
    log.print(f"  Wine prefix: {wine_prefix}")

    if proton and not compatdata:
        log.print("\n  [!] Proton is available but this exe isn't inside a Proton")
        log.print("      prefix (no pfx/ ancestor) — the launcher will use raw wine.")

    launch = write_launch_script(LAUNCHERS_DIR / f"{slug}.sh", compatdata,
                                 wine_prefix, exe, slug, proton, display_name,
                                 runner=runner, launch=(clash or {}).get('launch'))
    log.print(f"\n  Launcher:    {launch}")

    artwork = None
    if not args.no_artwork:
        log.print("\nFetching artwork from SteamGridDB...")
        artwork = _best_effort("Artwork", fetch_artwork, display_name, slug, config)
        if artwork:
            log.print(f"  Got: {', '.join(sorted(k for k in artwork if not k.endswith('_url')))}")
        else:
            log.print("  No artwork (missing API key, no match, or offline) — skipping.")

    steam_appid_val = None
    if not args.no_steam and config.get('steam', True):
        if exe and compatdata:
            prof = (clash or {}).get('launch')
            steam_appid_val = add_steam_shortcut_native(
                display_name, slug, exe, compatdata, proton, artwork,
                launch_options=(format_launch_options(prof)
                                if profile_owns_options(prof) else None))
        else:
            log.print("  [i] Steam: need a game exe inside a Proton prefix — skipping.")
    desktop_path = None
    if not args.no_desktop and config.get('desktop', True):
        desktop_path = write_desktop_entry(display_name, slug, launch, artwork)
    heroic_app_name = None
    if not args.no_heroic and config.get('heroic', True):
        prof = (clash or {}).get('launch')
        heroic_app_name = add_heroic_entry(
            display_name, slug, exe, wine_prefix, proton, artwork,
            force_proton=_proton_is_explicit(clash or {}, config),
            launch=prof if profile_owns_options(prof) else None)

    register_game(
        slug=slug, display_name=display_name, exe=exe, compatdata=compatdata,
        wine_prefix=wine_prefix, launcher=launch, source='add-steam',
        install_base=None, steam_appid=steam_appid_val,
        has_desktop=bool(desktop_path), artwork_dir=ARTWORK_DIR / slug,
        heroic_app_name=heroic_app_name,
        proton=proton.parent.name if proton else None)

    log.print("\nDone. Restart Steam/Heroic to see the game in your library.")


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import (_entry_paths, _proton_is_explicit, _require_entry, cmd_apply,
    profile_owns_options, regen_launch_sh, sync_heroic, sync_steam)  # noqa: E402
from .desktop import _desktop_path, write_desktop_entry  # noqa: E402
from .install import _best_effort  # noqa: E402
from .heroic import add_heroic_entry  # noqa: E402
from .launchprofile import format_launch_options  # noqa: E402
from .manage import _installed_games, _steam_appnames  # noqa: E402
from .registry import (load_registry, register_game, registry_get, set_registry_steam_appid,
    update_registry_entry)  # noqa: E402
from .repack import (detect_compatdata, detect_game_name, find_exe_in_dir, find_game_exe,
    find_setup_exe, read_incomplete_marker, repack_complete, repack_fingerprint, repack_info)  # noqa: E402
from .runner import (_proton_wine_bins, _umu_env, resolve_game_proton, select_runner,
    write_launch_script)  # noqa: E402
from .sgdb import _art_file, fetch_artwork  # noqa: E402
from .steam import _copy_grid_art, add_steam_shortcut_native, find_steam_userdata  # noqa: E402
