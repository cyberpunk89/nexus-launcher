"""Steam: non-Steam-game shortcuts, accounts, compat-tool mappings."""

import json
import os
import re
import shutil
import sys
import zlib
from datetime import datetime
from pathlib import Path

from nexus import features, paths as NX, platform

from .common import (atomic_write, backup_file, log, make_slug, SAVES_DIR, STEAM_CONFIG_VDF,
    STEAM_ROOT, VDF_ERRORS)


def shortcut_appid(exe: str, appname: str) -> int:
    """Steam's non-Steam appid: unsigned 32-bit (high bit set)."""
    crc = zlib.crc32(f'"{exe}"{appname}'.encode('utf-8')) & 0xffffffff
    return crc | 0x80000000


def _stored_appid(slug: str) -> int | None:
    """The appid already recorded for this game in games.json, if it is a valid
    non-Steam shortcut id (unsigned 32-bit with the high bit set)."""
    try:
        val = int((registry_get(slug) or {}).get('steam_appid') or 0)
    except (TypeError, ValueError):
        return None
    return val if 0x80000000 <= val <= 0xFFFFFFFF else None


def _appid_signed(unsigned: int) -> int:
    """shortcuts.vdf stores appid as a signed int32."""
    return unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned


def steam_is_running() -> bool:
    """The client or its web helper (comm is cut at 15 characters, so the
    helper is matched by its start)."""
    return any(n == 'steam' or n.startswith('steamwebhelper')
               for n in (p.name.lower() for p in platform.processes()))


def _load_vdf_text(path: Path) -> dict:
    """Parse a text-mode .vdf. Returns {} on any failure (missing lib/file)."""
    from nexus import steamlib
    return steamlib._load_vdf(path)


def _steam_login_users() -> dict[int, dict]:
    """config/loginusers.vdf keyed by Steam3 accountid. Carries AccountName,
    PersonaName, AutoLogin and the last-login Timestamp for each account."""
    users = _load_vdf_text(STEAM_ROOT / 'config' / 'loginusers.vdf').get('users', {})
    out: dict[int, dict] = {}
    for steamid64, entry in users.items():
        if not isinstance(entry, dict):
            continue
        try:
            accountid = int(steamid64) & 0xFFFFFFFF
        except (TypeError, ValueError):
            continue
        try:
            ts = int(entry.get('Timestamp', 0))
        except (TypeError, ValueError):
            ts = 0
        out[accountid] = {
            'steamid64': str(steamid64),
            'account_name': entry.get('AccountName', ''),
            'persona': entry.get('PersonaName', ''),
            'autologin': entry.get('AutoLogin') == '1',
            # Steam sets MostRecent on the account it last signed in as.
            'most_recent': entry.get('MostRecent') == '1',
            'timestamp': ts,
        }
    return out


def _steam_last_logged_in() -> int | None:
    """The accountid Steam most recently *signed in as*, from its connection
    log. This is the only signal that survives a manual account switch --
    loginusers' AutoLogin flag keeps pointing at the auto-login account even
    after you switch away from it."""
    path = STEAM_ROOT / 'logs' / 'connection_log.txt'
    try:
        with open(path, 'rb') as f:
            # Tail only; this log grows without bound.
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode('utf-8', errors='replace')
    except OSError:
        return None
    hit = None
    for m in re.finditer(r'\[Logged On,[^\]]*\]\s*\[U:1:(\d+)\]', tail):
        hit = m.group(1)
    try:
        return int(hit) if hit else None
    except ValueError:
        return None


def _count_shortcuts(path: Path) -> int:
    """How many non-Steam shortcuts a profile actually holds. An emptied
    shortcuts.vdf still exists on disk, so presence of the file says nothing."""
    try:
        import vdf
        with open(path, 'rb') as f:
            return len(vdf.binary_load(f).get('shortcuts', {}))
    except VDF_ERRORS:
        return 0


def steam_accounts() -> list[dict]:
    """Every Steam userdata profile on disk, newest-login first. Each dict has
    accountid/account_name/persona/autologin/logged_in plus whether the profile
    already holds shortcuts."""
    base = STEAM_ROOT / 'userdata'
    if not base.is_dir():
        return []
    logins = _steam_login_users()
    last = _steam_last_logged_in()
    out = []
    for d in base.iterdir():
        # '0' is Steam's anonymous/offline stub, never a real account.
        if not d.is_dir() or d.name == '0':
            continue
        try:
            accountid = int(d.name)
        except ValueError:
            continue
        cfg = d / 'config'
        if not cfg.is_dir():
            continue
        info = dict(logins.get(accountid, {}))
        localconfig = cfg / 'localconfig.vdf'
        try:
            mtime = localconfig.stat().st_mtime
        except OSError:
            mtime = 0.0
        info.update({
            'accountid': accountid,
            'config_dir': cfg,
            'shortcut_count': _count_shortcuts(cfg / 'shortcuts.vdf'),
            'has_localconfig': localconfig.exists(),
            'mtime': mtime,
            'logged_in': accountid == last,
        })
        info.setdefault('account_name', '')
        info.setdefault('persona', '')
        info.setdefault('autologin', False)
        info.setdefault('timestamp', 0)
        out.append(info)
    out.sort(key=lambda a: (a['timestamp'], a['mtime']), reverse=True)
    return out


