"""Gaming news (experimental): Linux gaming feeds, Proton releases and Steam
news for the games you have installed, merged newest-first.

Text is sanitised: HTML is stripped and only http(s) links are kept. Each
story may carry one `image` (an https URL from the feed — an enclosure,
media:thumbnail, or the first <img> — which the GUI fetches lazily); nothing
else from the article is rendered. Feeds are cached for an hour; nothing is
fetched until the News page asks."""

import hashlib
import html
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import net
from .features import has
from .paths import NEWS_CONFIG

MAX_AGE = 3600
# The sources you can tick on the News page. `on` lists the platforms where a
# source starts switched on; the rest are one tick away in Manage feeds.
CATALOGUE = [
    {'id': 'gol', 'name': 'GamingOnLinux', 'url': 'https://www.gamingonlinux.com/article_rss.php',
     'on': ('linux',)},
    {'id': 'boilingsteam', 'name': 'Boiling Steam', 'url': 'https://boilingsteam.com/feed/',
     'on': ('linux',)},
    {'id': 'pcgamer', 'name': 'PC Gamer', 'url': 'https://www.pcgamer.com/rss/',
     'on': ('windows',)},
    {'id': 'rps', 'name': 'Rock Paper Shotgun', 'url': 'https://www.rockpapershotgun.com/feed',
     'on': ('windows',)},
    {'id': 'eurogamer', 'name': 'Eurogamer', 'url': 'https://www.eurogamer.net/feed', 'on': ()},
    {'id': 'ign', 'name': 'IGN', 'url': 'https://feeds.feedburner.com/ign/games-all', 'on': ()},
    {'id': 'gamespot', 'name': 'GameSpot', 'url': 'https://www.gamespot.com/feeds/mashup/',
     'on': ()},
    {'id': 'polygon', 'name': 'Polygon', 'url': 'https://www.polygon.com/rss/index.xml',
     'on': ()},
    {'id': 'vg247', 'name': 'VG247', 'url': 'https://www.vg247.com/feed', 'on': ()},
    {'id': 'kotaku', 'name': 'Kotaku', 'url': 'https://kotaku.com/rss', 'on': ()},
]
OPTIONS = ('steam', 'proton')       # "Steam news for my games", "Proton releases"
SUMMARY_MAX = 600
MRSS = '{http://search.yahoo.com/mrss/}'
CONTENT = '{http://purl.org/rss/1.0/modules/content/}encoded'
STEAM_CLAN_IMAGE = "https://clan.akamai.steamstatic.com/images/"
_IMG_RE = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)', re.I)
_STEAM_IMG_RE = re.compile(r'\{STEAM_CLAN_IMAGE\}(/[^\s\[\]"\'<>]+)'
                           r'|(https://[^\s\[\]"\'<>]+\.(?:png|jpe?g|webp|gif))', re.I)
STEAM_NEWS = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
              "?appid={appid}&count=3&maxlength=600&format=json&feeds=steam_community_announcements")


def _text(s: str | None, limit: int = SUMMARY_MAX) -> str:
    s = re.sub(r'\[/?[a-z0-9=*_ "\'/:.?&-]+\]', ' ', s or '', flags=re.I)   # Steam BBCode
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s if len(s) <= limit else s[:limit].rsplit(' ', 1)[0] + '…'


def _link(u: str | None) -> str | None:
    u = (u or '').strip()
    return u if re.match(r'^https?://', u) else None


def _img(u: str | None) -> str | None:
    """An image URL the GUI may fetch: https only (the art cache refuses
    anything else), no whitespace or quotes."""
    u = html.unescape((u or '').strip())
    return u if re.match(r'^https://[^\s"\'<>]+$', u) else None


def _first_img(*htmls: str | None) -> str | None:
    for h in htmls:
        m = _IMG_RE.search(html.unescape(h or '')) if h else None
        if m and _img(m.group(1)):
            return _img(m.group(1))
    return None


def _feed_image(el) -> str | None:
    """enclosure / media:content / media:thumbnail, else the first <img>."""
    for enc in el.findall('enclosure'):
        if (enc.get('type') or '').startswith('image/') and _img(enc.get('url')):
            return _img(enc.get('url'))
    for tag in ('content', 'thumbnail'):
        for m in el.iter(MRSS + tag):
            if (m.get('medium') in (None, 'image')
                    and not (m.get('type') or 'image/').startswith(('video', 'audio'))
                    and _img(m.get('url'))):
                return _img(m.get('url'))
    return None


