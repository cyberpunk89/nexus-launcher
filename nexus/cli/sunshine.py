"""Sunshine (game streaming): one apps.json entry per game."""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from nexus import features, paths as NX, platform, saves

from .common import (APPLICATIONS_DIR, ARTWORK_DIR, atomic_write, backup_file, CLI_BIN,
    LAUNCHERS_DIR, LOG_DIR, make_slug, STEAM_ROOT, valid_slug)


SUNSHINE_APPS = Path.home() / '.config/sunshine/apps.json'
SUNSHINE_ART = NX.DATA_DIR / 'sunshine'


def _cli_command() -> str:
    """How Sunshine should call us: the nexus-cli on PATH, else this repo's."""
    return shutil.which('nexus-cli') or str(CLI_BIN)


def _sunshine_marker(g: dict) -> tuple[str, str]:
    """The option and value that identify our entry for a game inside its
    command line."""
    if g.get('source', 'nexus') == 'nexus':
        return '--launch', g['slug']
    return '--open-url', g['launcher']


def _has_option(cmd, opt: str, value: str) -> bool:
    """`opt value` as two whole words of a shell command line ('--launch foo'
    must not match '--launch foo-bar')."""
    try:
        toks = shlex.split(cmd) if isinstance(cmd, str) else []
    except ValueError:
        return False
    return any(a == opt and b == value for a, b in zip(toks, toks[1:]))


