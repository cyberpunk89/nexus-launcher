"""Is a newer Nexus release out? One anonymous request a day to GitHub's API
for the public nexus-launcher repo's latest release:

    [updates]
    check = false               # never ask (default: true)
    github_token = "ghp_..."    # optional: sent if set (higher rate limit)

The answer is cached for a day (net.cached_json), so the window start-up
check is one request a day at most; Settings' "Check for updates" skips the
cache (force=True). Nothing is downloaded: the notice links to the release
page (Windows: straight to the installer) and people install the new build
themselves.

After an update, the window shows that version's "What's new" once: a few
short cards bundled in nexus/data/whats_new.json (the release writes them),
so it needs no request and works offline."""

import json
import re
import tomllib
from pathlib import Path

from . import __version__, net
from .features import has
from .icons import ICONS
from .paths import CACHE_DIR, CONFIG_PATH

REPO = 'cyberpunk89/nexus-launcher'
API = f'https://api.github.com/repos/{REPO}/releases/latest'
CACHE = CACHE_DIR / 'update-check.json'
MAX_AGE = 24 * 3600
INSTALLER = re.compile(r'-Setup-[0-9][^/]*\.exe$')    # what build-windows.yml uploads
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
    """The latest release as {'tag', 'url', 'installer'} (the token is never
    cached). 'installer' is the Windows setup asset's download link ('' if
    the release has none)."""
    headers = {'Authorization': f'Bearer {tok}'} if tok else {}
    data = json.loads(net.get(API, timeout=10, headers=headers))
    setup = next((str(a.get('browser_download_url')) for a in data.get('assets') or []
                  if isinstance(a, dict) and INSTALLER.search(str(a.get('name')))
                  and _https(a.get('browser_download_url'))), '')
    return {'tag': str(data['tag_name']), 'url': str(data['html_url']), 'installer': setup}


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
    the release page."""
    if not enabled():
        return {'state': 'off'}
    tok = token()
    fetched = []

    def fetch():
        fetched.append(_latest(tok))
        return fetched[0]
    rel = net.cached_json(CACHE, MAX_AGE, fetch, refresh=force)
    if (force and not fetched) or not isinstance(rel, dict):
        return {'state': 'failed'}
    new, cur = parse(rel.get('tag', '')), parse(current)
    url = rel.get('url')
    if not new or not cur or not _https(url):
        return {'state': 'failed'}
    if new <= cur:
        return {'state': 'current', 'version': '.'.join(map(str, cur))}
    if not has('proton') and _https(rel.get('installer')):      # the Windows edition
        url = rel['installer']
    return {'state': 'available', 'version': '.'.join(map(str, new)), 'url': url}


def available(current: str = __version__, force: bool = False) -> dict | None:
    """{'version', 'url'} when a newer release exists, else None. With
    [updates] check = false: None, with no request."""
    r = check(current, force)
    return {'version': r['version'], 'url': r['url']} if r['state'] == 'available' else None


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
