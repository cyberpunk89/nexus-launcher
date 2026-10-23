"""Installing a repack on Windows: no prefix, no Proton. FitGirl's setup.exe
runs as administrator (the UAC prompt) straight into <install base>\\<slug>,
redists included, and the game is registered with its exe and uninstaller.
What `nexus-cli <repack dir>` does when has('proton') is off."""

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from nexus import appx, platform

from .common import (ARTWORK_DIR, dir_size_bytes, emit_event, log, LOG_DIR, Logger, make_slug,
    use_logger)

FITGIRL_FAQ = 'https://fitgirl-repacks.site/faq/'

# Room to leave under the 260-character limit for the game's own folders
# (Unreal games reach ~140: Game\\Content\\Paks\\..., Engine\\Binaries\\...).
LONG_PATH_ROOM = 260 - 150

# What an Inno log says when the archive couldn't be unpacked: usually an
# antivirus took unarc.dll / ISDone.dll (or a part) away mid-install.
_AV_SIGNS = re.compile(r'isdone\.dll|unarc\.dll|archive data (?:is )?corrupted|'
                       r'data error|crc check failed', re.I)


def cmd_install_win(args, install_base: Path) -> None:
    repack_dir = Path(args.repack_dir).resolve()
    if not repack_dir.is_dir():
        print(f"Error: '{repack_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)
    safety_gate(args, repack_dir)
    if args.from_torrent:
        _torrent_mark(args.from_torrent, install_started=time.strftime('%Y-%m-%dT%H:%M:%S'))

    info = repack_info(repack_dir)
    display_name, slug = ((args.name, make_slug(args.name)) if args.name
                          else (info['name'], info['slug']))
    slug = _case_free_slug(slug, install_base)
    game_dir = install_base / slug
    unattended = args.unattended or (not args.interactive and
                                     load_config().get('unattended', True))
    repack_size = dir_size_bytes(repack_dir)
    setup_log = LOG_DIR / f"{slug}-setup.log"

    log_path = None
    if args.log or args.debug:
        log_path = LOG_DIR / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}.log"
    use_logger(Logger(log_path))

    log.section("FitGirl Installer")
    log.print(f"  Game:        {display_name}")
    log.print(f"  Slug:        {slug}")
    log.print(f"  Source:      {repack_dir}")
    log.print(f"  Install to:  {game_dir}")
    log.print(f"  Installer:   {'silent (no setup window)' if unattended else 'setup window'}, "
              f"as administrator")
    for why in platform.install_base_warnings(install_base):
        log.print(f"  [!] {install_base}: {why}.")
    if not platform.long_paths_enabled() and len(str(game_dir)) > LONG_PATH_ROOM:
        log.print(f"  [!] {game_dir} is {len(str(game_dir))} characters long and Windows' long "
                  f"paths are off: a deep game folder may pass 260 and fail to install.")
    if args.force and game_dir.exists():
        log.print("  --force:     the existing install will be moved aside, not deleted")
    if log.log_path:
        log.print(f"  Log file:    {log.log_path}")
    log.print()

    if not args.yes:
        try:
            ans = input("Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans in ('n', 'no'):
            log.print("Aborted.")
            sys.exit(0)

    log.print("\n[1/3] Verifying files..." if args.verify
              else "\n[1/3] Skipping file verification (use --verify to enable).")
    if args.verify:
        if not verify_bins(repack_dir):
            log.print("\nVerification FAILED. Re-download the corrupted files.")
            sys.exit(1)
        log.print("  All checked files OK.")

    setup_exe = find_setup_exe(repack_dir)
    if not setup_exe:
        log.print("Error: No setup.exe found in the repack directory.", flush=True)
        sys.exit(1)
    if not preflight_hard_memory_check(skip=args.skip_mem_check):
        log.print("\nAborted due to low memory.")
        sys.exit(1)
    if not preflight_hard_disk_check(install_base, repack_size, skip=args.skip_disk_check):
        log.print("\nAborted due to insufficient disk space.")
        sys.exit(1)

    if args.force and game_dir.exists():
        aside = game_dir.with_name(f"{game_dir.name}.old-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            game_dir.rename(aside)
        except OSError as e:
            log.print(f"  [!] --force: could not move {game_dir} aside ({e}).")
            sys.exit(1)
        log.print(f"  --force: moved the old install to {aside}")
    created = not game_dir.exists()
    game_dir.mkdir(parents=True, exist_ok=True)
    resume = [str(repack_dir) if a == args.repack_dir else a
              for a in sys.argv[1:] if a != '--force']
    if args.repack_dir not in sys.argv[1:]:
        resume.insert(0, str(repack_dir))
    write_incomplete_marker(game_dir, {
        'name': display_name, 'source': str(repack_dir), 'argv': resume,
        'started': datetime.now().isoformat(timespec='seconds')})
    _install_session(slug, display_name, game_dir)

    log.print(f"\n[2/3] Running {setup_exe.name} as administrator...")
    setup_started = datetime.now().isoformat(timespec='seconds')
    rc = _run_setup(setup_exe, game_dir, setup_log, unattended)
    if rc is None:                          # declined at the UAC prompt
        log.print("\n  Install cancelled at the admin prompt. Nothing was installed.")
        clear_incomplete_marker(game_dir)
        if created:
            _remove_if_empty(game_dir)
        emit_event('install-cancelled', {'slug': slug, 'why': 'uac'})
        log.close()
        sys.exit(1)
    if rc == 0:
        log.print("\n  Installer closed.")
    else:
        log.print(f"\n  [!] Installer exited with code {rc} — it may not have finished. "
                  f"Checking for the game anyway...")

    log.print("\n[3/3] Searching for the game executable...")
    cands = exe_candidates(game_dir)
    game_exe = cands[0][1] if cands else None
    # Defender may take a game file mid-install (FF XV Pocket Edition's
    # vcruntime140_1_app.dll, in the VM) and the game then can't start, even
    # with its exe in place: not an install to register.
    quarantined = explain_failed_install(setup_log, [repack_dir, game_dir], slug,
                                         since=setup_started,
                                         read_log=not game_exe or rc != 0)
    if not game_exe or quarantined:
        log.print("\n  [!] " + ("NO GAME EXECUTABLE FOUND — the install did not complete."
                               if not game_exe else
                               "Windows Defender removed game files, so it won't run: "
                               "not adding it to the library."))
        log.print(f"      Setup log: {setup_log}")
        log.print(f"      Folder kept for a retry: {game_dir}")
        if unattended and not quarantined:
            log.print("      The silent installer may not suit this repack: try again with "
                      "the setup window (--interactive).")
            emit_event('retry-interactive', {'slug': slug})
        log.close()
        sys.exit(1)
    log.print(f"  Found: {game_exe.relative_to(game_dir)}")
    emit_event('exe-candidates', {'slug': slug, 'chosen': str(game_exe), 'candidates': [
        {'path': str(p), 'size': n} for n, p in cands[:3]]})

    done = _finish_install_win(args, slug=slug, display_name=display_name, game_dir=game_dir,
                               game_exe=game_exe, install_base=install_base, repack={
                                   'repack_fingerprint': repack_fingerprint(repack_dir),
                                   'repack_version': info['version'],
                                   'repack_source': str(repack_dir),
                                   'repack_installed_at':
                                       datetime.now().isoformat(timespec='seconds')})

    if created:                 # a fresh install may stand in for an uninstalled one
        _offer_save_backup(args, slug, game_dir)

    trashed = False
    if args.trash_repack or done['config'].get('trash_repack'):
        try:
            platform.trash(repack_dir)
            trashed = True
            log.print(f"\nMoved the repack folder to the Recycle Bin: {repack_dir}")
        except OSError as e:
            log.print(f"\n  [!] Could not move the repack to the Recycle Bin ({e}).")
    if args.from_torrent:
        _torrent_mark(args.from_torrent, installed=True, slug=slug)
    emit_event('installed', {'slug': slug, 'name': display_name, 'source': str(repack_dir),
                             'repack_trashed': trashed, 'steam_pending': done['steam_pending']})

    log.section("Done!")
    log.print(f"  Installed to:  {game_dir}")
    log.print(f"  Game:          {game_exe}")
    if done['desktop_path']:
        log.print(f"  Start Menu:    {done['desktop_path']}")
    if done['steam_appid']:
        log.print("  Steam:         added to library (restart Steam to see it)")
    elif done['steam_pending']:
        log.print(f"  Steam:         waiting for Steam to close (nexus-cli --steam {slug})")
    if log.log_path:
        log.print(f"  Log file:      {log.log_path}")
    log.print(f"\n  To play:  nexus-cli --launch {slug}\n")
    log.close()


def _finish_install_win(args, *, slug: str, display_name: str, game_dir: Path, game_exe: Path,
                        install_base: Path, repack: dict | None = None,
                        extra: dict | None = None, source: str = 'install') -> dict:
    """The end of a Windows install or import: artwork, Steam, the Start
    Menu, then the registry entry (no launch.sh, prefix or Proton)."""
    config = load_config()
    prev = registry_get(slug) or {}
    aumid = packaged_app(game_exe)
    if aumid:
        log.print(f"  A Store (UWP) app: it starts as its package ({aumid}).")
    artwork = None
    if not args.no_artwork:
        log.print("\nFetching artwork from SteamGridDB...")
        artwork = _best_effort("Artwork", fetch_artwork, display_name, slug, config)

    steam_appid = None
    steam_pending = False
    if not args.no_steam and config.get('steam', True) and args.defer_steam \
            and steam_is_running():
        steam_pending = True
        log.print("\nSteam is running — the Steam shortcut is added once Steam is closed.")
        emit_event('steam-pending', {'slug': slug})
    elif not args.no_steam and config.get('steam', True):
        exe, opts = steam_target(game_exe, aumid)
        steam_appid = _best_effort("The Steam shortcut", add_steam_shortcut_native,
                                   display_name, slug, exe, game_dir, None, artwork,
                                   launch_options=opts)

    desktop_path = None
    if not args.no_desktop and config.get('desktop', True):
        desktop_path = _best_effort("The Start Menu shortcut", write_start_menu_entry,
                                    display_name, slug, game_exe, aumid)

    unins = sorted(game_dir.glob('unins*.exe'))
    register_game(
        slug=slug, display_name=display_name, exe=game_exe, compatdata=None, wine_prefix=None,
        launcher=None, source=source, install_base=install_base,
        steam_appid=steam_appid or prev.get('steam_appid'), has_desktop=bool(desktop_path),
        artwork_dir=ARTWORK_DIR / slug, repack=repack,
        extra={'platform': 'windows', 'install_dir': str(game_dir), 'appx': aumid,
               'uninstaller': str(unins[0]) if unins else None,
               'menu_entry': str(desktop_path) if desktop_path else None, **(extra or {})})
    clear_incomplete_marker(game_dir)
    return {'config': config, 'desktop_path': desktop_path, 'steam_appid': steam_appid,
            'steam_pending': steam_pending}


def _offer_save_backup(args, slug: str, game_dir: Path) -> None:
    """--import-save on Windows: put the saves backed up at uninstall back
    where the game keeps them (files.json), after keeping whatever is there
    now: unlike a fresh Wine prefix, the user folder still has the old saves."""
    backup = latest_save_backup(slug)
    if backup is None or not is_manifest_backup(backup):
        return
    if args.import_save:
        do_restore = True
    elif args.yes:
        do_restore = False
        log.print(f"  Found a save backup from a previous install ({backup.name}) — NOT "
                  f"importing it (pass --import-save to opt in).")
    else:
        try:
            ans = input(f"  Found a save backup from a previous install ({backup.name}). "
                        f"Put it back? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        do_restore = ans in ('y', 'yes')
    if not do_restore:
        return
    save_entry, why = manifest_save_entry(registry_get(slug) or {'slug': slug})
    if save_entry is None:
        log.print(f"  [!] {why}, so it can't keep the current ones first: the backup "
                  f"({backup.name}) was not imported.")
        return
    before = backup_manifest_saves(slug, game_dir, save_entry, suffix=PRE_RESTORE)
    if before and before[2]:
        log.print(f"  [!] {len(before[2])} current save file(s) could not be kept, e.g. "
                  f"{before[2][0]}: the backup was not imported.")
        return
    if before:
        log.print(f"  Kept the current saves ({before[1]} file(s)) in {before[0]}")
    n, failed = restore_manifest_saves(slug) or (0, [])
    if n:
        log.print(f"  Restored {n} save file(s) from {backup.name}.")
    if failed:
        log.print(f"  [!] {len(failed)} save file(s) from {backup.name} could not be "
                  f"restored, e.g. {failed[0]}")


def cmd_import_win(args, install_base: Path) -> None:
    """--import on Windows: a game folder is added where it is (nothing
    copied; removing it from Nexus later leaves the folder alone). An
    installer runs as administrator: an Inno one silently into
    <install base>\\<slug>, anything else with its own window."""
    src = Path(args.import_path).expanduser()
    try:
        src = src.resolve()
    except OSError:
        pass
    is_installer = src.is_file() and src.suffix.lower() in ('.exe', '.msi')
    if not (is_installer or src.is_dir()):
        print(f"Error: {src} is neither a Windows installer (.exe/.msi) nor a folder",
              file=sys.stderr)
        sys.exit(1)
    display_name = args.name or guess_name(src)
    slug = _case_free_slug(make_slug(display_name), install_base)
    game_dir = src if src.is_dir() else install_base / slug
    if registry_get(slug) or (is_installer and game_dir.exists()):
        print(f"Error: '{slug}' already exists. Pick another name with --name.",
              file=sys.stderr)
        sys.exit(1)
    use_logger(Logger(LOG_DIR / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}.log"
                      if args.log or args.debug else None))
    log.section("Adding a game")
    log.print(f"  Game:        {display_name}")
    log.print(f"  Slug:        {slug}")
    log.print(f"  From:        {src} ({'installer' if is_installer else 'folder, left where it is'})")

    if is_installer:
        silent = (not args.interactive and src.suffix.lower() == '.exe'
                  and inno_header(src) is not None)
        if src.suffix.lower() == '.msi':
            exe, switches = Path('msiexec'), ['/i', str(src)]
        else:
            exe, switches = src, ([*INNO_SILENT, f'/DIR={game_dir}'] if silent else [])
        log.print(f"\n[1/2] Running {src.name} as administrator"
                  + (" silently (Inno Setup)..." if silent else
                     f" — install it into {game_dir} so Nexus finds it..."))
        try:
            rc = platform.run_elevated(exe, switches, cwd=src.parent).wait()
        except platform.ElevationCancelled:
            log.print("  Cancelled at the admin prompt. Nothing was added.")
            emit_event('install-cancelled', {'slug': slug, 'why': 'uac'})
            log.close()
            sys.exit(1)
        except OSError as e:
            log.print(f"  [!] Couldn't start {src.name}: {e}")
            sys.exit(1)
        log.print("  Installer closed." if rc == 0 else
                  f"  [!] The installer exited with code {rc}. Checking for the game anyway...")

    log.print(f"\n{'[2/2]' if is_installer else '[1/1]'} Looking for the game's .exe...")
    cands = exe_candidates(game_dir)
    if not cands:
        log.print(f"  [!] No game .exe found in {game_dir} — nothing was added.")
        log.close()
        sys.exit(1)
    game_exe = cands[0][1]
    log.print(f"  Found: {game_exe.relative_to(game_dir)}")
    emit_event('exe-candidates', {'slug': slug, 'chosen': str(game_exe), 'candidates': [
        {'path': str(p), 'size': n} for n, p in cands[:3]]})
    # A folder that isn't ours keeps its uninstaller to itself: "uninstall"
    # in Nexus then only forgets it.
    done = _finish_install_win(args, slug=slug, display_name=display_name, game_dir=game_dir,
                               game_exe=game_exe, install_base=install_base,
                               extra={'local': True, 'import_source': str(src),
                                      **({} if is_installer else {'uninstaller': None})})
    emit_event('installed', {'slug': slug, 'name': display_name, 'source': str(src),
                             'repack_trashed': False, 'steam_pending': done['steam_pending']})
    log.section("Done!")
    log.print(f"  {display_name} is in your library.  Play: nexus-cli --launch {slug}")
    log.close()


def packaged_app(exe: Path) -> str | None:
    """The AppUserModelID when `exe` is a Store app that's registered (FitGirl's
    setup registers UWP repacks), else None: then the exe runs by itself."""
    aumid = appx.app_id(exe)
    return aumid if aumid and platform.appx_registered(appx.family(aumid)) else None


def steam_target(exe: Path, aumid: str | None) -> tuple[Path, str | None]:
    """(Exe, LaunchOptions) for a Steam shortcut: the game's exe, or for a
    Store app explorer.exe on its shell:AppsFolder URI."""
    if not aumid:
        return exe, None
    return Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'explorer.exe', appx.shell_uri(aumid)


def _case_free_slug(slug: str, install_base: Path) -> str:
    """Windows folders ignore case: 'Doom' and 'doom' would be one folder. A
    slug that differs from a registered one only by case takes that one's
    spelling, so an update lands on the installed game."""
    for other in load_registry()['games']:
        if other != slug and other.casefold() == slug.casefold():
            return other
    return slug


def _run_setup(setup_exe: Path, game_dir: Path, setup_log: Path, unattended: bool) -> int | None:
    """setup.exe through the UAC prompt, into game_dir, while the folder's
    size is reported as `progress` events. The exit code, or None when the
    prompt was declined. Inno keeps its own log at setup_log."""
    setup_log.parent.mkdir(parents=True, exist_ok=True)
    setup_log.unlink(missing_ok=True)       # an older attempt's errors aren't this one's
    switches = [*INNO_SILENT] if unattended else []
    switches += [f'/DIR={game_dir}', f'/LOG={setup_log}']
    if unattended:
        log.print(f"  Installing silently into {game_dir} — no setup window "
                  f"(--interactive shows it).")
        log.print("  Its VC++/DirectX redists run too: Windows needs them. FitGirl's setup")
        log.print("  may play music and open her site when it's done; that's the installer.")
    else:
        log.print(f"  The installer window will open. Keep the folder it suggests ({game_dir}),")
        log.print("  or Nexus won't find the game afterwards.")
    log.print("  It runs as administrator, so Nexus can't stop it once it has started:")
    log.print("  cancel from its own window.\n")
    try:
        proc = platform.run_elevated(setup_exe, switches, cwd=setup_exe.parent)
    except platform.ElevationCancelled:
        return None
    except OSError as e:
        log.print(f"  [!] Couldn't start {setup_exe.name}: {e}")
        return 1
    progress = InstallProgress(game_dir)
    progress.start()
    try:
        while proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        if not proc.terminate():
            log.print("\n  [!] The installer runs as administrator and keeps going; close its "
                      "window to stop it.")
        raise
    finally:
        progress.stop()
    return proc.returncode


def _remove_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def explain_failed_install(setup_log: Path, folders: list[Path], slug: str,
                           since: str | None = None, read_log: bool = True) -> bool:
    """Why setup may have failed: the Inno log's unpacking errors, and what
    Windows Defender quarantined from the repack or the install folder
    (since `since`, a local ISO time, when given). True when Defender took
    something. Read-only: Defender's settings and exclusions stay the user's
    call."""
    lines = []
    for enc in ('utf-8', 'utf-16') if read_log else ():
        try:
            lines = setup_log.read_text(encoding=enc, errors='replace').splitlines()
            break
        except OSError:
            pass
    signs = [ln.strip() for ln in lines if _AV_SIGNS.search(ln)][:3]
    found = [d for d in platform.defender_detections(folders)
             if not since or d['at'] >= since]
    if not (signs or found):
        return False
    log.print("\n  [!] This looks like an antivirus problem, not a broken repack:")
    for ln in signs:
        log.print(f"      setup log: {ln[:160]}")
    for d in found:
        log.print(f"      Windows Defender removed {d['file']} ({d['threat']}, {d['at']})")
    log.print("      Repack files (unarc.dll, ISDone.dll, a game's cracked DLLs) are often "
              "flagged by mistake.")
    log.print(f"      Her FAQ explains it: {FITGIRL_FAQ}")
    log.print("      Restore the file from Windows Security > Protection history, or allow it")
    log.print("      there, then install again. Nexus doesn't change Defender settings.")
    emit_event('antivirus', {'slug': slug, 'log_lines': signs, 'detections': found,
                             'faq': FITGIRL_FAQ})
    return bool(found)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .config import load_config  # noqa: E402
from .desktop import write_start_menu_entry  # noqa: E402
from .downloads import _torrent_mark  # noqa: E402
from .importgame import guess_name  # noqa: E402
from .install import _best_effort, safety_gate  # noqa: E402
from .registry import load_registry, register_game, registry_get  # noqa: E402
from .repack import (_install_session, clear_incomplete_marker, exe_candidates, find_setup_exe,
    INNO_SILENT, InstallProgress, preflight_hard_disk_check, preflight_hard_memory_check,
    repack_fingerprint, repack_info, verify_bins, write_incomplete_marker)  # noqa: E402
from .sgdb import fetch_artwork  # noqa: E402
from nexus.inno import inno_header  # noqa: E402
from .steam import (add_steam_shortcut_native, backup_manifest_saves, is_manifest_backup,
    latest_save_backup, manifest_save_entry, PRE_RESTORE, restore_manifest_saves,
    steam_is_running)  # noqa: E402
