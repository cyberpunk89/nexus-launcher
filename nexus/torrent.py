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

import base64
import json
import re
import time
import tomllib
import urllib.parse
from pathlib import Path

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
        data = tomllib.loads(CONFIG_PATH.read_text())
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
    except (urllib.error.URLError, OSError) as e:
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
            data = json.loads(raw)
            if data.get('result') != 'success':
                raise TorrentError(f"Transmission: {data.get('result')}")
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
              'uploadRatio', 'addedDate', 'doneDate']
    STATES = {0: 'paused', 1: 'queued', 2: 'checking', 3: 'queued', 4: 'downloading',
              5: 'queued', 6: 'seeding'}

    def _row(self, t: dict) -> dict:
        pct = float(t.get('percentDone') or 0)
        return {'hash': t['hashString'].lower(), 'name': t.get('name') or '',
                'percent': round(pct * 100, 1), 'done': pct >= 1.0,
                'eta': t.get('eta') if (t.get('eta') or -1) >= 0 else None,
                'rate': t.get('rateDownload') or 0, 'up': t.get('rateUpload') or 0,
                'paused': t.get('status') == 0,
                'state': self.STATES.get(t.get('status'), 'unknown'),
                'size': t.get('sizeWhenDone'), 'dir': t.get('downloadDir'),
                'ratio': max(0.0, round(float(t.get('uploadRatio') or 0), 2)),
                'added': t.get('addedDate') or None,
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

    def remove(self, hashes: list[str], delete_data: bool = False) -> None:
        self.call('torrent-remove', {'ids': hashes, 'delete-local-data': bool(delete_data)})

    def info(self) -> dict:
        got = self.call('session-get', {'fields': ['version', 'download-dir']})
        st = self.call('session-stats', {})
        return {'client': 'Transmission', 'version': got.get('version') or '',
                'download_dir': got.get('download-dir'), 'down': st.get('downloadSpeed') or 0,
                'up': st.get('uploadSpeed') or 0}


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
            h = ''
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
        return {'hash': t['hash'].lower(), 'name': t.get('name') or '',
                'percent': round(pct * 100, 1), 'done': pct >= 1.0,
                'eta': t.get('eta') if 0 <= (t.get('eta') or -1) < 8640000 else None,
                'rate': t.get('dlspeed') or 0, 'up': t.get('upspeed') or 0, 'paused': paused,
                'state': 'paused' if paused else self.STATES.get(state, 'unknown'),
                'size': t.get('size'), 'dir': t.get('save_path'),
                'ratio': max(0.0, round(float(t.get('ratio') or 0), 2)),
                'added': t.get('added_on') or None,
                'error': 'error' if state in ('error', 'missingFiles') else None}

    def _info(self, query: str = '') -> list[dict]:
        self.login()
        status, _h, raw = self._req('/torrents/info' + query)
        if status != 200:
            raise TorrentError(f"qBittorrent answered HTTP {status}")
        return [self._row(t) for t in json.loads(raw)]

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

    def remove(self, hashes: list[str], delete_data: bool = False) -> None:
        self._act(('/torrents/delete',), {'hashes': '|'.join(hashes),
                                          'deleteFiles': 'true' if delete_data else 'false'})

    def info(self) -> dict:
        self.login()
        status, _h, raw = self._req('/app/version')
        if status != 200:
            raise TorrentError(f"qBittorrent answered HTTP {status}")
        st, _h, tr = self._req('/transfer/info')
        t = json.loads(tr) if st == 200 else {}
        return {'client': 'qBittorrent', 'version': raw.decode(errors='replace').strip(),
                'download_dir': None, 'down': t.get('dl_info_speed') or 0,
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
        data = json.loads(TRACKED.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_tracked(data: dict) -> None:
    TRACKED.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRACKED.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(TRACKED)


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
    tracked = load_tracked()
    name = got['name'] or urllib.parse.parse_qs(source.partition('?')[2]).get('dn', [''])[0]
    tracked[h] = {**(meta or {}), 'name': name,
                  'added_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                  'auto_install': bool(auto_install), 'installed': False}
    save_tracked(tracked)
    return {'hash': h, 'name': name}


def _content_path(folder, name) -> str | None:
    """`folder/name` for a torrent, or None when the name isn't one plain path
    component (a client's '..' or '/x' would point outside the download
    folder, and that path gets installed from and later trashed)."""
    if not folder or not name or name in ('.', '..') or '/' in name or '\0' in name:
        return None
    return str(Path(folder) / name)


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


def all_status(s: dict | None = None) -> list[dict]:
    """Every torrent in the client, newest first, with Nexus's tracking
    flags on the ones it added (and those it added that are gone)."""
    tracked = load_tracked()
    rows = client(s).list_all()
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
        tracked = load_tracked()
        if any(tracked.pop(h, None) is not None for h in hashes):
            save_tracked(tracked)
    else:
        raise TorrentError(f"unknown action {action!r}")


def detect() -> dict | None:
    """A torrent client answering on its default local port, unconfigured:
    {'client', 'url'} or None."""
    for name, url, path, codes in (
            ('transmission', 'http://127.0.0.1:9091', '/transmission/rpc', (409, 401)),
            ('qbittorrent', 'http://127.0.0.1:8080', '/api/v2/app/version', (200, 403))):
        try:
            status, headers, _raw = _http(url + path, b'{}' if name == 'transmission' else None,
                                          timeout=2)
        except TorrentError:
            continue
        if status in codes and (name != 'transmission' or status == 401 or any(
                k.lower() == 'x-transmission-session-id' for k in headers)):
            return {'client': name, 'url': url}
    return None
