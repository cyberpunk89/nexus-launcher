"""Reading and writing config.toml from the GUI."""

import json
import os
import re

from nexus import gamedb, net

from .common import CONFIG_PATH


class ConfigError(Exception):
    pass


def _read_config_toml() -> dict:
    """Parsed config.toml ({} if missing). Raises ConfigError if it exists
    but isn't valid TOML — callers must then not write over it."""
    import tomllib
    try:
        with open(CONFIG_PATH, 'rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"{CONFIG_PATH}: {e}") from None


def load_config() -> dict:
    """Flat view of config.toml. Values are None when unset, so the Settings
    dialog can tell "not configured" from "configured to the default"."""
    cfg = {'api_key': None, 'steam': True, 'steam_account': None,
           'desktop': True, 'heroic': True, 'proton': None, 'news': True,
           'heroic_proton': None, 'install_base': None,
           'scan_dir': None, 'scan_dirs': [], 'unattended': True, 'trash_repack': False,
           'runner': 'auto', 'error': None}
    try:
        data = _read_config_toml()
    except ConfigError as e:
        cfg['error'] = str(e)
        return cfg
    integ = data.get('integrations', {})
    cfg['api_key'] = data.get('steamgriddb', {}).get('api_key') or None
    for k in ('steam', 'desktop', 'heroic'):
        v = integ.get(k, True)
        cfg[k] = v if isinstance(v, bool) else str(v).lower() in ('1', 'true', 'yes', 'on')
    cfg['steam_account'] = integ.get('steam_account') or None
    cfg['heroic_proton'] = data.get('heroic', {}).get('proton') or None
    cfg['install_base'] = data.get('install', {}).get('base') or None
    cfg['scan_dir'] = data.get('install', {}).get('scan_dir') or None
    dirs = data.get('install', {}).get('scan_dirs') or []
    cfg['scan_dirs'] = [str(d) for d in dirs] if isinstance(dirs, list) else [str(dirs)]
    cfg['unattended'] = data.get('install', {}).get('unattended', True) is not False
    cfg['trash_repack'] = data.get('install', {}).get('trash_repack', False) is True
    cfg['runner'] = (data.get('runner', {}).get('backend') or 'auto').lower()
    cfg['proton'] = data.get('runner', {}).get('proton') or None
    cfg['news'] = data.get('news', {}).get('enabled', True) is not False
    gd = data.get('gamedb', {}) if isinstance(data.get('gamedb'), dict) else {}
    cfg['gamedb_url'] = gd.get('url') or None
    cfg['gamedb_token'] = gd.get('token') or None
    cfg['gamedb_cfg_enabled'] = gd.get('enabled', False) is True
    cfg['gamedb_enabled'] = gamedb.settings()['enabled']       # config, or the env token
    tr = data.get('torrent', {}) if isinstance(data.get('torrent'), dict) else {}
    client = str(tr.get('client') or '').lower()
    cfg['torrent_client'] = client if client in ('transmission', 'qbittorrent') else None
    for k in ('url', 'username', 'password', 'download_dir'):
        cfg[f'torrent_{k}'] = str(tr[k]) if tr.get(k) else None
    try:
        cfg['torrent_seed_ratio'] = float(tr['seed_ratio']) if 'seed_ratio' in tr else None
    except (TypeError, ValueError):
        cfg['torrent_seed_ratio'] = None
    al = data.get('alerts', {}) if isinstance(data.get('alerts'), dict) else {}
    for k in ('atl', 'fitgirl', 'repack_updates', 'proton'):
        cfg[f'alerts_{k}'] = al.get(k, True) is not False
    try:
        cfg['alerts_sale_min_cut'] = max(0, min(100, int(al.get('sale_min_cut', 0))))
    except (TypeError, ValueError):
        cfg['alerts_sale_min_cut'] = 0
    return cfg


def _toml_literal(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return '[' + ', '.join(json.dumps(str(x), ensure_ascii=False) for x in v) + ']'
    return json.dumps(str(v), ensure_ascii=False)   # a valid TOML basic string


def toml_update(text: str, updates: dict) -> str:
    """Set (or, with None, remove) simple `key = value` lines in [section]s,
    leaving comments, unknown keys, ordering and inline comments alone.
    `updates` maps (section, key) -> value."""
    lines = text.splitlines()
    header = re.compile(r'^\s*\[([^\[\]]+)\]\s*(#.*)?$')
    for (section, key), value in updates.items():
        start = next((i for i, l in enumerate(lines)
                      if (m := header.match(l)) and m.group(1).strip() == section), None)
        if start is None:
            if value is not None:
                if lines and lines[-1].strip():
                    lines.append('')
                lines += [f'[{section}]', f'{key} = {_toml_literal(value)}']
            continue
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])),
                   len(lines))
        kre = re.compile(rf'^(\s*{re.escape(key)}\s*=\s*)(\[[^\]]*\]|"(?:[^"\\]|\\.)*"|[^#\s]+)'
                         rf'(\s*#.*)?$')
        hit = next((i for i in range(start + 1, end) if kre.match(lines[i])), None)
        if hit is not None:
            if value is None:
                del lines[hit]
            else:
                m = kre.match(lines[hit])
                lines[hit] = f"{m.group(1)}{_toml_literal(value)}{m.group(3) or ''}"
        elif value is not None:
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, f'{key} = {_toml_literal(value)}')
    return '\n'.join(lines) + '\n'


def save_config(updates: dict) -> None:
    """Apply `updates` ((section, key) -> value|None) to config.toml in place.
    Raises ConfigError instead of overwriting a file that doesn't parse."""
    import tomllib
    _read_config_toml()                          # raises if currently invalid
    try:
        text = CONFIG_PATH.read_text()
    except FileNotFoundError:
        text = ''
    new = toml_update(text, updates)
    try:
        tomllib.loads(new)
    except ValueError as e:
        raise ConfigError(f"refusing to write invalid TOML: {e}") from None
    # 0600 from the first byte: it holds the SteamGridDB key and Game-db token
    net.write_atomic(CONFIG_PATH, new, mode=0o600)


def steam_running() -> bool:
    """Same test as the CLI's: the client or its web helper — read from
    /proc directly (two pgrep runs cost ~15 ms; this is a few)."""
    try:
        with os.scandir('/proc') as it:
            pids = [e.name for e in it if e.name.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f'/proc/{pid}/comm', 'rb') as f:
                if f.read().strip() in (b'steam', b'steamwebhelper'):
                    return True
        except OSError:
            continue
    return False
