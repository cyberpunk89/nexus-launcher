"""Per-game launch profiles and performance presets."""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .common import _cached_read, _load_toml, atomic_write, backup_file


#
# Stored in games.json as entry['launch']:
#   {"proton": str|None,          # per-game Proton build (overrides config)
#    "env": [[KEY, VALUE], ...],  # environment variables
#    "wrappers": [argv...],       # wrapper programs before the game, flat argv
#                                 #   e.g. ["gamescope","-W","1920","--","mangohud"]
#    "args": [argv...],           # arguments after the game exe
#    "source": "manual"|"protongen", "updated_at": iso8601}
# An entry without 'launch' has no profile: Steam launch options and Heroic
# env/wrappers are then left exactly as the user set them. A present-but-empty
# profile actively clears them.

PROTONGEN_STATE = Path.home() / ".config/protongen/state.toml"
PROTONGEN_CODE_PREFIX = "protongen:v1:"

# Wrapper programs a flat wrapper argv is split on (for Heroic's per-wrapper
# list), with protongen's outer->inner rank (params.toml `order`).
WRAPPER_RANK = {
    'gamescope': 0, 'prime-run': 10, 'game-performance': 20, 'gamemoderun': 30,
    'dlss-swapper': 40, 'obs-gamecapture': 45, 'strangle': 45, 'pw-jack': 45,
    'mangohud': 50,
}
_DEFAULT_WRAPPER_RANK = 45

# Env vars the launchers set themselves; a profile must not override them or
# the game would run against a different prefix / Steam root.
_RESERVED_ENV = {'WINEPREFIX', 'STEAM_COMPAT_DATA_PATH',
                 'STEAM_COMPAT_CLIENT_INSTALL_PATH', 'PROTONPATH'}
_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
# Env vars a shared preset code may not set: each one can make the loader, a
# shell, Python (Proton and umu-run are Python) or GIO run something else.
_UNSAFE_ENV_RE = re.compile(r'^(LD_|PYTHON|PERL5|GIO_|DYLD_)|^(BASH_ENV|ENV|BROWSER|PATH|'
                            r'SHELL|IFS|PROMPT_COMMAND|NODE_OPTIONS|RUBYOPT|GCONV_PATH)$')


class LaunchParseError(ValueError):
    pass


def _is_env_token(tok: str) -> bool:
    k, sep, _ = tok.partition('=')
    return bool(sep) and bool(_ENV_KEY_RE.match(k))


def empty_profile() -> dict:
    return {'proton': None, 'env': [], 'wrappers': [], 'args': []}


def parse_launch_options(text: str) -> dict:
    """Parse a launch line into a profile (env/wrappers/args, plus proton when
    the line names one via PROTONPATH). Understands:
      - a Steam launch-options string:  ENV=v ... wrappers %command% args
      - a umu command (protongen's umu mode):  ENV=v ... wrappers umu-run <exe> args
      - bare env assignments / game args with no %command% (Steam semantics:
        leading KEY=value are env, anything else is passed to the game).
    Raises LaunchParseError on unbalanced quotes."""
    try:
        toks = shlex.split(text or '')
    except ValueError as e:
        raise LaunchParseError(f"can't parse launch options: {e}") from None
    prof = empty_profile()
    before, after = toks, []
    if '%command%' in toks:
        i = toks.index('%command%')
        before, after = toks[:i], toks[i + 1:]
    elif any(os.path.basename(t) == 'umu-run' for t in toks):
        i = next(i for i, t in enumerate(toks) if os.path.basename(t) == 'umu-run')
        before, after = toks[:i], toks[i + 2:]   # skip umu-run and its exe
    else:
        n = 0
        while n < len(toks) and _is_env_token(toks[n]):
            n += 1
        before, after = toks[:n], toks[n:]
    n = 0
    while n < len(before) and _is_env_token(before[n]):
        n += 1
    for tok in before[:n]:
        k, _, v = tok.partition('=')
        if k == 'PROTONPATH':
            prof['proton'] = os.path.basename(v.rstrip('/')) or None
        elif k in _RESERVED_ENV:
            continue
        else:
            prof['env'] = [e for e in prof['env'] if e[0] != k] + [[k, v]]
    prof['wrappers'] = before[n:]
    prof['args'] = after
    return prof


