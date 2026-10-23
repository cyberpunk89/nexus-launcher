"""--doctor: the environment self-check."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from nexus import paths as NX

from .common import (ARTWORK_DIR, atomic_write, CONFIG_PATH, dir_size_bytes, LAUNCHERS_DIR,
    LOG_DIR, SAVES_DIR, STEAM_ROOT, VDF_ERRORS)


def cmd_list_steam_accounts() -> None:
    """Show every Steam account on this machine and which one gets shortcuts."""
    accounts = steam_accounts()
    if not accounts:
        print("No Steam userdata profiles found under", STEAM_ROOT / 'userdata')
        return
    chosen, why = resolve_steam_account()
    chosen_id = chosen['accountid'] if chosen else None
    print(f"Steam accounts under {STEAM_ROOT / 'userdata'}:\n")
    for a in accounts:
        mark = '->' if a['accountid'] == chosen_id else '  '
        flags = []
        if a.get('logged_in'):
            flags.append('signed in')
        if a.get('autologin'):
            flags.append('auto-login')
        if a.get('shortcut_count'):
            flags.append(f"{a['shortcut_count']} shortcut"
                         f"{'s' if a['shortcut_count'] != 1 else ''}")
        print(f"  {mark} {steam_account_label(a)}"
              + (f"  [{', '.join(flags)}]" if flags else ''))
    if chosen:
        print(f"\nShortcuts go to {steam_account_label(chosen)} — {why}.")
    print("\nTo pin a different one, either:")
    print("  nexus-cli --steam-account <accountid|login name> ...")
    print(f"or add to {CONFIG_PATH} under [integrations]:")
    print('  steam_account = "<accountid or login name>"')


ORPHAN_HOLDING_DIR = NX.ORPHAN_HOLDING_DIR

# Renamed-aside by _redirect_compatdata when it found something already
# sitting where a redirect symlink needed to go (name: "<appid>.bak-<ts>").
_ORPHAN_BAK_RE = re.compile(r'^(\d+)\.bak-\d{8}-\d{6}$')


def _all_shortcut_names() -> dict[int, str]:
    """{unsigned appid: AppName} for every non-Steam shortcut on every account."""
    try:
        import vdf
    except ImportError:
        return {}
    out: dict[int, str] = {}
    for acct in steam_accounts():
        path = acct['config_dir'] / 'shortcuts.vdf'
        try:
            with open(path, 'rb') as f:
                shortcuts = vdf.binary_load(f).get('shortcuts', {})
        except VDF_ERRORS:
            continue
        for e in shortcuts.values():
            if isinstance(e, dict) and 'appid' in e:
                out[_shortcut_uid(e)] = e.get('AppName') or str(_shortcut_uid(e))
    return out


def _all_shortcut_appids() -> set[int] | None:
    """Unsigned appids of every non-Steam shortcut across every local Steam
    account — compatdata is shared at the steamapps level regardless of which
    account's shortcuts.vdf references a given id. None when any account's
    file couldn't be read (or vdf is missing): then nobody can say a
    shortcut prefix is unused."""
    try:
        import vdf
    except ImportError:
        return None
    out: set[int] = set()
    for acct in steam_accounts():
        path = acct['config_dir'] / 'shortcuts.vdf'
        if not path.exists():
            continue
        try:
            with open(path, 'rb') as f:
                shortcuts = vdf.binary_load(f).get('shortcuts', {})
            shortcuts = dict(shortcuts)
        except VDF_ERRORS:
            return None
        for e in shortcuts.values():
            if isinstance(e, dict) and 'appid' in e:
                try:
                    out.add(int(e['appid']) & 0xFFFFFFFF)
                except (TypeError, ValueError):
                    pass
    return out


def _steam_libraries() -> list[Path]:
    from nexus import steamlib
    return steamlib.library_folders(STEAM_ROOT)


def _quarantine_dir(lib: Path) -> Path:
    """Where a stray prefix from library `lib` is quarantined: the Nexus data
    dir for the main Steam library, else a folder inside that same library —
    so quarantining is an instant rename, never a multi-GB copy."""
    if lib.resolve() == STEAM_ROOT.resolve():
        return ORPHAN_HOLDING_DIR
    return lib / 'steamapps' / 'nexus-quarantine'


def find_orphan_compatdata(install_base: Path) -> list[dict]:
    """compatdata/<id> entries, in every Steam library, that nothing uses:
      - a non-Steam-shortcut id (>= 0x80000000) no shortcut on any account
        and no managed game points at,
      - a Steam appid whose game (or tool) is no longer installed anywhere,
      - one of _redirect_compatdata's "<appid>.bak-<timestamp>" safety renames.
    Read-only; nothing here is deleted. `has_savedata` flags prefixes that
    look like they still hold saves (a stranded prefix is exactly how 007
    First Light's saves were once lost track of)."""
    from nexus import steamlib
    shortcut_ids = _all_shortcut_appids()
    referenced = set(shortcut_ids or ())
    for entry in load_registry()['games'].values():
        try:
            referenced.add(int(entry.get('steam_appid') or 0))
        except (TypeError, ValueError):
            pass
    installed = steamlib.manifest_appids(STEAM_ROOT) | steamlib.TOOL_APPIDS

    orphans = []
    for lib in _steam_libraries():
        cd_root = lib / "steamapps" / "compatdata"
        if not cd_root.is_dir():
            continue
        for d in sorted(cd_root.iterdir()):
            if not d.is_dir() or d.is_symlink():
                continue
            m = _ORPHAN_BAK_RE.match(d.name)
            if m:
                appid, reason = int(m.group(1)), 'renamed aside by a previous redirect'
            else:
                try:
                    appid = int(d.name)
                except ValueError:
                    continue
                if appid == 0 or appid in referenced:
                    continue
                if appid >= 0x80000000:
                    if shortcut_ids is None:
                        continue             # a shortcuts.vdf we couldn't read: can't tell
                    reason = 'no current shortcut references this id'
                elif appid in installed:
                    continue
                else:
                    reason = 'the game is no longer installed'
            orphans.append({
                'path': d, 'appid': appid, 'reason': reason, 'library': str(lib),
                'size_bytes': dir_size_bytes(d),
                'has_savedata': bool(find_prefix_savedata(d)),
            })
    return orphans


def quarantine_path(path: Path, lib: Path) -> Path:
    """Move a stray prefix into quarantine and remember where it came from."""
    qdir = _quarantine_dir(lib)
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / path.name
    if dest.exists():
        dest = qdir / f"{path.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(path), str(dest))
    atomic_write(dest.with_name(dest.name + '.origin'), json.dumps(
        {'origin': str(path), 'at': datetime.now().isoformat(timespec='seconds')}))
    return dest


def quarantined() -> list[dict]:
    """Everything currently in quarantine, with where it would be restored to."""
    out = []
    for lib in _steam_libraries():
        qdir = _quarantine_dir(lib)
        if not qdir.is_dir():
            continue
        for d in sorted(qdir.iterdir()):
            if not d.is_dir():
                continue
            try:
                meta = json.loads(d.with_name(d.name + '.origin').read_text())
            except (OSError, ValueError):
                meta = {'origin': str(lib / 'steamapps' / 'compatdata' /
                                      re.sub(r'-\d{8}-\d{6}$', '', d.name))}
            saves = find_prefix_savedata(d)
            out.append({'name': d.name, 'path': str(d), 'origin': meta.get('origin'),
                        'at': meta.get('at'), 'size': dir_size_bytes(d),
                        'has_savedata': bool(saves),
                        'save_hint': savedata_owners(d, saves)})
    return out


def cmd_quarantine_restore(name: str) -> None:
    hit = next((q for q in quarantined() if q['name'] == name), None)
    if not hit:
        print(f"'{name}' is not in quarantine", file=sys.stderr)
        sys.exit(1)
    origin = Path(hit['origin'])
    if origin.exists():
        print(f"Can't restore: {origin} exists again (Steam recreated it?). Move it "
              f"away first.", file=sys.stderr)
        sys.exit(1)
    shutil.move(hit['path'], str(origin))
    Path(hit['path'] + '.origin').unlink(missing_ok=True)
    print(f"Restored {origin}")


def cmd_clean_orphan_compatdata(install_base: Path, target: str, yes: bool = False) -> None:
    """Quarantine a stray compatdata dir (see find_orphan_compatdata). Never
    deletes — `--quarantine-restore` puts it back; the cleanup page can
    trash it later."""
    by_name = {o['path'].name: o for o in find_orphan_compatdata(install_base)}
    match = by_name.get(target) or next(
        (o for name, o in by_name.items()
         if name == target or name.startswith(f"{target}.")), None)
    if not match:
        print(f"'{target}' is not a currently-detected orphan compatdata dir "
              f"— run --doctor to see the current list.", file=sys.stderr)
        sys.exit(1)

    path = match['path']
    print(f"About to quarantine: {path}  ({match['size_bytes'] / 1e9:.2f} GB)")
    print(f"  Reason: {match['reason']}")
    if match['has_savedata']:
        print("  [!] This looks like it contains SAVE DATA. Check it before deleting it.")
    if not yes:
        try:
            ans = input("  Move it to quarantine? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans not in ('y', 'yes'):
            print("Aborted.")
            return
    dest = quarantine_path(path, Path(match['library']))
    print(f"Quarantined to {dest}")
    print(f"Undo with:  nexus-cli --quarantine-restore {shlex.quote(dest.name)}")


def cleanup_candidates(install_base: Path, config: dict) -> list[dict]:
    """Everything the Storage page can offer to clean up (see nexus/cleanup)."""
    from nexus import cleanup as C, steamlib
    reg = load_registry()['games']
    out: list[dict] = []
    for o in find_orphan_compatdata(install_base):
        out.append(C.candidate('orphan-prefix', o['path'], title=f"compatdata/{o['path'].name}",
                               reason=o['reason'], action='quarantine', size=o['size_bytes'],
                               saves=o['has_savedata'], extra={'library': o['library']}))
    shortcut_ids = _all_shortcut_appids()
    keep_ids = (steamlib.manifest_appids(STEAM_ROOT) | set(shortcut_ids or ())
                | {int(e.get('steam_appid') or 0) for e in reg.values()})
    caches = C.shader_caches(_steam_libraries(), keep_ids)
    if shortcut_ids is None:        # unreadable shortcuts: leave shortcut ids alone
        caches = [c for c in caches if int(Path(c['path']).name) < 0x80000000]
    out += caches
    out += C.mesa_caches(Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache'))
    seen: set[str] = set()
    for c in (c for d in resolve_scan_dirs(None) for c in scan_repacks(d)):
        if c['path'] in seen:
            continue
        seen.add(c['path'])
        if c.get('installed'):
            out.append(C.candidate('installed-repack', c['path'], title=c['name'],
                                   reason=f"installed as '{c['slug']}'", preselect=False,
                                   size=c.get('size_bytes')))
    bases = {install_base} | {Path(e['install_base']) for e in reg.values()
                              if e.get('install_base')}
    for b in sorted(bases):                     # a moved game's old copy sits in its old base
        out += C.force_leftovers(b, find_prefix_savedata)
    out += snapshot_candidates(bases, reg)
    out += C.old_logs(LOG_DIR)
    out += C.old_save_backups(SAVES_DIR)
    out += C.stale_data(set(reg), ARTWORK_DIR, INTEGRITY_DIR)
    return out


def snapshot_candidates(bases, reg: dict) -> list[dict]:
    """Game snapshots worth offering: all but the newest per game, every one
    of a game that's gone."""
    from nexus import cleanup as C
    out = []
    for b in sorted(bases):
        root = Path(b) / SNAP_DIR
        if not root.is_dir():
            continue
        for game in sorted(d for d in root.iterdir() if d.is_dir()):
            snaps = [s for s in list_snapshots({'install_base': str(b)}, game.name)]
            gone = game.name not in reg
            for s in (snaps if gone else snaps[1:]):
                out.append(C.candidate(
                    'game-snapshot', s['path'], title=f"{game.name} · {s['label']} "
                    f"({s['created'][:10]})", action='delete', preselect=gone, size=0,
                    reason="the game was removed" if gone else "a newer snapshot exists"))
    return out


def cmd_cleanup_scan(install_base: Path, as_json: bool) -> None:
    from nexus import cleanup as C
    cands = cleanup_candidates(install_base, load_config())
    quar = quarantined()
    if as_json:
        print(json.dumps({'candidates': cands, 'quarantine': quar,
                          'categories': {k: {'title': t, 'about': a}
                                         for k, (t, a) in C.CATEGORIES.items()},
                          'trash_bytes': C.trash_size()}))
        return
    total = sum(c['size'] for c in cands)
    print(f"Cleanup candidates ({total / 1e9:.1f} GB):")
    for c in cands:
        mark = '*' if c['preselect'] else ' '
        print(f"  {mark} {c['size'] / 1e9:7.2f} GB  {C.CATEGORIES[c['category']][0]}: "
              f"{c['title']}  ({c['reason']}){'  [SAVES?]' if c['saves'] else ''}")
        print(f"      id: {c['id']}")
    if quar:
        print("\nIn quarantine:")
        for q in quar:
            print(f"    {q['size'] / 1e9:7.2f} GB  {q['name']}  (from {q['origin']})")
    print("\nRemove with:  nexus-cli --cleanup <id> [...] --yes   (to Trash; add --forever to "
          "delete)")


def cmd_cleanup(install_base: Path, ids: list[str], forever: bool, yes: bool) -> None:
    """Remove the given cleanup candidates (re-scanned now, so a stale id
    can't target anything else). Trash by default."""
    from nexus import cleanup as C
    cands = {c['id']: c for c in cleanup_candidates(install_base, load_config())}
    quar = {f"quarantine:{q['path']}": q for q in quarantined()}
    unknown = [i for i in ids if i not in cands and i not in quar]
    if unknown:
        print(f"Not a current cleanup candidate: {', '.join(unknown)} — re-scan first.",
              file=sys.stderr)
        sys.exit(1)
    total = sum((cands.get(i) or quar.get(i)).get('size') or 0 for i in ids)
    verb = 'Deleting' if forever else 'Moving to Trash'
    if not yes:
        try:
            ans = input(f"{verb} {len(ids)} item(s), {total / 1e9:.2f} GB. Proceed? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans.strip().lower() not in ('y', 'yes'):
            print("Aborted.")
            return
    freed, failed = 0, 0
    for n, i in enumerate(ids, 1):
        c = cands.get(i) or quar[i]
        path = Path(c['path'])
        try:
            if c.get('action') == 'quarantine' and not forever:
                dest = quarantine_path(path, Path(c['library']))
                print(f"[{n}/{len(ids)}] quarantined {path} -> {dest}")
            elif forever or c.get('action') == 'delete':
                C.delete(path)
                Path(str(path) + '.origin').unlink(missing_ok=True)
                freed += c.get('size') or 0
                print(f"[{n}/{len(ids)}] deleted {path}")
            else:
                C.trash(path)
                Path(str(path) + '.origin').unlink(missing_ok=True)
                print(f"[{n}/{len(ids)}] moved to Trash: {path}")
        except OSError as e:
            failed += 1
            print(f"[{n}/{len(ids)}] [!] {path}: {e}", file=sys.stderr)
    if freed:
        print(f"Freed {freed / 1e9:.2f} GB.")
    if not forever:
        print("Trashed items still use space until you empty the Trash.")
    if failed:
        sys.exit(1)


def _launch_sh_proton_ok(entry: dict, proton: Path | None) -> bool:
    """Does the game's launch.sh run with `proton` (the play Proton)?"""
    launcher = entry.get('launcher')
    if not (launcher and proton):
        return True
    try:
        text = Path(launcher).read_text(errors='replace')
    except OSError:
        return True
    if 'umu-run' not in text and ' run ' not in text:
        return True                     # raw-wine launcher: no Proton involved
    return str(proton.parent) in text or str(proton) in text


def doctor_checks(install_base: Path) -> list[dict]:
    """Every environment/game check as data: {group, status (ok|warn|fail|info),
    title, detail, fix, actions:[{label, args, steam}]}. Read-only. The text
    report (--doctor) and the GUI's Health page both render this."""
    out: list[dict] = []

    def add(group, status, title, detail='', fix='', actions=None):
        out.append({'group': group, 'status': status, 'title': title,
                    'detail': detail, 'fix': fix, 'actions': actions or []})

    cfg = load_config()
    R = 'Requirements'
    wv = shutil.which('wine')
    try:
        wver = (subprocess.run(['wine', '--version'], capture_output=True, text=True,
                               timeout=15).stdout.strip() if wv else '')
    except (OSError, subprocess.TimeoutExpired):
        wver = ''
    add(R, 'ok' if wv else 'fail', 'Wine', wver or 'not found',
        '' if wv else f"Install it: {install_hint('wine', 'wine')}")
    umu = find_umu()
    add(R, 'ok' if umu else 'warn', 'umu-run', umu or 'not found',
        '' if umu else 'Install umu-launcher (AUR), or set [runner] backend = "proton"')
    proton = find_proton()
    add(R, 'ok' if proton else 'fail', 'Proton',
        ', '.join(sorted(list_protons())) or 'none installed',
        '' if proton else 'Install GE-Proton with ProtonPlus into compatibilitytools.d')
    for name, why in sorted(damaged_protons().items()):
        tag = proton_repair_tag(name)
        add(R, 'fail', f'{name} is damaged',
            f"{why}. Every game or installer run with it aborts at once "
            "(\"unimplemented function win32u.…\").",
            "Reinstall it (downloaded and verified again; the damaged copy goes to "
            "the Trash)" if tag else "Reinstall it with your package manager",
            [{'label': 'Reinstall', 'args': ['--proton-install', tag, '--force'],
              'steam': False}] if tag else None)
    add(R, 'ok' if shutil.which('winetricks') else 'info', 'winetricks',
        'found' if shutil.which('winetricks') else 'not found (only needed for --deps)')
    try:
        import vdf  # noqa: F401
        has_vdf = True
    except ImportError:
        has_vdf = False
    add(R, 'ok' if has_vdf else 'warn', 'python-vdf', 'found' if has_vdf else 'missing',
        '' if has_vdf else ("Steam shortcuts are disabled — "
                         + install_hint('python-vdf', 'python3-vdf (or: pip install --user vdf)')))
    base_ok = False
    try:
        install_base.mkdir(parents=True, exist_ok=True)
        base_ok = os.access(install_base, os.W_OK)
    except OSError:
        pass
    add(R, 'ok' if base_ok else 'fail', 'Install folder', str(install_base),
        '' if base_ok else f'Not writable — set [install] base in {CONFIG_PATH}')

    I = 'Integrations'
    account, why = resolve_steam_account()
    others = [steam_account_label(a) for a in steam_accounts()
              if not account or a['accountid'] != account['accountid']]
    add(I, 'ok' if account else 'info', 'Steam account',
        f"{steam_account_label(account)} — {why}" if account else 'no Steam profile found',
        (f"Other accounts on this machine: {', '.join(others)}. Pin one with "
         f"[integrations] steam_account.") if others else '')
    heroic_dir = find_heroic_config()
    add(I, 'ok' if heroic_dir else 'info', 'Heroic',
        str(heroic_dir) if heroic_dir else 'not installed (optional)')
    add(I, 'ok' if cfg.get('api_key') else 'info', 'SteamGridDB artwork',
        'API key set' if cfg.get('api_key') else 'no API key — games get placeholder art',
        '' if cfg.get('api_key') else 'Add an API key in Settings')
    hp = resolve_game_proton(None, cfg, quiet=True)
    src = ('[runner] proton' if cfg.get('proton') else
           '[heroic] proton' if cfg.get('heroic_proton') else 'newest installed')
    add(I, 'ok' if hp else 'warn', 'Play Proton',
        f"{hp.parent.name if hp else 'none'} ({src})")
    pg = protongen_bin()
    add(I, 'ok' if pg else 'info', 'protongen', pg or 'not installed (optional)')
    ag = agent_status()
    fix_agent = [{'label': 'Restart it', 'args': ['--agent-enable'], 'steam': False}]
    if not ag['installed']:
        add(I, 'info', 'Background agent',
            'off (optional) — downloads and alerts only while the window is open',
            'Settings → Background')
    elif not ag['current']:
        add(I, 'warn', 'Background agent',
            "its service points at another copy of Nexus", 'Set it up again from here',
            fix_agent)
    elif ag['enabled'] and not ag['running']:
        add(I, 'warn', 'Background agent', 'on, but not running',
            'See journalctl --user -u nexus-agent', fix_agent)
    else:
        add(I, 'ok' if ag['running'] else 'info', 'Background agent',
            f"running (last checked {(ag['updated_at'] or '?')[11:16]})" if ag['running']
            else 'installed but switched off')
    try:
        gamedb_doctor(add, I)
    except Exception:
        pass
    config_secrets_doctor(add, I)
    try:
        from nexus import protons as PR
        latest = (PR.releases('ge')['releases'] or [{}])[0].get('tag')
        have = [n for n in list_protons() if PR.family_of(n) == 'ge']
        newest = max(have, key=PR.version_key, default=None)
        if latest and (not newest or PR.version_key(latest) > PR.version_key(newest)[:2]):
            add(I, 'info', f'GE-Proton {latest} is available',
                f"Installed: {newest or 'none'}",
                actions=[{'label': 'Install', 'args': ['--proton-install', 'latest'],
                          'steam': False}])
    except Exception:
        pass                      # offline: the Proton page reports it

    G = 'Games'
    reg = load_registry()
    cd_root = STEAM_ROOT / "steamapps" / "compatdata"
    compat_map = _read_compat_mapping()
    protons = list_protons()
    for slug, entry in sorted(reg['games'].items()):
        name = entry.get('display_name') or slug
        gp = resolve_game_proton(entry, cfg, quiet=True)
        if not _launch_sh_proton_ok(entry, gp):
            add(G, 'warn', f'{name}: launcher uses a different Proton',
                f"launch.sh doesn't run {gp.parent.name}, but Steam/Heroic do — the prefix "
                f"gets switched between builds.",
                actions=[{'label': 'Re-apply settings', 'args': ['--apply-launch', slug],
                          'steam': bool(entry.get('steam_appid'))}])
        appid, compatdata = entry.get('steam_appid'), entry.get('compatdata')
        if appid and compatdata:
            link = cd_root / str(appid)
            try:
                redirected = link.is_symlink() and link.resolve() == Path(compatdata).resolve()
            except OSError:
                redirected = False
            if redirected and not (Path(compatdata) / 'tracked_files').exists():
                add(G, 'fail', f'{name}: will crash when launched from Steam',
                    "The prefix is missing Proton's tracked_files bookkeeping file.",
                    actions=[{'label': 'Repair Steam shortcut', 'args': ['--steam', slug],
                              'steam': True}])
            tool = compat_map.get(str(appid))
            path = proton_by_name(tool, protons) if tool else None
            if path and str(path).startswith('/usr/'):
                add(G, 'info', f'{name}: Steam uses a Proton under /usr',
                    f"{tool} lives in /usr, which the Steam Linux Runtime container can't "
                    f"always see.",
                    f"Optional: symlink it into {STEAM_ROOT}/compatibilitytools.d/{tool}")
        wp = entry.get('wine_prefix')
        if wp:
            wt = Path(wp) / 'winetricks.log'
            try:
                if wt.is_file() and 'dxvk' in wt.read_text(errors='replace').split():
                    add(G, 'warn', f'{name}: winetricks DXVK overrides Proton\'s',
                        'The prefix pins native d3d11/dxgi overrides, so Proton no longer '
                        'manages DXVK.',
                        'Remove the overrides in winecfg → Libraries, or reinstall with --force.',
                        actions=[{'label': 'Open winecfg',
                                  'args': ['--prefix-tool', slug, 'winecfg'], 'steam': False}])
            except OSError:
                pass
            if not Path(wp).is_dir():
                add(G, 'fail', f'{name}: install folder is missing', wp,
                    'Reinstall it, or prune the stale entry.',
                    actions=[{'label': 'Prune missing games', 'args': ['--prune', '--yes'],
                              'steam': True}])
        if pg:
            hit = protongen_memory_for(entry)
            if hit:
                cur = entry.get('launch') or {}
                pprof = profile_from_protongen_config(hit[1])
                if [pprof.get(k) for k in ('env', 'wrappers', 'args')] != \
                        [cur.get(k) for k in ('env', 'wrappers', 'args')]:
                    add(G, 'info', f'{name}: protongen has different tuning',
                        format_launch_options(pprof) or '(no options)',
                        actions=[{'label': 'Pull from protongen',
                                  'args': ['--from-protongen', slug],
                                  'steam': bool(entry.get('steam_appid'))}])

    try:
        from nexus import tips
        out += tips.system_checks()
    except Exception as e:                       # never let advice break the doctor
        add('Performance', 'info', 'Performance checks unavailable', str(e))

    S = 'Storage'
    for o in find_orphan_compatdata(install_base):
        add(S, 'warn', f"Stray Steam prefix compatdata/{o['path'].name}",
            f"{o['size_bytes'] / 1e9:.2f} GB · {o['reason']}"
            + (" · looks like it has SAVE DATA — check before cleaning" if o['has_savedata']
               else ''),
            actions=[{'label': 'Quarantine', 'steam': False,
                      'args': ['--clean-orphan-compatdata', o['path'].name, '--yes']}])
    for row in snapshot_doctor(install_base):
        add(S, *row)
    return out


def _looks_unmounted(path: Path, entry: dict) -> bool:
    """A missing game folder whose drive is probably just not mounted: its
    install base is gone too, or nothing exists below a mount point."""
    if entry.get('install_base') and not Path(entry['install_base']).is_dir():
        return True
    a = path
    while not a.exists() and a != a.parent:
        a = a.parent
    mounts = (Path('/mnt'), Path('/media'), Path('/run/media'))
    return a == Path('/') or a in mounts or a.parent in mounts \
        or a.parent.parent == Path('/run/media')


def stale_registry_entries() -> list[dict]:
    """Registry entries whose game is gone from disk (deleted by hand,
    outside Nexus): the prefix, or for an --add-steam game the exe."""
    out = []
    for slug, e in sorted(load_registry()['games'].items()):
        p = Path(e['wine_prefix']) if e.get('wine_prefix') else \
            Path(e['exe']) if e.get('exe') else None
        if p is None or p.exists():
            continue
        out.append({'slug': slug, 'name': e.get('display_name') or slug, 'path': str(p),
                    'unmounted': _looks_unmounted(p, e), 'entry': e})
    return out


def cmd_prune(yes: bool = False, as_json: bool = False) -> int:
    """Drop registry entries for games that were deleted outside Nexus, with
    their Steam shortcut, Heroic entry, app-menu entry and launcher. Lists
    them only, unless --yes. A game on a drive that isn't mounted is never
    pruned."""
    stale = stale_registry_entries()
    removed, skipped = [], []
    steam_up = None
    for s in stale:
        slug, e = s['slug'], s['entry']
        if s['unmounted']:
            skipped.append({'slug': slug, 'reason': 'its drive looks unmounted'})
            continue
        if not yes:
            continue
        if game_has_steam_shortcut(slug):
            steam_up = steam_is_running() if steam_up is None else steam_up
            if steam_up:
                skipped.append({'slug': slug, 'reason': 'Steam is running (it has a shortcut)'})
                continue
            if not remove_steam_shortcut(slug, require_steam_closed=False):
                skipped.append({'slug': slug, 'reason': "the Steam shortcut couldn't be removed"})
                continue
        remove_heroic_entry(slug, exe=e.get('exe'))
        _desktop_path(slug).unlink(missing_ok=True)
        launcher = Path(e['launcher']) if e.get('launcher') else None
        if launcher and launcher.parent == LAUNCHERS_DIR:
            launcher.unlink(missing_ok=True)
        unregister_game(slug)
        removed.append(slug)
    if as_json:
        print(json.dumps({'stale': [{k: v for k, v in s.items() if k != 'entry'}
                                    for s in stale],
                          'removed': removed, 'skipped': skipped}))
        return 0
    if not stale:
        print("Nothing to prune: every game in the library is still on disk.")
        return 0
    for s in stale:
        mark = 'removed' if s['slug'] in removed else \
            next((f"kept — {k['reason']}" for k in skipped if k['slug'] == s['slug']),
                 'missing')
        print(f"  {s['name']} ({s['slug']}): {s['path']}  [{mark}]")
    if not yes:
        print("Run again with --yes to remove the entries marked 'missing' "
              "(and their Steam/Heroic/app-menu entries).")
    return 0


def cmd_doctor(install_base: Path, as_json: bool = False) -> None:
    """Readiness report: every requirement, integration and per-game problem,
    each with a fix. Read-only; never changes anything."""
    checks = doctor_checks(install_base)
    if as_json:
        print(json.dumps({'checks': checks}))
        return
    sym = {'ok': '\033[32m✓\033[0m', 'fail': '\033[31m✗\033[0m',
           'warn': '\033[33m!\033[0m', 'info': '\033[36mi\033[0m'}
    if not sys.stdout.isatty():
        sym = {'ok': '✓', 'fail': '✗', 'warn': '!', 'info': 'i'}
    print("Nexus — environment check")
    group = None
    for c in checks:
        if c['group'] != group:
            group = c['group']
            print(f"\n{group}")
        print(f"  {sym[c['status']]}  {c['title']}" + (f" — {c['detail']}" if c['detail'] else ''))
        if c['fix']:
            print(f"       {c['fix']}")
        for a in c['actions']:
            print(f"       fix: nexus-cli {' '.join(shlex.quote(x) for x in a['args'])}")
    fails = [c for c in checks if c['status'] == 'fail' and c['group'] == 'Requirements']
    print("\n  " + ("Ready to install." if not fails else
                    "Not ready — resolve the ✗ items above."))


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .agent import agent_status  # noqa: E402
from .config import load_config, resolve_scan_dirs  # noqa: E402
from .desktop import _desktop_path  # noqa: E402
from .gamecmds import scan_repacks  # noqa: E402
from .heroic import find_heroic_config, remove_heroic_entry  # noqa: E402
from .info import config_secrets_doctor, gamedb_doctor  # noqa: E402
from .launchprofile import (format_launch_options, profile_from_protongen_config,
    protongen_bin, protongen_memory_for)  # noqa: E402
from .protoncmds import install_hint  # noqa: E402
from .registry import load_registry, unregister_game  # noqa: E402
from .runner import (damaged_protons, find_proton, find_umu, list_protons, proton_by_name,
    proton_repair_tag, resolve_game_proton)  # noqa: E402
from .steam import (_read_compat_mapping, _shortcut_uid, find_prefix_savedata, INTEGRITY_DIR,
    resolve_steam_account, savedata_owners, steam_account_label, steam_accounts,
    steam_is_running)  # noqa: E402
from .sunshine import game_has_steam_shortcut, remove_steam_shortcut  # noqa: E402
from .snapshots import list_snapshots, SNAP_DIR, snapshot_doctor  # noqa: E402