def match_steam_account(want: str, accounts: list[dict]) -> dict | None:
    """Resolve a user-supplied account selector against `accounts`. Accepts a
    Steam3 accountid, a steamid64, a login name or a persona name (the last two
    case-insensitively)."""
    want = str(want).strip()
    if not want:
        return None
    for a in accounts:
        if want == str(a['accountid']) or want == a.get('steamid64'):
            return a
    low = want.lower()
    for key in ('account_name', 'persona'):
        for a in accounts:
            if (a.get(key) or '').lower() == low:
                return a
    return None


# Set once in main() from --steam-account or config [integrations] steam_account.
STEAM_ACCOUNT_PIN: str | None = None


def set_steam_account_pin(pin) -> None:
    """Which Steam account gets the shortcuts (--steam-account / config)."""
    global STEAM_ACCOUNT_PIN
    STEAM_ACCOUNT_PIN = pin


_STEAM_PROFILE: tuple[Path | None, str, str] | None = None
_STEAM_ANNOUNCED = False


def resolve_steam_account() -> tuple[dict | None, str]:
    """Pick the Steam account to write shortcuts/artwork into, and say why.

    Precedence, most to least authoritative:
      1. an explicit pin (--steam-account / config [integrations] steam_account)
      2. the account Steam last signed in as (connection log)
      3. loginusers MostRecent, then AutoLogin=1
      4. the most recent loginusers Timestamp
      5. the profile whose localconfig.vdf was touched most recently

    Never falls back to sorting by directory name: with two profiles that both
    look "used", a name sort silently picks whichever id happens to be larger.
    """
    accounts = steam_accounts()
    if not accounts:
        return None, 'no userdata profile on disk'

    if STEAM_ACCOUNT_PIN:
        hit = match_steam_account(STEAM_ACCOUNT_PIN, accounts)
        if hit:
            return hit, f'pinned to {STEAM_ACCOUNT_PIN!r}'
        known = ', '.join(steam_account_label(a) for a in accounts) or 'none'
        print(f"  [!] Steam: pinned account {STEAM_ACCOUNT_PIN!r} not found "
              f"(have: {known}) — falling back to auto-detect.", file=sys.stderr)

    for a in accounts:
        if a.get('logged_in'):
            return a, 'last signed in to Steam'
    for a in accounts:
        if a.get('most_recent'):
            return a, 'most recent in loginusers.vdf'
    for a in accounts:
        if a.get('autologin'):
            return a, 'Steam auto-login account'
    dated = [a for a in accounts if a.get('timestamp')]
    if dated:
        return dated[0], 'newest login timestamp'
    return max(accounts, key=lambda a: a['mtime']), 'most recently used profile'


def steam_account_label(account: dict) -> str:
    """'12345678 (mylogin / My Persona)' — for logs and --doctor."""
    names = [n for n in (account.get('account_name'), account.get('persona')) if n]
    seen, uniq = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    suffix = f" ({' / '.join(uniq)})" if uniq else ''
    return f"{account['accountid']}{suffix}"


def find_steam_userdata(announce: bool = False) -> Path | None:
    """The userdata/<id>/config dir shortcuts and grid art belong in. Decided
    once per run by resolve_steam_account().

    Pass announce=True from paths that *write* to Steam, so a wrong account is
    visible instead of silent. Read-only callers stay quiet -- `--list --json`
    parses stdout, so a stray line there breaks the GUI."""
    global _STEAM_PROFILE, _STEAM_ANNOUNCED
    if _STEAM_PROFILE is None:
        account, why = resolve_steam_account()
        _STEAM_PROFILE = ((account['config_dir'] if account else None), why,
                          (steam_account_label(account) if account else ''))
    cfg_dir, why, label = _STEAM_PROFILE
    if announce and cfg_dir and not _STEAM_ANNOUNCED:
        _STEAM_ANNOUNCED = True
        log.print(f"  Steam account: {label} — {why}.")
    return cfg_dir