def profile_is_empty(prof: dict | None) -> bool:
    return not prof or not (prof.get('env') or prof.get('wrappers') or prof.get('args'))


def format_launch_options(prof: dict | None) -> str:
    """The Steam launch-options string for a profile ('' when it sets nothing)."""
    if profile_is_empty(prof):
        return ''
    parts = [f"{k}={shlex.quote(v)}" for k, v in prof.get('env', [])]
    parts += [shlex.quote(t) for t in prof.get('wrappers', [])]
    parts.append('%command%')
    parts += [shlex.quote(t) for t in prof.get('args', [])]
    return ' '.join(parts)


def split_wrappers(wrappers: list[str]) -> list[list[str]]:
    """Split a flat wrapper argv into one argv per program: a new program starts
    at the first token, after a `--`, or at a known wrapper name / absolute path
    to an executable."""
    segs: list[list[str]] = []
    for tok in wrappers:
        starts = (not segs or segs[-1][-1:] == ['--']
                  or os.path.basename(tok) in WRAPPER_RANK
                  or (tok.startswith('/') and os.access(tok, os.X_OK)))
        if starts:
            segs.append([tok])
        else:
            segs[-1].append(tok)
    return segs


# ---------------------------------------------------------------------------
# Performance presets: named switches over a profile's env and wrappers
# ---------------------------------------------------------------------------
#
# Nothing extra is stored: perf_state() reads the switches back out of the
# profile, so hand-written options, protongen presets and these toggles all
# describe one command line.

FPS_ENV = ('DXVK_FRAME_RATE', 'VKD3D_FRAME_RATE')     # D3D8-11 / D3D12 frame caps
PERF_MODES = ('game-performance', 'gamemoderun')     # CachyOS power profile / Feral
UPSCALERS = {'off': [], 'fsr4': [('PROTON_FSR4_UPGRADE', '1')],
             'optiscaler': [('PROTON_USE_OPTISCALER', '1'), ('PROTON_FSR4_UPGRADE', '1')]}
_UPSCALER_KEYS = {'PROTON_FSR4_UPGRADE', 'PROTON_USE_OPTISCALER', 'PROTON_OPTISCALER_CONFIG'}
_GAMESCOPE_RE = re.compile(r'^(\d{3,5})x(\d{3,5})(?:@(\d{2,3}))?$')


def _env_get(prof: dict, key: str) -> str | None:
    return next((v for k, v in prof.get('env', []) if k == key), None)


def _env_set(env: list, key: str, value: str | None) -> list:
    out = [e for e in env if e[0] != key]
    if value is not None:
        out.append([key, value])
    return out


def parse_gamescope(seg: list[str]) -> dict:
    """{w, h, r, render_w, render_h, fsr, fullscreen, hdr} from a gamescope
    wrapper argv (its own options, up to `--`)."""
    g = {'w': None, 'h': None, 'r': None, 'render_w': None, 'render_h': None,
         'fsr': False, 'fullscreen': False, 'hdr': False}
    flags = {'-W': 'w', '--output-width': 'w', '-H': 'h', '--output-height': 'h',
             '-r': 'r', '--nested-refresh': 'r', '-w': 'render_w', '--nested-width': 'render_w',
             '-h': 'render_h', '--nested-height': 'render_h'}
    it = iter(seg[1:])
    for tok in it:
        if tok == '--':
            break
        if tok in flags:
            v = next(it, '')
            g[flags[tok]] = int(v) if v.isdigit() else None
        elif tok in ('-F', '--filter'):
            g['fsr'] = next(it, '') == 'fsr'
        elif tok in ('-U', '--fsr-upscaling'):
            g['fsr'] = True
        elif tok in ('-f', '--fullscreen'):
            g['fullscreen'] = True
        elif tok == '--hdr-enabled':
            g['hdr'] = True
    return g


