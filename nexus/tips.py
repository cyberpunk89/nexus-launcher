"""Optimisation advice. Two kinds:

- system checks (read-only): things like an inactive sched-ext scheduler, a
  low vm.max_map_count or the power-saver profile. Each suggests a command
  for the user to run; Nexus never changes system settings itself.
- per-game suggestions: protongen's recipe catalog filtered by this machine's
  GPU/features (and ProtonDB tier where known), as launch-option strings."""

import os
import re
import resource
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

from .paths import PROTONGEN_CONFIG

BUNDLED_RECIPES = Path(__file__).resolve().parent / 'data' / 'recipes.toml'
PCI_IDS = [Path('/usr/share/hwdata/pci.ids'), Path('/usr/share/misc/pci.ids')]
VENDORS = {'0x1002': 'amd', '0x10de': 'nvidia', '0x8086': 'intel'}

# Recent AMD dGPUs a distro's pci.ids may not know yet: device id -> (name, gen).
AMD_IDS = {
    '0x7550': ('Radeon RX 9070 / 9070 XT', 'rdna4'), '0x7551': ('Radeon AI PRO R9700', 'rdna4'),
    '0x7590': ('Radeon RX 9060 XT', 'rdna4'), '0x7591': ('Radeon RX 9060', 'rdna4'),
    '0x744c': ('Radeon RX 7900 XT / XTX', 'rdna3'), '0x7448': ('Radeon PRO W7900', 'rdna3'),
    '0x747e': ('Radeon RX 7800 XT / 7700 XT', 'rdna3'), '0x7480': ('Radeon RX 7600', 'rdna3'),
    '0x7483': ('Radeon RX 7600M', 'rdna3'), '0x7489': ('Radeon RX 7400 / 7500', 'rdna3'),
}


def _protongen_gpu_gen() -> str | None:
    """The GPU generation the user picked in protongen (it wins there too)."""
    try:
        return tomllib.loads((PROTONGEN_CONFIG / 'state.toml').read_text()).get('gpu_gen') or None
    except (OSError, ValueError):
        return None


# ---- hardware -------------------------------------------------------------

def _pci_name(vendor: str, device: str) -> str | None:
    v, d = vendor.removeprefix('0x').lower(), device.removeprefix('0x').lower()
    for p in PCI_IDS:
        try:
            lines = p.read_text(errors='replace').splitlines()
        except OSError:
            continue
        in_vendor = False
        for line in lines:
            if line.startswith(v + ' '):
                in_vendor = True
            elif in_vendor and line.startswith('\t') and not line.startswith('\t\t'):
                if line[1:5].lower() == d:
                    return line[7:].strip()
            elif in_vendor and line and not line.startswith('\t'):
                break
    return None


def hardware() -> dict:
    """GPUs from /sys/class/drm, plus desktop features recipes can depend on.
    AMD generation (RDNA3/4) comes from the PCI name ("Navi 3x"/"Navi 4x"),
    the same way protongen detects it."""
    gpus, seen = [], set()
    for card in sorted(Path('/sys/class/drm').glob('card[0-9]*')):
        dev = card / 'device'
        try:
            vendor = (dev / 'vendor').read_text().strip()
            device = (dev / 'device').read_text().strip()
            slot = dev.resolve().name
        except OSError:
            continue
        if slot in seen or '-' in card.name:
            continue
        seen.add(slot)
        known = AMD_IDS.get(device.lower()) if VENDORS.get(vendor) == 'amd' else None
        name = _pci_name(vendor, device) or (known and known[0]) or f"{vendor}:{device}"
        gen = known[1] if known else None
        m = re.search(r'Navi\s*(\d)', name)
        if VENDORS.get(vendor) == 'amd' and m and not gen:
            gen = {'3': 'rdna3', '4': 'rdna4', '2': 'rdna2', '1': 'rdna1'}.get(m.group(1))
        boot = (dev / 'boot_vga').read_text().strip() == '1' if (dev / 'boot_vga').exists() else False
        gpus.append({'vendor': VENDORS.get(vendor, vendor), 'name': name, 'gen': gen,
                     'slot': slot, 'boot_vga': boot,
                     'integrated': bool(re.search(r'Raphael|Granite Ridge|Phoenix|Rembrandt|'
                                                  r'Renoir|Cezanne|Strix|HD Graphics|UHD|Iris',
                                                  name, re.IGNORECASE))})
    override = _protongen_gpu_gen()
    if override in ('rdna3', 'rdna4'):
        for g in gpus:
            if g['vendor'] == 'amd' and not g['integrated']:
                g['gen'] = override
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '')
    features = {
        'wayland': bool(os.environ.get('WAYLAND_DISPLAY')),
        'kde': 'KDE' in desktop.upper(),
        'ntsync': Path('/dev/ntsync').exists(),
        'rdna3': any(g['gen'] == 'rdna3' for g in gpus),
        'rdna4': any(g['gen'] == 'rdna4' for g in gpus),
    }
    features['fsr4'] = features['rdna4'] or features['rdna3']
    discrete = [g for g in gpus if not g['integrated']]
    return {'gpus': gpus, 'vendors': sorted({g['vendor'] for g in discrete or gpus}),
            'features': features, 'hybrid': len(gpus) > 1 and bool(discrete)}


# ---- protongen recipes ----------------------------------------------------

def load_recipes() -> tuple[list[dict], str]:
    """protongen's recipes: the user's override if present, else our bundled copy."""
    for p in (PROTONGEN_CONFIG / 'recipes.toml', BUNDLED_RECIPES):
        try:
            return tomllib.loads(p.read_text()).get('recipe', []), str(p)
        except (OSError, ValueError):
            continue
    return [], ''


