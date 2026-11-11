"""Hand a repack's magnet link or .torrent to your torrent client, follow
the download, and install it when it finishes.

Talks to the client's own local API — Transmission's RPC or qBittorrent's
Web UI — on this machine only (loopback; never another host). Configured
in config.toml:

    [torrent]
    client = "transmission"          # or "qbittorrent"
    url = "http://127.0.0.1:9091"    # qBittorrent's default is :8080
    username = ""                    # if the client asks for one
    password = ""
    download_dir = "/home/you/Games/Install"   # default: the first [install] scan_dirs
    seed_ratio = 1.0                 # optional: stop seeding at this ratio

What Nexus added is remembered in DATA_DIR/torrents.json (hash, name,
whether to install when done), so it can pick the finished folder. The
Downloads page also shows and controls everything else in the client
(all_status / control): pause, resume, remove."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import time
import tomllib
import urllib.parse
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import net, platform
from .paths import CONFIG_PATH, DATA_DIR

TRACKED = DATA_DIR / 'torrents.json'
_LOOPBACK = {'127.0.0.1', '::1', 'localhost'}
HASH_RE = re.compile(r'^[0-9a-f]{40}$')
MAGNET_RE = re.compile(r'^magnet:\?(?:[^#\s]*&)?xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})',
                       re.I)


class TorrentError(Exception):
    pass


def settings() -> dict:
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        data = {}
    t = data.get('torrent') or {}
    client = str(t.get('client') or '').lower()
    dirs = (data.get('install') or {}).get('scan_dirs') or []
    if isinstance(dirs, str):
        dirs = [dirs]
    default_url = {'transmission': 'http://127.0.0.1:9091',
                   'qbittorrent': 'http://127.0.0.1:8080'}.get(client, '')
    try:
        ratio = float(t['seed_ratio']) if 'seed_ratio' in t else None
    except (TypeError, ValueError):
        ratio = None
    return {'client': client if client in ('transmission', 'qbittorrent') else None,
            'url': str(t.get('url') or default_url).rstrip('/'),
            'username': str(t.get('username') or ''), 'password': str(t.get('password') or ''),
            'download_dir': str(t.get('download_dir') or (dirs[0] if dirs else '')) or None,
            'seed_ratio': ratio}


def infohash(magnet: str) -> str | None:
    m = MAGNET_RE.match(magnet.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 32:                                       # base32 form
        h = base64.b32decode(h.upper()).hex()
    return h.lower()


def _bencode_end(b: bytes, i: int) -> int:
    """Index just past the bencoded value starting at b[i]."""
    c = b[i:i + 1]
    if c == b'i':
        return b.index(b'e', i) + 1
    if c in (b'l', b'd'):
        i += 1
        while b[i:i + 1] != b'e':
            if not b[i:i + 1]:
                raise ValueError("truncated")
            i = _bencode_end(b, i)
        return i + 1
    if c.isdigit():
        colon = b.index(b':', i)
        return colon + 1 + int(b[i:colon])
    raise ValueError(f"bad bencode at {i}")


def file_infohash(path: str) -> str:
    """The v1 info-hash of a .torrent file: the SHA-1 of its bencoded `info`
    dict, exactly as the client computes it (qBittorrent's add API doesn't
    say which torrent a file upload became)."""
    try:
        b = Path(path).read_bytes()
        if b[:1] != b'd':
            raise ValueError("not a dictionary")
        i = 1
        while b[i:i + 1] != b'e':
            k_end = _bencode_end(b, i)
            v_end = _bencode_end(b, k_end)
            if b[i:k_end].endswith(b':info'):
                return hashlib.sha1(b[k_end:v_end]).hexdigest()
            i = v_end
    except (OSError, ValueError, IndexError) as e:
        raise TorrentError(f"can't read that .torrent file ({e})") from None
    raise TorrentError("that .torrent file has no info section")


def _json(raw: bytes, who: str):
    try:
        return json.loads(raw)
    except ValueError:
        raise TorrentError(f"{who} sent back something that isn't JSON — is that "
                           f"URL really its API?") from None


def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: float = 10):
    """A request to the local client only: http(s) on a loopback host."""
    import urllib.error
    import urllib.request
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or (parts.hostname or '') not in _LOOPBACK:
        raise TorrentError(f"the torrent client must be on this machine (got {url})")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method='POST' if data is not None else 'GET')
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(4 << 20)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers or {}), e.read(1 << 16) if e.fp else b''
    except net.errors() as e:
        raise TorrentError(f"can't reach the torrent client at {url} "
                           f"({getattr(e, 'reason', e)}) — is it running with remote "
                           f"access on?") from None


class Transmission:
    def __init__(self, s: dict):
        self.url = s['url'] + '/transmission/rpc'
        self.auth = ({'Authorization': 'Basic ' + base64.b64encode(
            f"{s['username']}:{s['password']}".encode()).decode()} if s['username'] else {})
        self.sid = ''

    def call(self, method: str, args: dict) -> dict:
        body = json.dumps({'method': method, 'arguments': args}).encode()
        for _ in range(2):
            status, headers, raw = _http(self.url, body, {
                **self.auth, 'Content-Type': 'application/json',
                'X-Transmission-Session-Id': self.sid})
            if status == 409:                              # first call: learn the session id
                self.sid = next((v for k, v in headers.items()
                                 if k.lower() == 'x-transmission-session-id'), '')
                continue
            if status == 401:
                raise TorrentError("Transmission wants a username and password "
                                   "([torrent] username / password)")
            if status != 200:
                raise TorrentError(f"Transmission answered HTTP {status}")
            data = _json(raw, "Transmission")
            if not isinstance(data, dict) or data.get('result') != 'success':
                raise TorrentError(f"Transmission: {data.get('result') if isinstance(data, dict) else data}")
            return data.get('arguments') or {}
        raise TorrentError("Transmission kept refusing the session id")

    def add(self, source: str, download_dir: str | None, seed_ratio: float | None) -> dict:
        args = {'paused': False}
        if source.startswith('magnet:'):
            args['filename'] = source
        else:
            args['metainfo'] = base64.b64encode(Path(source).read_bytes()).decode()
        if download_dir:
            args['download-dir'] = download_dir
        got = self.call('torrent-add', args)
        t = got.get('torrent-added') or got.get('torrent-duplicate') or {}
        if seed_ratio is not None and t.get('id') is not None:
            self.call('torrent-set', {'ids': [t['id']], 'seedRatioMode': 1,
                                      'seedRatioLimit': seed_ratio})
        return {'hash': (t.get('hashString') or '').lower(), 'name': t.get('name') or ''}

    FIELDS = ['hashString', 'name', 'percentDone', 'eta', 'rateDownload', 'rateUpload',
              'status', 'downloadDir', 'error', 'errorString', 'sizeWhenDone',
              'uploadRatio', 'addedDate', 'doneDate', 'recheckProgress', 'queuePosition']
    STATES = {0: 'paused', 1: 'queued', 2: 'checking', 3: 'queued', 4: 'downloading',
              5: 'queued', 6: 'seeding'}

    @staticmethod
    def _pick(t: dict, legacy: str, snake: str):
        # Transmission 4.1's JSON-RPC 2.0 uses snake_case; legacy requests are
        # answered in the old names, so read those first.
        return t.get(legacy) if t.get(legacy) is not None else t.get(snake)

    def _row(self, t: dict) -> dict:
        pct = float(t.get('percentDone') or 0)
        check = self._pick(t, 'recheckProgress', 'recheck_progress')
        queue = self._pick(t, 'queuePosition', 'queue_position')
        done_at = self._pick(t, 'doneDate', 'done_date')
        return {'hash': t['hashString'].lower(), 'name': t.get('name') or '',
                'percent': round(pct * 100, 1), 'done': pct >= 1.0,
                'eta': t.get('eta') if (t.get('eta') or -1) >= 0 else None,
                'rate': t.get('rateDownload') or 0, 'up': t.get('rateUpload') or 0,
                'paused': t.get('status') == 0,
                'state': self.STATES.get(t.get('status'), 'unknown'),
                'size': t.get('sizeWhenDone'), 'dir': t.get('downloadDir'),
                'ratio': max(0.0, round(float(t.get('uploadRatio') or 0), 2)),
                'added': t.get('addedDate') or None,
                'queue': queue if isinstance(queue, int) and queue >= 0 else None,
                'check': (min(1.0, max(0.0, float(check)))
                          if t.get('status') == 2 and isinstance(check, (int, float)) else None),
                'completed': isinstance(done_at, (int, float)) and done_at > 0,
                'error': t.get('errorString') if t.get('error') else None}

    def list(self, hashes: list[str]) -> list[dict]:
        if not hashes:
            return []
        got = self.call('torrent-get', {'ids': hashes, 'fields': self.FIELDS})
        return [self._row(t) for t in got.get('torrents') or []]

    def list_all(self) -> list[dict]:
        got = self.call('torrent-get', {'fields': self.FIELDS})
        return [self._row(t) for t in got.get('torrents') or []]

    def start(self, hashes: list[str]) -> None:
        self.call('torrent-start', {'ids': hashes})

    def stop(self, hashes: list[str]) -> None:
        self.call('torrent-stop', {'ids': hashes})

    def queue_move(self, hashes: list[str], where: str) -> None:
        self.call('queue-move-' + where, {'ids': hashes})

    def remove(self, hashes: list[str], delete_data: bool = False) -> None:
        self.call('torrent-remove', {'ids': hashes, 'delete-local-data': bool(delete_data)})

    def info(self) -> dict:
        got = self.call('session-get', {'fields': ['version', 'download-dir', 'config-dir']})
        st = self.call('session-stats', {})
        return {'client': 'Transmission', 'version': got.get('version') or '',
                'download_dir': got.get('download-dir'), 'down': st.get('downloadSpeed') or 0,
                'up': st.get('uploadSpeed') or 0, 'config_dir': got.get('config-dir')}


class QBittorrent:
    def __init__(self, s: dict):
        self.base, self.s, self.cookie = s['url'] + '/api/v2', s, ''

    def _req(self, path: str, form: dict | None = None, files: bytes | None = None,
             ctype: str | None = None):
        headers = {'Referer': self.s['url'], 'Origin': self.s['url']}
        if self.cookie:
            headers['Cookie'] = self.cookie
        data = files if files is not None else (
            urllib.parse.urlencode(form).encode() if form is not None else None)
        if ctype:
            headers['Content-Type'] = ctype
        elif form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        return _http(self.base + path, data, headers)

    def login(self):
        if self.cookie or not self.s['username']:
            return
        status, headers, raw = self._req('/auth/login', {'username': self.s['username'],
                                                         'password': self.s['password']})
        cookie = next((v for k, v in headers.items() if k.lower() == 'set-cookie'), '')
        if status != 200 or raw.strip() != b'Ok.' or not cookie:
            raise TorrentError("qBittorrent refused the login ([torrent] username / password)")
        self.cookie = cookie.split(';', 1)[0]

    def add(self, source: str, download_dir: str | None, seed_ratio: float | None) -> dict:
        self.login()
        form = {'paused': 'false'}
        if download_dir:
            form['savepath'] = download_dir
        if seed_ratio is not None:
            form['ratioLimit'] = str(seed_ratio)
        if source.startswith('magnet:'):
            status, _h, raw = self._req('/torrents/add', {**form, 'urls': source})
            h = infohash(source) or ''
        else:
            h = file_infohash(source)
            boundary = '----nexus' + str(int(time.time() * 1000))
            parts = []
            for k, v in form.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                             f'\r\n\r\n{v}\r\n'.encode())
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; '
                         f'filename="x.torrent"\r\nContent-Type: application/x-bittorrent'
                         f'\r\n\r\n'.encode() + Path(source).read_bytes() + b'\r\n')
            parts.append(f'--{boundary}--\r\n'.encode())
            status, _h, raw = self._req('/torrents/add', files=b''.join(parts),
                                        ctype=f'multipart/form-data; boundary={boundary}')
        if status != 200 or raw.strip().lower().startswith(b'fails'):
            raise TorrentError(f"qBittorrent didn't take it (HTTP {status})")
        return {'hash': h, 'name': ''}

    STATES = {'downloading': 'downloading', 'forcedDL': 'downloading', 'metaDL': 'downloading',
              'stalledDL': 'downloading', 'uploading': 'seeding', 'forcedUP': 'seeding',
              'stalledUP': 'seeding', 'queuedDL': 'queued', 'queuedUP': 'queued',
              'checkingDL': 'checking', 'checkingUP': 'checking',
              'checkingResumeData': 'checking', 'moving': 'checking'}

    def _row(self, t: dict) -> dict:
        pct = float(t.get('progress') or 0)
        state = str(t.get('state') or '')
        paused = 'paused' in state.lower() or 'stopped' in state.lower()
        prio, done_at = int(t.get('priority') or 0), int(t.get('completion_on') or 0)
        return {'hash': t['hash'].lower(), 'name': t.get('name') or '',
                'percent': round(pct * 100, 1), 'done': pct >= 1.0,
                'eta': t.get('eta') if 0 <= (t.get('eta') or -1) < 8640000 else None,
                'rate': t.get('dlspeed') or 0, 'up': t.get('upspeed') or 0, 'paused': paused,
                'state': 'paused' if paused else self.STATES.get(state, 'unknown'),
                'size': t.get('size'), 'dir': t.get('save_path'),
                'ratio': max(0.0, round(float(t.get('ratio') or 0), 2)),
                'added': t.get('added_on') or None,
                'queue': prio - 1 if prio > 0 else None, 'check': None,
                'completed': 0 < done_at < 4294967295,
                'error': 'error' if state in ('error', 'missingFiles') else None}

    def _info(self, query: str = '') -> list[dict]:
        self.login()
        status, _h, raw = self._req('/torrents/info' + query)
        if status != 200:
            raise TorrentError(f"qBittorrent answered HTTP {status}")
        rows = _json(raw, "qBittorrent")
        return [self._row(t) for t in rows if isinstance(t, dict)] if isinstance(rows, list) else []

    def list(self, hashes: list[str]) -> list[dict]:
        return self._info('?hashes=' + '|'.join(hashes)) if hashes else []

    def list_all(self) -> list[dict]:
        return self._info()

    def _act(self, paths: tuple[str, ...], form: dict) -> None:
        self.login()
        for path in paths:             # qBittorrent 5 renamed pause/resume to stop/start
            status, _h, _raw = self._req(path, form)
            if status == 200:
                return
            if status != 404:
                break
        raise TorrentError(f"qBittorrent answered HTTP {status}")

    def start(self, hashes: list[str]) -> None:
        self._act(('/torrents/start', '/torrents/resume'), {'hashes': '|'.join(hashes)})

    def stop(self, hashes: list[str]) -> None:
        self._act(('/torrents/stop', '/torrents/pause'), {'hashes': '|'.join(hashes)})

    QUEUE_PATHS = {'top': '/torrents/topPrio', 'up': '/torrents/increasePrio',
                   'down': '/torrents/decreasePrio', 'bottom': '/torrents/bottomPrio'}

    def queue_move(self, hashes: list[str], where: str) -> None:
        self.login()
        status, _h, _raw = self._req(self.QUEUE_PATHS[where], {'hashes': '|'.join(hashes)})
        if status == 409:               # qBittorrent answers 409 while queueing is off
            raise TorrentError("queueing is off in your client "
                               "(qBittorrent: Options › BitTorrent › Torrent Queueing)")
        if status != 200:
            raise TorrentError(f"qBittorrent answered HTTP {status}")

    def remove(self, hashes: list[str], delete_data: bool = False) -> None:
        self._act(('/torrents/delete',), {'hashes': '|'.join(hashes),
                                          'deleteFiles': 'true' if delete_data else 'false'})

    def info(self) -> dict:
        self.login()
        status, _h, raw = self._req('/app/version')
        if status != 200:
            raise TorrentError(f"qBittorrent answered HTTP {status}")
        st, _h, tr = self._req('/transfer/info')
        t = _json(tr, "qBittorrent") if st == 200 else {}
        t = t if isinstance(t, dict) else {}
        save = None
        try:
            ps, _h, pr = self._req('/app/preferences')
            prefs = _json(pr, "qBittorrent") if ps == 200 else {}
            save = (prefs.get('save_path') or None) if isinstance(prefs, dict) else None
        except TorrentError:
            pass
        return {'client': 'qBittorrent', 'version': raw.decode(errors='replace').strip(),
                'download_dir': save, 'down': t.get('dl_info_speed') or 0,
                'up': t.get('up_info_speed') or 0}


def client(s: dict | None = None):
    s = s or settings()
    if s['client'] == 'transmission':
        return Transmission(s)
    if s['client'] == 'qbittorrent':
        return QBittorrent(s)
    raise TorrentError("no torrent client configured ([torrent] client = \"transmission\" "
                       "or \"qbittorrent\" in config.toml)")


def load_tracked() -> dict:
    try:
        data = json.loads(TRACKED.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_tracked(data: dict) -> None:
    net.write_atomic(TRACKED, json.dumps(data, indent=1))


@contextlib.contextmanager
def edit_tracked():
    """Load torrents.json, let the caller change it, and save it — under an
    exclusive lock, so the GUI adding a torrent and an install marking its own
    can't undo each other. Saved only if something changed; an exception in
    the block saves nothing."""
    TRACKED.parent.mkdir(parents=True, exist_ok=True)
    with platform.file_lock(TRACKED.with_name(TRACKED.name + '.lock')):
        data = load_tracked()
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            save_tracked(data)


PIPELINE_DIR = DATA_DIR / 'pipeline'
_PRUNE_S = 86400            # a progress file older than a day is a crashed install's


def write_progress(h: str, data: dict) -> None:
    """The install of download `h` as it runs (nexus/pipeline.py reads it):
    DATA_DIR/pipeline/<hash>.json, replaced atomically. Raises OSError."""
    net.write_atomic(PIPELINE_DIR / f'{check_hash(h)}.json', json.dumps(data))


def clear_progress(h: str) -> None:
    """Remove the progress file of `h` (none: fine). Raises OSError."""
    (PIPELINE_DIR / f'{check_hash(h)}.json').unlink(missing_ok=True)


def _progress_files():
    try:
        return [f for f in PIPELINE_DIR.glob('*.json') if HASH_RE.match(f.stem)]
    except OSError:
        return []


def load_progress() -> dict:
    """{hash: progress} of the installs that wrote a file (junk skipped).
    Read only: it runs on every poll. Whether a file is current is
    pipeline.fresh's call; prune_progress deletes the old ones."""
    out = {}
    for f in _progress_files():
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('hash') == f.stem:
            out[f.stem] = data
    return out


