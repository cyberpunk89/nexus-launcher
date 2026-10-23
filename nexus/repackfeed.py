"""Has FitGirl posted something newer than the repack you installed?

The site's RSS feed (pinned to fitgirl-repacks.site — clone domains serve
malware) carries two kinds of post: a new repack, and the "Updates Digest",
which lists games whose repack page got an update, each with a link to that
page. Both are remembered in DATA_DIR/repack-feed.json under the repack
page's slug, so a game is noticed even after its post has scrolled out of
the feed. Nothing is downloaded and no download link is ever kept: the
result is a date, a title and the repack page.

A game counts as updated when a post for it is newer than the repack it
was installed from (the newest file in that repack folder, else the install
date)."""

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import net
from .paths import DATA_DIR

SITE = "https://fitgirl-repacks.site/"
FEED = SITE + "feed/"
STORE = DATA_DIR / 'repack-feed.json'
CONTENT = '{http://purl.org/rss/1.0/modules/content/}encoded'
MAX_AGE = 6 * 3600           # how often the feed is fetched
KEEP_DAYS = 120              # posts older than this are forgotten
GRACE = 12 * 3600            # a post this close to the install isn't "newer"
_PAGE_RE = re.compile(r'^https://fitgirl-repacks\.site/([a-z0-9][a-z0-9-]*)/?$')
_SPOILER_RE = re.compile(
    r'su-spoiler-title[^>]*>(?:<span[^>]*></span>)?\s*([^<]+?)\s*</div>\s*'
    r'<div class="su-spoiler-content[^"]*">\s*<a href="([^"]+)"', re.S)


def title_key(name: str) -> str:
    return re.sub(r'[\W_]+', ' ', html.unescape(name).lower().replace('’', "'")
                  .replace("'", '')).strip()


def base_title(post_title: str) -> str:
    """A repack post title without its version tail: "Frosthaven – v1.0.3"
    -> "Frosthaven"."""
    t = html.unescape(post_title)
    t = re.split(r'\s[–—-]\s(?=v?\d|Build|Update|Patch|\w+ Edition)', t, maxsplit=1)[0]
    t = re.split(r',\s*v\d|\s\+\s', t, maxsplit=1)[0]
    return t.strip()


def page_slug(url: str | None) -> str | None:
    m = _PAGE_RE.match((url or '').strip())
    return m.group(1) if m else None


def parse_feed(xml_text: str) -> list[dict]:
    """[{slug, title, kind ('repack'|'update'), date (ISO), url}] from one
    feed page. Digest posts yield one entry per listed game."""
    out = []
    root = ET.fromstring(xml_text)
    for item in root.iter('item'):
        title = (item.findtext('title') or '').strip()
        try:
            when = parsedate_to_datetime(item.findtext('pubDate') or '')
            date = when.astimezone(timezone.utc).isoformat(timespec='seconds')
        except (TypeError, ValueError):
            continue
        cats = {(c.text or '').strip() for c in item.findall('category')}
        if 'Updates Digest' in cats or title.startswith('Updates Digest'):
            for name, link in _SPOILER_RE.findall(item.findtext(CONTENT) or ''):
                slug = page_slug(html.unescape(link))
                if slug:
                    out.append({'slug': slug, 'title': html.unescape(name).strip(),
                                'kind': 'update', 'date': date, 'url': SITE + slug + '/'})
        elif 'Lossless Repack' in cats:
            slug = page_slug(item.findtext('link'))
            if slug:
                out.append({'slug': slug, 'title': html.unescape(title), 'kind': 'repack',
                            'date': date, 'url': SITE + slug + '/'})
    return out


def load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('posts'), dict):
            return data
    except (OSError, ValueError):
        pass
    return {'posts': {}, 'fetched_at': None}


def _save(data: dict) -> None:
    net.write_atomic(STORE, json.dumps(data, indent=1))


def refresh(force: bool = False) -> dict:
    """Fetch the feed (two pages; four the first time) and merge it into
    the store. Returns the store. Offline, the store is returned as it was."""
    data = load()
    pages = 2 if data['posts'] else 4
    got = False
    for page in range(1, pages + 1):
        url = FEED if page == 1 else f"{FEED}?paged={page}"
        try:
            text, _at, stale = net.cached(url, f"fitgirl-feed-{page}.xml", MAX_AGE, force,
                                          as_json=False, timeout=20)
            items = parse_feed(text)
        except (net.NetError, ET.ParseError):
            break
        got = got or not stale
        for it in items:
            old = data['posts'].get(it['slug'])
            if old is None or it['date'] > old['date']:
                data['posts'][it['slug']] = it
    cutoff = datetime.fromtimestamp(time.time() - KEEP_DAYS * 86400, timezone.utc).isoformat()
    data['posts'] = {k: v for k, v in data['posts'].items() if v['date'] >= cutoff}
    if got:
        data['fetched_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    try:
        _save(data)
    except OSError:
        pass
    return data


def installed_at(entry: dict) -> datetime | None:
    """When the installed repack was downloaded: the newest file directly in
    its repack folder (setup.exe, the .bin parts), else the install date."""
    src = entry.get('repack_source')
    newest = None
    if src:
        try:
            for p in Path(src).iterdir():
                if p.is_file():
                    m = p.stat().st_mtime
                    newest = m if newest is None or m > newest else newest
        except OSError:
            pass
    if newest is not None:
        return datetime.fromtimestamp(newest, timezone.utc)
    for key in ('repack_installed_at', 'added_at'):
        try:
            return datetime.fromisoformat(entry[key]).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
    return None


def match(entry: dict, posts: dict) -> dict | None:
    """The post for this registry entry: its slug is the repack page's slug
    (Nexus slugs are made the same way), else the titles agree."""
    hit = posts.get(entry.get('slug') or '')
    if hit:
        return hit
    key = title_key(entry.get('display_name') or '')
    if not key:
        return None
    for p in posts.values():
        if title_key(base_title(p['title'])) == key:
            return p
    return None


def updates(entries, data: dict | None = None) -> dict[str, dict]:
    """{slug: post} for the installed repacks that have a newer post."""
    posts = (data or load())['posts']
    out = {}
    for e in entries:
        if not e.get('repack_source') and not e.get('repack_installed_at'):
            continue                      # not installed from a repack
        p = match(e, posts)
        base = installed_at(e) if p else None
        if base is None:
            continue
        try:
            posted = datetime.fromisoformat(p['date'])
        except ValueError:
            continue
        if (posted - base).total_seconds() > GRACE:
            out[e['slug']] = p
    return out
