"""The scrubber every string in a bug report goes through: names and paths
out, Nexus's own frames kept readable.

    scrub(text, ctx) -> str
    context() -> ctx          what to look for on this machine

ctx is a plain dict, so tests build their own:
    home     the home folder                         -> <home>
    user     the user name (whole word, 3+ chars)    -> <user>
    hosts    the hostname, "This PC's name"          -> <host>
    paths    the install base, the Steam root        -> <path>
    games    registry slugs, names, display names and exe stems
             (4+ chars, whole words)                 -> <game>
    secrets  credential values (config, env)         -> <secret>
Always: quoted strings -> '<s>'; emails, IP and MAC addresses, UUIDs,
Steam ids, JWTs, `token=...`-style values and long hex/base64 runs go;
any other home folder (/home/x, C:\\Users\\x) -> <home>; a URL's host
(Game-db, a torrent client) -> <host> unless it is GitHub's or loopback;
a game's folder under steamapps/common -> <game>.

A traceback frame (`File "...", line N, in f`, also faulthandler's form)
keeps its line and function; its file becomes nexus/<rel>.py for our own
code and <stdlib> / <lib> for everything else."""

import json
import os
import re

# A name or placeholder edge: not inside a word, and not one of ours (<game>).
_L, _R = r'(?<![\w<])', r'(?![\w>])'

_FRAME = re.compile(r'^(\s*File )"([^"\n]+)"(, line \d+,? in \S+.*)$')
_NEXUS_FILE = re.compile(r'(?:.*[/\\])?(nexus[/\\][\w/\\.-]+\.py)$')
_ANY_HOME = re.compile(r'(?i)(?:/home/|/Users/|/var/home/|\b[A-Z]:[/\\]+Users[/\\]+)'
                       r'[^/\\\s\'"<>]+')
_STEAM_GAME = re.compile(r'(?i)(steamapps[/\\]+(?:common|compatdata|shadercache|workshop'
                         r'[/\\]+content)[/\\]+)'
                         r'(?!Proton\b|SteamLinuxRuntime|Steamworks Shared)[^/\\\n"<>]+')
_USERDATA = re.compile(r'(?i)(userdata[/\\]+)\d+')
_SQUOTE = re.compile(r"(?<![\w'])'[^'\n]{1,300}'(?![\w])")
_DQUOTE = re.compile(r'"[^"\n]{1,300}"')
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_UUID = re.compile(r'(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')
_JWT = re.compile(r'\beyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]{4,}')
_KEYVAL = re.compile(r'(?i)\b(token|access_token|api[_-]?key|apikey|password|passwd|pass|'
                     r'secret|authorization|auth|session|sid|key)(\s*[=:]\s*)'
                     r'(?:bearer\s+)?[^\s"\',;&)<]+')
_BEARER = re.compile(r'(?i)\bbearer\s+[\w.~+/-]+=*')
_IPV4 = re.compile(r'(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}'
                   r'(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])')
_IPV6 = re.compile(r'(?i)(?<![\w:])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}'
                   r'|(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?'
                   r'|::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6}))(?![\w:])')
_MAC = re.compile(r'(?i)(?<![\w:])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![\w:])')
_STEAMID = re.compile(r'(?<!\d)7656119\d{10}(?!\d)')
_HEX = re.compile(r'(?i)(?<![\w])[0-9a-f]{32,}(?![\w])')
_B64 = re.compile(r'(?<![\w+-])(?=[\w+-]*\d)(?=[\w+-]*[a-z])(?=[\w+-]*[A-Z])'
                  r'[\w+-]{32,}={0,2}')
_URL_HOST = re.compile(r'(?i)\b(https?://)([^/\s:\'"<>]+)')
_KEEP_HOSTS = ('github.com', 'api.github.com', 'raw.githubusercontent.com', 'localhost',
               '127.0.0.1')


def _path_re(path: str, eat: bool = False) -> re.Pattern | None:
    """`path` in either slash style, any case, with doubled backslashes
    (repr) too; None for a path too short to be worth it."""
    parts = [p for p in re.split(r'[/\\]+', path.strip()) if p]
    if len(''.join(parts)) < 4:
        return None
    lead = r'[/\\]*' if path[:1] in '/\\' else ''
    # A game's own folder right below it goes too ("<path>/setup.log"),
    # but not Steam's folders, which say where something is.
    below = r'(?:[/\\]+(?!(?:steamapps|compatibilitytools\.d|userdata|config|logs|ubuntu12_\d+)' \
            r'(?:[/\\]|$))[^/\\\n"<>]+(?=[/\\]))?' if eat else ''
    return re.compile('(?i)' + lead + r'[/\\]+'.join(map(re.escape, parts)) + r'(?![\w-])'
                      + below)


def _word_re(words, min_len: int) -> re.Pattern | None:
    words = sorted({w.strip() for w in words if w and len(w.strip()) >= min_len},
                   key=len, reverse=True)
    if not words:
        return None
    return re.compile('(?i)' + _L + '(?:' + '|'.join(map(re.escape, words)) + ')' + _R)


def _frame(m: re.Match) -> str:
    path = m.group(2)
    rel = _NEXUS_FILE.match(path)
    if rel:
        where = rel.group(1).replace('\\', '/')
    elif re.search(r'(?i)site-packages|dist-packages|PyQt|sip', path):
        where = '<lib>'
    else:
        where = '<stdlib>'
    return f'{m.group(1)}"{where}"{m.group(3)}'