def _wait_for_steam_closed() -> bool:
    """If Steam is running, prompt to close it. Returns True if OK to write."""
    if not steam_is_running():
        return True
    log.print("\n  [!] Steam is running. It rewrites shortcuts.vdf on exit, so a")
    log.print("      shortcut written now would be lost. Fully quit Steam first.")
    while True:
        try:
            ans = input("  [Enter] once Steam is closed, or 's' to skip Steam: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans == 's':
            return False
        if not steam_is_running():
            return True
        log.print("  Steam still appears to be running...")


def _copy_grid_art(grid_dir: Path, unsigned_appid: int, artwork: dict) -> None:
    """Copy fetched artwork into Steam's grid/ dir using appid-based names."""
    grid_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        'cover': f"{unsigned_appid}p",       # vertical capsule
        'hero':  f"{unsigned_appid}_hero",
        'logo':  f"{unsigned_appid}_logo",
        'icon':  f"{unsigned_appid}_icon",
    }
    for kind, stem in mapping.items():
        src = artwork.get(kind)
        if not src:
            continue
        ext = src.suffix or '.png'
        try:
            shutil.copyfile(src, grid_dir / f"{stem}{ext}")
        except OSError:
            pass


def _ci_key(node, key: str):
    """Case-insensitive dict key lookup (Steam's VDF casing varies)."""
    if not isinstance(node, dict):
        return None
    for k in node:
        if k.lower() == key.lower():
            return k
    return None


def _write_compat_mapping(unsigned_appid: int, proton_name: str | None) -> bool:
    """Set (proton_name given) or clear (None) the Steam Play compat tool for a
    non-Steam appid in config.vdf. Steam must be closed (it rewrites config.vdf
    on exit). Best-effort, never raises."""
    try:
        import vdf
    except ImportError:
        return False
    if not STEAM_CONFIG_VDF.exists():
        return False
    try:
        with open(STEAM_CONFIG_VDF, encoding='utf-8', errors='replace') as f:
            data = vdf.load(f)
    except VDF_ERRORS:
        return False
    node = data
    for key in ('InstallConfigStore', 'Software', 'Valve', 'Steam'):
        k = _ci_key(node, key)
        if k is None:
            if proton_name is None:
                return False
            node[key] = {}          # build the path when forcing a tool
            k = key
        node = node[k]
    mk = _ci_key(node, 'CompatToolMapping')
    if mk is None:
        if proton_name is None:
            return False
        node['CompatToolMapping'] = {}
        mk = 'CompatToolMapping'
    mapping = node[mk]
    aid = str(unsigned_appid)
    if proton_name is None and aid not in mapping:
        return False  # nothing to clear
    if proton_name is None:
        del mapping[aid]
    else:
        cur = mapping.get(aid)
        if isinstance(cur, dict) and cur.get('name') == proton_name:
            return True     # already set — don't churn config.vdf + a backup
        mapping[aid] = {"name": proton_name, "config": "", "priority": "250"}
    try:
        payload = vdf.dumps(data, pretty=True)
        backup_file(STEAM_CONFIG_VDF)
        atomic_write(STEAM_CONFIG_VDF, payload)
    except (OSError, ValueError):
        return False
    return True


def set_steam_compat_tool(unsigned_appid: int, proton_name: str) -> bool:
    """Force a Steam Play compat tool for a non-Steam appid (native mode: Steam
    runs the game .exe through this Proton)."""
    return _write_compat_mapping(unsigned_appid, proton_name)


def _read_compat_mapping() -> dict[str, str]:
    """Read-only: {appid_str: proton_name} from Steam's CompatToolMapping in
    config.vdf. Never writes; returns {} on any failure."""
    try:
        import vdf
    except ImportError:
        return {}
    if not STEAM_CONFIG_VDF.exists():
        return {}
    try:
        with open(STEAM_CONFIG_VDF, encoding='utf-8', errors='replace') as f:
            data = vdf.load(f)
    except VDF_ERRORS:
        return {}
    node = data
    for key in ('InstallConfigStore', 'Software', 'Valve', 'Steam'):
        k = _ci_key(node, key)
        if k is None:
            return {}
        node = node[k]
    mk = _ci_key(node, 'CompatToolMapping')
    if mk is None:
        return {}
    mapping = node[mk]
    return {aid: e.get('name', '') for aid, e in mapping.items() if isinstance(e, dict)}


# Where Windows games (and the Steam-emu shims FitGirl repacks ship) put saves.
_SAVE_DIRS = (
    'drive_c/users/Public/Documents',
    'drive_c/users/steamuser/Documents',
    'drive_c/users/steamuser/AppData/Roaming',
    'drive_c/users/steamuser/AppData/Local',
    'drive_c/users/steamuser/Saved Games',
)


def find_prefix_savedata(prefix_root: Path) -> list[Path]:
    """Files under a prefix that look like save data. `prefix_root` is the
    compat-data dir (the one containing pfx/). Ignores the empty skeleton Wine
    creates in every prefix."""
    hits: list[Path] = []
    pfx = prefix_root / 'pfx'
    base = pfx if pfx.is_dir() else prefix_root
    for f in _iter_savedata(base):
        hits.append(f)
        if len(hits) >= 50:              # enough to prove the point
            return hits
    return hits


