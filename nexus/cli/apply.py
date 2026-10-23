"""Applying a game's settings to its launchers (launch.sh, Steam, Heroic)."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .common import glob_escape, log, LOG_DIR, Logger, use_logger


def profile_owns_options(prof: dict | None) -> bool:
    """A launch profile owns the launch options once env/wrappers/args have
    been set (even to empty). One holding only a per-game `proton` doesn't,
    so the user's own Steam launch options / Heroic tuning are left alone."""
    return bool(prof) and 'env' in prof


def _entry_paths(entry: dict) -> tuple[Path | None, Path | None, Path | None]:
    """(exe, game_dir/compatdata, wine_prefix) for a registry entry."""
    wine_prefix = Path(entry['wine_prefix']) if entry.get('wine_prefix') else None
    exe = Path(entry['exe']) if entry.get('exe') else None
    if not exe and wine_prefix:
        exe = find_game_exe(wine_prefix)
    game_dir = (Path(entry['compatdata']) if entry.get('compatdata')
                else (wine_prefix.parent if wine_prefix else None))
    return exe, game_dir, wine_prefix


def _proton_is_explicit(entry: dict, config: dict) -> bool:
    return bool((entry.get('launch') or {}).get('proton')
                or config.get('proton') or config.get('heroic_proton'))


def regen_launch_sh(entry: dict, config: dict, proton: Path | None) -> Path | None:
    """Rewrite a game's launch.sh from its registry entry + launch profile."""
    launcher = entry.get('launcher')
    if not launcher:
        return None
    wine_prefix = Path(entry['wine_prefix']) if entry.get('wine_prefix') else None
    compatdata = Path(entry['compatdata']) if entry.get('compatdata') else None
    game_exe = _entry_paths(entry)[0]
    if game_exe and not entry.get('exe'):
        # Older entries never recorded the exe; without this a regenerate would
        # replace a working launcher with the "no executable" stub.
        update_registry_entry(entry['slug'], exe=str(game_exe))
    runner = select_runner(config.get('runner', 'auto'), proton)
    # A raw-wine / add-steam launcher has no compatdata: fall back to wine.
    if runner != 'wine' and not compatdata:
        runner = 'wine'
    gameid = entry.get('umu_gameid')
    if runner == 'umu' and gameid is None and entry.get('display_name'):
        gameid = resolve_umu_gameid(entry['display_name'])
        if gameid:
            update_registry_entry(entry['slug'], umu_gameid=gameid)
    return write_launch_script(Path(launcher), compatdata,
                               wine_prefix or Path(launcher).parent, game_exe,
                               entry['slug'], proton, entry.get('display_name'),
                               runner=runner, launch=entry.get('launch'), gameid=gameid)


def sync_steam(entry: dict, config: dict, proton: Path | None,
               artwork: dict | None = None) -> int | None:
    """Add or update the game's Steam shortcut from its registry entry."""
    exe, game_dir, _ = _entry_paths(entry)
    if not (exe and game_dir):
        log.print(f"  [i] Steam: '{entry['slug']}' needs a detected exe + prefix — skipping.")
        return None
    prof = entry.get('launch')
    return add_steam_shortcut_native(
        entry['display_name'], entry['slug'], exe, game_dir, proton, artwork,
        launch_options=format_launch_options(prof) if profile_owns_options(prof) else None)


def sync_heroic(entry: dict, config: dict, proton: Path | None,
                artwork: dict | None = None) -> str | None:
    """Add or update the game's Heroic sideload entry from its registry entry."""
    exe, _, wine_prefix = _entry_paths(entry)
    prof = entry.get('launch')
    return add_heroic_entry(entry['display_name'], entry['slug'], exe, wine_prefix,
                            proton, artwork,
                            force_proton=_proton_is_explicit(entry, config),
                            launch=prof if profile_owns_options(prof) else None,
                            known_app_name=entry.get('heroic_app_name'))


def _require_entry(slug: str) -> dict:
    entry = registry_get(slug)
    if not entry:
        print(f"'{slug}' is not a managed game (see --list)", file=sys.stderr)
        sys.exit(1)
    return entry