def gamescope_argv(g: dict) -> list[str]:
    out = ['gamescope']
    for flag, key in (('-W', 'w'), ('-H', 'h'), ('-r', 'r'), ('-w', 'render_w'),
                      ('-h', 'render_h')):
        if g.get(key):
            out += [flag, str(int(g[key]))]
    if g.get('fsr'):
        out += ['-F', 'fsr']
    if g.get('fullscreen'):
        out.append('-f')
    if g.get('hdr'):
        out.append('--hdr-enabled')
    return out + ['--']


def perf_state(prof: dict | None) -> dict:
    prof = prof or {}
    segs = split_wrappers(prof.get('wrappers', []))
    names = [os.path.basename(s[0]) for s in segs]
    fps = _env_get(prof, 'DXVK_FRAME_RATE') or _env_get(prof, 'VKD3D_FRAME_RATE')
    up = ('optiscaler' if _env_get(prof, 'PROTON_USE_OPTISCALER') == '1' else
          'fsr4' if _env_get(prof, 'PROTON_FSR4_UPGRADE') == '1' else 'off')
    gs = next((parse_gamescope(s) for s in segs if os.path.basename(s[0]) == 'gamescope'),
              None)
    return {'fps': int(fps) if fps and fps.isdigit() else 0,
            'mode': next((m for m in PERF_MODES if m in names), 'off'),
            'mangohud': 'mangohud' in names or _env_get(prof, 'MANGOHUD') == '1',
            'wayland': _env_get(prof, 'PROTON_ENABLE_WAYLAND') == '1',
            'no_hidraw': _env_get(prof, 'PROTON_DISABLE_HIDRAW') == '1',
            'upscaler': up, 'gamescope': gs}


def _rank(seg: list[str]) -> int:
    return WRAPPER_RANK.get(os.path.basename(seg[0]), _DEFAULT_WRAPPER_RANK)


def apply_perf(prof: dict | None, changes: dict) -> dict:
    """A copy of `prof` with the given switches set (keys as perf_state);
    everything else in it is kept. Wrappers stay in protongen's outer→inner
    order."""
    prof = {**empty_profile(), **(prof or {})}
    env = [list(e) for e in prof.get('env', [])]
    segs = split_wrappers(prof.get('wrappers', []))
    if 'fps' in changes:
        n = int(changes['fps'] or 0)
        for k in FPS_ENV:
            env = _env_set(env, k, str(n) if n > 0 else None)
    if 'mode' in changes:
        segs = [s for s in segs if os.path.basename(s[0]) not in PERF_MODES]
        if changes['mode'] in PERF_MODES:
            segs.append([changes['mode']])
    if 'mangohud' in changes:
        segs = [s for s in segs if os.path.basename(s[0]) != 'mangohud']
        env = _env_set(env, 'MANGOHUD', None)
        if changes['mangohud']:
            segs.append(['mangohud'])
    if 'wayland' in changes:
        env = _env_set(env, 'PROTON_ENABLE_WAYLAND', '1' if changes['wayland'] else None)
    if 'no_hidraw' in changes:
        env = _env_set(env, 'PROTON_DISABLE_HIDRAW', '1' if changes['no_hidraw'] else None)
    if 'upscaler' in changes:
        want = changes['upscaler'] if changes['upscaler'] in UPSCALERS else 'off'
        keep_cfg = _env_get(prof, 'PROTON_OPTISCALER_CONFIG') if want == 'optiscaler' else None
        env = [e for e in env if e[0] not in _UPSCALER_KEYS]
        env += [[k, v] for k, v in UPSCALERS[want]]
        if keep_cfg:
            env.append(['PROTON_OPTISCALER_CONFIG', keep_cfg])
    if 'gamescope' in changes:
        segs = [s for s in segs if os.path.basename(s[0]) != 'gamescope']
        if changes['gamescope']:
            segs.append(gamescope_argv(changes['gamescope']))
    segs.sort(key=_rank)
    prof['env'] = env
    prof['wrappers'] = [t for s in segs for t in s]
    return prof


