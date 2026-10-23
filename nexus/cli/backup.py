"""Your Nexus settings in one file, for a new PC or a reinstall
(`--export-settings FILE`, `--import-settings FILE`): config.toml, your
news feeds, library.json (favourites, collections, notes, the dashboard,
the look), the game registry, play history and your artwork picks
(`--with-art` adds the images). Never saves (those have their own
backups), caches or logs. Secrets (API keys, tokens, the torrent password)
are left out unless `--with-secrets`; importing keeps the ones already on
this machine.

Import is careful: every file it replaces gets a .bak first, TOML and
JSON are checked before anything is written, only known file names are
read from the archive (no paths of its own), and a game from the registry
is added only if its folder exists here — the rest are listed, to
reinstall or move into place. `--dry-run` says all that without writing."""

import io
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path

from nexus import paths as NX

from .common import atomic_write, backup_file

FORMAT = 1
MAX_MEMBER = 64 << 20                 # bytes one archived file may have
SIMPLE = {                             # archive name -> where it lives
    'config/config.toml': NX.CONFIG_PATH,
    'config/news.toml': NX.NEWS_CONFIG,
    'config/library.json': NX.CONFIG_DIR / 'library.json',
}
REGISTRY = 'data/games.json'
SESSIONS = 'data/sessions.jsonl'
ART_RE = re.compile(r'^art/([A-Za-z0-9][\w-]{0,80})/([\w.-]{1,120})$')
ART_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.ico')
SECRETS = (('steamgriddb', 'api_key'), ('gamedb', 'token'), ('torrent', 'password'),
           ('notify', 'ntfy_token'))


class BackupError(Exception):
    pass


# ── config.toml secrets, by line (no TOML writer in the stdlib) ──────────

_SECTION = re.compile(r'^\s*\[([^\[\]]+)\]\s*(#.*)?$')


def _key_re(key: str):
    return re.compile(rf'^\s*{re.escape(key)}\s*=')


def strip_secrets(text: str) -> tuple[str, list[str]]:
    out, removed, section = [], [], None
    for line in text.splitlines(keepends=True):
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip()
        elif any(section == s and _key_re(k).match(line) for s, k in SECRETS):
            removed.append(f"{section}.{line.split('=', 1)[0].strip()}")
            continue
        out.append(line)
    return ''.join(out), removed


def _secret_lines(text: str) -> dict[tuple, str]:
    found, section = {}, None
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip()
            continue
        for s, k in SECRETS:
            if section == s and _key_re(k).match(line):
                found[(s, k)] = line.strip()
    return found


def keep_secrets(new: str, old: str) -> tuple[str, list[str]]:
    """`new` config text with the secrets `old` has and `new` lacks put back
    under their sections (appended as a section when it's missing)."""
    have, want = _secret_lines(new), _secret_lines(old)
    lines = new.splitlines()
    kept = []
    for (s, k), line in want.items():
        if (s, k) in have:
            continue
        idx = next((i for i, ln in enumerate(lines)
                    if (m := _SECTION.match(ln)) and m.group(1).strip() == s), None)
        if idx is None:
            lines += ['', f'[{s}]', line]
        else:
            lines.insert(idx + 1, line)
        kept.append(f"{s}.{k}")
    return '\n'.join(lines) + '\n', kept


# ── export ───────────────────────────────────────────────────────────────

def _add(tf: tarfile.TarFile, name: str, data: bytes):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tf.addfile(info, io.BytesIO(data))


