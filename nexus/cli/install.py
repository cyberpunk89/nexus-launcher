"""Installing a repack: the step-by-step flow behind `nexus-cli <repack dir>`."""

import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from .common import (ARTWORK_DIR, dir_size_bytes, disable_cow, emit_event, existing_ancestor,
    log, LOG_DIR, Logger, make_slug, run, STEAM_ROOT, use_logger, win_path, WINE_ABORTS)


def _best_effort(what: str, fn, *a, **kw):
    """An integration after the game is installed (artwork, Steam, Heroic,
    the app menu): a failure there is reported, never fatal — the install
    still gets registered."""
    try:
        return fn(*a, **kw)
    except Exception as e:                 # noqa: BLE001
        log.print(f"  [!] {what} failed ({type(e).__name__}: {e}). The game is installed; "
                  f"add it again later from Nexus.")
        return None


def safety_gate(args, repack_dir: Path) -> None:
    """Stop (exit 1) before anything happens when the repack looks tampered
    with, unless --ignore-safety. Shared by the Linux and Windows installs."""
    if args.ignore_safety:
        return
    from nexus import safety
    verdict = safety.check(repack_dir)
    if verdict['verdict'] == 'danger':
        print("  [!] This repack looks tampered with:", file=sys.stderr)
        for f in verdict['findings']:
            if f['level'] == 'danger':
                print(f"      {f['file']}: {f['why']}", file=sys.stderr)
        print("      Not installing it. If you're sure it's fine, pass --ignore-safety.",
              file=sys.stderr)
        sys.exit(1)