def parse_perf_args(items: list[str]) -> dict:
    """`fps=60 mode=game-performance mangohud=on wayland=off hidraw=off
    upscaler=fsr4 gamescope=2560x1440@144,fsr,fullscreen,render=1920x1080`
    -> changes for apply_perf. Raises LaunchParseError."""
    on = {'on': True, 'yes': True, 'true': True, '1': True,
          'off': False, 'no': False, 'false': False, '0': False}
    out = {}
    for item in items:
        key, sep, val = item.partition('=')
        key, val = key.strip().lower(), val.strip()
        if not sep:
            raise LaunchParseError(f"expected KEY=VALUE, got {item!r}")
        if key == 'fps':
            if not (val.isdigit() and int(val) <= 1000) and val != 'off':
                raise LaunchParseError("fps= takes a number (0 or off = no cap)")
            out['fps'] = 0 if val == 'off' else int(val)
        elif key == 'mode':
            if val not in (*PERF_MODES, 'off'):
                raise LaunchParseError(f"mode= is one of {', '.join(PERF_MODES)}, off")
            out['mode'] = val
        elif key in ('mangohud', 'wayland', 'hidraw'):
            if val.lower() not in on:
                raise LaunchParseError(f"{key}= is on or off")
            out['no_hidraw' if key == 'hidraw' else key] = \
                (not on[val.lower()]) if key == 'hidraw' else on[val.lower()]
        elif key == 'lsfg':
            if val not in ('off', '2', '3', '4'):
                raise LaunchParseError("lsfg= is off, 2, 3 or 4 (frame multiplier)")
            out['lsfg'] = None if val == 'off' else int(val)
        elif key == 'upscaler':
            if val not in UPSCALERS:
                raise LaunchParseError(f"upscaler= is one of {', '.join(UPSCALERS)}")
            out['upscaler'] = val
        elif key == 'gamescope':
            if val == 'off':
                out['gamescope'] = None
                continue
            parts = [x.strip() for x in val.split(',') if x.strip()]
            m = _GAMESCOPE_RE.match(parts[0]) if parts else None
            if not m:
                raise LaunchParseError("gamescope= starts with WIDTHxHEIGHT[@REFRESH]")
            g = {'w': int(m.group(1)), 'h': int(m.group(2)),
                 'r': int(m.group(3)) if m.group(3) else None}
            for opt in parts[1:]:
                if opt in ('fsr', 'fullscreen', 'hdr'):
                    g[opt] = True
                elif opt.startswith('render='):
                    rm = _GAMESCOPE_RE.match(opt[7:])
                    if not rm or rm.group(3):
                        raise LaunchParseError("render= is WIDTHxHEIGHT")
                    g['render_w'], g['render_h'] = int(rm.group(1)), int(rm.group(2))
                else:
                    raise LaunchParseError(f"unknown gamescope option {opt!r}")
            out['gamescope'] = g
        else:
            raise LaunchParseError(f"unknown setting {key!r} (fps, mode, mangohud, wayland, "
                                   f"hidraw, upscaler, gamescope, lsfg)")
    return out


# lsfg-vk (Lossless Scaling frame generation as a Vulkan layer): one
# [[profile]] per game in its conf.toml, matched on the game's exe name.

LSFG_CONF = Path.home() / '.config/lsfg-vk/conf.toml'
LSFG_LAYER = 'VkLayer_LSFGVK_frame_generation.json'
LSFG_PREFIX = 'Nexus: '


