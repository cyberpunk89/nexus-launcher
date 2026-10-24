"""Which of your wishlist games already have a FitGirl repack?

repackfeed follows the RSS feed, which only reaches back a few weeks. This
keeps the whole catalogue instead: FitGirl's site is WordPress, and its
public REST API (wp-json/wp/v2/posts, category "Lossless Repack") lists
every repack post. The first sync pages through all of them (about 75
requests, slowly); later syncs ask only for posts modified since the last
one. The catalogue is rebuilt from scratch monthly so deleted posts drop out.

Stored in DATA_DIR/repack-index.json: slug, title and dates — titles
only, like repackfeed — plus 'hv' on the posts in the "Hypervisor Bypass"
category (nexus/hypervisor.py), re-read on every sync. A repack's magnet link is read from its page
when you ask for it (fetch_page) and handed straight to the torrent client;
it is never written down. Every link is pinned to fitgirl-repacks.site
(clone domains serve malware)."""

import difflib
import html
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import hypervisor, net
from .paths import DATA_DIR
from .repackfeed import SITE, base_title, page_slug

API = SITE + "wp-json/wp/v2/"
CATEGORY = 5                     # "Lossless Repack"
PER_PAGE = 100
STORE = DATA_DIR / 'repack-index.json'
MAX_AGE = 6 * 3600               # how often an incremental sync runs
REBUILD_DAYS = 30                # a full rebuild after this long
PAUSE = 0.4                      # seconds between pages of a full build
FUZZY = 0.9
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
_EDITION = re.compile(
    r"\b(?:edition|collection|complete|definitive|deluxe|goty|game of the year|"
    r"remaster(?:ed)?|director'?s cut|ultimate|gold|premium|enhanced|anniversary|"
    r"bundle|dlcs?|bonus(?:es)?|expansion|season pass)\b", re.I)
_MAGNET_RE = re.compile(r'href="(magnet:\?[^"]+)"')
_SIZE_RE = {
    'repack_size': re.compile(r'Repack Size[^<]*<strong>([^<]+)</strong>', re.I),
    'original_size': re.compile(r'Original Size[^<]*<strong>([^<]+)</strong>', re.I),
}


_MODIFIERS = {'digital', 'premium', 'deluxe', 'ultimate', 'complete', 'definitive', 'special',
              'gold', 'standard', 'collectors', "collector's", 'anniversary'}


def _key(name: str) -> str:
    n = re.sub(r'[™®©]', '', html.unescape(name or '').lower()).replace('’', "'")
    n = n.replace('&', ' and ')
    return re.sub(r'[\W_]+', ' ', n.replace("'", '')).strip()


def _near(a: str, b: str) -> bool:
    """A spelling slip, not another game: the same words but one, which
    is long and nearly the same ("Core Keeper" is not "Corpse Keeper")."""
    wa, wb = a.split(), b.split()
    diff = [(x, y) for x, y in zip(wa, wb) if x != y]
    return len(diff) == 1 and min(map(len, diff[0])) >= 6 and \
        difflib.SequenceMatcher(None, *diff[0]).ratio() >= 0.8


def keys(title: str) -> list[str]:
    """Match keys for a repack title, best first: the title without its
    version tail, then without a trailing edition ("Savant: Ascent REMIX –
    Bundle" -> "Savant: Ascent REMIX"). A third, looser key — the part
    before the colon when the rest is only an edition ("Lost Eidolons:
    Digital Deluxe Edition" -> "Lost Eidolons") — is prefixed with '~'."""
    base = re.split(r'\s\(', base_title(title), maxsplit=1)[0].strip()
    base = re.sub(r'\s+(?:v\d|build\s?\d)\S*$', '', base, flags=re.I) or base   # "Golf v1.02"
    out = [_key(base)]
    head, sep, tail = base.rpartition(' – ')
    if not sep:
        head, sep, tail = base.rpartition(' - ')
    if sep and head and _EDITION.search(tail):
        out.append(_key(head))
        base = head
    head, sep, tail = base.partition(':')
    if sep and head.strip() and _EDITION.search(tail):
        # "Death Howl: Deluxe Edition" is the game; "ELDEN RING: Shadow of
        # the Erdtree Deluxe Edition" is probably the game with its DLC.
        words = tail.split()
        plain = len(words) <= 2 or (len(words) == 3 and words[0].lower() in _MODIFIERS)
        out.append(('' if plain else '~') + _key(head))
    seen, uniq = set(), []
    for k in out:
        if k.strip('~') and k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('posts'), dict):
            data.setdefault('rejected', {})
            return data
    except (OSError, ValueError):
        pass
    return {'posts': {}, 'built_at': None, 'synced_at': None, 'max_modified': None,
            'rejected': {}}


def save(data: dict) -> None:
    net.write_atomic(STORE, json.dumps(data, separators=(',', ':')))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _age(iso: str | None) -> float:
    try:
        return time.time() - datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return math.inf