def cmd_apply(slug: str, config: dict, steam: bool | None = None,
              heroic: bool | None = None) -> None:
    """Re-apply a game's registry entry (exe, name, Proton, launch profile) to
    every launcher it has: launch.sh, the desktop entry, and — when the game is
    already in them (or `steam`/`heroic` is True) — Steam and Heroic."""
    use_logger(Logger())
    entry = _require_entry(slug)
    proton = resolve_game_proton(entry, config)
    if proton and not entry.get('proton'):
        update_registry_entry(slug, proton=proton.parent.name)
        entry['proton'] = proton.parent.name
    log.print(f"Applying settings for '{slug}' (Proton: "
              f"{proton.parent.name if proton else 'none found'})")
    sh = regen_launch_sh(entry, config, proton)
    if sh:
        log.print(f"  launch.sh: {sh}")
    if entry.get('has_desktop') and sh:
        write_desktop_entry(entry['display_name'], slug, sh, _local_artwork(slug) or None)
    if steam is None:
        steam = bool(entry.get('steam_appid')) or game_has_steam_shortcut(slug)
    if steam:
        appid = sync_steam(entry, config, proton)
        if appid:
            set_registry_steam_appid(slug, appid)
    if heroic is None:
        heroic = bool(entry.get('heroic_app_name'))
    if heroic:
        app_name = sync_heroic(entry, config, proton)
        if app_name:
            update_registry_entry(slug, heroic_app_name=app_name)


def _save_profile(slug: str, prof: dict, source: str) -> dict:
    prof = {**prof, 'source': source,
            'updated_at': datetime.now().isoformat(timespec='seconds')}
    update_registry_entry(slug, launch=prof)
    return prof


def cmd_set_launch(slug: str, text: str, config: dict) -> None:
    """Set a game's launch profile from a Steam launch-options string, a umu
    command line or a protongen preset share code, then apply it everywhere.
    An empty string clears env/wrappers/args (the per-game Proton is kept)."""
    entry = _require_entry(slug)
    old = entry.get('launch') or {}
    try:
        if text.strip().startswith(PROTONGEN_CODE_PREFIX):
            name, prof = decode_protongen_code(text)
            source = f"protongen preset '{name}'"
        else:
            prof, source = parse_launch_options(text), 'manual'
    except LaunchParseError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not prof.get('proton'):
        prof['proton'] = old.get('proton')
    if prof.get('proton') and not proton_by_name(prof['proton']):
        print(f"  [!] Proton '{prof['proton']}' is not installed — keeping the current "
              f"one. Installed: {', '.join(sorted(list_protons())) or 'none'}")
        prof['proton'] = old.get('proton')
    prof = _save_profile(slug, prof, source)
    print(f"Launch profile for '{slug}' ({source}):")
    print(f"  {format_launch_options(prof) or '(no options)'}")
    cmd_apply(slug, config)