def _sunshine_png(g: dict) -> str | None:
    """Sunshine's box art must be a PNG: convert the game's cover once (Qt's
    image reader, else ImageMagick). None when there is no local cover."""
    src = g.get('cover') or _cover_path(g.get('artwork_dir'))
    if not src or not Path(src).is_file():
        return None
    dest = SUNSHINE_ART / f"{make_slug(g['slug'])}.png"
    try:
        if dest.exists() and dest.stat().st_mtime >= Path(src).stat().st_mtime:
            return str(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if src.lower().endswith('.png'):
        try:
            shutil.copyfile(src, dest)
            return str(dest)
        except OSError:
            return None
    try:
        from PyQt6.QtGui import QImage
        img = QImage(src)
        if not img.isNull() and img.save(str(dest), 'PNG'):
            return str(dest)
    except ImportError:
        pass
    if shutil.which('magick'):
        r = subprocess.run(['magick', src, str(dest)], capture_output=True, timeout=30)
        if r.returncode == 0 and dest.exists():
            return str(dest)
    return None


def sunshine_entry(g: dict) -> dict:
    """The apps.json entry for a library game. A repack runs in the
    foreground (`--launch … --wait`), so the stream ends when the game does;
    Steam and Heroic games are handed to their launcher (detached)."""
    entry = {'name': g.get('display_name') or g.get('name') or g['slug'],
             'auto-detach': True, 'wait-all': True, 'exit-timeout': 5,
             'exclude-global-prep-cmd': False}
    cli = shlex.quote(_cli_command())
    if g.get('source', 'nexus') == 'nexus':
        entry['cmd'] = f"{cli} --launch {shlex.quote(g['slug'])} --wait"
    else:
        entry['detached'] = [f"{cli} --open-url {shlex.quote(g['launcher'])}"]
    png = _sunshine_png(g)
    if png:
        entry['image-path'] = png
    return entry


def _sunshine_find(apps: list, g: dict) -> int | None:
    opt, value = _sunshine_marker(g)
    for i, a in enumerate(apps):
        cmds = [a.get('cmd') or ''] + list(a.get('detached') or [])
        if any(_has_option(c, opt, value) for c in cmds):
            return i
    return None


def sunshine_has(g: dict) -> bool:
    try:
        data = json.loads(SUNSHINE_APPS.read_text(encoding='utf-8'))
        return _sunshine_find(data.get('apps') or [], g) is not None
    except (OSError, ValueError, AttributeError):
        return False


def cmd_sunshine(ident: str, remove: bool = False) -> None:
    """Add (or update) / remove a game's Sunshine entry. apps.json is backed
    up first and never rewritten when it can't be parsed."""
    g = _game_by_id(ident)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    if g.get('source', 'nexus') != 'nexus' and not re.match(
            r'^(steam|heroic)://[\w/.?=&-]+$', g.get('launcher') or ''):
        print(f"{ident!r} has no launch URL Sunshine could use", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(SUNSHINE_APPS.read_text(encoding='utf-8'))
        apps = data['apps']
        if not isinstance(apps, list):
            raise ValueError("apps is not a list")
    except FileNotFoundError:
        if remove:
            print("Sunshine has no apps.json — nothing to remove.")
            return
        if not SUNSHINE_APPS.parent.is_dir():
            print("Sunshine isn't set up (no ~/.config/sunshine).", file=sys.stderr)
            sys.exit(1)
        data, apps = {'env': {}, 'apps': []}, None
        data['apps'] = apps = []
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Not touching {SUNSHINE_APPS}: it can't be read ({e}).", file=sys.stderr)
        sys.exit(1)
    i = _sunshine_find(apps, g)
    name = g.get('display_name') or g.get('name') or g['slug']
    if remove:
        if i is None:
            print(f"{name} isn't in Sunshine.")
            return
        apps.pop(i)
        verb = "Removed"
    else:
        new = sunshine_entry(g)
        if i is None:
            apps.append(new)
        else:
            # Keep anything the user added in Sunshine's UI (prep commands,
            # its uuid…); refresh only what Nexus owns.
            apps[i] = {**apps[i], **new}
        verb = "Updated" if i is not None else "Added"
    backup_file(SUNSHINE_APPS)
    atomic_write(SUNSHINE_APPS, json.dumps(data, indent=4) + '\n')
    prep = {'Added': 'to', 'Updated': 'in', 'Removed': 'from'}[verb]
    print(f"{verb} {name} {prep} Sunshine. Restart Sunshine "
          f"(or save any app in its web UI) for Moonlight to see the change.")


def game_has_steam_shortcut(slug: str) -> bool:
    """Read-only: does the chosen Steam profile hold a shortcut for this game?"""
    vdf, sc_path = _read_shortcuts()
    if not (vdf and sc_path and sc_path.exists()):
        return False
    data = load_shortcuts(sc_path)
    return bool(data and split_game_shortcuts(data['shortcuts'], slug)[1])


def remove_steam_shortcut(slug: str, require_steam_closed: bool = True) -> bool:
    """Remove this game's Steam shortcut(s) (see split_game_shortcuts), their
    grid art, forced compat tool and compatdata symlink. Returns True if the
    file was rewritten. Best-effort."""
    vdf, sc_path = _read_shortcuts(announce=True)
    if not (vdf and sc_path and sc_path.exists()):
        return False
    if require_steam_closed and not _wait_for_steam_closed():
        print("  [i] Skipped Steam shortcut removal (Steam running / opted out).")
        return False
    data = load_shortcuts(sc_path)
    if data is None:
        return False
    others, ours = split_game_shortcuts(data['shortcuts'], slug)
    if not ours:
        return False
    data['shortcuts'] = {str(i): e for i, e in enumerate(others)}
    if not save_shortcuts(sc_path, data):
        return False
    grid = sc_path.parent / 'grid'
    for au in (_shortcut_uid(e) for e in ours):
        for p in grid.glob(f"{au}*"):
            try:
                p.unlink()
            except OSError:
                pass
        # Native-mode cleanup: drop the forced compat tool and the
        # compatdata symlink we created for this appid.
        _write_compat_mapping(au, None)
        link = STEAM_ROOT / "steamapps" / "compatdata" / str(au)
        if link.is_symlink():
            try:
                link.unlink()
            except OSError:
                pass
    set_registry_steam_appid(slug, None)
    print(f"  Removed Steam shortcut(s) for '{slug}'.")
    return True


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
                  save_backup: bool = False) -> None:
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
    unregister_game(slug)
    print(f"Removed '{slug}'.")


def launch_log_path(slug: str) -> Path:
    return LOG_DIR / f"{slug}-last-launch.log"


def _prefix_markers(entry: dict) -> set[str]:
    """The real paths a game's Wine processes carry as WINEPREFIX (umu, wine)
    or STEAM_COMPAT_DATA_PATH (proton)."""
    out = set()
    for k in ('wine_prefix', 'compatdata'):
        if entry.get(k):
            out.add(os.path.realpath(entry[k]).rstrip('/'))
    return out


def prefix_processes(markers: set[str]) -> list[int]:
    """Processes running in one of these prefixes, found by the environment
    every Wine process inherits — whatever the game's exe ends up being
    called (a launcher that starts the real game and exits included)."""
    pids, me = [], os.getpid()
    if not markers:
        return pids
    for p in platform.processes():
        if p.pid == me:
            continue
        env = p.environ()
        for k in (b'WINEPREFIX', b'STEAM_COMPAT_DATA_PATH'):
            if k in env and \
                    os.path.realpath(env[k].decode('utf-8', 'replace')).rstrip('/') in markers:
                pids.append(p.pid)
                break
    return pids


def dir_processes(install_dir: str) -> list[int]:
    """Processes whose exe sits under install_dir: how a Windows game is
    followed (no prefix there), whatever its launcher stub hands over to."""
    root = os.path.normcase(os.path.realpath(install_dir)).rstrip('\\/') + os.sep
    me = os.getpid()
    return [p.pid for p in platform.processes()
            if p.pid != me and os.path.normcase(p.exe or '').startswith(root)]


def game_processes(entry: dict) -> list[int]:
    """A game's processes: by its prefix on Linux, by its folder on Windows."""
    markers = _prefix_markers(entry)
    if markers:
        return prefix_processes(markers)
    return dir_processes(entry['install_dir']) if entry.get('install_dir') else []


def steam_shortcut_url(entry: dict) -> str | None:
    """steam://rungameid/ for a game's non-Steam shortcut (its 64-bit game
    id: the shortcut appid in the high half, 0x02000000 marking a shortcut)."""
    try:
        appid = int(entry.get('steam_appid') or 0)
    except (TypeError, ValueError):
        return None
    if appid < 0x80000000:
        return None
    return f"steam://rungameid/{(appid << 32) | 0x02000000}"


def cmd_launch_via(slug: str, via: str) -> None:
    """Whether Play starts a repack itself (launch.sh, Nexus's tracker) or
    through its Steam shortcut (Steam Input, overlay, recording, presence)."""
    entry = _require_entry(slug)
    if via == 'steam' and not steam_shortcut_url(entry):
        print(f"'{slug}' isn't in Steam yet — add it first (nexus-cli --steam {slug}).",
              file=sys.stderr)
        sys.exit(1)
    update_registry_entry(slug, launch_via='steam' if via == 'steam' else None)
    print(f"'{slug}' now launches {'through Steam' if via == 'steam' else 'from Nexus'}.")


def _launch_through_steam(slug: str, entry: dict, wait: bool) -> None:
    from nexus import steamlib
    url = steam_shortcut_url(entry)
    if not steamlib.open_url(url):
        print(f"Nothing here can open {url} (on Linux: no `steam` or `xdg-open`).",
              file=sys.stderr)
        sys.exit(1)
    touch_last_played(slug)
    print(f"Launched '{slug}' through Steam ({url}).")
    if wait:                     # Sunshine: wait for the game to show up, then to end
        deadline = time.monotonic() + 120
        while not game_processes(entry) and time.monotonic() < deadline:
            time.sleep(2)
        while game_processes(entry):
            time.sleep(5)


def cmd_launch(install_base: Path, slug: str, force: bool = False, wait: bool = False,
               try_proton: str | None = None) -> None:
    """Start a game through its launch tracker. `try_proton`: this one run
    with another installed Proton (umu launchers read PROTONPATH from the
    environment), nothing saved — the crash helper's "Try another Proton"."""
    entry = registry_get(slug) or {}
    if not force:
        from nexus import steamlib
        if slug in steamlib.running()['slugs'] or game_processes(entry):
            print(f"'{slug}' is already running (pass --force to start it again).",
                  file=sys.stderr)
            sys.exit(3)
    if entry.get('launch_via') == 'steam' and steam_shortcut_url(entry) \
            and not os.environ.get('NEXUS_MANGOHUD_EXTRA'):      # a benchmark runs here
        _launch_through_steam(slug, entry, wait)
        return
    launcher = entry.get('launcher')
    if not launcher and not features.has('proton'):
        launcher = entry.get('exe')              # Windows: the game's exe itself...
        if 'appx' not in entry and launcher:     # (an entry from before Store apps)
            from .install_win import packaged_app
            entry['appx'] = packaged_app(Path(launcher))
            update_registry_entry(slug, appx=entry['appx'])
        if entry.get('appx'):                    # ...or a Store app, as its package
            from nexus import appx
            launcher = appx.shell_uri(entry['appx'])
    if launcher and launcher.startswith('shell:AppsFolder\\'):
        pass                                     # nothing on disk to check
    elif not launcher:
        cand = install_base / slug / 'launch.sh'
        launcher = str(cand) if cand.exists() else None
    if not launcher or not (launcher.startswith('shell:AppsFolder\\')
                            or Path(launcher).exists()):
        print(f"No launcher found for '{slug}'", file=sys.stderr)
        sys.exit(1)
    env = None
    if try_proton and not features.has('proton'):
        print("Trying another Proton is a Linux thing.", file=sys.stderr)
        sys.exit(1)
    if try_proton:
        hit = proton_by_name(try_proton)
        if not hit:
            print(f"Proton '{try_proton}' isn't installed (see --list-protons)", file=sys.stderr)
            sys.exit(1)
        try:
            umu = 'DEFAULT_PROTONPATH=' in Path(launcher).read_text(encoding='utf-8',
                                                                     errors='replace')
        except OSError:
            umu = False
        if not umu:
            print("Trying another Proton for one run needs a umu launcher (runner umu); "
                  f"set it for good with --proton {slug} {hit.parent.name} instead.",
                  file=sys.stderr)
            sys.exit(1)
        env = {**os.environ, 'PROTONPATH': str(hit.parent)}
        print(f"This run only: {hit.parent.name}.")
    # A detached tracker runs the game and times the session (play time), and
    # keeps its output where the GUI's log viewer finds it.
    out = launch_log_path(slug)
    launched = datetime.now()
    tracker = platform.spawn_detached(
        [*NX.cli_command(same_python=True), '--track-session', slug, launcher], env=env)
    try:
        rc = tracker.wait(timeout=3)
    except subprocess.TimeoutExpired:
        rc = None      # still running — the normal case for a game
    if rc not in (None, 0):
        print(f"'{slug}' exited immediately with code {rc}. Output: {out}", file=sys.stderr)
        for line in _defender_notes(entry, launched):
            print(f"  {line}", file=sys.stderr)
        try:
            tail = out.read_text(encoding='utf-8', errors='replace').strip().splitlines()[-10:]
            for line in tail:
                print(f"  {line}", file=sys.stderr)
        except OSError:
            pass
        sys.exit(1)
    print(f"Launched '{slug}' (output: {out}).")
    if wait and rc is None:             # Sunshine: the stream lasts as long as the game
        sys.stdout.flush()
        sys.exit(tracker.wait())


def cmd_track_session(slug: str, launcher: str) -> None:
    """Internal (spawned by --launch): run the game, wait for it, and add the
    session's length to the registry's play time. Exits with the game's code."""
    started = datetime.now()
    touch_last_played(slug)
    out = launch_log_path(slug)
    out.parent.mkdir(parents=True, exist_ok=True)
    entry = registry_get(slug) or {}
    config = load_config()
    from nexus import presence, compat, session
    name = entry.get('display_name') or slug
    awake = presence.inhibit(f"Playing {name}") if config['keep_awake'] else None
    discord = presence.discord_presence(config['discord_client_id'], name,
                                        compat.store_appid(entry))
    notes: list[str] = []
    undo = session.enter(say=notes.append)
    watcher = _achievement_watcher(entry, name)
    try:
        rc, elapsed = _run_session(slug, launcher, out, started, notes)
    finally:
        if watcher:
            watcher.stop()
        notes = []
        session.leave(undo, say=notes.append)
        if notes:
            try:
                with open(out, 'a', encoding='utf-8') as fh:
                    fh.write('\n'.join(notes) + '\n')
            except OSError:
                pass
        if discord:
            discord.close()
        presence.release(awake)
    if elapsed >= 30:           # an immediate crash isn't play time
        from nexus import history
        history.record(slug, 'nexus', started, datetime.now(), name)
        with registry_lock():
            reg = load_registry()
            e = reg['games'].get(slug)
            if e is not None:
                e['playtime_seconds'] = int(e.get('playtime_seconds') or 0) + int(elapsed)
                e['last_played'] = datetime.now().isoformat(timespec='seconds')
                save_registry(reg)
    sys.exit(rc)


def _achievement_watcher(entry: dict, game_name: str):
    """While a repack plays: a desktop notification for every achievement
    its Steam emulator records (with the achievement's icon)."""
    from nexus import emuach, compat
    if not entry.get('wine_prefix') or not shutil.which('notify-send'):
        return None
    prefix = Path(entry['wine_prefix'])
    icons = NX.CACHE_DIR / 'achicons'

    def unlocked(a: dict):
        args = ['notify-send', '-a', 'Nexus', '-u', 'normal']
        url = a.get('icon_url') or ''
        if url.startswith('https://'):
            dest = icons / (hashlib.sha1(url.encode()).hexdigest()[:20] + '.jpg')
            try:
                if not dest.exists():
                    from nexus import net
                    net.download(url, dest, timeout=10, limit=1 << 20)
                args += ['-i', str(dest)]
            except Exception:                    # noqa: BLE001 — the text is enough
                pass
        subprocess.run(args + ['--', f"Achievement unlocked — {game_name}",
                               f"{a.get('title') or a.get('name')}"
                               + (f"\n{a['desc']}" if a.get('desc') else '')],
                       capture_output=True, timeout=10)
    w = emuach.Watcher(prefix, unlocked, STEAM_ROOT)
    appid = compat.store_appid(entry)
    if appid:                                    # warm the schema cache off the clock
        threading.Thread(target=emuach.schema, args=(appid, STEAM_ROOT), daemon=True).start()
    w.start()
    return w


# Windows: how long a launcher stub that exited cleanly may take to hand
# over to the real game before the session is over, and a Store app to
# start once Windows was asked to.
HANDOVER_GRACE = 10
APPX_GRACE = 30
APPX_RETRY = 10


def _defender_notes(entry: dict, since: datetime) -> list[str]:
    """Windows: what Defender took from the game's folder since `since`,
    the usual reason a game that installed fine won't start (a cracked DLL)."""
    if not entry.get('install_dir'):
        return []
    return [f"Windows Defender removed {d['file']} ({d['threat']}) at {d['at']}: restore or "
            f"allow it in Windows Security > Protection history"
            for d in platform.defender_detections([entry['install_dir']])
            if d['at'] >= since.isoformat(timespec='seconds')]


def _run_session(slug: str, launcher: str, out: Path, started: datetime,
                 notes: list[str] = ()) -> tuple[int, float]:
    """Run launch.sh (Linux) or the game's exe from its own folder (Windows),
    then wait for anything left in the game's prefix or folder. Returns
    (exit code, seconds played)."""
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(f"# {slug} launched {started.isoformat(timespec='seconds')}\n")
        for n in notes:
            fh.write(n + '\n')
        fh.flush()
        packaged = launcher.startswith('shell:AppsFolder\\')
        exe = not launcher.endswith('.sh')
        if packaged:                # a Store app: Windows starts it, we follow its folder
            rc = 0 if platform.open_url(launcher) else 1
        else:
            rc = subprocess.run([launcher] if exe else ['bash', launcher],
                                cwd=Path(launcher).parent if exe else None,
                                stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL).returncode
        # A launcher stub can start the real game and exit; the session lasts
        # as long as anything still runs in the game's prefix (or folder).
        entry = registry_get(slug) or {}
        grace = (0 if not exe or rc != 0          # a crash hands over nothing
                 else APPX_GRACE if packaged else HANDOVER_GRACE)
        t0 = time.monotonic()
        lingering, quiet_since, last_seen, asked_again = False, t0, t0, False
        while True:
            now = time.monotonic()
            if game_processes(entry):
                lingering, quiet_since = True, now
                last_seen = now
            elif now - quiet_since >= grace:
                break
            elif packaged and not lingering and not asked_again and now - t0 >= APPX_RETRY:
                # Windows ignores a start while the app is still closing from
                # the last run: ask once more.
                asked_again = True
                platform.open_url(launcher)
                fh.write(f"# not up after {APPX_RETRY} s; asked Windows again\n")
            time.sleep(5 if lingering or not grace else 1)
        if lingering:
            fh.write("\n# the launcher exited first; waited for the game's processes\n")
        elif grace:                 # Windows: it never came up (a crash: cmd_launch says)
            for line in _defender_notes(entry, started):
                fh.write(f"# {line}\n")
        elapsed = (datetime.now() - started).total_seconds()
        if grace:                   # Windows: play ends when the game was last seen
            elapsed = last_seen - t0 if lingering else 0.0
        fh.write(f"\n# exited {rc} after {elapsed / 60:.1f} min\n")
    return rc, elapsed


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import _require_entry  # noqa: E402
from .config import load_config  # noqa: E402
from .desktop import _desktop_path  # noqa: E402
from .heroic import remove_heroic_entry  # noqa: E402
from .manage import _cover_path, _game_by_id, _read_shortcuts  # noqa: E402
from .registry import (load_registry, registry_get, registry_lock, save_registry,
    set_registry_steam_appid, touch_last_played, unregister_game, update_registry_entry)  # noqa: E402
from .steam import (_is_save_noise, _report_failed_copies, _shortcut_uid,
    _wait_for_steam_closed, _write_compat_mapping, backup_manifest_saves, backup_prefix_saves,
    find_prefix_savedata, load_shortcuts, manifest_save_entry, save_shortcuts,
    split_game_shortcuts, steam_is_running)  # noqa: E402
from .runner import proton_by_name  # noqa: E402
