"""Proton commands: --protons, install/remove/migrate builds, ProtonDB."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from nexus import paths as NX

from .common import atomic_write, backup_file, Logger, STEAM_ROOT, use_logger


def cmd_list_protons(config: dict, as_json: bool = False) -> None:
    protons = list_protons()
    default = resolve_game_proton(None, config, quiet=True)
    rows = [{'name': n, 'tool_name': proton_tool_name(p), 'path': str(p.parent),
             'default': bool(default and p == default)} for n, p in sorted(protons.items())]
    if as_json:
        print(json.dumps({'protons': rows, 'default': default.parent.name if default else None}))
        return
    for r in rows:
        print(f"  {'*' if r['default'] else ' '} {r['name']}  ({r['path']})")


def _heroic_proton_bins() -> dict[str, list[str]]:
    """{proton binary path: [Heroic game titles]} from every GamesConfig."""
    out: dict[str, list[str]] = {}
    cfg_dir = find_heroic_config()
    if not cfg_dir:
        return out
    titles = {}
    for name in ('sideload_apps/library.json', 'store_cache/legendary_library.json',
                 'store_cache/gog_library.json'):
        try:
            data = json.loads((cfg_dir / name).read_text())
        except (OSError, ValueError):
            continue
        lib = data.get('games', data.get('library', [])) if isinstance(data, dict) else data
        for g in lib if isinstance(lib, list) else ():
            if isinstance(g, dict) and g.get('app_name'):
                titles[g['app_name']] = g.get('title') or g['app_name']
    for gc in (cfg_dir / 'GamesConfig').glob('*.json'):
        try:
            data = json.loads(gc.read_text())
        except (OSError, ValueError):
            continue
        inner = data.get(gc.stem)
        wv = inner.get('wineVersion') if isinstance(inner, dict) else None
        if isinstance(wv, dict) and wv.get('type') == 'proton' and wv.get('bin'):
            out.setdefault(wv['bin'], []).append(titles.get(gc.stem, gc.stem))
    return out


def proton_usage(config: dict) -> dict[str, list[str]]:
    """{build dir name: ['Nexus: 007 First Light', 'Steam: PEAK', ...]} —
    who plays with each installed build, across Nexus, Steam and Heroic."""
    from nexus import steamlib
    protons = list_protons()
    by_path = {str(p): n for n, p in protons.items()}
    use: dict[str, list[str]] = {n: [] for n in protons}
    for e in load_registry()['games'].values():
        p = resolve_game_proton(e, config, quiet=True)
        if p and p.parent.name in use:
            use[p.parent.name].append(f"Nexus: {e.get('display_name') or e['slug']}")
    names = {g['appid']: g['display_name'] for g in steamlib.scan(STEAM_ROOT)}
    ours = {int(e.get('steam_appid') or 0) for e in load_registry()['games'].values()}
    shortcut_names = _all_shortcut_names()
    for appid, tool in _read_compat_mapping().items():
        p = proton_by_name(tool, protons)
        if not p:
            continue
        aid = int(appid) if appid.isdigit() else 0
        if aid in names:
            use[p.parent.name].append(f"Steam: {names[aid]}")
        elif aid in shortcut_names and aid not in ours:
            use[p.parent.name].append(f"Steam shortcut: {shortcut_names[aid]}")
    for binpath, titles in _heroic_proton_bins().items():
        n = by_path.get(binpath) or by_path.get(str(Path(binpath)))
        if n:
            use[n] += [f"Heroic: {t}" for t in titles if f"Nexus: {t}" not in use[n]]
    default = resolve_game_proton(None, config, quiet=True)
    if default and default.parent.name in use:
        use[default.parent.name].insert(0, 'Default for new games')
    return use


def _valve_protons() -> list[dict]:
    from nexus import steamlib
    out = []
    for lib in steamlib.library_folders(STEAM_ROOT):
        for p in sorted((lib / 'steamapps' / 'common').glob('Proton*/proton')):
            out.append({'name': p.parent.name, 'path': str(p.parent), 'source': 'valve'})
    return out


def install_hint(arch: str, other: str) -> str:
    """How to install a package here: the pacman line on Arch-based systems,
    the generic package name elsewhere."""
    if shutil.which('pacman'):
        return f"sudo pacman -S --needed {arch}"
    return f"install {other} with your distro's package manager"


def _pacman_version(pkg: str) -> str | None:
    if not shutil.which('pacman'):
        return None
    try:
        r = subprocess.run(['pacman', '-Q', pkg], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.split()[1] if r.returncode == 0 and len(r.stdout.split()) > 1 else None


def ge_is_newer(latest_ge: str | None, installed: list[str]) -> bool:
    """Whether GE-Proton's latest release is newer than every GE build in
    `installed` (names), or none is installed."""
    from nexus import protons as PR
    have = [n for n in installed if PR.family_of(n) == 'ge']
    newest = max(have, key=PR.version_key, default=None)
    return bool(latest_ge and (not newest or
                               PR.version_key(latest_ge) > PR.version_key(newest)[:2]))


def ge_update() -> tuple[str | None, bool]:
    """(latest GE-Proton tag, whether it's newer than what's installed),
    from the 6-hour release cache. (None, False) offline."""
    from nexus import protons as PR, net
    try:
        rel = PR.releases('ge', False)
    except net.NetError:
        return None, False
    latest = rel['releases'][0]['tag'] if rel['releases'] else None
    return latest, ge_is_newer(latest, list(list_protons()))


def cmd_protons(config: dict, as_json: bool, refresh: bool = False) -> None:
    """Installed builds (with who uses them) plus upstream release status."""
    from nexus import protons as PR, net
    usage = proton_usage(config)
    rows = []
    for name, binp in sorted(list_protons().items(), key=lambda kv: PR.version_key(kv[0]),
                             reverse=True):
        d = binp.parent
        rows.append({
            'name': name, 'tool_name': proton_tool_name(binp), 'path': str(d),
            'family': PR.family_of(name),
            'source': 'user' if PR.is_user_build(d) else 'system',
            'used_by': usage.get(name, []),
            'removable': PR.is_user_build(d),
        })
    families = {}
    for fam in ('ge', 'cachyos'):
        try:
            rel = PR.releases(fam, refresh)
            latest = rel['releases'][0] if rel['releases'] else None
            families[fam] = {'latest': latest and latest['tag'], 'stale': rel['stale'],
                             'fetched_at': rel['fetched_at'], 'error': None}
        except net.NetError as e:
            families[fam] = {'latest': None, 'error': str(e)}
    families['ge']['update'] = ge_is_newer(families['ge'].get('latest'),
                                           [r['name'] for r in rows])
    families['cachyos']['installed'] = _pacman_version('proton-cachyos-slr') \
        or _pacman_version('proton-cachyos')
    try:
        dismissed = __import__('tomllib').loads(
            (NX.PROTONGEN_CONFIG / 'state.toml').read_text()).get('dismissed_runtime_updates', [])
    except (OSError, ValueError):
        dismissed = []
    info = {'installed': rows, 'valve': _valve_protons(), 'families': families,
            'protongen_dismissed': dismissed,
            'default': (resolve_game_proton(None, config, quiet=True) or Path('')).parent.name
                       or None}
    if as_json:
        print(json.dumps(info))
        return
    print("Installed Proton builds:")
    for r in rows:
        print(f"  {r['name']:<28} {r['source']:<7} used by: "
              f"{', '.join(r['used_by']) or 'nothing'}")
    for v in info['valve']:
        print(f"  {v['name']:<28} valve   (managed by Steam)")
    ge = families['ge']
    if ge.get('latest'):
        print(f"\nGE-Proton latest: {ge['latest']}"
              + ("  — update available: nexus-cli --proton-install latest" if ge['update'] else
                 "  (installed)"))
    cy = families['cachyos']
    if cy.get('installed'):
        print(f"proton-cachyos (pacman): {cy['installed']}; upstream: {cy.get('latest')}. "
              f"Update with: sudo pacman -Syu")


def cmd_proton_releases(refresh: bool, as_json: bool) -> None:
    from nexus import protons as PR, net
    try:
        rel = PR.releases('ge', refresh)
    except net.NetError as e:
        print(f"Couldn't reach GitHub: {e}", file=sys.stderr)
        sys.exit(1)
    have = set(list_protons())
    for r in rel['releases']:
        r['installed'] = any(n == r['tag'] or n.startswith(f"{r['tag']}-") for n in have)
    if as_json:
        print(json.dumps(rel))
        return
    for r in rel['releases']:
        print(f"  {r['tag']:<16} {r['published_at'][:10]}  "
              f"{(r['tar_size'] or 0) / 1e6:6.0f} MB{'  (installed)' if r['installed'] else ''}")


def cmd_proton_install(tag: str, force: bool = False) -> None:
    """Install a GE-Proton release ('latest' or a tag) into Steam's
    compatibilitytools.d. Steam picks it up on its next start. With force,
    an installed copy is replaced: the new one is downloaded, verified and
    extracted first, then the old one goes to the Trash."""
    from nexus import protons as PR, net, cleanup as C
    try:
        rel = PR.releases('ge')['releases']
    except net.NetError as e:
        print(f"Couldn't reach GitHub: {e}", file=sys.stderr)
        sys.exit(1)
    pick = rel[0] if tag == 'latest' and rel else next((r for r in rel if r['tag'] == tag), None)
    if not pick:
        print(f"No GE-Proton release {tag!r} (see --proton-releases)", file=sys.stderr)
        sys.exit(1)
    have = [n for n in list_protons() if n == pick['tag'] or n.startswith(f"{pick['tag']}-")]
    if have and not force:
        broken = damaged_protons()
        print(f"{pick['tag']} is already installed."
              + (f" It's damaged ({broken[have[0]]}): reinstall it with --force."
                 if have[0] in broken else ''))
        return
    if have and not all(PR.is_user_build(list_protons()[n].parent) for n in have):
        print(f"{have[0]} is a system package — reinstall it with your package manager.",
              file=sys.stderr)
        sys.exit(1)
    try:
        final = PR.install(pick, say=lambda m: print(m, flush=True),
                           replace=C.trash if have else None)
    except PR.InstallError as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)
    if have:
        print(f"Reinstalled {final.name} → {final} (the old copy is in the Trash)")
        return
    print(f"Installed {final.name} → {final}")
    print("Restart Steam to see it in Steam's compatibility list.")


def cmd_proton_remove(name: str, force: bool, config: dict) -> None:
    from nexus import protons as PR, cleanup as C
    binp = list_protons().get(name)
    if not binp:
        print(f"No installed Proton named {name!r}", file=sys.stderr)
        sys.exit(1)
    if not PR.is_user_build(binp.parent):
        print(f"{name} is a system package — remove it with your package manager.",
              file=sys.stderr)
        sys.exit(1)
    users = [u for u in proton_usage(config).get(name, []) if u != 'Default for new games']
    if users and not force:
        print(f"{name} is still used by: {', '.join(users)}. Move them to another build "
              f"first (--proton-migrate {name} <other>), or pass --force.", file=sys.stderr)
        sys.exit(1)
    C.trash(binp.parent)
    print(f"Moved {name} to the Trash.")


def cmd_proton_migrate(old: str, new: str, config: dict) -> None:
    """Point everything using build `old` at `new`: Nexus games (their pin or
    recorded build), Steam games' forced compat tool, and Heroic entries."""
    protons = list_protons()
    if old not in protons or new not in protons:
        print(f"Both builds must be installed (have: {', '.join(sorted(protons))})",
              file=sys.stderr)
        sys.exit(1)
    newp = protons[new]
    use_logger(Logger())
    changed = []
    # Nexus games
    for slug, e in list(load_registry()['games'].items()):
        prof = dict(e.get('launch') or {})
        touched = False
        if prof.get('proton') == old:
            prof['proton'] = new
            update_registry_entry(slug, launch=prof)
            touched = True
        if e.get('proton') == old:
            update_registry_entry(slug, proton=new)
            touched = True
        if touched:
            changed.append(slug)
    for slug in changed:
        cmd_apply(slug, config)
    # Steam games
    old_tool, new_tool = proton_tool_name(protons[old]), proton_tool_name(newp)
    steam_ids = [aid for aid, tool in _read_compat_mapping().items() if tool == old_tool]
    if steam_ids:
        if steam_is_running():
            print(f"  [!] Steam is running — skipped re-pointing {len(steam_ids)} Steam "
                  f"game(s). Quit Steam and run this again.")
        else:
            for aid in steam_ids:
                set_steam_compat_tool(int(aid), new_tool)
            print(f"  Steam: {len(steam_ids)} game(s) now use {new_tool}.")
    # Heroic entries not owned by Nexus (Nexus's were re-applied above)
    cfg_dir = find_heroic_config()
    if cfg_dir:
        n = 0
        for gc in (cfg_dir / 'GamesConfig').glob('*.json'):
            try:
                data = json.loads(gc.read_text())
            except (OSError, ValueError):
                continue
            inner = data.get(gc.stem)
            wv = inner.get('wineVersion') if isinstance(inner, dict) else None
            if isinstance(wv, dict) and wv.get('bin') == str(protons[old]):
                inner['wineVersion'] = _heroic_wine_version(newp)
                backup_file(gc)
                atomic_write(gc, json.dumps(data, indent=4))
                n += 1
        if n:
            print(f"  Heroic: {n} game(s) now use {new}.")
    print(f"Moved {len(changed)} Nexus game(s) from {old} to {new}.")


def cmd_protondb(appids: list[str], refresh: bool, as_json: bool) -> None:
    from nexus import protondb
    tiers = protondb.tiers([int(a) for a in appids if a.isdigit()], refresh)
    if as_json:
        print(json.dumps({str(k): v for k, v in tiers.items()}))
        return
    for appid, t in tiers.items():
        print(f"  {appid}: {t.get('tier') or 'no reports'}"
              + (f" (trending {t['trending']}, {t.get('total')} reports)" if t.get('tier') else ''))


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import cmd_apply  # noqa: E402
from .doctor import _all_shortcut_names  # noqa: E402
from .heroic import _heroic_wine_version, find_heroic_config  # noqa: E402
from .registry import load_registry, update_registry_entry  # noqa: E402
from .runner import (damaged_protons, list_protons, proton_by_name, proton_tool_name,
    resolve_game_proton)  # noqa: E402
from .steam import _read_compat_mapping, set_steam_compat_tool, steam_is_running  # noqa: E402
