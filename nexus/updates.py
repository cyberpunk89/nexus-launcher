"""Is a newer Nexus release out? One anonymous request a day to GitHub's API
for the public nexus-launcher repo's latest release:

    [updates]
    check = false               # never ask (default: true)
    github_token = "ghp_..."    # optional: sent if set (higher rate limit)

The answer is cached for a day (net.cached_json), so the window start-up
check is one request a day at most. Nothing is downloaded: the notice links
to the release page and people install the new build themselves."""

import json
import tomllib

from . import __version__, net
from .paths import CACHE_DIR, CONFIG_PATH

REPO = 'cyberpunk89/nexus-launcher'
API = f'https://api.github.com/repos/{REPO}/releases/latest'
CACHE = CACHE_DIR / 'update-check.json'
MAX_AGE = 24 * 3600


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


def _latest(tok: str) -> dict:
    """The latest release as {'tag', 'url'} (the token is never cached)."""
    headers = {'Authorization': f'Bearer {tok}'} if tok else {}
    data = json.loads(net.get(API, timeout=10, headers=headers))
    return {'tag': str(data['tag_name']), 'url': str(data['html_url'])}


def available(current: str = __version__) -> dict | None:
    """{'version', 'url'} when a newer release exists, else None. With
    [updates] check = false: None, with no request."""
    if not enabled():
        return None
    tok = token()
    rel = net.cached_json(CACHE, MAX_AGE, lambda: _latest(tok))
    if not isinstance(rel, dict):
        return None
    new, cur = parse(rel.get('tag', '')), parse(current)
    url = rel.get('url')
    if not new or not cur or new <= cur or not isinstance(url, str) \
            or not url.startswith('https://'):
        return None
    return {'version': '.'.join(map(str, new)), 'url': url}