# Folders that hold many games' data (look one level deeper) or none at all.
# Steam-emulator folders (Steam/RUNE/<appid>) and engine folders
# (Godot/app_userdata/<game>) are containers too.
_SAVE_CONTAINERS = {'my games', 'saved games', 'savegames', 'documents', 'steam', 'rune',
                    'codex', 'cpy', 'empress', 'onlinefix', 'goldberg steamemu saves',
                    'godot', 'app_userdata'}
_SAVE_NOISE = {'microsoft', 'temp', 'd3dscache', 'nvidia', 'amd', 'mesa_shader_cache',
               'crashdumps', 'packages', 'connecteddevicesplatform', 'unrealengine',
               'cef', 'unity', 'openal', 'fontconfig', 'dxvk', 'vkd3d', 'mangohud'}
# Below a game's own folder: logs and crash reports, not progress.
_SAVE_NOISE_ANY = {'logs', 'crashes', 'crashreports', 'crash reports', 'temp', 'tmp'}
_SAVE_NOISE_EXT = {'.lnk', '.url', '.log', '.dmp', '.tmp'}


def _is_save_noise(rel: Path) -> bool:
    """`rel` is relative to one of _SAVE_DIRS. Caches, shader caches, Start
    Menu shortcuts, logs and crash dumps live there too, but aren't saves —
    counting them made every prefix "have save data" and swelled backups."""
    if rel.name.lower() == 'desktop.ini' or rel.suffix.lower() in _SAVE_NOISE_EXT:
        return True
    dirs = [p.lower() for p in rel.parts[:-1]]
    if dirs and dirs[0] in _SAVE_NOISE:
        return True
    return any('cache' in d or d in _SAVE_NOISE_ANY for d in dirs)


def _iter_savedata(base: Path):
    """Save-looking files under a prefix's drive (`base` holds drive_c)."""
    for rel in _SAVE_DIRS:
        root = base / rel
        if not root.is_dir():
            continue
        for f in root.rglob('*'):
            if f.is_file() and not f.is_symlink() and not _is_save_noise(f.relative_to(root)):
                yield f


def savedata_owners(prefix_root: Path, hits: list[Path], limit: int = 3) -> list[str]:
    """Best-effort "whose saves are these?" — the folder names right under the
    save locations (e.g. "Larian Studios", "Remedy"), so an unnamed prefix
    can be recognised before it is restored or trashed."""
    pfx = prefix_root / 'pfx'
    base = pfx if pfx.is_dir() else prefix_root
    owners: list[str] = []
    for f in hits:
        for rel in _SAVE_DIRS:
            try:
                parts = f.relative_to(base / rel).parts[:-1]
            except ValueError:
                continue
            while parts and parts[0].lower() in _SAVE_CONTAINERS:
                parts = parts[1:]
            if parts and parts[0].lower() not in _SAVE_NOISE:
                owner = f"Steam app {parts[0]}" if parts[0].isdigit() else parts[0]
                if owner not in owners:
                    owners.append(owner)
            break
        if len(owners) >= limit:
            break
    return owners


# A copy of the saves taken just before a restore overwrote them: kept (so
# the restore can be undone by hand) but never itself offered for restore.
PRE_RESTORE = '-before-restore'


def _unique_dir(parent: Path, name: str, suffix: str = '') -> Path:
    """parent/name+suffix, or name-2+suffix, -3… if that already exists (two
    backups in the same second must not merge into one)."""
    d, n = parent / (name + suffix), 2
    while d.exists():
        d, n = parent / f"{name}-{n}{suffix}", n + 1
    return d


def backup_prefix_saves(slug: str, game_dir: Path,
                        suffix: str = '') -> tuple[Path, int, list[Path]] | None:
    """Copy known save-data directories out of game_dir/pfx into a timestamped
    backup under SAVES_DIR/<slug>/, so an uninstalled game's saves survive and
    can be reinjected into a future reinstall (see restore_prefix_saves).
    Returns (backup_dir, file_count, files_that_failed), or None if no save
    data was found."""
    pfx = game_dir / 'pfx'
    base = pfx if pfx.is_dir() else game_dir
    if not base.is_dir():
        return None
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest_root = _unique_dir(SAVES_DIR / slug, ts, suffix)
    count, failed = 0, []
    for f in _iter_savedata(base):
        dest = dest_root / f.relative_to(base)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            count += 1
        except OSError:
            failed.append(f)
    if count == 0 and not failed:
        shutil.rmtree(dest_root, ignore_errors=True)
        return None
    return dest_root, count, failed


