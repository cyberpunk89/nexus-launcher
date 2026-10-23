"""config.toml: reading it, and where games get installed."""

import os
import sys
from pathlib import Path

from .common import CONFIG_PATH, default_install_base, default_scan_dir


def load_config() -> dict:
    """Load ~/.config/nexus/config.toml. Returns a flat dict with
    keys: api_key, steam, steam_account, desktop, install_base, runner.
    Missing file / parse error -> defaults."""
    cfg = {'api_key': None, 'steam': True, 'steam_account': None,
           'desktop': True, 'heroic': True, 'proton': None,
           'heroic_proton': None, 'install_base': None, 'scan_dir': None, 'scan_dirs': [],
           'unattended': True, 'trash_repack': False, 'runner': 'auto',
           'discord_client_id': None, 'keep_awake': True, 'subvolumes': True,
           'snapshots_keep': 1}

    def as_bool(v, default=True):
        # `steam = "no"` is a truthy string; only real TOML booleans count.
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ('1', 'true', 'yes', 'on')
        return default

    try:
        import tomllib
    except ImportError:
        print("  [!] Python < 3.11 (no tomllib) — config.toml ignored.", file=sys.stderr)
        tomllib = None
    try:
        if tomllib is None:
            raise OSError
        data = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))  # Notepad's BOM
        cfg['api_key'] = data.get('steamgriddb', {}).get('api_key') or None
        integ = data.get('integrations', {})
        cfg['steam'] = as_bool(integ.get('steam', True))
        # steam_mode is gone -- Steam shortcuts always point at the game .exe
        # now. Warn once so an old config doesn't look like it still does
        # something.
        if (integ.get('steam_mode') or '').lower() == 'launcher':
            print("  [i] config [integrations] steam_mode is no longer used — Steam "
                  "shortcuts always point at the game .exe now.", file=sys.stderr)
        cfg['steam_account'] = (integ.get('steam_account') or None)
        cfg['desktop'] = as_bool(integ.get('desktop', True))
        cfg['heroic'] = as_bool(integ.get('heroic', True))
        cfg['heroic_proton'] = data.get('heroic', {}).get('proton') or None
        cfg['install_base'] = data.get('install', {}).get('base') or None
        cfg['scan_dir'] = data.get('install', {}).get('scan_dir') or None
        dirs = data.get('install', {}).get('scan_dirs') or []
        cfg['scan_dirs'] = [str(d) for d in dirs] if isinstance(dirs, list) else [str(dirs)]
        # Silent installs (no setup window) unless turned off.
        cfg['unattended'] = as_bool(data.get('install', {}).get('unattended', True))
        cfg['trash_repack'] = as_bool(data.get('install', {}).get('trash_repack', False),
                                      default=False)
        cfg['runner'] = (data.get('runner', {}).get('backend') or 'auto').lower()
        # The Proton games are *played* with (launch.sh, Steam, Heroic). The
        # older [heroic] proton key means the same thing and is still honoured.
        cfg['proton'] = data.get('runner', {}).get('proton') or None
        # Discord Rich Presence needs the id of an application you created at
        # discord.com/developers; none set = no presence.
        cid = str(data.get('discord', {}).get('client_id') or '').strip()
        cfg['discord_client_id'] = cid if cid.isdigit() else None
        cfg['keep_awake'] = as_bool(integ.get('keep_awake', True))
        # On btrfs each game gets its own subvolume, so it can be snapshotted.
        cfg['subvolumes'] = as_bool(data.get('install', {}).get('subvolumes', True))
        try:
            cfg['snapshots_keep'] = max(1, min(20, int(
                data.get('snapshots', {}).get('keep', 1))))
        except (TypeError, ValueError):
            pass
    except OSError:
        pass
    except ValueError as e:
        print(f"  [!] {CONFIG_PATH} is not valid TOML ({e}) — using defaults.",
              file=sys.stderr)
    # Environment override for the API key
    cfg['api_key'] = os.environ.get('STEAMGRIDDB_API_KEY') or cfg['api_key']
    return cfg


def resolve_install_base(cli_value: str | None) -> Path:
    """Resolve the install base with precedence: --install-dir > config
    [install] base > default. `cli_value` is None when the flag is absent."""
    if cli_value:
        return Path(cli_value).expanduser()
    configured = load_config().get('install_base')
    if configured:
        return Path(configured).expanduser()
    return default_install_base()


def resolve_scan_dirs(cli_value: str | None) -> list[Path]:
    """Folders --scan looks in: --scan-dir > config [install] scan_dir plus
    scan_dirs (a list, for repacks kept in more than one place) >
    ~/Downloads."""
    if cli_value:
        return [Path(cli_value).expanduser()]
    cfg = load_config()
    out: list[Path] = []
    for d in [cfg.get('scan_dir'), *(cfg.get('scan_dirs') or [])]:
        if d and Path(d).expanduser() not in out:
            out.append(Path(d).expanduser())
    return out or [default_scan_dir()]