def steam_image(contents: str | None) -> str | None:
    """The first image in a Steam announcement: {STEAM_CLAN_IMAGE}/… or a
    plain https image URL."""
    m = _STEAM_IMG_RE.search(contents or '')
    if not m:
        return None
    return _img(STEAM_CLAN_IMAGE + m.group(1).lstrip('/') if m.group(1) else m.group(2))


def _date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone().replace(tzinfo=None).isoformat(timespec='seconds')


def parse_feed(xml_text: str, source: str) -> list[dict]:
    """RSS 2.0 or Atom → items {source, title, link, date, summary, image}."""
    root = ET.fromstring(xml_text)
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    items = []
    if root.tag.endswith('feed'):                         # Atom
        for e in root.findall('a:entry', ns):
            link = next((l.get('href') for l in e.findall('a:link', ns)
                         if l.get('rel') in (None, 'alternate')), None)
            items.append({
                'source': source,
                'title': _text(e.findtext('a:title', '', ns), 200),
                'link': _link(link),
                'date': _date(e.findtext('a:published', None, ns) or
                              e.findtext('a:updated', None, ns)),
                'summary': _text(e.findtext('a:summary', '', ns) or
                                 e.findtext('a:content', '', ns)),
                'image': _feed_image(e) or _first_img(e.findtext('a:content', '', ns),
                                                      e.findtext('a:summary', '', ns)),
            })
    else:                                                 # RSS
        for it in root.iter('item'):
            items.append({
                'source': source,
                'title': _text(it.findtext('title'), 200),
                'link': _link(it.findtext('link')),
                'date': _date(it.findtext('pubDate')),
                'summary': _text(it.findtext('description')),
                'image': _feed_image(it) or _first_img(it.findtext('description'),
                                                       it.findtext(CONTENT)),
            })
    return [i for i in items if i['title']]