def parse_posts(rows) -> list[dict]:
    """Index entries from one page of the posts API; anything not on the
    real site is dropped."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        slug = page_slug(r.get('link'))
        title = html.unescape(((r.get('title') or {}).get('rendered') or '')).strip()
        if not slug or not title or slug != r.get('slug', slug):
            continue
        date = str(r.get('date_gmt') or r.get('date') or '')
        out.append({'id': r.get('id'), 'slug': slug, 'title': title,
                    'date': date + '+00:00' if date and '+' not in date else date,
                    'modified': str(r.get('modified') or '')})
    return out


def _page(page: int, since: str | None = None) -> list | None:
    """One page of repack posts, or None past the last page."""
    q = {'categories': CATEGORY, 'per_page': PER_PAGE, 'page': page,
         '_fields': 'id,slug,link,title,date_gmt,modified'}
    if since:
        q.update(modified_after=since, orderby='modified', order='asc')
    else:
        q.update(orderby='id', order='asc')
    try:
        return json.loads(net.get(API + 'posts?' + urlencode(q), timeout=30, limit=4 << 20))
    except net.NetError as e:
        if e.status == 400:               # rest_post_invalid_page_number
            return None
        raise
    except ValueError:
        raise net.NetError("FitGirl's site sent something that isn't JSON") from None


def _total_pages() -> int | None:
    try:
        n = json.loads(net.get(f"{API}categories/{CATEGORY}?_fields=count", timeout=20,
                               limit=1 << 12)).get('count')
        return max(1, math.ceil(int(n) / PER_PAGE))
    except (net.NetError, ValueError, TypeError, AttributeError):
        return None


def sync(force: bool = False, full: bool = False, progress=None) -> dict:
    """Bring the catalogue up to date and return it. A full build when the
    store is empty, a month old, or `full`; otherwise only what changed
    since the last sync (skipped when that was under MAX_AGE ago, unless
    `force`). progress(page, pages) is called during a full build. Raises
    NetError when the site can't be reached (the store is kept)."""
    data = load()
    rebuild = full or not data['posts'] or _age(data.get('built_at')) > REBUILD_DAYS * 86400
    if not rebuild and not force and _age(data.get('synced_at')) < MAX_AGE:
        return data
    since = None if rebuild else data.get('max_modified')
    fresh, page = {}, 1
    pages = _total_pages() if rebuild else None
    try:
        while True:
            if progress and rebuild:
                progress(page, pages)
            rows = _page(page, since)
            if not rows:
                break
            for p in parse_posts(rows):
                fresh[p['slug']] = p
            if len(rows) < PER_PAGE:
                break
            page += 1
            if rebuild:
                time.sleep(PAUSE)
    except net.NetError:
        if fresh:                         # keep what arrived; a rebuild is retried next time
            data['posts'].update(fresh)
            _bump(data, fresh)
            save(data)
        raise
    if rebuild:
        data['posts'] = fresh
        data['built_at'] = _now()
    else:
        data['posts'].update(fresh)
    _bump(data, fresh)
    _mark_hv(data)
    data['synced_at'] = _now()
    save(data)
    return data


def _hv_posts() -> dict[str, str] | None:
    """{slug: 'hv'|'both'} for every post in the Hypervisor Bypass category
    (a hundred or so: one or two requests), None when it can't be read."""
    out, page = {}, 1
    while True:
        q = {'categories': hypervisor.CATEGORY, 'per_page': PER_PAGE, 'page': page,
             '_fields': 'slug,link,title'}
        try:
            rows = json.loads(net.get(API + 'posts?' + urlencode(q), timeout=30, limit=4 << 20))
        except net.NetError as e:
            if e.status == 400 and page > 1:      # past the last page
                break
            return None
        except ValueError:
            return None
        for p in parse_posts(rows):
            out[p['slug']] = hypervisor.kind(p['title'])
        if not isinstance(rows, list) or len(rows) < PER_PAGE:
            break
        page += 1
    return out


def _mark_hv(data: dict) -> None:
    """Set or clear 'hv' on the stored posts. Offline, the old marks stay."""
    hv = _hv_posts()
    if hv is None:
        return
    for slug, post in data['posts'].items():
        if slug in hv:
            post['hv'] = hv[slug]
        else:
            post.pop('hv', None)


def _bump(data: dict, posts: dict) -> None:
    mods = [p['modified'] for p in posts.values() if p.get('modified')]
    if mods:
        data['max_modified'] = max([data.get('max_modified') or '', *mods])


# ── matching ──────────────────────────────────────────────────────────────

def _digits(key: str) -> list[str]:
    return [t for t in key.split() if t.isdigit() or re.fullmatch(r'[ivx]+', t)]