def recipe_launch_string(r: dict) -> str:
    parts = [f"{k}={shlex.quote(str(v))}" for k, v in r.get('env', [])]
    for w in r.get('wrappers', []):
        cmd, args = (w[0], w[1] if len(w) > 1 else '')
        if cmd == 'gamescope':
            parts += ['gamescope', *shlex.split(args or ''), '--']
        else:
            parts += [cmd, *shlex.split(args or '')]
    parts.append('%command%')
    return ' '.join(parts)


def recipes_for(hw: dict, tier: str | None = None, kinds=('profile', 'fix')) -> list[dict]:
    """Recipes that fit this machine. A recipe restricted to ProtonDB tiers
    only shows when the game's tier is known and listed."""
    recipes, source = load_recipes()
    feats = hw['features']
    out = []
    for r in recipes:
        if r.get('kind', 'profile') not in kinds:
            continue
        gpu = r.get('gpu', 'any')
        if gpu != 'any' and gpu not in hw['vendors']:
            continue
        needs = r.get('needs', [])
        unknown = [n for n in needs if n not in feats]      # e.g. hdr: can't detect
        if any(n in feats and not feats[n] for n in needs):
            continue
        tiers = r.get('protondb_tiers')
        if tiers and (not tier or tier not in tiers):
            continue
        out.append({
            'name': r.get('name'), 'kind': r.get('kind', 'profile'),
            'symptom': r.get('symptom'), 'description': r.get('description', ''),
            'tags': r.get('tags', []), 'env': r.get('env', []),
            'wrappers': r.get('wrappers', []), 'needs_unverified': unknown,
            'launch_options': recipe_launch_string(r), 'source': source,
        })
    return out


# ---- system checks --------------------------------------------------------

def _run(cmd: list[str], timeout: float = 3) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def system_checks(hw: dict | None = None) -> list[dict]:
    """Doctor-style rows (group 'Performance'). Read-only; fixes are commands
    to copy, never run."""
    hw = hw or hardware()
    out = []

    def add(status, title, detail='', fix=''):
        out.append({'group': 'Performance', 'status': status, 'title': title,
                    'detail': detail, 'fix': fix, 'actions': []})

    # sched-ext (CachyOS): a gaming scheduler helps frame pacing under load.
    scx_bins = sorted(p.name for d in os.environ.get('PATH', '').split(':') if d
                      for p in Path(d).glob('scx_*') if p.is_file()) if os.environ.get('PATH') else []
    if scx_bins:
        active = _run(['systemctl', 'is-active', 'scx_loader']) == 'active' or \
            _run(['systemctl', 'is-active', 'scx']) == 'active'
        current = _run(['scxctl', 'get'])
        if active or (current and 'no scx' not in current.lower()):
            add('ok', 'sched-ext scheduler', current or 'running')
        else:
            gp = shutil.which('game-performance')
            add('info', 'No sched-ext scheduler is running',
                f"{len(scx_bins)} schedulers are installed (e.g. scx_lavd) but none is active"
                + (" — game-performance in a game's launch options switches one on while "
                   "it runs." if gp else "."),
                "sudo systemctl enable --now scx_loader   # then pick one: scxctl start "
                "--sched lavd --mode gaming")

    prof = _run(['powerprofilesctl', 'get'])
    if prof == 'power-saver':
        add('warn', 'Power profile is power-saver', 'Games will run noticeably slower.',
            'powerprofilesctl set balanced   # or: performance')
    elif prof:
        add('ok', 'Power profile', prof)

    try:
        mmc = int(Path('/proc/sys/vm/max_map_count').read_text())
    except (OSError, ValueError):
        mmc = None
    if mmc is not None and mmc < 1048576:
        add('warn', f'vm.max_map_count is low ({mmc})',
            'Some games (and anti-cheat) crash when they run out of memory mappings.',
            "echo 'vm.max_map_count = 2147483642' | sudo tee /etc/sysctl.d/80-gamecompatibility.conf"
            " && sudo sysctl --system")
    elif mmc:
        add('ok', 'vm.max_map_count', str(mmc))

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 524288:
        add('warn', f'Open-file limit is low ({hard})', 'esync needs a high file limit.',
            'Raise DefaultLimitNOFILE in /etc/systemd/system.conf (e.g. 1048576), then reboot')

    if hw['features']['ntsync']:
        add('ok', 'ntsync', '/dev/ntsync is available — recent Proton builds use it for '
                            'faster Windows synchronisation')
    elif Path('/sys/module').exists():
        add('info', 'ntsync not available', 'Kernel 6.14+ has it as a module.',
            'sudo modprobe ntsync   # and add it to /etc/modules-load.d/ntsync.conf')

    tools = {'gamemoderun': 'GameMode', 'game-performance': 'game-performance (CachyOS)',
             'mangohud': 'MangoHud', 'gamescope': 'gamescope'}
    have = [label for b, label in tools.items() if shutil.which(b)]
    missing = [label for b, label in tools.items() if not shutil.which(b)]
    add('ok' if have else 'info', 'Gaming tools', ', '.join(have) or 'none installed',
        f"Not installed: {', '.join(missing)}" if missing else '')

    if hw['hybrid']:
        dgpu = next((g for g in hw['gpus'] if not g['integrated']), None)
        igpu = next((g for g in hw['gpus'] if g['integrated']), None)
        if dgpu and igpu:
            add('info', 'Two GPUs', f"Games should use {dgpu['name']} rather than "
                f"{igpu['name']}. If one starts on the integrated GPU, add DRI_PRIME=1 to "
                f"its launch options.")

    for g in hw['gpus']:
        if not g['integrated'] and g['gen']:
            add('ok', 'GPU', f"{g['name']} ({g['gen'].upper()})")
    return out
