"""Uninstall: remove a game Nexus installed, and only what it owns."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

from nexus import platform, saves

from .common import APPLICATIONS_DIR, ARTWORK_DIR, LAUNCHERS_DIR, valid_slug


def _rmtree_collect(path: Path) -> list[str]:
    """rmtree that keeps going past errors and returns the paths it couldn't
    remove (rmtree(ignore_errors=True) hides a partial delete)."""
    failed: list[str] = []
    def on_err(_func, p, _exc):
        failed.append(str(p))
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_err)
    else:
        shutil.rmtree(path, onerror=on_err)
    return failed


def _owned_game_dir(entry: dict, install_base: Path, slug: str) -> Path | None:
    """The install directory this tool created for a game, i.e. the one it may
    delete: <install_base recorded at install time>/<slug>. An --add-steam game
    lives wherever the user put it, so nothing is owned."""
    if entry.get('source') == 'add-steam' or not valid_slug(slug):
        return None
    base = Path(entry.get('install_base') or install_base)
    d = base / slug
    # Never anything but a direct child of the base: a symlinked game folder
    # pointing elsewhere is left alone.
    try:
        if d.is_symlink() or d.resolve().parent != base.resolve():
            return None
    except OSError:
        return None
    return d


def _owned_artwork_dir(entry: dict, slug: str) -> Path | None:
    """The artwork folder uninstall may delete: only one directly inside
    ARTWORK_DIR, whatever the registry entry says."""
    d = Path(entry.get('artwork_dir') or (ARTWORK_DIR / slug))
    try:
        if d.is_symlink() or d.resolve().parent != ARTWORK_DIR.resolve():
            return None
    except OSError:
        return None
    return d


UNINSTALL_TIMEOUT = 1800        # a big game's uninstaller can take a while


def _run_uninstaller(unins: Path) -> bool:
    """A Windows game's Inno uninstaller, silently, as administrator. Inno
    copies it to %TEMP% and runs the copy, so the process we start exits at
    once; the uninstall is over when unins000.exe itself is gone. False when
    the admin prompt was declined (nothing removed then)."""
    print(f"Running {unins.name} (Windows asks for admin)...")
    try:
        proc = platform.run_elevated(unins, ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'],
                                     cwd=unins.parent)
    except platform.ElevationCancelled:
        print("Uninstall cancelled at the admin prompt. Nothing was removed.", file=sys.stderr)
        return False
    except OSError as e:
        print(f"  [!] Couldn't run {unins.name} ({e}); removing the folder instead.")
        return True
    proc.wait()
    deadline = time.monotonic() + UNINSTALL_TIMEOUT
    while unins.exists() and time.monotonic() < deadline:
        time.sleep(1)
    if unins.exists():
        print(f"  [!] {unins.name} is still there after {UNINSTALL_TIMEOUT // 60} min; "
              f"removing what's left anyway.")
    return True


def cmd_uninstall(install_base: Path, slug: str, yes: bool = False,
                  save_backup: bool = False, remove_snapshots: bool = False) -> None:
    entry = registry_get(slug) or {}
    owned_dir = _owned_game_dir(entry, install_base, slug)
    if owned_dir is not None and not owned_dir.exists():
        owned_dir = None
    desktop = _desktop_path(slug)
    artwork_dir = _owned_artwork_dir(entry, slug)
    launcher = Path(entry['launcher']) if entry.get('launcher') else None

    if not entry and not owned_dir and not desktop.exists():
        print(f"Nothing found for slug '{slug}'")
        sys.exit(1)

    # Deleting the folder of a running game fails halfway (Windows) or pulls
    # files out from under it (Linux); the GUI checks too, the CLI is the gate.
    if entry and game_processes(entry):
        print(f"'{entry.get('display_name') or slug}' is running. Quit it and try again; "
              f"nothing was removed.", file=sys.stderr)
        sys.exit(2)

    # Steam must be closed to remove the shortcut. Check this BEFORE deleting
    # anything: deleting the prefix and registry entry while leaving the
    # shortcut, its compatdata symlink and forced-tool mapping behind strands
    # all three with nothing left that knows about them.
    has_shortcut = game_has_steam_shortcut(slug)
    if has_shortcut and steam_is_running():
        if yes:
            print("Steam is running, and this game has a Steam shortcut that can only be "
                  "removed while Steam is closed. Quit Steam and try again.",
                  file=sys.stderr)
            sys.exit(2)
        if not _wait_for_steam_closed():
            print("Aborted — nothing was removed (Steam is still running).")
            sys.exit(2)

    windows = entry.get('platform') == 'windows'
    save_entry = None
    if windows:                 # no prefix: the Ludusavi manifest says where (nexus.saves)
        save_hits = []
        if owned_dir and (save_backup or not yes):
            save_entry, why = manifest_save_entry(entry)
            if save_entry is None:
                print(f"  {why}: nothing is backed up, and any saves in your user folder "
                      f"are left where they are.")
            else:
                save_hits = [f for f, _root in saves.resolve(save_entry, owned_dir,
                                                             noise=_is_save_noise)]
    else:
        save_hits = find_prefix_savedata(owned_dir) if owned_dir else []

    # Snapshots hold copies of the saves, so they go only when asked; the
    # integrity baseline is only checksums and would be wrong for a reinstall.
    snaps = snapshot_dirs(entry, slug) if entry else []
    baseline = integrity_manifest_path(slug)

    print(f"About to remove '{slug}':")
    unins = Path(entry['uninstaller']) if windows and entry.get('uninstaller') else None
    if unins and unins.is_file():
        print(f"  - run its uninstaller: {unins} (asks for admin)")
    elif windows and owned_dir:
        print("  - no uninstaller found: the folder is removed instead")
    if owned_dir:
        print(f"  - install: {owned_dir}")
    if launcher and launcher.exists() and launcher.parent == LAUNCHERS_DIR:
        print(f"  - launcher: {launcher}")
    if desktop.exists():
        print(f"  - desktop: {desktop}")
    if artwork_dir and artwork_dir.exists():
        print(f"  - artwork: {artwork_dir}")
    if baseline.exists():
        print(f"  - integrity baseline: {baseline}")
    print("  - matching Steam shortcut + Heroic entry (if any)")
    print("  (install logs are kept)")

    do_backup = save_backup
    if save_hits and windows:
        print(f"  Found {len(save_hits)} save file(s) where this game keeps them.")
        if save_backup:
            print("  They will be backed up and offered back if you reinstall this game.")
        else:
            try:
                ans = input("  Back them up before removing? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
            do_backup = ans in ('y', 'yes')
    elif save_hits:
        if save_backup:
            print(f"  Found what looks like save data ({len(save_hits)}+ file(s)) — "
                  f"it will be backed up to ~/.local/share/nexus/saves/{slug}/ "
                  f"and offered back if you reinstall this game.")
        elif yes:
            print(f"  Found what looks like save data ({len(save_hits)}+ file(s)) — "
                  f"NOT backing it up (pass --save-backup to keep it). It will be deleted "
                  f"with the rest of the prefix.")
        else:
            print(f"  Found what looks like save data ({len(save_hits)}+ file(s)).")
            try:
                ans = input("  Back it up before removing? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
            do_backup = ans in ('y', 'yes')

    drop_snaps = remove_snapshots
    if snaps:
        where = snaps[0].parent
        if remove_snapshots:
            print(f"  Its {len(snaps)} snapshot(s) in {where} will be deleted too, and the "
                  f"save copies in them with them.")
        elif yes:
            print(f"  Its {len(snaps)} snapshot(s) in {where} are kept (they hold copies of "
                  f"its saves); pass --remove-snapshots to delete them, or use Storage > "
                  f"Clean up later.")
        else:
            print(f"  It has {len(snaps)} snapshot(s) in {where}; they hold copies of its "
                  f"saves.")
            try:
                ans = input("  Delete them too? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
            drop_snaps = ans in ('y', 'yes')

    if not yes:
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans not in ('y', 'yes'):
            print("Aborted.")
            return

    if save_hits and do_backup:
        result = (backup_manifest_saves(slug, owned_dir, save_entry) if windows
                  else backup_prefix_saves(slug, owned_dir))
        if result and result[2]:
            _report_failed_copies(result[2], "save file(s)")
            print(f"  Aborting before deleting anything; the partial backup is in "
                  f"{result[0]}.", file=sys.stderr)
            sys.exit(1)
        if result:
            backup_dir, count, _ = result
            print(f"  Backed up {count} save file(s) to {backup_dir}")
        elif save_backup:
            print("  [!] Save backup found nothing to copy — aborting before deleting "
                  "anything.", file=sys.stderr)
            sys.exit(1)

    if unins and unins.is_file() and not _run_uninstaller(unins):
        sys.exit(1)

    if has_shortcut and not remove_steam_shortcut(slug, require_steam_closed=False):
        print("  [!] Could not remove the Steam shortcut — aborting before deleting "
              "anything else.", file=sys.stderr)
        sys.exit(1)
    remove_heroic_entry(slug, exe=entry.get('exe'))

    if desktop.exists():
        try:
            desktop.unlink()
        except OSError:
            pass
    # Only delete an external launcher (in LAUNCHERS_DIR); a game_dir/launch.sh
    # is removed with the prefix below.
    if launcher and launcher.exists() and launcher.parent == LAUNCHERS_DIR:
        try:
            launcher.unlink()
        except OSError:
            pass
    if artwork_dir and artwork_dir.exists():
        shutil.rmtree(artwork_dir, ignore_errors=True)
    if windows:                 # what the uninstaller left: to the Recycle Bin
        failed = []
        if owned_dir and owned_dir.exists():
            try:
                platform.trash(owned_dir)
            except OSError as e:
                failed = [f"{owned_dir} ({e})"]
    else:
        failed = _rmtree_collect(owned_dir) if owned_dir else []
    if shutil.which('update-desktop-database'):
        try:
            subprocess.run(['update-desktop-database', str(APPLICATIONS_DIR)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if failed:
        # Keep the registry entry so the game stays visible and can be retried.
        print(f"  [!] Could not delete {len(failed)} path(s) under {owned_dir}, e.g. "
              f"{failed[0]}", file=sys.stderr)
        print(f"Partially removed '{slug}' — fix the permissions and run --uninstall "
              f"again.", file=sys.stderr)
        sys.exit(1)
    try:
        baseline.unlink(missing_ok=True)
    except OSError as e:
        print(f"  [!] Could not delete the integrity baseline {baseline} ({e})",
              file=sys.stderr)
    if snaps and drop_snaps:
        left = [p for s in snaps for p in remove_game_dir(s)]
        if left:
            print(f"  [!] Could not delete {len(left)} path(s) of its snapshots, e.g. "
                  f"{left[0]}; Storage > Clean up lists them.", file=sys.stderr)
        else:
            try:
                snaps[0].parent.rmdir()
            except OSError:
                pass
            print(f"  Deleted {len(snaps)} snapshot(s).")
    unregister_game(slug)
    print(f"Removed '{slug}'.")


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .desktop import _desktop_path  # noqa: E402
from .heroic import remove_heroic_entry  # noqa: E402
from .integrity import integrity_manifest_path  # noqa: E402
from .registry import registry_get, unregister_game  # noqa: E402
from .savedata import (_is_save_noise, _report_failed_copies, backup_manifest_saves,
    backup_prefix_saves, find_prefix_savedata, manifest_save_entry)  # noqa: E402
from .snapshots import remove_game_dir, snapshot_dirs  # noqa: E402
from .steam import _wait_for_steam_closed, steam_is_running  # noqa: E402
from .sunshine import game_has_steam_shortcut, game_processes, remove_steam_shortcut  # noqa: E402