class Matcher:
    """Look titles up in the catalogue: exact key, then the looser
    before-the-colon key, then a close spelling with the same words count
    and the same numbers ("Dead Space" never becomes "Dead Space 2")."""

    def __init__(self, posts: dict):
        self.exact, self.loose, self.by_len = {}, {}, {}
        for p in posts.values():
            for i, k in enumerate(keys(p.get('title') or '')):     # ~30 ms for them all
                table = self.loose if k.startswith('~') else self.exact
                table.setdefault(k.lstrip('~'), []).append(p)
                if i == 0:
                    self.by_len.setdefault(len(k.split()), []).append((k, p))

    @staticmethod
    def _newest(posts: list[dict]) -> dict:
        return max(posts, key=lambda p: (p.get('modified') or '', p.get('date') or ''))

    def find(self, title: str):
        """(post, 'exact'|'close') or None."""
        # Only the title itself: a wishlisted DLC ("Satisfactory - Core
        # Values Expansion") is not its base game.
        want = keys(title)[:1]
        for k in want:
            if k in self.exact:
                return self._newest(self.exact[k]), 'exact'
        for k in want:
            if k in self.loose:
                return self._newest(self.loose[k]), 'close'
        if not want:
            return None
        key = want[0]
        best, best_r = None, FUZZY
        for k, p in self.by_len.get(len(key.split()), []):
            sm = difflib.SequenceMatcher(None, key, k)
            if sm.real_quick_ratio() < best_r or sm.quick_ratio() < best_r:
                continue
            r = sm.ratio()
            if r >= best_r and _digits(k) == _digits(key) and _near(key, k):
                best, best_r = p, r
        return (best, 'close') if best else None


def entry(post: dict, confidence: str) -> dict:
    return {'slug': post['slug'], 'title': post['title'], 'url': SITE + post['slug'] + '/',
            'date': post.get('date'), 'modified': post.get('modified'),
            'confidence': confidence, 'hv': post.get('hv')}


def wishlist_matches(wishlist, data: dict | None = None) -> dict[str, dict]:
    """{appid (str): match} for the wishlist items that have a repack,
    from the stored catalogue only (no network). Rejected pairs are
    skipped."""
    data = data or load()
    if not data['posts']:
        return {}
    m, rejected, out = Matcher(data['posts']), data.get('rejected') or {}, {}
    for item in wishlist or []:
        if not isinstance(item, dict) or not item.get('title') or item.get('appid') is None:
            continue
        hit = m.find(item['title'])
        appid = str(item['appid'])
        if hit and rejected.get(appid) != hit[0]['slug']:
            out[appid] = entry(*hit)
    return out


def reject(appid: str, slug: str | None) -> None:
    """Hide a wrong match (slug), or forget the rejection (None)."""
    data = load()
    if slug:
        data['rejected'][str(appid)] = slug
    else:
        data['rejected'].pop(str(appid), None)
    save(data)


def new_matches(matches: dict, data: dict) -> tuple[list[str], bool]:
    """The appids not seen matched before, and whether this is the first
    time anything was recorded (then they're a summary, not news). Marks
    them all as seen in `data` (the caller saves it)."""
    first = 'alerted' not in data
    seen = set(data.get('alerted') or [])
    fresh = [a for a in matches if a not in seen]
    data['alerted'] = sorted(seen | set(matches))
    return fresh, first


# ── one repack page, on demand ────────────────────────────────────────────

def fetch_page(slug: str) -> dict:
    """{url, title, magnets [{hash, uri}], repack_size, original_size, hv}
    for one repack, read from the site now ('hv': None, 'hv' or 'both').
    Nothing here is stored."""
    from .torrent import infohash
    if not _SLUG_RE.match(slug or ''):
        raise net.NetError(f"not a repack page: {slug!r}")
    q = urlencode({'slug': slug, '_fields': 'link,title,content,categories'})
    try:
        rows = json.loads(net.get(API + 'posts?' + q, timeout=30, limit=8 << 20))
    except ValueError:
        raise net.NetError("FitGirl's site sent something that isn't JSON") from None
    post = next((r for r in rows if isinstance(r, dict) and page_slug(r.get('link')) == slug),
                None) if isinstance(rows, list) else None
    if post is None:
        raise net.NetError(f"FitGirl has no repack page called {slug!r}")
    content = (post.get('content') or {}).get('rendered') or ''
    magnets, hashes = [], set()
    for raw in _MAGNET_RE.findall(content):
        uri = html.unescape(raw)
        h = infohash(uri)
        if h and h not in hashes:
            hashes.add(h)
            magnets.append({'hash': h, 'uri': uri})
    out = {'url': SITE + slug + '/', 'slug': slug, 'magnets': magnets,
           'title': html.unescape((post.get('title') or {}).get('rendered') or '').strip()}
    for k, rx in _SIZE_RE.items():
        m = rx.search(content)
        out[k] = html.unescape(m.group(1)).strip() if m else None
    cats = post.get('categories')
    out['hv'] = (hypervisor.kind(out['title'])
                 if isinstance(cats, list) and hypervisor.CATEGORY in cats else None)
    return out
