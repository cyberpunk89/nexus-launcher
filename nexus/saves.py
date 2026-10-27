"""Where a Windows game keeps its saves, from the Ludusavi manifest
(github.com/mtkennerly/ludusavi-manifest, built from PCGamingWiki).

On Linux a game's saves live in its Wine prefix (cli/savedata.py walks the
prefix's user folders). A native Windows install has no prefix: its saves are
spread over the user's real AppData and Documents, so we need to know which
folders are this game's. The manifest says so for ~20k games.

It is one YAML file of ~18 MB (October 2026; 53k games, most with no save
paths). Rather than ship PyYAML in the Windows build and parse all of that
each time, a small reader for the manifest's own regular layout turns it
once into a compact JSON index: save paths for the games that have any, plus
lookups by Steam appid and by normalised title."""

import glob
import json
import os
import re
from pathlib import Path

from . import net
from .paths import CACHE_DIR

MANIFEST_URL = ('https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/'
                'master/data/manifest.yaml')
MANIFEST_DIR = CACHE_DIR / 'ludusavi'
INDEX_PATH = MANIFEST_DIR / 'index.json'
ETAG_PATH = MANIFEST_DIR / 'etag'
MAX_AGE = 7 * 86400                  # the manifest is rebuilt daily; weekly is plenty
INDEX_VERSION = 1


# ---------------------------------------------------------------- manifest

def _scalar(s: str) -> str:
    """A YAML key or value as the manifest writes it: "double-quoted" (JSON
    escapes), 'single-quoted' ('' is a quote) or plain."""
    s = s.strip()
    if s.startswith('"'):
        try:
            return json.loads(s)
        except ValueError:
            return s.strip('"')
    if s.startswith("'") and s.endswith("'") and len(s) > 1:
        return s[1:-1].replace("''", "'")
    return s


def _split_key(line: str) -> tuple[str, str]:
    """'  "a: b": rest' → ('a: b', 'rest'): the key may itself hold ': '."""
    s = line.strip()
    if s.startswith('"'):
        i = 1
        while i < len(s):
            if s[i] == '\\':
                i += 2
                continue
            if s[i] == '"':
                break
            i += 1
        return _scalar(s[:i + 1]), s[i + 1:].lstrip(':').strip()
    if s.startswith("'"):
        m = re.match(r"'((?:[^']|'')*)'", s)
        if m:
            return _scalar(m.group(0)), s[m.end():].lstrip(':').strip()
    k, _, rest = s.partition(':')
    return k.strip(), rest.strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))


def build_index(lines) -> dict:
    """The index from the manifest's lines. Only what saves need is kept:
    each game's `files` (path → tags, oses), its Steam appids and aliases.
    Paths for other OSes are dropped. The layout is fixed (two-space indents, one key per line), so this reads
    it line by line instead of being a YAML parser."""
    games: dict[str, list] = {}
    steam: dict[str, str] = {}
    aliases: dict[str, str] = {}
    title = section = path = sub = None
    cur: dict | None = None            # the file entry being read
    ids: list[str] = []

    def finish():
        if title is None:
            return
        files = [[p, sorted(e['os'])] for p, e in entries.items()
                 if (not e['tags'] or 'save' in e['tags'])
                 and (not e['os'] or 'windows' in e['os'])]
        if files:
            games[title] = files
        for i in ids:
            steam.setdefault(i, title)

    entries: dict[str, dict] = {}
    for raw in lines:
        line = raw.rstrip('\r\n')
        if not line.strip() or line.startswith(('---', '#')):
            continue
        ind = _indent(line)
        if ind == 0:
            finish()
            title, _ = _split_key(line)
            section = path = sub = cur = None
            entries, ids = {}, []
            continue
        if title is None:
            continue
        if ind == 2:
            section, val = _split_key(line)
            sub = None
            if section == 'alias' and val:
                aliases[title] = _scalar(val)
            continue
        if section == 'files':
            if ind == 4:
                path, _ = _split_key(line)
                cur = entries.setdefault(path, {'tags': set(), 'os': set()})
                sub = None
            elif ind == 6 and cur is not None:
                sub, _ = _split_key(line)
            elif cur is not None and line.strip().startswith('- '):
                item = line.strip()[2:]
                if sub == 'tags':
                    cur['tags'].add(_scalar(item))
                elif sub == 'when' and item.startswith('os:'):
                    cur['os'].add(_scalar(item[3:]))
            elif cur is not None and sub == 'when' and line.strip().startswith('os:'):
                cur['os'].add(_scalar(line.strip()[3:]))
        elif section == 'steam' and ind == 4:
            k, v = _split_key(line)
            if k == 'id' and v:
                ids.insert(0, _scalar(v))
        elif section == 'id' and ind == 4:
            sub, _ = _split_key(line)
        elif section == 'id' and sub == 'steamExtra' and line.strip().startswith('- '):
            ids.append(_scalar(line.strip()[2:]))
    finish()
    titles: dict[str, str] = {}
    for t in games:
        titles.setdefault(norm_title(t), t)
    for alias, target in aliases.items():
        if target in games:
            titles.setdefault(norm_title(alias), target)
    return {'version': INDEX_VERSION, 'games': games,
            'steam': {k: v for k, v in steam.items() if v in games}, 'titles': titles}


def norm_title(title: str) -> str:
    """Lower-case letters and digits only, so "DOOM Eternal", "Doom: Eternal"
    and a repack's "Doom Eternal – Deluxe Edition"-less name meet."""
    t = re.sub(r'[™®©]', '', title).lower().replace('&', 'and')
    return re.sub(r'[^a-z0-9]+', '', t)