def lsfg_installed() -> bool:
    dirs = [Path.home() / '.local/share/vulkan/implicit_layer.d',
            Path('/usr/share/vulkan/implicit_layer.d'), Path('/etc/vulkan/implicit_layer.d')]
    return any((d / LSFG_LAYER).exists() for d in dirs) and LSFG_CONF.exists()


def _toml_chunks(text: str) -> list[str]:
    """The file split before every table header line ([x] / [[x]]); the
    first chunk holds whatever precedes the first header."""
    chunks, cur = [], []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('[') and cur:
            chunks.append(''.join(cur))
            cur = []
        cur.append(line)
    if cur:
        chunks.append(''.join(cur))
    return chunks


def _lsfg_name(entry: dict) -> str:
    return LSFG_PREFIX + (entry.get('display_name') or entry['slug'])


def lsfg_state(entry: dict) -> dict | None:
    """This game's lsfg-vk profile ({multiplier, flow_scale, performance_mode}),
    {} when it has none, None when lsfg-vk isn't set up."""
    if not lsfg_installed():
        return None
    import tomllib
    try:
        data = tomllib.loads(LSFG_CONF.read_text())
    except (OSError, ValueError):
        return None
    name = _lsfg_name(entry)
    hit = next((p for p in data.get('profile', []) if p.get('name') == name), None)
    return ({k: hit.get(k) for k in ('multiplier', 'flow_scale', 'performance_mode')}
            if hit else {})


def lsfg_set(entry: dict, multiplier: int | None, flow: float = 1.0,
             performance: bool = False) -> None:
    """Add/replace (multiplier 2–4) or remove (None) this game's profile.
    The rest of the file is kept byte for byte; an unparseable file, or a
    result lsfg-vk-cli rejects, is never left in place."""
    import tomllib
    exe = _entry_paths(entry)[0]
    if multiplier and not exe:
        raise ValueError("the game's exe isn't known (set it in Game settings)")
    text = LSFG_CONF.read_text()
    try:
        data = tomllib.loads(text)
    except ValueError as e:
        raise ValueError(f"{LSFG_CONF} can't be parsed ({e}) — not touching it") from None
    if data.get('version') not in (None, 2):
        raise ValueError(f"lsfg-vk config version {data.get('version')} isn't supported")
    name = _lsfg_name(entry)
    keep = []
    for chunk in _toml_chunks(text):
        if chunk.lstrip().startswith('[[profile]]'):
            try:
                if (tomllib.loads(chunk.replace('[[profile]]', '[p]', 1))
                        .get('p', {}).get('name')) == name:
                    continue
            except ValueError:
                pass
        keep.append(chunk)
    new = ''.join(keep).rstrip('\n') + '\n'
    if multiplier:
        base = next((p for p in data.get('profile', []) if not p.get('active_in')), {})
        q = json.dumps
        block = ["[[profile]]", f"name = {q(name)}", f"active_in = {q(exe.name)}",
                 f"multiplier = {int(multiplier)}", f"flow_scale = {float(flow)!r}",
                 f"performance_mode = {'true' if performance else 'false'}"]
        for k in ('pacing_mode', 'override_present_mode', 'preserve_swapchain_image_count'):
            if k in base:
                v = base[k]
                block.append(f"{k} = {'true' if v is True else 'false' if v is False else q(v)}")
        new = new.rstrip('\n') + '\n\n' + '\n'.join(block) + '\n'
    tomllib.loads(new)                              # never write something unparseable
    backup_file(LSFG_CONF)
    old = text
    atomic_write(LSFG_CONF, new)
    cli = shutil.which('lsfg-vk-cli')
    if cli:
        r = subprocess.run([cli, 'validate', '-c', str(LSFG_CONF)], capture_output=True,
                           text=True, timeout=20)
        if r.returncode != 0:
            atomic_write(LSFG_CONF, old)
            raise ValueError("lsfg-vk-cli rejected the change: "
                             + (r.stderr or r.stdout).strip()[:300])