def _compiled(ctx: dict) -> dict:
    """The patterns for one ctx, built once per scrub call."""
    paths = [p for p in ctx.get('paths') or () if p]
    return {
        'secrets': sorted({s for s in ctx.get('secrets') or () if s and len(s) >= 4},
                          key=len, reverse=True),
        'paths': [r for r in (_path_re(p, eat=True)
                              for p in sorted(paths, key=len, reverse=True)) if r],
        'home': _path_re(ctx.get('home') or ''),
        'games': _word_re(ctx.get('games') or (), 4),
        'user': _word_re([ctx.get('user') or ''], 3),
        'hosts': _word_re(ctx.get('hosts') or (), 3),
    }


def _host(m: re.Match) -> str:
    """A URL's host goes, unless it is GitHub's or this machine's loopback."""
    host = m.group(2)
    keep = host.lower() in _KEEP_HOSTS or host.lower().endswith('.github.com')
    return m.group(0) if keep else f"{m.group(1)}<host>"


def _line(line: str, c: dict) -> str:
    for s in c['secrets']:
        line = line.replace(s, '<secret>')
    line = _URL_HOST.sub(_host, line)
    for r in c['paths']:
        line = r.sub('<path>', line)
    if c['home']:
        line = c['home'].sub('<home>', line)
    line = _ANY_HOME.sub('<home>', line)
    line = _STEAM_GAME.sub(r'\1<game>', line)
    line = _USERDATA.sub(r'\1<id>', line)
    line = _EMAIL.sub('<email>', line)
    line = _JWT.sub('<token>', line)
    line = _BEARER.sub('Bearer <secret>', line)
    line = _KEYVAL.sub(r'\1\2<secret>', line)
    line = _UUID.sub('<uuid>', line)
    line = _MAC.sub('<mac>', line)
    line = _IPV4.sub('<ip>', line)
    line = _IPV6.sub('<ip>', line)
    line = _STEAMID.sub('<id>', line)
    line = _HEX.sub('<token>', line)
    line = _B64.sub('<token>', line)
    line = _SQUOTE.sub("'<s>'", line)
    line = _DQUOTE.sub("'<s>'", line)
    for key, mark in (('hosts', '<host>'), ('games', '<game>'), ('user', '<user>')):
        if c[key]:
            line = c[key].sub(mark, line)
    return line


def scrub(text, ctx: dict) -> str:
    """`text` with everything personal replaced (see the module notes)."""
    if not text:
        return ''
    c = _compiled(ctx or {})
    out = []
    for line in str(text).splitlines():
        m = _FRAME.match(line)
        out.append(_FRAME.sub(_frame, line) if m else _line(line, c))
    return '\n'.join(out)


def scrub_lines(lines, ctx: dict) -> list[str]:
    """scrub() for a list of lines, one per line, keeping the count."""
    c = _compiled(ctx or {})
    out = []
    for line in lines:
        line = str(line).rstrip('\r\n')
        out.append(_FRAME.sub(_frame, line) if _FRAME.match(line) else _line(line, c))
    return out


# ---- what to look for on this machine ------------------------------------

def _registry_names(path) -> list[str]:
    try:
        with open(path, encoding='utf-8') as f:
            games = (json.load(f) or {}).get('games') or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for slug, g in games.items() if isinstance(games, dict) else ():
        out.append(str(slug))
        if not isinstance(g, dict):
            continue
        for k in ('name', 'display_name', 'title', 'heroic_app_name'):
            if g.get(k):
                out.append(str(g[k]))
        for k in ('exe', 'install_dir'):
            if g.get(k):
                stem = re.split(r'[/\\]', str(g[k]).rstrip('/\\'))[-1]
                out.append(stem.rsplit('.', 1)[0] if k == 'exe' else stem)
    return out


def context() -> dict:
    """ctx for this machine: HOME, the user, the hostname and [gamedb]
    device, the install base and Steam root, every registry name, and the
    credentials in config.toml and the environment. Never raises."""
    ctx = {'home': '', 'user': '', 'hosts': [], 'paths': [], 'games': [], 'secrets': []}
    try:
        from nexus import paths
        from nexus.cli.common import SECRET_ENV
        from nexus.cli.info import CONFIG_SECRETS
    except Exception:                 # noqa: BLE001 — a scrubber with less to go on
        return ctx
    ctx['home'] = str(paths.HOME)
    try:
        import getpass
        ctx['user'] = getpass.getuser()
    except Exception:                 # noqa: BLE001 — no user name in the environment
        ctx['user'] = os.path.basename(str(paths.HOME))
    try:
        import socket
        name = socket.gethostname()
        ctx['hosts'] += [name, name.split('.')[0]]
    except OSError:
        pass
    data = {}
    try:
        import tomllib
        with open(paths.CONFIG_PATH, 'rb') as f:
            raw = f.read().decode('utf-8-sig')
        data = tomllib.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    gd = data.get('gamedb') if isinstance(data.get('gamedb'), dict) else {}
    if gd.get('device'):
        ctx['hosts'].append(str(gd['device']))
    for sec, key, _what in CONFIG_SECRETS:
        v = (data.get(sec) or {}).get(key) if isinstance(data.get(sec), dict) else None
        if v:
            ctx['secrets'].append(str(v))
    for var in SECRET_ENV:
        if os.environ.get(var):
            ctx['secrets'].append(os.environ[var])
    inst = data.get('install') if isinstance(data.get('install'), dict) else {}
    try:
        from nexus import platform
        ctx['paths'] += [str(paths.STEAM_ROOT),
                         os.environ.get('NEXUS_INSTALL_BASE') or '',
                         str(inst.get('base') or ''),
                         str(platform.default_install_base(paths.HOME))]
    except Exception:                 # noqa: BLE001 — a drive that can't be asked
        pass
    for d in inst.get('scan_dirs') or ():
        ctx['paths'].append(str(d))
    ctx['paths'] = [p for p in ctx['paths'] if p]
    ctx['games'] = _registry_names(paths.REGISTRY_PATH)
    return ctx