def _read_index() -> dict | None:
    try:
        idx = json.loads(INDEX_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return idx if isinstance(idx, dict) and idx.get('version') == INDEX_VERSION else None


def load_index(refresh: bool = False) -> dict | None:
    """The save index, downloading the manifest when ours is a week old (or
    `refresh`). The ETag makes an unchanged manifest a 304, not 18 MB. A
    failed fetch serves the last index we have (or None), and isn't retried
    for net.FAIL_TTL, as net.cached_json does."""
    idx = _read_index()
    try:
        if idx is not None and not refresh and net._age(INDEX_PATH) < MAX_AGE:
            return idx
    except OSError:
        pass
    failed = INDEX_PATH.with_name('index.failed')
    try:
        if not refresh and net._age(failed) < net.FAIL_TTL:
            return idx
    except OSError:
        pass
    headers = {}
    if idx is not None:
        try:
            headers['If-None-Match'] = ETAG_PATH.read_text(encoding='utf-8').strip()
        except OSError:
            pass
    try:
        raw, etag = _fetch(headers)
    except net.NetError as e:
        if e.status == 304 and idx is not None:
            try:
                os.utime(INDEX_PATH)        # still current: fresh for another week
            except OSError:
                pass
            return idx
        try:
            net.write_atomic(failed, str(e)[:200])
        except OSError:
            pass
        return idx
    idx = build_index(raw.decode('utf-8', 'replace').splitlines())
    if net.save_json(INDEX_PATH, idx, separators=(',', ':')):
        failed.unlink(missing_ok=True)
        try:
            if etag:
                net.write_atomic(ETAG_PATH, etag)
            else:
                ETAG_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    return idx


def _fetch(headers: dict) -> tuple[bytes, str]:
    """The manifest and its ETag. A 304 is a NetError with code 304."""
    import urllib.error
    if headers:
        try:
            with net._open(net._request(MANIFEST_URL, headers), 60) as r:
                return r.read(64 << 20), r.headers.get('ETag', '')
        except urllib.error.HTTPError as e:
            raise net.NetError(f"{MANIFEST_URL}: {e.reason}", e.code) from None
        except net.errors() as e:
            raise net.NetError(f"{MANIFEST_URL}: {getattr(e, 'reason', e)}") from None
    return net.get(MANIFEST_URL, timeout=60, limit=64 << 20), ''


# ---------------------------------------------------------------- matching

def find_game(idx: dict, steam_appid=None, title: str = '') -> str | None:
    """The manifest title for a game: by Steam appid (the registry's
    `steam_appid`, or the repack page's store link), else by title."""
    if not idx:
        return None
    if steam_appid:
        hit = idx.get('steam', {}).get(str(steam_appid))
        if hit:
            return hit
    if title:
        return idx.get('titles', {}).get(norm_title(title))
    return None


def windows_folders(home: Path | None = None) -> dict[str, str]:
    """The manifest's placeholders for this user's folders. From the
    environment where it has them, else the default layout under `home`
    (so a test can point it anywhere)."""
    env = os.environ if home is None else {}
    h = Path(env.get('USERPROFILE') or home or Path.home())
    public = Path(env.get('PUBLIC') or h.parent / 'Public')
    return {
        '<home>': str(h),
        '<winAppData>': env.get('APPDATA') or str(h / 'AppData' / 'Roaming'),
        '<winLocalAppData>': env.get('LOCALAPPDATA') or str(h / 'AppData' / 'Local'),
        '<winDocuments>': str(h / 'Documents'),
        '<winPublic>': str(public),
        '<winProgramData>': env.get('PROGRAMDATA') or str(h.parent.parent / 'ProgramData'),
    }


_PLACEHOLDER = re.compile(r'<[A-Za-z]+>')


def expand(path: str, base: Path, folders: dict[str, str]) -> str | None:
    """A manifest path with its placeholders filled in, as a glob pattern;
    None when it uses one we can't fill (<root>, <xdgData>, ...)."""
    subs = {k: glob.escape(v) for k, v in dict(folders, **{'<base>': str(base)}).items()}
    subs['<storeUserId>'] = '*'

    def sub(m):
        if m.group(0) not in subs:
            raise KeyError(m.group(0))
        return subs[m.group(0)]
    try:
        return _PLACEHOLDER.sub(sub, path)
    except KeyError:
        return None


def _fixed_root(pattern: str) -> Path:
    """The part of a glob pattern before its first wildcard."""
    parts = Path(pattern).parts
    for i, p in enumerate(parts):
        if glob.has_magic(p):
            return Path(*parts[:i])
    return Path(pattern)


def resolve(entry: list, base: Path, folders: dict[str, str] | None = None,
            noise=None) -> list[tuple[Path, Path]]:
    """Existing save files for a game's manifest `entry` (its index list of
    [path, oses]), installed in `base`. Each is (file, root): `root` is the
    folder the pattern names, so noise is judged below it (a log or cache in
    the game's own save folder, as `noise(rel)` decides)."""
    folders = windows_folders() if folders is None else folders
    seen: set[Path] = set()
    out: list[tuple[Path, Path]] = []
    for path, oses in entry:
        if oses and 'windows' not in oses:
            continue
        pattern = expand(path, base, folders)
        if pattern is None:
            continue
        root = _fixed_root(pattern)
        for hit in glob.glob(pattern, recursive=True):
            p = Path(hit)
            files = [p] if p.is_file() else (sorted(f for f in p.rglob('*') if f.is_file())
                                             if p.is_dir() else [])
            top = root if root != p or p.is_dir() else p.parent    # a pattern naming one file
            for f in files:
                if f in seen or f.is_symlink():
                    continue
                if noise is not None and noise(f.relative_to(top)):
                    continue
                seen.add(f)
                out.append((f, top))
    return out
