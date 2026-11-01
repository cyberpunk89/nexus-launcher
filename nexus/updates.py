"""Is a newer Nexus release out? One anonymous request a day to GitHub's API
for the public nexus-launcher repo's latest release:

    [updates]
    check = false               # never ask (default: true)
    github_token = "ghp_..."    # optional: sent if set (higher rate limit)

The answer is cached for a day (net.cached_json), so the window start-up
check is one request a day at most; Settings' "Check for updates" skips the
cache (force=True). The notice links to the release page (Windows: straight
to the installer), except on the installed Windows edition (in_app()):
there Update downloads the release's Setup exe to the cache (fetch(),
checked against the release's SHA256SUMS; `nexus-cli --update-fetch` does
it as a job) and "Restart to update" runs it silently (install_argv()),
which closes Nexus and starts it again (nexus.iss, /RELAUNCH). A portable
zip copy, a source checkout and Linux keep the link.

After an update, the window shows that version's "What's new" once: a few
short cards bundled in nexus/data/whats_new.json (the release writes them),
so it needs no request and works offline."""

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

from . import __version__, net
from .features import has
from .icons import ICONS
from .paths import CACHE_DIR, CONFIG_PATH

REPO = 'cyberpunk89/nexus-launcher'
API = f'https://api.github.com/repos/{REPO}/releases/latest'
CACHE = CACHE_DIR / 'update-check.json'
MAX_AGE = 24 * 3600
INSTALLER = re.compile(r'-Setup-[0-9][^/]*\.exe$')    # what build-windows.yml uploads
SUMS = 'SHA256SUMS'                             # what packaging/release.py attaches
DOWNLOADS = CACHE_DIR / 'update'                # the downloaded Setup, until the next one
INSTALL_LOG = CACHE_DIR / 'update-install.log'
SUMS_LIMIT, SETUP_LIMIT = 64 << 10, 1 << 30
# Inno Setup with no window or question; CLOSEAPPLICATIONS closes a running
# Nexus (and its agent), RELAUNCH is ours: nexus.iss starts Nexus again.
SILENT = ('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', '/RELAUNCH')
NOTES = Path(__file__).parent / 'data' / 'whats_new.json'
NOTES_MAX, NOTE_LEN, TITLE_LEN = 6, 140, 40
KINDS = ('new', 'improved', 'fixed')            # the What's new groups, in order


def _settings() -> dict:
    """The [updates] table of config.toml ({} when missing or unreadable)."""
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig')).get('updates') or {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled() -> bool:
    """[updates] check: on unless set to false."""
    return _settings().get('check', True) is not False


def token() -> str:
    """[updates] github_token from config.toml ('' = unset)."""
    t = _settings().get('github_token')
    return t.strip() if isinstance(t, str) else ''


def parse(version: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3); () when it isn't a dotted number."""
    out = []
    for part in str(version).strip().lstrip('vV').split('-')[0].split('.'):
        if not part.isdigit():
            return ()
        out.append(int(part))
    return tuple(out)


def _https(url) -> bool:
    return isinstance(url, str) and url.startswith('https://')


def _latest(tok: str) -> dict:
    """The latest release as {'tag', 'url', 'installer', 'sums'} (the token
    is never cached). 'installer' is the Windows setup asset's download link,
    'sums' its SHA256SUMS ('' when the release has none)."""
    headers = {'Authorization': f'Bearer {tok}'} if tok else {}
    data = json.loads(net.get(API, timeout=10, headers=headers))
    assets = [a for a in data.get('assets') or []
              if isinstance(a, dict) and _https(a.get('browser_download_url'))]
    setup = next((str(a['browser_download_url']) for a in assets
                  if INSTALLER.search(str(a.get('name')))), '')
    sums = next((str(a['browser_download_url']) for a in assets if a.get('name') == SUMS), '')
    return {'tag': str(data['tag_name']), 'url': str(data['html_url']), 'installer': setup,
            'sums': sums}


def _current_schema(rel) -> bool:
    """A cached answer from before 'sums' was kept is refetched (and still
    served when that fails)."""
    return isinstance(rel, dict) and 'sums' in rel


def last_checked() -> float | None:
    """When the answer was last fetched (epoch seconds), None if never."""
    try:
        return CACHE.stat().st_mtime
    except OSError:
        return None


def check(current: str = __version__, force: bool = False) -> dict:
    """{'state', 'version', 'url'}: state is 'off' ([updates] check = false,
    no request), 'failed' (no answer), 'current' or 'available'. force=True
    skips the daily cache, and a failed forced check isn't covered by an old
    answer. 'url' is the Windows installer when the release has one, else
    the release page. 'available' also carries 'page' (the release page),
    'installer' and 'sums' (what fetch() needs; '' when missing)."""
    if not enabled():
        return {'state': 'off'}
    tok = token()
    fetched = []

    def fetch():
        fetched.append(_latest(tok))
        return fetched[0]
    rel = net.cached_json(CACHE, MAX_AGE, fetch, refresh=force, valid=_current_schema)
    if (force and not fetched) or not isinstance(rel, dict):
        return {'state': 'failed'}
    new, cur = parse(rel.get('tag', '')), parse(current)
    url = rel.get('url')
    if not new or not cur or not _https(url):
        return {'state': 'failed'}
    if new <= cur:
        return {'state': 'current', 'version': '.'.join(map(str, cur))}
    page = url
    setup = rel.get('installer') if _https(rel.get('installer')) else ''
    if not has('proton') and setup:                             # the Windows edition
        url = setup
    return {'state': 'available', 'version': '.'.join(map(str, new)), 'url': url,
            'page': page, 'installer': setup,
            'sums': rel.get('sums') if _https(rel.get('sums')) else ''}