def heroic_launch_fields(prof: dict) -> dict:
    """The GamesConfig keys a profile owns — the same four protongen's own
    "Apply to Heroic" writes (plus launcherArgs for game arguments). mangohud
    and gamemoderun map to Heroic's native toggles; everything else is a
    wrapperOptions entry."""
    mangohud = gamemode = False
    wrapper_opts = []
    for seg in split_wrappers(prof.get('wrappers', [])):
        name, rest = os.path.basename(seg[0]), seg[1:]
        if name == 'mangohud' and not rest:
            mangohud = True
        elif name == 'gamemoderun' and not rest:
            gamemode = True
        else:
            wrapper_opts.append({'exe': seg[0], 'args': shlex.join(rest)})
    return {
        'enviromentOptions': [{'key': k, 'value': v} for k, v in prof.get('env', [])],
        'wrapperOptions': wrapper_opts,
        'showMangohud': mangohud,
        'useGameMode': gamemode,
        'launcherArgs': shlex.join(prof.get('args', [])),
    }


def profile_from_protongen_config(cfg: dict) -> dict:
    """Convert a protongen Config (state.toml game_memory entry, or a share
    code's `config`) to a profile, emitting wrappers in protongen's own
    outer->inner order so the command matches what protongen shows."""
    prof = empty_profile()
    env: dict[str, str] = {}
    for pair in cfg.get('env') or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            k, v = str(pair[0]), str(pair[1])
            if _ENV_KEY_RE.match(k) and v != '' and k not in _RESERVED_ENV:
                env[k] = v
    try:
        extra = shlex.split(str(cfg.get('extra_env') or ''))
    except ValueError:
        extra = []
    for tok in extra:
        if _is_env_token(tok):
            k, _, v = tok.partition('=')
            if k not in _RESERVED_ENV:
                env.setdefault(k, v)
    prof['env'] = [[k, v] for k, v in env.items()]
    wr = [(str(p[0]), str(p[1])) for p in (cfg.get('wrappers') or [])
          if isinstance(p, (list, tuple)) and len(p) == 2]
    wr.sort(key=lambda p: WRAPPER_RANK.get(p[0], _DEFAULT_WRAPPER_RANK))
    for key, arg in wr:
        if key == 'gamescope':
            try:
                prof['wrappers'] += ['gamescope', *shlex.split(arg), '--']
            except ValueError:
                prof['wrappers'] += ['gamescope', '--']
        else:
            prof['wrappers'].append(key)
    try:
        prof['args'] = shlex.split(str(cfg.get('game_args') or ''))
    except ValueError:
        prof['args'] = []
    runtime = cfg.get('runtime')
    prof['proton'] = str(runtime) if runtime else None
    return prof