def prune_progress(now: float | None = None) -> None:
    """Delete progress files older than a day (their updated_at, or the
    mtime of one that can't be read): installs that were killed. Called when
    an install starts, not on every poll."""
    now = time.time() if now is None else now
    for f in _progress_files():
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            stamp = float(data['updated_at'])
        except (OSError, ValueError, TypeError, KeyError):
            try:
                stamp = f.stat().st_mtime
            except OSError:
                continue
        if now - stamp > _PRUNE_S:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


STATS = DATA_DIR / 'pipeline-stats.json'
STATS_KEEP = 50             # install samples remembered


def _sample_ok(x) -> bool:
    if not isinstance(x, dict):
        return False

    def num(k, positive=False, none=False):
        v = x.get(k)
        if v is None:
            return none
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and (v > 0 if positive else v >= 0))
    return (num('at') and num('written', True) and num('install_s', True) and num('finish_s')
            and num('expected', none=True) and num('verify_bytes', none=True)
            and num('verify_s', none=True) and isinstance(x.get('drive'), (str, type(None))))


def load_stats() -> list[dict]:
    """The remembered installs (see record_install), oldest first. Read
    only: it runs on every poll. A missing or unreadable file, or a junk
    entry, is no history."""
    try:
        data = json.loads(STATS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    rows = data.get('installs') if isinstance(data, dict) else None
    return [x for x in rows if _sample_ok(x)][-STATS_KEEP:] if isinstance(rows, list) else []


def record_install(sample: dict) -> None:
    """Remember one finished install ({at, drive, written, expected,
    install_s, verify_bytes, verify_s, finish_s}) in DATA_DIR/pipeline-stats.json,
    the last STATS_KEEP of them: how long an install takes per byte, which
    the pipeline's one ETA is made from. Under a lock and replaced
    atomically, like edit_tracked. Never fails the install: an OSError is
    swallowed."""
    try:
        STATS.parent.mkdir(parents=True, exist_ok=True)
        with platform.file_lock(STATS.with_name(STATS.name + '.lock')):
            rows = load_stats() + [sample]
            net.write_atomic(STATS, json.dumps({'v': 1, 'installs': rows[-STATS_KEEP:]}))
    except OSError:
        pass


def claim_install(h: str) -> bool:
    """Record that a finished download's install is starting — True only for
    the first caller. The GUI and the background agent both follow the
    client; this is how just one of them installs it."""
    with edit_tracked() as tracked:
        meta = tracked.get(h)
        if not isinstance(meta, dict) or meta.get('installed') or meta.get('install_started'):
            return False
        meta['install_started'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        return True


def check_hash(h: str) -> str:
    h = (h or '').strip().lower()
    if not HASH_RE.match(h):
        raise TorrentError(f"not a torrent hash: {h!r}")
    return h


def add(source: str, auto_install: bool = False, s: dict | None = None,
        meta: dict | None = None) -> dict:
    s = s or settings()
    if source.startswith('magnet:'):
        if not infohash(source):
            raise TorrentError("that isn't a magnet link with a BitTorrent hash")
    elif not (Path(source).is_file() and source.lower().endswith('.torrent')):
        raise TorrentError(f"not a magnet link or a .torrent file: {source}")
    got = client(s).add(source, s['download_dir'], s['seed_ratio'])
    h = got['hash'] or (infohash(source) if source.startswith('magnet:') else '')
    if not h:
        raise TorrentError("the client didn't say which torrent it added")
    name = got['name'] or urllib.parse.parse_qs(source.partition('?')[2]).get('dn', [''])[0]
    with edit_tracked() as tracked:
        tracked[h] = {**(meta or {}), 'name': name,
                      'added_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                      'auto_install': bool(auto_install), 'installed': False}
    return {'hash': h, 'name': name}


_WIN_FOLDER = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\)')


def _content_path(folder, name) -> str | None:
    """`folder/name` for a torrent, or None when the name isn't one plain path
    component (a client's '..' or '/x' would point outside the download
    folder, and that path gets installed from and later trashed). Joined in
    the client's own style: a Windows client's folder with backslashes, a
    Linux one's (a NAS behind a tunnel included) with slashes, wherever Nexus
    runs."""
    win = bool(folder and _WIN_FOLDER.match(folder))
    if not folder or not name or name in ('.', '..') or '/' in name or '\0' in name \
            or (win and ('\\' in name or ':' in name)):
        return None
    return str(PureWindowsPath(folder, name) if win else PurePosixPath(folder, name))


def _merge(t: dict, meta: dict | None) -> dict:
    path = _content_path(t.get('dir'), t.get('name'))
    if meta is None:
        return {**t, 'tracked': False, 'path': path}
    return {**t, **meta, 'name': t['name'] or meta.get('name'), 'tracked': True, 'path': path}


def status(s: dict | None = None) -> list[dict]:
    """The tracked torrents as the client sees them now, with 'path' (the
    finished folder) and the tracking flags."""
    tracked = load_tracked()
    if not tracked:
        return []
    live = {t['hash']: t for t in client(s).list(list(tracked))}
    out = []
    for h, meta in tracked.items():
        t = live.get(h)
        if t is None:
            out.append({'hash': h, **meta, 'tracked': True, 'gone': True})
            continue
        out.append(_merge(t, meta))
    return out


def all_status(s: dict | None = None, c=None) -> list[dict]:
    """Every torrent in the client, newest first, with Nexus's tracking
    flags on the ones it added (and those it added that are gone). Pass the
    caller's client `c` to reuse its Transmission session id."""
    tracked = load_tracked()
    rows = (c or client(s)).list_all()
    seen = {t['hash'] for t in rows}
    out = [_merge(t, tracked.get(t['hash'])) for t in rows]
    out.sort(key=lambda r: -(r.get('added') or 0))
    out += [{'hash': h, **meta, 'tracked': True, 'gone': True}
            for h, meta in tracked.items() if h not in seen]
    return out


def control(action: str, hashes: list[str], delete_data: bool = False,
            s: dict | None = None) -> None:
    """pause / resume / remove torrents in the client. Removing also
    forgets Nexus's tracking of them."""
    hashes = [check_hash(h) for h in hashes]
    c = client(s)
    if action == 'pause':
        c.stop(hashes)
    elif action == 'resume':
        c.start(hashes)
    elif action == 'remove':
        c.remove(hashes, delete_data)
        with edit_tracked() as tracked:
            for h in hashes:
                tracked.pop(h, None)
    else:
        raise TorrentError(f"unknown action {action!r}")


QUEUE_MOVES = ('top', 'up', 'down', 'bottom')


def queue_move(h: str, where: str, s: dict | None = None) -> None:
    """Move torrent `h` in the client's own queue: to the 'top', 'up' or
    'down' one place, or to the 'bottom'."""
    if where not in QUEUE_MOVES:
        raise TorrentError(f"unknown queue move {where!r}")
    client(s).queue_move([check_hash(h)], where)


TRANSMISSIONS = {'transmission-daemon': 'daemon', 'transmission-qt': 'qt',
                 'transmission-gtk': 'gtk'}


def _config_arg(args: list[str]) -> str | None:
    """The settings folder a Transmission was started with (-g / --config-dir)."""
    for i, a in enumerate(args):
        if a in ('-g', '--config-dir') and i + 1 < len(args):
            return args[i + 1]
        if a.startswith('--config-dir='):
            return a.partition('=')[2]
    return None


def _same_dir(a, b) -> bool:
    def norm(p):
        try:
            return os.path.normcase(str(Path(p).expanduser().resolve()))
        except (OSError, RuntimeError, ValueError):
            return os.path.normcase(str(p).rstrip('/\\'))
    return norm(a) == norm(b)


def _remote_ui(config_dir: Path) -> bool:
    """A Qt/GTK Transmission set to drive another session (its "Connect to
    remote session" switch) runs no torrents of its own."""
    try:
        cfg = json.loads((config_dir / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(cfg, dict) and cfg.get('remote-session-enabled') is True


def other_transmissions(served: str | None) -> list[dict]:
    """Transmission sessions running here besides the one that answers Nexus
    (whose settings folder is `served`, session-get's config-dir):
    [{'name', 'pid', 'config_dir'}]. Each keeps its own torrents, so a
    download the user follows in the other one never moves here (2026-11-02:
    transmission-qt finished a repack while the transmission-daemon Nexus
    talked to was at 85% of the same torrent). A session whose folder isn't
    known is left out rather than guessed."""
    if not served:
        return []
    out = []
    for p in platform.processes():
        args = p.cmdline
        prog = re.sub(r'\.exe$', '', (PureWindowsPath(args[0]).name if args else p.name).lower())
        kind = TRANSMISSIONS.get(prog)
        if not kind:
            continue
        cfg = _config_arg(args[1:])
        cfg = Path(cfg).expanduser() if cfg else platform.transmission_config_dir(kind)
        if cfg is None or _same_dir(cfg, served) or (kind != 'daemon' and _remote_ui(cfg)):
            continue
        out.append({'name': prog, 'pid': p.pid, 'config_dir': str(cfg)})
    return out


def installed_clients() -> list[dict]:
    """Torrent clients whose settings Nexus can read here (Windows: their
    files under AppData): [{'client', 'url', 'remote'}], where `remote` says
    whether its web/RPC access is on. Linux: [] (detect() probes ports).
    A hint only: Transmission writes settings.json when it quits, so a
    switch flipped in the running client shows here only after a restart."""
    out = []
    for client, path in platform.torrent_client_settings():
        try:
            text = Path(path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if client == 'transmission':
            try:
                cfg = json.loads(text)
            except ValueError:
                continue
            out.append({'client': client, 'remote': bool(cfg.get('rpc-enabled', True)),
                        'url': f"http://127.0.0.1:{int(cfg.get('rpc-port', 9091))}"})
        else:                                   # qBittorrent.ini: [Preferences] WebUI\...
            on = re.search(r'^WebUI\\Enabled=(\w+)', text, re.M)
            port = re.search(r'^WebUI\\Port=(\d+)', text, re.M)
            out.append({'client': client, 'remote': bool(on and on.group(1) == 'true'),
                        'url': f"http://127.0.0.1:{port.group(1) if port else 8080}"})
    return out


def detect() -> dict | None:
    """A torrent client answering on its local port, unconfigured: {'client',
    'url'} or None. The default ports, and on Windows the ports its settings
    file names."""
    probes = [(c['client'], c['url']) for c in installed_clients() if c['remote']]
    probes += [('transmission', 'http://127.0.0.1:9091'), ('qbittorrent', 'http://127.0.0.1:8080')]
    paths = {'transmission': ('/transmission/rpc', (409, 401)),
             'qbittorrent': ('/api/v2/app/version', (200, 403))}
    for name, url in dict.fromkeys(probes):
        path, codes = paths[name]
        try:
            status, headers, _raw = _http(url + path, b'{}' if name == 'transmission' else None,
                                          timeout=2)
        except TorrentError:
            continue
        if status in codes and (name != 'transmission' or status == 401 or any(
                k.lower() == 'x-transmission-session-id' for k in headers)):
            return {'client': name, 'url': url}
    return None