def available(current: str = __version__, force: bool = False) -> dict | None:
    """{'version', 'url', 'page', 'installer', 'sums'} when a newer release
    exists, else None. With [updates] check = false: None, with no request."""
    r = check(current, force)
    return {k: r[k] for k in ('version', 'url', 'page', 'installer', 'sums')} \
        if r['state'] == 'available' else None


# ---- updating in place (the installed Windows edition) ----

class UpdateError(Exception):
    """Why the update couldn't be downloaded or checked, said for people."""


def in_app(exe: str | None = None) -> bool:
    """Whether this copy can update itself: the Windows edition as its Setup
    installed it (Inno leaves unins000.exe beside the exe). A portable zip,
    a source checkout and Linux keep the release link."""
    if has('proton') or not (exe or getattr(sys, 'frozen', False)):
        return False
    try:
        return any(Path(exe or sys.executable).parent.glob('unins*.exe'))
    except OSError:
        return False


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def expected_sum(text: str, name: str) -> str:
    """`name`'s hash in a SHA256SUMS file ('<hex>  <name>' or '<hex> *<name>'
    per line), lower-case; '' when it isn't listed."""
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lstrip('*') == name \
                and re.fullmatch(r'[0-9a-fA-F]{64}', parts[0]):
            return parts[0].lower()
    return ''


def fetch(rel: dict, progress=None) -> tuple[Path, str]:
    """Download the release's Setup exe into DOWNLOADS and check it against
    the release's SHA256SUMS: (path, sha256). A copy already there with the
    right hash isn't downloaded again; a mismatch is deleted and refused.
    Older downloads go. Raises UpdateError."""
    url, sums_url = rel.get('installer'), rel.get('sums')
    if not _https(url) or not _https(sums_url):
        raise UpdateError("This release has no installer with checksums to update from.")
    name = unquote(url.rsplit('/', 1)[-1])
    if not INSTALLER.search(name) or '/' in name or '\\' in name:
        raise UpdateError(f"Unexpected installer name: {name}")
    try:
        want = expected_sum(net.get(sums_url, timeout=20, limit=SUMS_LIMIT)
                            .decode('utf-8', 'replace'), name)
    except net.NetError as e:
        raise UpdateError(f"Couldn't get the release's checksums ({e}).") from None
    if not want:
        raise UpdateError(f"The release's {SUMS} doesn't list {name}.")
    dest = DOWNLOADS / name
    for old in DOWNLOADS.glob('*') if DOWNLOADS.is_dir() else ():
        if old.name != name and old.is_file():
            old.unlink(missing_ok=True)
    try:
        if dest.is_file() and sha256(dest) == want:
            return dest, want
        net.download(url, dest, progress, timeout=60, limit=SETUP_LIMIT)
        got = sha256(dest)
    except net.NetError as e:
        raise UpdateError(f"The download failed ({e}).") from None
    except OSError as e:
        raise UpdateError(f"Couldn't save the download ({e}).") from None
    if got != want:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"The download doesn't match the release's {SUMS}, so it was "
                          f"deleted and won't be run. Try again later.")
    return dest, want


def prune() -> None:
    """Remove downloaded installers (nothing newer is out, or it's installed)."""
    for old in DOWNLOADS.glob('*') if DOWNLOADS.is_dir() else ():
        if old.is_file():
            old.unlink(missing_ok=True)


def install_argv(path) -> list[str]:
    """The silent update: the Setup with no window or question, and its log
    (INSTALL_LOG, kept when the download goes) to look at if it went wrong."""
    return [str(path), *SILENT, f'/LOG={INSTALL_LOG}']


def _clip(s, n: int) -> str:
    s = ' '.join(s.split()) if isinstance(s, str) else ''
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def whats_new(version: str = __version__) -> list[dict]:
    """The "What's new" cards bundled for `version` ([] when none), at most
    NOTES_MAX: {'text', 'title', 'icon', 'kind'}, plain and short. An entry
    is a plain line (0.4.1) or {"text", "title", "icon", "kind", "platform"}:
    icon a name in nexus.icons, kind new|improved|fixed, platform linux|
    windows (has('proton'), never sys.platform); anything unknown is dropped."""
    try:
        notes = json.loads(NOTES.read_text(encoding='utf-8')).get(version)
    except (OSError, ValueError, AttributeError):
        return []
    here = 'linux' if has('proton') else 'windows'
    out = []
    for n in notes if isinstance(notes, list) else []:
        n = {'text': n} if isinstance(n, str) else n if isinstance(n, dict) else {}
        if n.get('platform', here) != here:
            continue
        text = _clip(n.get('text'), NOTE_LEN)
        if text:
            out.append({'text': text, 'title': _clip(n.get('title'), TITLE_LEN),
                        'icon': n['icon'] if n.get('icon') in ICONS else None,
                        'kind': n['kind'] if n.get('kind') in KINDS else None})
    return out[:NOTES_MAX]


def release_url(version: str = __version__) -> str:
    return f'https://github.com/{REPO}/releases/tag/v{version}'


def show_whats_new(last_seen: str | None, current: str, used_before: bool) -> bool:
    """Whether this start is the first one of a newer version. last_seen is
    None for people who updated from a build before this existed
    (used_before: they have a library.json) and for fresh installs (no)."""
    if last_seen is None:
        return used_before
    seen, cur = parse(last_seen), parse(current)
    return bool(seen and cur and cur > seen)