def cmd_perf(slug: str, items: list[str], config: dict) -> None:
    """Set performance switches on a game's launch profile and apply it
    everywhere (no items: just show them)."""
    entry = _require_entry(slug)
    prof = entry.get('launch') or {}
    if not items:
        print(json.dumps(perf_state(prof), indent=1))
        return
    try:
        changes = parse_perf_args(items)
    except LaunchParseError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if changes.get('gamescope') and not shutil.which('gamescope'):
        print("gamescope isn't installed.", file=sys.stderr)
        sys.exit(1)
    if changes.get('mode') in PERF_MODES and not shutil.which(changes['mode']):
        print(f"{changes['mode']} isn't installed.", file=sys.stderr)
        sys.exit(1)
    if changes.get('mangohud') and not shutil.which('mangohud'):
        print("mangohud isn't installed.", file=sys.stderr)
        sys.exit(1)
    if 'lsfg' in changes:
        mult = changes.pop('lsfg')
        cur = lsfg_state(entry)
        if cur is None:
            if mult:
                print("lsfg-vk isn't set up (its Vulkan layer and ~/.config/lsfg-vk/conf.toml).",
                      file=sys.stderr)
                sys.exit(1)
        elif (cur.get('multiplier') or None) != mult:
            try:
                lsfg_set(entry, mult)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                print(f"lsfg-vk: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"lsfg-vk: {f'{mult}× frame generation' if mult else 'off'} for '{slug}'.")
        if not changes:
            return
    new = apply_perf(prof, changes)
    src = prof.get('source') or 'manual'
    if 'performance' not in src:
        src = f"{src} + performance settings"
    new = _save_profile(slug, new, src)
    print(f"Launch options for '{slug}':")
    print(f"  {format_launch_options(new) or '(no options)'}")
    cmd_apply(slug, config)


def cmd_set_proton(slug: str, name: str, config: dict) -> None:
    """Pin (or with '' / 'default', unpin) the Proton a game is played with."""
    entry = _require_entry(slug)
    prof = dict(entry.get('launch') or {})
    if name.strip().lower() in ('', 'default', 'none', 'auto'):
        prof.pop('proton', None)
    else:
        hit = proton_by_name(name)
        if not hit:
            print(f"Proton '{name}' is not installed. Installed: "
                  f"{', '.join(sorted(list_protons())) or 'none'}", file=sys.stderr)
            sys.exit(1)
        prof['proton'] = hit.parent.name
    if prof.get('proton') != (entry.get('launch') or {}).get('proton'):
        auto_snapshot(slug, 'proton', resolve_install_base(None))
    update_registry_entry(slug, launch=prof or None)
    cmd_apply(slug, config)


def cmd_from_protongen(slug: str, config: dict) -> None:
    """Import protongen's remembered tuning for this game (read-only from its
    state.toml) as the launch profile, then apply it everywhere."""
    entry = _require_entry(slug)
    hit = protongen_memory_for(entry)
    if not hit:
        print(f"protongen has no saved tuning for '{slug}'. Tune it in protongen "
              f"first (it lists the game via its Steam shortcut or Heroic entry).",
              file=sys.stderr)
        sys.exit(1)
    which, cfg = hit
    prof = profile_from_protongen_config(cfg)
    if prof.get('proton') and not proton_by_name(prof['proton']):
        print(f"  [!] protongen's Proton '{prof['proton']}' is not installed — ignoring it.")
        prof['proton'] = (entry.get('launch') or {}).get('proton')
    prof = _save_profile(slug, prof, f"protongen ({which} entry)")
    print(f"Imported protongen tuning for '{slug}' from its {which} entry:")
    print(f"  {format_launch_options(prof) or '(no options)'}")
    if prof.get('proton'):
        print(f"  Proton: {prof['proton']}")
    cmd_apply(slug, config)


def cmd_protongen_default(target: str, config: dict) -> None:
    """Apply protongen's default profile to one game (replacing its launch
    options), or with 'all' to every game that has no launch options of its
    own yet — tuned games are left alone."""
    if protongen_default_config() is None:
        print("protongen has no default profile. Set one in protongen (build a "
              "command, then Settings → Save as default profile).", file=sys.stderr)
        sys.exit(1)
    if target != 'all':
        slugs = [target]
    else:
        games = load_registry()['games']
        slugs = sorted(s for s, e in games.items()
                       if not profile_owns_options(e.get('launch')))
        tuned = len(games) - len(slugs)
        if tuned:
            print(f"Leaving {tuned} game(s) that already have launch options alone.")
        if not slugs:
            print("Every game already has launch options — nothing to do.")
            return
    for slug in slugs:
        entry = _require_entry(slug)
        prof = _save_profile(slug, protongen_default_profile(entry),
                             "protongen default profile")
        print(f"Applied protongen's default profile to '{slug}':")
        print(f"  {format_launch_options(prof) or '(no options)'}")
        if prof.get('proton'):
            print(f"  Proton: {prof['proton']}")
        cmd_apply(slug, config)


def cmd_show_launch(slug: str, config: dict, as_json: bool = False) -> None:
    entry = _require_entry(slug)
    use_logger(Logger())
    prof = entry.get('launch') or {}
    proton = resolve_game_proton(entry, config, quiet=True)
    pg = protongen_memory_for(entry)
    pg_prof = profile_from_protongen_config(pg[1]) if pg else None
    info = {
        'slug': slug,
        'profile': prof or None,
        'owns_options': profile_owns_options(prof),
        'launch_options': format_launch_options(prof),
        'proton': proton.parent.name if proton else None,
        'proton_pinned': prof.get('proton'),
        'protons': sorted(list_protons()),
        'protongen': ({'source': pg[0], 'launch_options': format_launch_options(pg_prof),
                       'proton': pg_prof.get('proton'),
                       'differs': [pg_prof.get(k) for k in ('env', 'wrappers', 'args', 'proton')]
                                  != [prof.get(k) for k in ('env', 'wrappers', 'args', 'proton')]}
                      if pg else None),
        'protongen_installed': bool(protongen_bin()),
        'protongen_default': ({'launch_options': format_launch_options(pd),
                               'proton': pd.get('proton'),
                               'applied': [pd.get(k) for k in ('env', 'wrappers', 'args')]
                                          == [prof.get(k) for k in ('env', 'wrappers', 'args')]}
                              if (pd := protongen_default_profile(entry)) else None),
        'perf': perf_state(prof),
        'launch_via': entry.get('launch_via') or 'nexus',
        'in_steam': bool(steam_shortcut_url(entry)),
        'lsfg': lsfg_state(entry),
        'perf_tools': {t: bool(shutil.which(t)) for t in
                       ('gamescope', 'mangohud', *PERF_MODES)},
        'niri': bool(os.environ.get('NIRI_SOCKET')),
        'display_name': entry.get('display_name'),
        'umu_gameid': entry.get('umu_gameid'),
        'exe': str(_entry_paths(entry)[0] or '') or None,
        'install_log': str(latest_install_log(slug) or '') or None,
        'launch_log': str(launch_log_path(slug)) if launch_log_path(slug).exists() else None,
    }
    if as_json:
        print(json.dumps(info))
        return
    print(f"{slug}:")
    print(f"  Proton:          {info['proton'] or 'none found'}"
          f"{'  (pinned for this game)' if prof.get('proton') else ''}")
    print(f"  umu game id:     {info['umu_gameid'] or 'umu-0 (no protonfixes match)'}")
    if info['owns_options']:
        print(f"  Launch options:  {info['launch_options'] or '(none — cleared)'}")
        print(f"  Source:          {prof.get('source', '?')}  ({prof.get('updated_at', '?')})")
    else:
        print("  Launch options:  not managed (Steam/Heroic keep their own)")
    if pg:
        print(f"  protongen:       has tuning via its {pg[0]} entry"
              f"{' — differs from this profile' if info['protongen']['differs'] else ' — in sync'}")
        print(f"                   {info['protongen']['launch_options'] or '(no options)'}")
        print(f"                   import with: nexus-cli --from-protongen {slug}")
    if info['protongen_default'] and not info['protongen_default']['applied']:
        print(f"  protongen default profile: {info['protongen_default']['launch_options']}")
        print(f"                   apply with: nexus-cli --protongen-default {slug}")


def latest_install_log(slug: str) -> Path | None:
    logs = sorted(LOG_DIR.glob(f"{glob_escape(slug)}-[0-9]*.log")) if LOG_DIR.is_dir() else []
    return logs[-1] if logs else None


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .desktop import write_desktop_entry  # noqa: E402
from .gamecmds import _local_artwork  # noqa: E402
from .heroic import add_heroic_entry  # noqa: E402
from .launchprofile import (apply_perf, decode_protongen_code, format_launch_options,
    LaunchParseError, lsfg_set, lsfg_state, parse_launch_options, parse_perf_args, PERF_MODES,
    perf_state, profile_from_protongen_config, protongen_bin, PROTONGEN_CODE_PREFIX,
    protongen_default_config, protongen_default_profile, protongen_memory_for)  # noqa: E402
from .registry import (load_registry, registry_get, set_registry_steam_appid,
    update_registry_entry)  # noqa: E402
from .repack import find_game_exe  # noqa: E402
from .runner import (list_protons, proton_by_name, resolve_game_proton, resolve_umu_gameid,
    select_runner, write_launch_script)  # noqa: E402
from .steam import add_steam_shortcut_native  # noqa: E402
from .sunshine import game_has_steam_shortcut, launch_log_path, steam_shortcut_url  # noqa: E402
from .snapshots import auto_snapshot  # noqa: E402
from .config import resolve_install_base  # noqa: E402