def export_settings(dest: Path, with_art: bool = False, with_secrets: bool = False) -> dict:
    files, removed = [], []
    dest = Path(dest).expanduser()
    if dest.suffix not in ('.gz', '.tgz'):
        dest = dest.with_name(dest.name + '.tar.gz')
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        with tarfile.open(tmp, 'w:gz') as tf:
            for name, src in SIMPLE.items():
                try:
                    data = src.read_bytes()
                except OSError:
                    continue
                if name == 'config/config.toml' and not with_secrets:
                    text, removed = strip_secrets(data.decode('utf-8', 'replace'))
                    data = text.encode()
                _add(tf, name, data)
                files.append(name)
            for name, src in ((REGISTRY, NX.REGISTRY_PATH), (SESSIONS, NX.DATA_DIR / 'sessions.jsonl')):
                try:
                    _add(tf, name, src.read_bytes())
                    files.append(name)
                except OSError:
                    pass
            art = NX.ARTWORK_DIR
            for d in sorted(art.iterdir()) if art.is_dir() else ():
                picks = d / 'picks.json'
                if not picks.is_file() or not ART_RE.match(f"art/{d.name}/picks.json"):
                    continue
                _add(tf, f"art/{d.name}/picks.json", picks.read_bytes())
                files.append(f"art/{d.name}/picks.json")
                if with_art:
                    for f in sorted(d.iterdir()):
                        if f.is_file() and f.suffix.lower() in ART_EXT \
                                and f.stat().st_size <= MAX_MEMBER and ART_RE.match(f"art/{d.name}/{f.name}"):
                            _add(tf, f"art/{d.name}/{f.name}", f.read_bytes())
                            files.append(f"art/{d.name}/{f.name}")
            manifest = {'format': FORMAT, 'app': 'nexus', 'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
                        'files': files, 'secrets': with_secrets, 'art_images': with_art}
            _add(tf, 'manifest.json', json.dumps(manifest, indent=1).encode())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {'path': str(dest), 'files': len(files), 'secrets_left_out': removed,
            'size': dest.stat().st_size}


# ── import ───────────────────────────────────────────────────────────────

def _read_archive(src: Path) -> dict[str, bytes]:
    """The archive's known files ({name: bytes}); anything else is ignored."""
    out = {}
    try:
        tf = tarfile.open(Path(src).expanduser(), 'r:*')
    except (OSError, tarfile.TarError) as e:
        raise BackupError(f"not a Nexus settings file: {e}") from None
    with tf:
        for m in tf.getmembers():
            name = m.name
            if not m.isfile() or m.size > MAX_MEMBER or name.startswith('/') or '..' in name.split('/'):
                continue
            if name in SIMPLE or name in (REGISTRY, SESSIONS, 'manifest.json') or ART_RE.match(name):
                if ART_RE.match(name) and not (name.endswith('/picks.json') or
                                               name.lower().endswith(ART_EXT)):
                    continue
                f = tf.extractfile(m)
                out[name] = f.read(MAX_MEMBER + 1) if f else b''
    try:
        man = json.loads(out.get('manifest.json', b'{}'))
    except ValueError:
        man = {}
    if man.get('app') != 'nexus':
        raise BackupError("not a Nexus settings file (no manifest)")
    if int(man.get('format') or 0) > FORMAT:
        raise BackupError("made by a newer Nexus — update this one first")
    return out


def _game_dir(e: dict) -> Path | None:
    for k in ('compatdata', 'wine_prefix'):
        if e.get(k):
            return Path(e[k])
    return None