def decode_protongen_code(code: str) -> tuple[str, dict]:
    """Decode a `protongen:v1:<base64 JSON>` preset share code into
    (preset name, profile). Validates like protongen's own decoder."""
    import base64
    text = re.sub(r'\s+', '', code or '')
    if not text.startswith(PROTONGEN_CODE_PREFIX):
        raise LaunchParseError("not a protongen preset code")
    if len(text) > 32 * 1024:
        raise LaunchParseError("preset code is too large")
    try:
        raw = json.loads(base64.b64decode(text[len(PROTONGEN_CODE_PREFIX):],
                                          validate=True).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise LaunchParseError("preset code is damaged or incomplete") from None
    if not isinstance(raw, dict) or not isinstance(raw.get('config'), dict):
        raise LaunchParseError("preset code has no config in it")
    _check_shared_config(raw['config'])
    name = str(raw.get('name') or 'protongen preset')[:64]
    return name, profile_from_protongen_config(raw['config'])


def _check_shared_config(cfg: dict) -> None:
    """A share code comes from someone else: refuse env vars that can run code
    and wrappers that aren't known launch tools, instead of writing them into
    the game's launcher."""
    keys = [str(p[0]) for p in cfg.get('env') or []
            if isinstance(p, (list, tuple)) and len(p) == 2]
    try:
        keys += [t.partition('=')[0] for t in shlex.split(str(cfg.get('extra_env') or ''))
                 if _is_env_token(t)]
    except ValueError:
        pass
    bad = sorted({k for k in keys if _UNSAFE_ENV_RE.match(k)})
    if bad:
        raise LaunchParseError(f"preset code sets unsafe environment variables: {', '.join(bad)}")
    wrappers = sorted({str(p[0]) for p in cfg.get('wrappers') or []
                       if isinstance(p, (list, tuple)) and len(p) == 2} - set(WRAPPER_RANK))
    if wrappers:
        raise LaunchParseError(f"preset code runs unknown programs: {', '.join(wrappers)}")


def protongen_heroic_id(app_name: str) -> int:
    """protongen's synthetic id for a Heroic game (games.rs heroic_app_id):
    FNV-1a-32 of the app_name with the high bit set."""
    h = 0x811c9dc5
    for b in app_name.encode('utf-8'):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h | 0x80000000


def protongen_memory_for(entry: dict) -> tuple[str, dict] | None:
    """protongen's remembered tuning for a managed game, read-only from its
    state.toml: looked up by the Steam shortcut appid first, then by the Heroic
    entry's id. Returns (which, config) or None. Never writes protongen's files
    — it rewrites state.toml wholesale on save."""
    try:
        memory = _cached_read(PROTONGEN_STATE, _load_toml).get('game_memory', {})
    except (OSError, ValueError):
        return None
    ids = []
    if entry.get('steam_appid'):
        ids.append(('Steam', str(int(entry['steam_appid']) & 0xFFFFFFFF)))
    heroic_names = [entry['heroic_app_name']] if entry.get('heroic_app_name') else []
    exe = entry.get('exe') or (str(_entry_paths(entry)[0] or '') or None)
    cfg_dir = find_heroic_config()
    if exe and cfg_dir:
        # A Heroic entry registered before we tracked its app_name.
        lib = _cached_read(_heroic_lib_path(cfg_dir), _load_heroic_library) or {}
        heroic_names += [g['app_name'] for g in lib.get('games', [])
                         if isinstance(g, dict) and g.get('app_name')
                         and _heroic_exe(g) == exe]
    ids += [('Heroic', str(protongen_heroic_id(n))) for n in heroic_names]
    for which, key in ids:
        cfg = memory.get(key)
        if isinstance(cfg, dict):
            return which, cfg
    # Last resort: an entry built in protongen's umu mode for this exact exe.
    for cfg in memory.values():
        if isinstance(cfg, dict) and exe and cfg.get('umu_exe') == exe:
            return 'umu', cfg
    return None


def protongen_default_config() -> dict | None:
    """protongen's default profile (Settings → "Save as default profile",
    `global_profile` in state.toml) — what it offers to apply to any game it
    has no tuning for. Read-only; None when none is saved."""
    try:
        gp = _cached_read(PROTONGEN_STATE, _load_toml).get('global_profile')
    except (OSError, ValueError, ImportError):
        return None
    return gp if isinstance(gp, dict) else None


def protongen_default_profile(entry: dict | None = None) -> dict | None:
    """The default profile as a launch profile for `entry`: its Proton named
    by folder when installed, else the game's current pin is kept."""
    cfg = protongen_default_config()
    if cfg is None:
        return None
    prof = profile_from_protongen_config(cfg)
    hit = proton_by_name(prof.get('proton'))
    prof['proton'] = (hit.parent.name if hit
                      else ((entry or {}).get('launch') or {}).get('proton'))
    return prof


def protongen_bin() -> str | None:
    return shutil.which('protongen') or (
        str(p) if (p := Path.home() / '.local/bin/protongen').exists() else None)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import _entry_paths  # noqa: E402
from .heroic import (_heroic_exe, _heroic_lib_path, _load_heroic_library, find_heroic_config)  # noqa: E402
from .runner import proton_by_name  # noqa: E402