def latest_save_backup(slug: str) -> Path | None:
    """Most recent save backup dir for `slug`, if any."""
    slug_dir = SAVES_DIR / slug
    if not slug_dir.is_dir():
        return None
    backups = sorted((d for d in slug_dir.iterdir()
                      if d.is_dir() and not d.name.endswith(PRE_RESTORE)), reverse=True)
    return backups[0] if backups else None


def restore_prefix_saves(slug: str, wine_prefix: Path) -> tuple[int, list[Path]] | None:
    """Copy the most recent save backup for `slug` back into wine_prefix
    (game_dir/pfx), so a reinstall picks up where the old one left off.
    Returns (files_restored, files_that_failed), or None if there's no backup."""
    latest = latest_save_backup(slug)
    if not latest:
        return None
    count, failed = 0, []
    for f in latest.rglob('*'):
        if not f.is_file():
            continue
        dest = wine_prefix / f.relative_to(latest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            count += 1
        except OSError:
            failed.append(f.relative_to(latest))
    return count, failed


# A manifest backup's map of each copied file to where it came from.
SAVE_FILES_MAP = 'files.json'


def backup_manifest_saves(slug: str, game_dir: Path, entry: list,
                          folders: dict[str, str] | None = None,
                          suffix: str = '') -> tuple[Path, int, list[Path]] | None:
    """backup_prefix_saves for a game with no prefix (a native Windows
    install): copy the files its Ludusavi manifest `entry` names (see
    nexus.saves) into SAVES_DIR/<slug>/<timestamp>/, with files.json mapping
    each copy to its original path so restore_manifest_saves can put it back.
    Same return as backup_prefix_saves."""
    from nexus import saves
    hits = saves.resolve(entry, game_dir, folders, noise=_is_save_noise)
    if not hits:
        return None
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest_root = _unique_dir(SAVES_DIR / slug, ts, suffix)
    # One numbered folder per save location keeps two locations' same-named files apart.
    roots = list(dict.fromkeys(root for _f, root in hits))
    mapping, failed = {}, []
    for f, root in hits:
        rel = Path(str(roots.index(root))) / f.relative_to(root)
        try:
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            mapping[rel.as_posix()] = str(f)
        except OSError:
            failed.append(f)
    dest_root.mkdir(parents=True, exist_ok=True)
    atomic_write(dest_root / SAVE_FILES_MAP, json.dumps(mapping, indent=1))
    return dest_root, len(mapping), failed


def restore_manifest_saves(slug: str) -> tuple[int, list[Path]] | None:
    """Put the latest backup_manifest_saves backup for `slug` back where its
    files.json says each file came from. Same return as restore_prefix_saves;
    None also when the latest backup isn't a manifest one."""
    latest = latest_save_backup(slug)
    if not latest:
        return None
    try:
        mapping = json.loads((latest / SAVE_FILES_MAP).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    count, failed = 0, []
    for rel, original in mapping.items():
        try:
            dest = Path(original)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(latest / rel, dest)
            count += 1
        except OSError:
            failed.append(Path(rel))
    return count, failed


# What a game outside the manifest gets, rather than a guess at its saves.
NO_SAVE_PATHS = "Nexus doesn't know where this game keeps its saves"


def manifest_save_entry(entry: dict) -> tuple[list | None, str]:
    """The Ludusavi manifest's save paths for a registry entry (a native
    Windows install), matched by store appid, else by name; (None, why) when
    there are none to use."""
    from nexus import compat, saves
    idx = saves.load_index()
    if idx is None:
        return None, "Nexus couldn't load its list of save locations (offline?)"
    appid = compat.store_appid(entry)
    sid = entry.get('steam_appid')               # a shortcut's own id is never a store's
    if appid is None and str(sid or '').isdigit() and 0 < int(sid) < 0x80000000:
        appid = int(sid)
    title = saves.find_game(idx, appid, entry.get('display_name') or entry.get('slug') or '')
    files = (idx.get('games') or {}).get(title) if title else None
    return (files, '') if files else (None, NO_SAVE_PATHS)


def is_manifest_backup(backup: Path) -> bool:
    """True for a backup_manifest_saves backup (restore_manifest_saves can put
    it back), False for one copied out of a Wine prefix."""
    return (backup / SAVE_FILES_MAP).is_file()


def _report_failed_copies(failed: list[Path], what: str) -> None:
    print(f"  [!] {len(failed)} {what} could not be copied, e.g.:", file=sys.stderr)
    for f in failed[:5]:
        print(f"      {f}", file=sys.stderr)


INTEGRITY_DIR = NX.INTEGRITY_DIR

# Everything under pfx/drive_c EXCEPT these two top-level dirs is treated as
# "game content" worth snapshotting: `users` holds saves/config (_SAVE_DIRS)
# and other per-user state that's supposed to change, and `windows` is
# Wine/Proton's own runtime, churned by every Proton update — neither is
# useful (or stable enough) to check for corruption.
_INTEGRITY_SKIP_TOPLEVEL = {'users', 'windows'}


def _integrity_files(game_dir: Path) -> list[Path]:
    """Installed game files worth an integrity snapshot (see
    _INTEGRITY_SKIP_TOPLEVEL for what's excluded and why)."""
    drive_c = game_dir / 'pfx' / 'drive_c'
    if not drive_c.is_dir():
        return []
    files = []
    for top in drive_c.iterdir():
        if top.name.lower() in _INTEGRITY_SKIP_TOPLEVEL:
            continue
        if top.is_file():
            files.append(top)
        elif top.is_dir():
            files.extend(f for f in top.rglob('*') if f.is_file())
    return files


def integrity_manifest_path(slug: str) -> Path:
    return INTEGRITY_DIR / f"{slug}.json"


def load_integrity_manifest(slug: str) -> dict | None:
    try:
        return json.loads(integrity_manifest_path(slug).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def save_integrity_manifest(slug: str, game_dir: Path, files: list[Path]) -> None:
    hashes = dict(zip(files, _parallel_md5(files)))
    manifest = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'files': {
            str(f.relative_to(game_dir)): {'size': f.stat().st_size, 'md5': h}
            for f, h in hashes.items()
        },
    }
    atomic_write(integrity_manifest_path(slug), json.dumps(manifest, indent=2))


def cmd_check_integrity(slug: str, rebaseline: bool = False) -> None:
    """Compare installed game files against a saved MD5 manifest (or create
    one if this is the first run) to catch silent corruption — relevant here
    because the game dir has btrfs checksums disabled (see disable_cow)."""
    entry = registry_get(slug)
    if not entry or not entry.get('wine_prefix'):
        print(f"'{slug}' is not a managed game with a prefix", file=sys.stderr)
        sys.exit(1)
    game_dir = Path(entry['wine_prefix']).parent
    if not game_dir.is_dir():
        print(f"Install directory for '{slug}' is missing: {game_dir}", file=sys.stderr)
        sys.exit(1)

    files = _integrity_files(game_dir)
    if not files:
        print("No installed game files found under pfx/drive_c to check "
              "(everything is under users/ or windows/, which are skipped).")
        return

    existing = None if rebaseline else load_integrity_manifest(slug)
    if existing is None:
        total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
        print(f"{'Rebaselining' if rebaseline else 'No baseline yet — creating one'}: "
              f"hashing {len(files)} file(s), {total_mb:.0f} MB...")
        save_integrity_manifest(slug, game_dir, files)
        print(f"Baseline saved ({len(files)} files). Run this again later to "
              f"check for changes.")
        return

    current_rel = {str(f.relative_to(game_dir)) for f in files}
    known_rel = set(existing.get('files', {}))
    missing = sorted(known_rel - current_rel)
    added = sorted(current_rel - known_rel)
    common = [f for f in files if str(f.relative_to(game_dir)) in known_rel]

    total_mb = sum(f.stat().st_size for f in common) / 1024 / 1024
    print(f"Checking against baseline from {existing.get('created_at', 'unknown')}: "
          f"hashing {len(common)} known file(s), {total_mb:.0f} MB...")
    changed = []
    for f, actual in zip(common, _parallel_md5(common)):
        rel = str(f.relative_to(game_dir))
        if actual != existing['files'][rel]['md5']:
            changed.append(rel)
    changed.sort()

    def _list(label, items):
        print(f"  [!] {len(items)} file(s) {label}:")
        for rel in items[:20]:
            print(f"        {rel}")
        if len(items) > 20:
            print(f"        ... and {len(items) - 20} more")

    print()
    if not missing and not changed:
        print(f"  OK — all {len(common)} file(s) match the baseline.")
    else:
        if changed:
            _list("CHANGED since the baseline (possible corruption)", changed)
        if missing:
            _list("MISSING since the baseline", missing)
    if added:
        print(f"  [i] {len(added)} file(s) not in the baseline (new — not flagged; "
              f"likely a game update or a config/settings write).")

    if changed or missing:
        print(f"\n  If this is expected (e.g. you updated or modded the game), accept")
        print(f"  the current files as the new baseline with:")
        print(f"    nexus-cli --check-integrity {slug} --rebaseline")
        sys.exit(1)


def _ensure_tracked_files(game_dir: Path) -> None:
    """Create an empty <game_dir>/tracked_files if it doesn't exist.

    Real Proton only creates this bookkeeping file via its own first-run
    copy_pfx() ('w' mode), gated on game_dir/pfx/creation_sync_guard being
    absent. umu-run's bootstrap already drops that guard file (and
    .update-timestamp) in every prefix this tool creates, so real Proton
    permanently skips copy_pfx() here and jumps straight to
    update_builtin_libs(), which unconditionally opens tracked_files for
    reading and crashes with FileNotFoundError before the game starts (seen
    live in Steam's own logs, identically, for both GE-Proton and
    proton-cachyos-slr — this is why Steam-launched shortcuts crash while
    Heroic/launch.sh, which never go through this Proton code path, don't).

    An empty file is exactly what a genuinely-fresh Proton-managed prefix
    looks like right before its own first update_builtin_libs() call, so this
    is always safe. Never touch creation_sync_guard/.update-timestamp/version/
    config_info here — synthesizing those would re-enable copy_pfx() and have
    Proton copy its own prefix skeleton over the umu-managed one. Never
    truncate an existing (non-empty) tracked_files."""
    tf = game_dir / 'tracked_files'
    try:
        if not tf.exists():
            game_dir.mkdir(parents=True, exist_ok=True)
            tf.touch()
    except OSError as e:
        log.print(f"  [!] Steam: could not create {tf} ({e}) — Proton may still crash on launch.")


def _redirect_compatdata(unsigned_appid: int, game_dir: Path) -> None:
    """Make Steam's per-game prefix dir a symlink to our install's game_dir, so
    Steam's forced Proton uses the prefix the game was installed into
    (game_dir/pfx) instead of a fresh empty one."""
    cd = STEAM_ROOT / "steamapps" / "compatdata"
    cd.mkdir(parents=True, exist_ok=True)
    link = cd / str(unsigned_appid)
    try:
        if link.is_symlink():
            if link.resolve() == game_dir.resolve():
                _ensure_tracked_files(game_dir)
                return
            link.unlink()
        elif link.is_dir():
            # A throwaway prefix Steam made earlier. It is usually empty — but
            # if the game was ever launched against it, this is where its saves
            # went, and renaming it silently would look exactly like data loss.
            saves = find_prefix_savedata(link)
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            dest = cd / f"{unsigned_appid}.bak-{ts}"
            link.rename(dest)
            if saves:
                log.print(f"\n  [!] Steam had its own prefix at compatdata/{unsigned_appid}")
                log.print(f"      and it contains {len(saves)} file(s) that look like SAVE DATA.")
                log.print(f"      Moved to: {dest}")
                log.print(f"      Your install prefix ({game_dir}/pfx) is used from now on.")
                log.print( "      If the game has lost progress, copy the saves across:")
                # `saves` was collected before the rename, so rebase onto dest.
                sub = 'pfx/drive_c/users' if (dest / 'pfx').is_dir() else 'drive_c/users'
                log.print(f"        cp -a \"{dest}/{sub}/.\" \\")
                log.print(f"              \"{game_dir}/pfx/drive_c/users/\"")
                log.print()
        elif link.exists():
            link.unlink()
        link.symlink_to(game_dir)
        _ensure_tracked_files(game_dir)
        log.print(f"  Steam: prefix redirected to {game_dir}/pfx (compatdata symlink).")
    except OSError as e:
        log.print(f"  [!] Steam: could not redirect compatdata ({e}).")


def _shortcut_uid(entry: dict) -> int:
    """A shortcuts.vdf entry's appid as unsigned (the file stores int32)."""
    try:
        return int(entry.get('appid', 0)) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def load_shortcuts(path: Path) -> dict | None:
    """Parse a binary shortcuts.vdf. A missing file is an empty library; a file
    that exists but can't be parsed returns None — callers must then leave it
    alone, because writing back an empty dict would delete every other
    non-Steam shortcut the user has."""
    try:
        import vdf
    except ImportError:
        return None
    if not path.exists():
        return {'shortcuts': {}}
    try:
        with open(path, 'rb') as f:
            data = vdf.binary_load(f)
    except VDF_ERRORS as e:
        # stderr: this also runs under --list --json, whose stdout is parsed.
        print(f"  [!] Steam: could not parse {path} ({e}) — leaving it untouched.",
              file=sys.stderr)
        return None
    if not isinstance(data.get('shortcuts'), dict):
        data['shortcuts'] = {}
    return data


def save_shortcuts(path: Path, data: dict) -> bool:
    """Back up then atomically rewrite shortcuts.vdf. Returns success."""
    import vdf
    try:
        payload = vdf.binary_dumps(data)
        backup_file(path)
        atomic_write(path, payload)
    except (OSError, ValueError, TypeError) as e:
        log.print(f"  [!] Steam: failed to write {path.name} ({e}).")
        return False
    return True


def split_game_shortcuts(shortcuts: dict, slug: str) -> tuple[list[dict], list[dict]]:
    """(others, ours): the shortcut(s) belonging to this game, and the rest.
    Matched by the appid recorded in games.json when that entry still exists
    (survives a rename inside Steam, and never catches an unrelated shortcut
    that merely has the same name); otherwise by AppName slug."""
    entries = [e for e in shortcuts.values() if isinstance(e, dict)]
    stored = _stored_appid(slug)
    ours = [e for e in entries if stored and _shortcut_uid(e) == stored]
    if not ours:
        ours = [e for e in entries if make_slug(e.get('AppName', '')) == slug]
    others = [e for e in entries if not any(e is o for o in ours)]
    return others, ours


def add_steam_shortcut_native(display_name: str, slug: str, exe: Path,
                              game_dir: Path, proton: Path,
                              artwork: dict | None,
                              launch_options: str | None = None) -> int | None:
    """Add a non-Steam shortcut that Steam runs the *native* way: the raw game
    .exe through a forced Proton, with the prefix redirected to our installed
    game_dir. This matches how Heroic runs it. On Windows (no compatdata)
    it's just the exe: no Proton, no prefix. Returns the appid or None.

    Re-adding an existing shortcut updates it in place: the user's collections,
    hidden flag, play time, custom icon and launch options are kept.
    `launch_options` (from the game's launch profile) replaces the launch
    options when given; None leaves whatever is there."""
    try:
        import vdf  # noqa: F401
    except ImportError:
        log.print("  [i] Steam: python 'vdf' library not available — skipping.")
        return None
    compat = features.has('compatdata')    # Linux: through Proton, in our prefix
    if not (exe and (proton or not compat)):
        log.print("  [i] Steam: need a game exe and a Proton for native mode — skipping.")
        return None
    cfg_dir = find_steam_userdata(announce=True)
    if not cfg_dir:
        log.print("  [i] Steam: no userdata profile found — skipping.")
        return None
    if not _wait_for_steam_closed():
        log.print("  [i] Steam: skipped (Steam running / user opted out).")
        return None

    exe_str = str(exe)
    # Reuse the appid this game already has, if any. The appid is
    # crc32(exe + display_name), so recomputing it after a rename or an
    # exe-path change produces a *different* id — which orphans the game's
    # compatdata prefix (and every save in it), its grid art and its forced
    # Proton mapping, while looking like nothing happened.
    unsigned = _stored_appid(slug) or shortcut_appid(exe_str, display_name)
    icon = str(artwork['icon']) if artwork and artwork.get('icon') else ''

    shortcuts_path = cfg_dir / 'shortcuts.vdf'
    data = load_shortcuts(shortcuts_path)
    if data is None:
        log.print("  [!] Steam: shortcut NOT added — fix or remove the damaged "
                  "shortcuts.vdf first (Steam recreates it).")
        return None
    others, ours = split_game_shortcuts(data['shortcuts'], slug)
    prev = next((e for e in ours if _shortcut_uid(e) == unsigned), ours[0] if ours else {})
    old_appids = [_shortcut_uid(e) for e in ours]

    entry = {
        'IsHidden': 0, 'AllowDesktopConfig': 1, 'AllowOverlay': 1, 'OpenVR': 0,
        'Devkit': 0, 'DevkitGameID': '', 'DevkitOverrideAppID': 0,
        'LastPlayTime': 0, 'FlatpakAppID': '', 'tags': {},
        'ShortcutPath': '', 'LaunchOptions': '', 'icon': '',
    }
    entry.update(prev)                     # keep everything the user set
    entry.update({                         # ...but these are ours
        'appid': _appid_signed(unsigned),
        'AppName': display_name,
        'Exe': f'"{exe_str}"',
        'StartDir': str(exe.parent),
    })
    if icon and not prev.get('icon'):
        entry['icon'] = icon
    if launch_options is not None:
        entry['LaunchOptions'] = launch_options
    data['shortcuts'] = {str(i): e for i, e in enumerate(others + [entry])}
    if not save_shortcuts(shortcuts_path, data):
        return None

    # Force the chosen Proton, redirect the prefix, and clean up the old entry's
    # forced-tool mapping so it doesn't dangle.
    if compat:
        for au in old_appids:
            if au != unsigned:
                _write_compat_mapping(au, None)
        tool = proton_tool_name(proton)
        if set_steam_compat_tool(unsigned, tool):
            log.print(f"  Steam: forced compat tool {tool}.")
        _redirect_compatdata(unsigned, game_dir)
    if artwork:
        _copy_grid_art(cfg_dir / 'grid', unsigned, artwork)

    verb = 'updated' if ours else 'added'
    log.print(f"  Steam: {verb} '{display_name}' natively (appid {unsigned}).")
    return unsigned


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .registry import registry_get  # noqa: E402
from .repack import _parallel_md5  # noqa: E402
from .runner import proton_tool_name  # noqa: E402