def import_settings(src: Path, dry_run: bool = False) -> dict:
    import tomllib
    files = _read_archive(src)
    plan = {'replace': [], 'keep_secrets': [], 'games_added': [], 'games_missing': [],
            'games_here': [], 'sessions_added': 0, 'art': 0, 'dry_run': dry_run}
    writes: list[tuple[Path, bytes, int | None]] = []
    for name, dest in SIMPLE.items():
        if name not in files:
            continue
        data = files[name]
        mode = None
        if name.endswith('.toml'):
            text = data.decode('utf-8', 'replace')
            if name == 'config/config.toml':
                try:
                    old = dest.read_text()
                except OSError:
                    old = ''
                text, kept = keep_secrets(text, old)
                plan['keep_secrets'] = kept
                mode = 0o600
            try:
                tomllib.loads(text)
            except ValueError as e:
                raise BackupError(f"{name} in the file isn't valid TOML: {e}") from None
            data = text.encode()
        else:
            try:
                json.loads(data)
            except ValueError as e:
                raise BackupError(f"{name} in the file isn't valid JSON: {e}") from None
        writes.append((dest, data, mode))
        plan['replace'].append(str(dest))
    if REGISTRY in files:
        try:
            theirs = json.loads(files[REGISTRY]).get('games') or {}
        except (ValueError, AttributeError) as e:
            raise BackupError(f"the game list in the file is damaged: {e}") from None
        mine = load_registry()['games']
        for slug, e in sorted(theirs.items()):
            if not isinstance(e, dict) or not valid_slug(slug):
                continue
            d = _game_dir(e)
            if slug in mine:
                plan['games_here'].append(slug)
            elif d is not None and d.is_dir():
                plan['games_added'].append(slug)
            else:
                plan['games_missing'].append({'slug': slug, 'name': e.get('display_name') or slug,
                                              'was': str(d) if d else None})
    if SESSIONS in files:
        try:
            have = set((NX.DATA_DIR / 'sessions.jsonl').read_text().splitlines())
        except OSError:
            have = set()
        new = [ln for ln in files[SESSIONS].decode('utf-8', 'replace').splitlines()
               if ln.strip() and ln not in have]
        plan['sessions_added'] = len(new)
    art = [n for n in files if ART_RE.match(n)]
    plan['art'] = len(art)
    if dry_run:
        return plan

    for dest, data, mode in writes:
        if dest.exists():
            backup_file(dest)
        if mode:                                    # config.toml: private from the first byte
            from nexus import net
            net.write_atomic(dest, data, fsync=True, mode=mode)
        else:
            atomic_write(dest, data)
    if plan['games_added']:
        with registry_lock():
            reg = load_registry()
            theirs = json.loads(files[REGISTRY])['games']
            for slug in plan['games_added']:
                reg['games'].setdefault(slug, theirs[slug])
            if NX.REGISTRY_PATH.exists():
                backup_file(NX.REGISTRY_PATH)
            save_registry(reg)
    if plan['sessions_added']:
        p = NX.DATA_DIR / 'sessions.jsonl'
        p.parent.mkdir(parents=True, exist_ok=True)
        have = set(p.read_text().splitlines()) if p.exists() else set()
        with open(p, 'a') as f:
            for ln in files[SESSIONS].decode('utf-8', 'replace').splitlines():
                if ln.strip() and ln not in have:
                    f.write(ln + '\n')
    for name in art:
        slug, fname = ART_RE.match(name).groups()
        dest = NX.ARTWORK_DIR / slug / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        if fname == 'picks.json':
            if dest.exists():
                backup_file(dest)
            atomic_write(dest, files[name].decode('utf-8', 'replace'))
        elif not dest.exists():
            dest.write_bytes(files[name])
    return plan


def cmd_export_settings(dest: str, with_art: bool, with_secrets: bool, as_json: bool) -> int:
    try:
        r = export_settings(Path(dest), with_art, with_secrets)
    except OSError as e:
        print(json.dumps({'ok': False, 'error': str(e)}) if as_json else f"  [!] {e}")
        return 1
    if as_json:
        print(json.dumps({'ok': True, **r}))
    else:
        print(f"Saved {r['files']} files to {r['path']} ({r['size'] / 1e6:.1f} MB).")
        if r['secrets_left_out']:
            print(f"  Left out: {', '.join(r['secrets_left_out'])} (add --with-secrets to keep them).")
    return 0


def cmd_import_settings(src: str, dry_run: bool, as_json: bool) -> int:
    try:
        plan = import_settings(Path(src), dry_run)
    except (BackupError, OSError) as e:
        if as_json:
            print(json.dumps({'ok': False, 'error': str(e)}))
        print(f"  [!] {e}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({'ok': True, **plan}))
        return 0
    verb = "Would replace" if dry_run else "Replaced"
    for p in plan['replace']:
        print(f"  {verb} {p}" + ("" if dry_run else " (the old one is a .bak)"))
    if plan['keep_secrets']:
        print(f"  Kept this machine's {', '.join(plan['keep_secrets'])}.")
    print(f"  Games: {len(plan['games_added'])} added, {len(plan['games_here'])} already here, "
          f"{len(plan['games_missing'])} not on this machine.")
    for g in plan['games_missing']:
        print(f"    {g['name']}: was at {g['was']} — reinstall it, or put it back and run --reindex")
    print(f"  Play sessions: {plan['sessions_added']} added. Artwork files: {plan['art']}.")
    return 0


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .common import valid_slug  # noqa: E402
from .registry import load_registry, registry_lock, save_registry  # noqa: E402