def cmd_install(args, install_base: Path) -> None:
    """Install a repack: verify, prefix, setup.exe (silent unless
    --interactive), launch.sh, then register the game and add it to Steam,
    Heroic and the app menu. What nexus-cli does when no command flag is
    given (args.repack_dir is the repack folder)."""
    repack_dir = Path(args.repack_dir).resolve()

    if not repack_dir.is_dir():
        print(f"Error: '{repack_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)
    safety_gate(args, repack_dir)

    if args.from_torrent:
        _torrent_mark(args.from_torrent, install_started=time.strftime('%Y-%m-%dT%H:%M:%S'))

    # Warn about characters that break Wine's internal shell path handling
    BAD_CHARS = set('&|;<>()^%!')
    bad = [c for c in BAD_CHARS if c in str(repack_dir)]
    if bad:
        chars = ' '.join(f'"{c}"' for c in bad)
        print(f"\n  [!] WARNING: The repack folder path contains {chars}")
        print(f"      Wine's installer shell interprets these as special characters")
        print(f"      and will likely crash at low progress %.")
        print(f"      Rename the folder to remove them before continuing.")
        print(f"      e.g.  mv \"{repack_dir}\" \"{str(repack_dir).translate(str.maketrans({'&': 'and', '|': '-', ';': '-'}))}\"")
        print()
        if not args.yes:
            try:
                ans = input("  Continue anyway? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                sys.exit(0)
            if ans not in ('y', 'yes'):
                sys.exit(0)

    # Early soft warning about low RAM (a hard, re-checked gate runs again
    # right before setup.exe — memory can shift during verify/prefix-init)
    meminfo = read_meminfo()
    if meminfo:
        avail_mb = meminfo.get('MemAvailable', 0) // 1024
        if avail_mb < 3000:
            print(f"\n  [!] WARNING: Only {avail_mb} MB RAM available.")
            print(f"      FitGirl's decompressor needs ~3 GB free to avoid crashing.")
            print(f"      Close other applications (browser, Heroic, etc.) first.")
            print()

    # Same shape as the RAM warning above: an early soft heads-up here, and a
    # hard, re-checked gate right before setup.exe runs.
    repack_size = dir_size_bytes(repack_dir)
    if repack_size:
        try:
            free = shutil.disk_usage(existing_ancestor(install_base)).free
        except OSError:
            free = None
        if free is not None and free < repack_size * DISK_SPACE_WARN_MULTIPLIER:
            print(f"\n  [!] WARNING: Only {free / 1e9:.1f} GB free at {install_base}.")
            print(f"      This repack is {repack_size / 1e9:.1f} GB — installs often need "
                  f"as much again to extract, so this could run tight or fail partway through.")
            print()

    info = repack_info(repack_dir)
    if args.name:
        display_name = args.name
        slug = make_slug(args.name)
    else:
        display_name, slug = info['name'], info['slug']
    # Silent by default: FitGirl's Inno installer runs with no window into a
    # known folder (see inno_args). --interactive shows the setup window.
    unattended = args.unattended or (not args.interactive and
                                     load_config().get('unattended', True))

    # Prefix model: game_dir is the Proton compat-data dir; the actual Wine
    # prefix is game_dir/pfx (Proton always uses STEAM_COMPAT_DATA_PATH/pfx and
    # ignores a separately-set WINEPREFIX). Setting WINEPREFIX=game_dir/pfx keeps
    # winetricks pointed at the SAME prefix the game installs into.
    game_dir = install_base / slug
    wine_prefix = game_dir / 'pfx'
    proton = find_proton()
    runner = select_runner(load_config().get('runner', 'auto'), proton)
    if runner == 'umu':
        runner_label = f"umu-run + {proton.parent.name}"
    elif runner == 'proton':
        runner_label = proton.parent.name
    else:
        runner_label = "Wine (system)"

    base_env: dict[str, str] = {'WINEPREFIX': str(wine_prefix)}
    if runner in ('umu', 'proton'):
        base_env['STEAM_COMPAT_CLIENT_INSTALL_PATH'] = str(STEAM_ROOT)
        # umu derives its own compat-data path from WINEPREFIX; only the raw
        # `proton run` path needs STEAM_COMPAT_DATA_PATH set explicitly.
        if runner == 'proton':
            base_env['STEAM_COMPAT_DATA_PATH'] = str(game_dir)
    if args.debug:
        base_env['WINEDEBUG'] = 'warn,err,fixme'
        base_env['PROTON_LOG'] = '1'
        base_env['PROTON_LOG_DIR'] = str(LOG_DIR)
    else:
        # FitGirl installers spam Wine's default err+fixme channels, and we
        # capture that output — silencing it is a real speedup on a long install.
        base_env['WINEDEBUG'] = '-all'

    # Set up logger
    log_path: Path | None = None
    if args.log or args.debug:
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        log_path = LOG_DIR / f"{slug}-{ts}.log"
    use_logger(Logger(log_path))

    log.section("FitGirl Installer")
    log.print(f"  Game:        {display_name}")
    log.print(f"  Slug:        {slug}")
    log.print(f"  Source:      {repack_dir}")
    log.print(f"  Runner:      {runner_label}")
    log.print(f"  Wine prefix: {wine_prefix}")
    if args.force and game_dir.exists():
        log.print(f"  --force:     the existing install will be moved aside, not deleted")
    if log.log_path:
        log.print(f"  Log file:    {log.log_path}")
    if args.debug:
        log.print(f"  Debug:       ON (WINEDEBUG=warn,err,fixme  PROTON_LOG=1)")
    log.print(f"  Installer:   {'silent (no setup window)' if unattended else 'setup window'}")
    log.print()

    # Write system info to log before anything else
    log.system_info(repack_dir, wine_prefix, runner_label, base_env)

    if not args.yes:
        try:
            ans = input("Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            log.print("\nAborted.")
            sys.exit(0)
        if ans in ('n', 'no'):
            log.print("Aborted.")
            sys.exit(0)

    # 1. Verify
    if args.verify:
        log.print("\n[1/4] Verifying files...")
        if not verify_bins(repack_dir):
            log.print("\nVerification FAILED. Re-download the corrupted files.")
            sys.exit(1)
        log.print("  All checked files OK.")
    else:
        log.print("\n[1/4] Skipping file verification (use --verify to enable).")

    # 2. Find installer
    setup_exe = find_setup_exe(repack_dir)
    if not setup_exe:
        log.print("Error: No setup.exe found in the repack directory.", flush=True)
        sys.exit(1)

    # 3. Prefix
    log.print("\n[2/4] Setting up prefix...")
    if args.force and game_dir.exists():
        # "Wipe" without destroying anything irrecoverable: saves live in the
        # old prefix, so move it aside for the user to delete once the fresh
        # install works (same rename-not-delete rule as compatdata cleanup).
        aside = game_dir.with_name(f"{game_dir.name}.old-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            game_dir.rename(aside)
        except OSError as e:
            log.print(f"  [!] --force: could not move {game_dir} aside ({e}).")
            sys.exit(1)
        log.print(f"  --force: moved the old install to {aside}")
        log.print(f"           (saves are still in there; delete it once the reinstall works)")
    # Created only now, after every "Proceed?" — aborting earlier leaves nothing
    # behind. Proton/umu's file-lock code expects the dir to exist, and +C
    # (no CoW/compression) only takes effect while it is still empty.
    if game_dir.exists():
        # Over an installed game (an update, a resume): a snapshot first,
        # where the game has them, so "Roll back" can undo it.
        if registry_get(slug):
            auto_snapshot(slug, 'update', install_base)
    elif make_game_dir(game_dir):
        log.print(f"  Filesystem: a btrfs subvolume (snapshots can undo updates), "
                  f"CoW + compression off")
    else:
        log.print(f"  Filesystem: CoW + compression disabled for {game_dir}")
    # Until the game is registered, the folder is an unfinished install that
    # the library can resume (same arguments, minus --force: resuming reuses
    # what's there) or discard.
    resume = [str(repack_dir) if a == args.repack_dir else a
              for a in sys.argv[1:] if a != '--force']
    if args.repack_dir not in sys.argv[1:]:
        resume.insert(0, str(repack_dir))
    write_incomplete_marker(game_dir, {
        'name': display_name, 'source': str(repack_dir), 'argv': resume,
        'started': datetime.now().isoformat(timespec='seconds')})
    _install_session(slug, display_name, game_dir)
    already_installed = (wine_prefix / 'drive_c').exists()
    if already_installed:
        log.print(f"  [!] '{slug}' already appears to be installed at {wine_prefix}.")
        log.print(f"      Continuing reuses the existing prefix (e.g. to install an update")
        log.print(f"      or DLC repack on top). Pass --force for a fresh prefix instead.")
        if not args.yes:
            try:
                ans = input("  Continue with the existing installation? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
            if ans not in ('y', 'yes'):
                log.print("Aborted.")
                sys.exit(0)
        fresh_prefix = False
    else:
        fresh_prefix = True
        init_prefix(game_dir, base_env, proton, runner, wine_prefix)

    # A fresh prefix (new slug, or --force reinit) may be standing in for one
    # that was uninstalled earlier — offer to reinject its saved backup.
    if fresh_prefix:
        backup = latest_save_backup(slug)
        if backup:
            if args.import_save:
                do_restore = True
            elif args.yes:
                do_restore = False
                log.print(f"  Found a save backup from a previous install "
                          f"({backup.name}) — NOT importing it (pass --import-save "
                          f"to opt in).")
            else:
                try:
                    ans = input(f"  Found a save backup from a previous install "
                                f"({backup.name}). Import it into the new "
                                f"prefix? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = 'n'
                do_restore = ans in ('y', 'yes')
            if do_restore:
                n, failed = restore_prefix_saves(slug, wine_prefix) or (0, [])
                if n:
                    log.print(f"  Restored {n} save file(s) from {backup.name}.")
                if failed:
                    log.print(f"  [!] {len(failed)} save file(s) from {backup.name} could "
                              f"not be restored, e.g. {failed[0]}")

    # Where a silent install goes: over the existing game when installing on
    # top (an update), else C:\\Games\\<slug>.
    target = None
    if unattended:
        target = (existing_install_root((registry_get(slug) or {}).get('exe'), wine_prefix)
                  if already_installed else None) or silent_target(wine_prefix, slug)
    setup_extra = inno_args(win_path(target, wine_prefix)) if target else []
    # Silent installs also skip the VC++/DirectX redists and the installer's
    # music; every install keeps setup from opening FitGirl's site at the end
    # (browser_muted). The user's own WINEDLLOVERRIDES (from the environment)
    # is kept.
    skipped = setup_redists(setup_exe) if target else set()
    overrides = silent_setup_overrides(setup_exe, skipped) if target else None
    setup_env = {**base_env, 'WINEDLLOVERRIDES': ';'.join(
        o for o in (os.environ.get('WINEDLLOVERRIDES'),
                    base_env.get('WINEDLLOVERRIDES'), overrides) if o)}

    # 4. Installer (bounded retry: a signal-killed attempt can retry in-place;
    #    a normal exit — success or the installer's own nonzero code — does not)
    with browser_muted(wine_prefix, base_env, runner, proton):
        for attempt in range(MAX_SETUP_RETRIES + 1):
            if not preflight_hard_memory_check(skip=args.skip_mem_check):
                log.print("\nAborted due to low memory.")
                sys.exit(1)
            if not preflight_hard_disk_check(install_base, repack_size, skip=args.skip_disk_check):
                log.print("\nAborted due to insufficient disk space.")
                sys.exit(1)

            suffix = f" (attempt {attempt + 1}/{MAX_SETUP_RETRIES + 1})" if attempt else ""
            log.print(f"\n[3/4] Running {setup_exe.name} via {runner_label}...{suffix}")
            if target:
                log.print(f"  Installing silently into {win_path(target, wine_prefix)} — no setup "
                          f"window (--interactive shows it).")
                log.print("  Skipping its VC++/DirectX redists (Proton ships them) and its music:")
                log.print(f"    {', '.join(sorted(skipped))}\n")
            else:
                log.print("  The installer GUI will open. Follow the on-screen steps.")
                log.print("  You can install to any path — it stays inside this prefix.\n")

            progress = InstallProgress(target) if target else None
            if progress:
                progress.start()
            try:
                if runner == 'umu':
                    rc = run(['umu-run', str(setup_exe), *setup_extra],
                              extra_env=_umu_env(setup_env, wine_prefix, proton),
                              capture=True, diagnose=True, label="setup.exe (via umu-run)")
                elif runner == 'proton':
                    rc = run([str(proton), 'run', str(setup_exe), *setup_extra],
                              extra_env=setup_env,
                              capture=True, diagnose=True, label="setup.exe (via Proton)")
                else:
                    rc = run(['wine', str(setup_exe), *setup_extra], extra_env=setup_env,
                              capture=True, diagnose=True, label="setup.exe (via Wine)")
            finally:
                if progress:
                    progress.stop()

            if rc >= 0:
                if rc == 0:
                    log.print("\n  Installer closed.")
                else:
                    log.print(f"\n  [!] Installer exited with code {rc} — it may not have "
                              f"finished. Checking for the game anyway...")
                break

            if attempt == MAX_SETUP_RETRIES:
                log.print("\n  [!] Max retries reached — giving up on the installer step.")
                log.print(f"      Prefix retained at {wine_prefix} for manual retry:")
                if runner == 'umu':
                    log.print(f"        WINEPREFIX=\"{wine_prefix}\" GAMEID=umu-0 "
                              f"PROTONPATH=\"{proton.parent}\" umu-run \"{setup_exe}\"")
                else:
                    log.print(f"        WINEPREFIX=\"{wine_prefix}\" wine \"{setup_exe}\"")
                sys.exit(1)

            if args.yes:
                ans = 'y'
                log.print("  Retrying in the same prefix (--yes)...")
            else:
                try:
                    ans = input("  Retry running the installer in the same prefix? "
                                "[Y/n] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = 'n'
            if ans in ('n', 'no'):
                log.print("Aborted.")
                sys.exit(1)

    # 5. Deps — opt-in. Proton bundles the VC++ runtimes, d3dcompiler_47 and
    #    DXVK, so the winetricks pass is dead weight (and harmful for dxvk) on
    #    every modern build. --deps is the escape hatch for the rare game that
    #    genuinely needs a verb.
    if args.deps and not args.no_deps:
        log.print(f"\n[4/4] Installing requested dependencies ({args.deps})...")
        install_deps(wine_prefix, base_env, spec=args.deps, runner=runner, proton=proton)
    else:
        log.print("\n[4/4] Skipping winetricks deps — Proton ships the runtimes.")
        log.print("      Need one anyway?  --deps vcrun2019[,other_verb]")

    # 6. Launch script
    log.print("\nSearching for game executable...")
    cands = exe_candidates(target) if target else []
    if not cands:
        cands = exe_candidates(wine_prefix / 'drive_c')
    game_exe = cands[0][1] if cands else None
    if game_exe:
        log.print(f"  Found: {game_exe.relative_to(wine_prefix)}")
        # The pick is the largest exe; the GUI shows the runners-up so a
        # wrong guess (a launcher, a bundled tool) is one click to fix.
        emit_event('exe-candidates', {'slug': slug, 'chosen': str(game_exe), 'candidates': [
            {'path': str(p), 'size': n} for n, p in cands[:3]]})
    else:
        # No real game exe (Windows/Steam stubs are skipped) usually means the
        # installer never actually ran — FitGirl's setup.exe often crashes on
        # launch under a non-GE Proton. Warn loudly and DON'T create phantom
        # Steam/desktop entries for a game that isn't there.
        log.print("\n  [!] NO GAME EXECUTABLE FOUND — the install did not complete.")
        build = proton.parent.name if proton else None
        if WINE_ABORTS:
            fn = re.search(r'unimplemented function (\S+?),', WINE_ABORTS[0])
            log.print(f"      Wine aborted before setup.exe could run: it called "
                      f"{fn.group(1) if fn else 'a function'},")
            log.print(f"      which {build or 'this Proton'} doesn't have. That means the "
                      f"Proton build is")
            log.print("      damaged (its Windows DLLs don't match its Linux side) — "
                      "not the repack.")
            why = damaged_protons().get(build or '')
            if why:
                log.print(f"      {build}: {why}.")
            tag = proton_repair_tag(build) if build else None
            log.print(f"      Fix: nexus-cli --proton-install {tag} --force, then retry."
                      if tag else "      Fix: reinstall that Proton build, then retry.")
            log.print(f"      Prefix kept for retry: {wine_prefix}")
            log.print("\n  Skipping Steam/desktop/artwork (nothing to launch).")
            log.close()
            sys.exit(1)
        log.print("      The prefix contains only Windows/Steam stubs, so setup.exe")
        log.print("      likely crashed on launch (a quick exit + no extraction).")
        log.print("      FitGirl installers need a working x86-64 GE-Proton; the")
        log.print(f"      current runner is '{runner_label}'.")
        log.print("      Fix: check the Proton build (nexus-cli --doctor), or install a")
        log.print(f"      GE-Proton release (--proton-install latest), then retry.")
        log.print(f"      Prefix kept for retry: {wine_prefix}")
        if target:
            log.print("      The silent installer may not suit this repack: try again with")
            log.print("      the setup window (--interactive).")
            emit_event('retry-interactive', {'slug': slug})
        log.print("\n  Skipping Steam/desktop/artwork (nothing to launch).")
        log.close()
        sys.exit(1)

    done = _finish_install(args, slug=slug, display_name=display_name, game_dir=game_dir,
                           wine_prefix=wine_prefix, game_exe=game_exe,
                           install_base=install_base, runner=runner, proton=proton,
                           repack={'repack_fingerprint': repack_fingerprint(repack_dir),
                                   'repack_version': info['version'],
                                   'repack_source': str(repack_dir),
                                   'repack_installed_at':
                                       datetime.now().isoformat(timespec='seconds')})
    config, launch = done['config'], done['launch']
    desktop_path, steam_appid_val = done['desktop_path'], done['steam_appid']
    steam_pending, heroic_app_name = done['steam_pending'], done['heroic_app_name']
    trashed = False
    if args.trash_repack or config.get('trash_repack'):
        from nexus import cleanup as C
        try:
            C.trash(repack_dir)
            trashed = True
            log.print(f"\nMoved the repack folder to the Trash: {repack_dir}")
        except OSError as e:
            log.print(f"\n  [!] Could not move the repack to the Trash ({e}).")
    if args.from_torrent:
        _torrent_mark(args.from_torrent, installed=True, slug=slug)
    emit_event('installed', {'slug': slug, 'name': display_name, 'source': str(repack_dir),
                             'repack_trashed': trashed, 'steam_pending': steam_pending})

    log.section("Done!")
    log.print(f"  Wine prefix:   {wine_prefix}")
    log.print(f"  Launch script: {launch}")
    if desktop_path:
        log.print(f"  Desktop entry: {desktop_path}")
    if steam_appid_val:
        log.print(f"  Steam:         added to library (restart Steam to see it)")
    elif steam_pending:
        log.print(f"  Steam:         waiting for Steam to close (nexus-cli --steam {slug})")
    if heroic_app_name:
        log.print(f"  Heroic:        added as a sideloaded game (restart Heroic to see it)")
    if log.log_path:
        log.print(f"  Log file:      {log.log_path}")
    if game_exe:
        log.print(f"\n  To play:")
        log.print(f"    bash \"{launch}\"")
    else:
        log.print(f"\n  Edit {launch} to set the game .exe, then:")
        log.print(f"    bash \"{launch}\"")
    log.print()

    log.close()


def _finish_install(args, *, slug: str, display_name: str, game_dir: Path, wine_prefix: Path,
                    game_exe: Path, install_base: Path, runner: str, proton: Path | None,
                    repack: dict | None = None, extra: dict | None = None) -> dict:
    """The end of every install, once the game's files are in its prefix:
    launch.sh, artwork, Steam, the app menu, Heroic, then the registry entry.
    Shared by FitGirl installs (cmd_install) and any other game (cmd_import)."""
    # The Proton the game is *played* with (may differ from the installer's:
    # see resolve_game_proton). A reinstall keeps an existing launch profile.
    config = load_config()
    prev = registry_get(slug) or {}
    prof = prev.get('launch')
    pg_default = None
    if args.use_protongen_default and not profile_owns_options(prof):
        pg_default = protongen_default_profile(prev)
        if pg_default:
            prof = pg_default
            prev = {**prev, 'launch': prof}
            log.print(f"\nUsing protongen's default profile: "
                      f"{format_launch_options(prof) or '(no options)'}")
        else:
            log.print("\n  [!] protongen has no default profile — launch options left empty.")
    play_proton = resolve_game_proton(prev, config) or proton
    umu_gameid = prev.get('umu_gameid')
    if runner == 'umu' and not umu_gameid:
        umu_gameid = resolve_umu_gameid(display_name)
        if umu_gameid:
            log.print(f"  umu-protonfixes id: {umu_gameid}")
    launch = write_launch_script(game_dir / 'launch.sh', game_dir, wine_prefix,
                                 game_exe, slug, play_proton, display_name, runner=runner,
                                 launch=prof, gameid=umu_gameid)

    # 7. Integrations (Steam shortcut, desktop entry) — best-effort, never fatal
    artwork = None
    if not args.no_artwork:
        log.print("\nFetching artwork from SteamGridDB...")
        artwork = _best_effort("Artwork", fetch_artwork, display_name, slug, config)
        if artwork:
            log.print(f"  Got: {', '.join(sorted(k for k in artwork if not k.endswith('_url')))}")
        else:
            log.print("  No artwork (missing API key, no match, or offline) — skipping.")

    steam_appid_val = None
    steam_pending = False
    if not args.no_steam and config.get('steam', True) and args.defer_steam \
            and steam_is_running():
        # Steam would overwrite the shortcut on exit; the GUI adds it with
        # `--steam <slug>` once Steam is closed, instead of the whole install
        # waiting for that.
        steam_pending = True
        log.print("\nSteam is running — the Steam shortcut is added once Steam is closed.")
        emit_event('steam-pending', {'slug': slug})
    elif not args.no_steam and config.get('steam', True):
        steam_appid_val = _best_effort(
            "The Steam shortcut", add_steam_shortcut_native,
            display_name, slug, game_exe, game_dir, play_proton, artwork,
            launch_options=format_launch_options(prof) if profile_owns_options(prof) else None)

    desktop_path = None
    if not args.no_desktop and config.get('desktop', True):
        desktop_path = _best_effort("The app-menu entry", write_desktop_entry,
                                    display_name, slug, launch, artwork)

    heroic_app_name = None
    if not args.no_heroic and config.get('heroic', True):
        heroic_app_name = _best_effort(
            "The Heroic entry", add_heroic_entry,
            display_name, slug, game_exe, wine_prefix, play_proton, artwork,
            force_proton=_proton_is_explicit(prev, config),
            launch=prof if profile_owns_options(prof) else None)

    register_game(
        slug=slug, display_name=display_name, exe=game_exe, compatdata=game_dir,
        wine_prefix=wine_prefix, launcher=launch, source='install',
        install_base=install_base, steam_appid=steam_appid_val or prev.get('steam_appid'),
        has_desktop=bool(desktop_path), artwork_dir=ARTWORK_DIR / slug,
        heroic_app_name=heroic_app_name,
        proton=play_proton.parent.name if play_proton else None,
        umu_gameid=umu_gameid, repack=repack, extra=extra)
    clear_incomplete_marker(game_dir)
    if pg_default:
        _save_profile(slug, pg_default, "protongen default profile")
    return {'config': config, 'launch': launch, 'desktop_path': desktop_path,
            'steam_appid': steam_appid_val, 'steam_pending': steam_pending,
            'heroic_app_name': heroic_app_name}


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import _proton_is_explicit, _save_profile, profile_owns_options  # noqa: E402
from .config import load_config  # noqa: E402
from .desktop import write_desktop_entry  # noqa: E402
from .downloads import _torrent_mark  # noqa: E402
from .heroic import add_heroic_entry  # noqa: E402
from .launchprofile import format_launch_options, protongen_default_profile  # noqa: E402
from .registry import register_game, registry_get  # noqa: E402
from .repack import (_install_session, clear_incomplete_marker, DISK_SPACE_WARN_MULTIPLIER,
    exe_candidates, existing_install_root, find_setup_exe, inno_args, InstallProgress,
    MAX_SETUP_RETRIES, preflight_hard_disk_check, preflight_hard_memory_check,
    read_meminfo, repack_fingerprint, repack_info, setup_redists, silent_setup_overrides,
    silent_target, verify_bins, write_incomplete_marker)  # noqa: E402
from .runner import (_umu_env, browser_muted, damaged_protons, find_proton, init_prefix,
    install_deps, proton_repair_tag, resolve_game_proton, resolve_umu_gameid, select_runner,
    write_launch_script)  # noqa: E402
from .sgdb import fetch_artwork  # noqa: E402
from .snapshots import auto_snapshot, make_game_dir  # noqa: E402
from .steam import (add_steam_shortcut_native, latest_save_backup, restore_prefix_saves,
    steam_is_running)  # noqa: E402