def _news_config() -> dict:
    try:
        data = tomllib.loads(NEWS_CONFIG.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def platform() -> str:
    return 'linux' if has('proton') else 'windows'     # what this build can do, not the OS name


def _listed(data: dict, key: str) -> list[str]:
    d = data.get(key, [])
    return [str(x) for x in d] if isinstance(d, list) else []


def _disabled(data: dict) -> list[str]:
    return _listed(data, 'disabled')


def user_feeds() -> list[dict]:
    """The user's own feeds from ~/.config/nexus/news.toml, disabled or not."""
    out = []
    for f in _news_config().get('feed', []):
        if isinstance(f, dict) and _link(f.get('url')):
            out.append({'id': f"user:{f['url']}", 'name': f.get('name') or f['url'],
                        'url': f['url'], 'user': True})
    return out


def _default_on(f: dict) -> bool:
    return True if f.get('user') else platform() in f['on']


def all_feeds() -> list[dict]:
    """Every source you can pick, each with `on`: its platform default,
    changed by the `enabled` / `disabled` lists in news.toml."""
    cfg = _news_config()
    enabled, disabled = set(_listed(cfg, 'enabled')), set(_disabled(cfg))
    out = []
    for f in [{**c, 'user': False} for c in CATALOGUE] + user_feeds():
        on = f['id'] in enabled or (_default_on(f) and f['id'] not in disabled)
        out.append({'id': f['id'], 'name': f['name'], 'url': f['url'],
                    'user': f['user'], 'on': on})
    return out


def feeds() -> list[dict]:
    """The sources that are switched on."""
    return [f for f in all_feeds() if f['on']]


def _option_default(name: str) -> bool:
    return name == 'steam' or platform() == 'linux'      # Proton releases mean little on Windows


def options() -> dict:
    """{'steam': bool, 'proton': bool}: the two non-feed sources, on by
    default (Proton releases: on Linux only) unless news.toml says otherwise."""
    cfg = _news_config()
    return {k: cfg[k] if isinstance(cfg.get(k), bool) else _option_default(k) for k in OPTIONS}


def _save(user: list[dict], disabled: list[str], enabled: list[str], opts: dict) -> None:
    def q(s):
        return json.dumps(str(s), ensure_ascii=False)
    lines = ["# Nexus news feeds (RSS or Atom). Managed from the News page.", ""]
    for key, ids in (('disabled', disabled), ('enabled', enabled)):
        if ids:
            lines += [f"{key} = [{', '.join(q(d) for d in ids)}]", ""]
    for k in OPTIONS:
        if opts[k] != _option_default(k):
            lines += [f"{k} = {'true' if opts[k] else 'false'}", ""]
    for f in user:
        lines += ["[[feed]]", f"name = {q(f['name'])}", f"url = {q(f['url'])}", ""]
    net.write_atomic(NEWS_CONFIG, "\n".join(lines))


def save_user_feeds(user: list[dict]) -> None:
    """Rewrite the [[feed]] list, keeping the on/off choices."""
    cfg = _news_config()
    _save(user, _disabled(cfg), _listed(cfg, 'enabled'), options())


def set_source(feed_id: str, on: bool) -> bool:
    """Switch one source on or off. Only a difference from its default is
    stored. False if there is no such source."""
    f = next((f for f in [{**c, 'user': False} for c in CATALOGUE] + user_feeds()
              if f['id'] == feed_id), None)
    if f is None:
        return False
    cfg = _news_config()
    disabled = [d for d in _disabled(cfg) if d != feed_id]
    enabled = [e for e in _listed(cfg, 'enabled') if e != feed_id]
    if on != _default_on(f):
        (enabled if on else disabled).append(feed_id)
    _save(user_feeds(), disabled, enabled, options())
    return True


def set_option(name: str, on: bool) -> bool:
    if name not in OPTIONS:
        return False
    cfg = _news_config()
    _save(user_feeds(), _disabled(cfg), _listed(cfg, 'enabled'), {**options(), name: on})
    return True


def _fetch_feed(f: dict, refresh: bool):
    # Readable prefix, plus a hash so two long URLs from one site can't share a file.
    name = ('news-' + re.sub(r'[^a-z0-9]+', '-', f['id'].lower())[:40] + '-'
            + hashlib.sha1(f['url'].encode()).hexdigest()[:10] + '.xml')
    text, _at, stale = net.cached(f['url'], name, MAX_AGE, refresh, as_json=False)
    return parse_feed(text, f['name']), stale


def _fetch_steam(game: dict, refresh: bool):
    data, _at, stale = net.cached(STEAM_NEWS.format(appid=game['appid']),
                                  f"steamnews-{game['appid']}.json", MAX_AGE, refresh)
    items = []
    for n in (data.get('appnews') or {}).get('newsitems', []):
        ts = n.get('date')
        items.append({
            'source': game['name'], 'kind': 'steam', 'appid': game['appid'],
            'title': _text(n.get('title'), 200),
            'link': _link(n.get('url')),
            'date': datetime.fromtimestamp(ts).isoformat(timespec='seconds') if ts else None,
            'summary': _text(n.get('contents')),
            'image': steam_image(n.get('contents')),
        })
    return items, stale


def collect(steam_games: list[dict], proton_releases: list[dict], refresh: bool = False) -> dict:
    """All sources in parallel; a failing source is reported, not fatal."""
    jobs = [('feed', f) for f in feeds()] + [('steam', g) for g in steam_games]
    items: list[dict] = []
    sources: list[dict] = []

    def run(job):
        kind, x = job
        try:
            got, stale = (_fetch_feed(x, refresh) if kind == 'feed' else _fetch_steam(x, refresh))
            return kind, x, got, stale, None
        except (net.NetError, ET.ParseError, ValueError, TypeError, AttributeError,
                OverflowError, OSError) as e:      # a source that sends junk is one error
            return kind, x, [], False, str(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for kind, x, got, stale, err in pool.map(run, jobs):
            for i in got:
                i.setdefault('kind', 'feed')
            items += got
            sources.append({'name': x['name'], 'kind': kind, 'count': len(got),
                            'error': err, 'stale': stale, 'url': x.get('url'),
                            'user': bool(x.get('user'))})
    for r in proton_releases:
        items.append({'source': r.get('family_label', 'GE-Proton'), 'kind': 'proton',
                      'title': f"{r['name']} released", 'link': _link(r.get('html_url')),
                      'date': _date(r.get('published_at')), 'summary': _text(r.get('body')),
                      'image': None})
    if proton_releases:
        sources.append({'name': 'Proton releases', 'kind': 'proton', 'count': len(proton_releases),
                        'error': None, 'stale': False})
    seen, uniq = set(), []
    for i in sorted(items, key=lambda i: i.get('date') or '', reverse=True):
        key = i.get('link') or i['title']
        if key not in seen:
            seen.add(key)
            uniq.append(i)
    return {'items': uniq[:300], 'sources': sources}
