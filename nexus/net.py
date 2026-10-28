"""The one HTTP client Nexus uses: stdlib urllib, a fixed User-Agent,
timeouts, a small JSON/text disk cache, and streamed downloads."""

import json
import os
import shutil
import threading
import time
from pathlib import Path

from . import __version__, platform
from .paths import CACHE_DIR

UA = f"nexus/{__version__} (+https://github.com/cyberpunk89/nexus-launcher)"


class NetError(Exception):
    def __init__(self, message: str = '', status: int | None = None):
        super().__init__(message)
        self.status = status               # the HTTP status, when there was one


def errors() -> tuple:
    """What a request can fail with: http.client's IncompleteRead /
    BadStatusLine (a connection dropped mid-body) are neither OSError nor
    URLError. urllib loads lazily — most CLI calls never touch the network,
    and it's a good share of their start-up."""
    import http.client
    import urllib.error
    return (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException)


def _tmp_name(path: Path) -> Path:
    """A temp file next to `path` no other process or thread shares."""
    return path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.part")


def write_atomic(path: Path, data: str | bytes, *, fsync: bool = False,
                 keep_mode: bool = False, mode: int | None = None) -> None:
    """Replace `path` with `data` all at once: a temp file of its own (pid +
    thread, so concurrent writers never share one), then os.replace — a
    reader or a crash never sees half a file. `fsync` for files whose loss
    hurts (Steam's and Heroic's libraries); `keep_mode` keeps the old file's
    permissions; `mode` (e.g. 0o600 for a file holding a token) applies from
    the first byte. Raises OSError, leaving no temp file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(path)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o666 if mode is None else mode)
        if mode is not None and hasattr(os, 'fchmod'):
            os.fchmod(fd, mode)                # umask can't widen or narrow it
        with os.fdopen(fd, 'wb') as f:
            f.write(data.encode('utf-8') if isinstance(data, str) else data)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if keep_mode and path.exists():
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        if mode is not None and not mode & 0o077:
            platform.keep_private(tmp)         # Windows: an owner-only ACL (kept by the rename)
        platform.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_json(path: Path, obj, **dumps) -> bool:
    """Best-effort JSON cache write (write_atomic): False if it couldn't be
    written — a cache that can't be saved is never a reason to fail."""
    try:
        write_atomic(path, json.dumps(obj, **dumps))
        return True
    except (OSError, TypeError, ValueError):
        return False


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or '').lower()
    except ValueError:
        return ''


def _request(url: str, headers: dict | None = None):
    import urllib.request
    h = {'User-Agent': UA, **(headers or {})}
    # The exact host, not a substring: a feed at evil.example/api.github.com
    # must not receive the user's token.
    if _host(url) == 'api.github.com':
        h.setdefault('Accept', 'application/vnd.github+json')
        token = os.environ.get('GITHUB_TOKEN')
        if token:
            h['Authorization'] = f'Bearer {token}'
    return urllib.request.Request(url, headers=h)


_opener = None


def _open(req, timeout: float):
    """urlopen, except that a redirect off https is refused (an HTTPError),
    and one to another host drops the Authorization header — urllib forwards
    it everywhere by default."""
    global _opener
    if _opener is None:
        import urllib.request

        class _Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if (req.full_url.startswith('https://')
                        and not newurl.startswith('https://')):
                    return None            # urllib then raises the 3xx as an HTTPError
                new = super().redirect_request(req, fp, code, msg, headers, newurl)
                if new is not None and _host(newurl) != _host(req.full_url):
                    for k in [k for k in new.headers if k.lower() == 'authorization']:
                        del new.headers[k]
                    new.unredirected_hdrs.pop('Authorization', None)
                return new
        _opener = urllib.request.build_opener(_Redirect())
    return _opener.open(req, timeout=timeout)


def get(url: str, timeout: float = 20, headers: dict | None = None,
        limit: int = 32 << 20) -> bytes:
    if not url.startswith('https://'):
        raise NetError(f"refusing non-https URL: {url}")
    try:
        with _open(_request(url, headers), timeout) as r:
            data = r.read(limit + 1)
    except errors() as e:
        raise NetError(f"{url}: {getattr(e, 'reason', e)}", getattr(e, 'code', None)) from None
    if len(data) > limit:
        raise NetError(f"{url}: response larger than {limit} bytes")
    return data


def post(url: str, data: bytes, headers: dict | None = None, timeout: float = 15) -> bytes:
    """A small POST (push notifications). https only; the headers go to this
    host alone — a redirect elsewhere drops Authorization (see _open)."""
    import urllib.request
    if not url.startswith('https://'):
        raise NetError(f"refusing non-https URL: {url}")
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'User-Agent': UA, **(headers or {})})
    try:
        with _open(req, timeout) as r:
            return r.read(1 << 16)
    except errors() as e:
        raise NetError(f"{url}: {getattr(e, 'reason', e)}", getattr(e, 'code', None)) from None


def final_url(url: str, timeout: float = 20) -> str:
    """Where `url` ends up after redirects (only the headers are read)."""
    if not url.startswith('https://'):
        raise NetError(f"refusing non-https URL: {url}")
    try:
        with _open(_request(url), timeout) as r:
            return r.geturl()
    except errors() as e:
        raise NetError(f"{url}: {getattr(e, 'reason', e)}", getattr(e, 'code', None)) from None


FAIL_TTL = 600            # seconds a failed fetch is remembered


def _age(path: Path) -> float:
    """Seconds since `path` was written. Never negative: on Windows a file
    written just now can carry an mtime a little ahead of time.time(), and a
    max age of 0 must still mean "stale"."""
    return max(0.0, time.time() - path.stat().st_mtime)


def cached_json(path: Path, max_age: float, fetch, refresh: bool = False,
                errors_: tuple = (NetError, ValueError, KeyError, TypeError), valid=None):
    """fetch() through a JSON file cache at `path` (for per-game lookups:
    ProtonDB, PCGamingWiki, Steam store pages...). Fresh for `max_age`
    seconds. A failed fetch serves the last good copy (or None) and is
    remembered for FAIL_TTL, so offline the next lookups don't each wait out
    a timeout. None from fetch() is a real "nothing here" and is cached.
    `valid(value)` false (an older schema) makes a cached copy stale, but it
    is still served when the refetch fails."""
    path = Path(path)
    _corrupt = object()

    def read():                         # the cached value, or _corrupt (missing/unreadable)
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return _corrupt

    def last():
        v = read()
        return None if v is _corrupt else v
    try:
        if not refresh and _age(path) < max_age:
            hit = read()
            if hit is not _corrupt and (valid is None or valid(hit)):
                return hit
    except OSError:
        pass
    failed = path.with_name(path.name + '.failed')
    try:
        if not refresh and _age(failed) < FAIL_TTL:
            return last()
    except OSError:
        pass
    try:
        value = fetch()
    except errors_ as e:
        try:
            write_atomic(failed, str(e)[:200])
        except OSError:
            pass
        return last()
    if save_json(path, value):
        failed.unlink(missing_ok=True)
    return value


def peek_json(path: Path, max_age: float, valid=None) -> tuple[bool, object]:
    """What cached_json would serve from `path` without fetching: (fresh,
    value). fresh is False when the file is missing, older than `max_age` or
    fails `valid`; value is the copy at any age (None when there's none)."""
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        age = _age(path)
    except (OSError, ValueError):
        return False, None
    return age < max_age and (valid is None or valid(value)), value


def cached(url: str, name: str, max_age: float, refresh: bool = False,
           as_json: bool = True, timeout: float = 20):
    """Fetch through a disk cache under ~/.cache/nexus/http. Returns
    (data, fetched_at, stale): on a network failure the last good copy is
    returned with stale=True; with no copy at all, NetError is raised."""
    path = CACHE_DIR / 'http' / name

    def read():                    # the cached copy, or None (missing/corrupt)
        try:
            raw = path.read_bytes()
            return (json.loads(raw) if as_json else raw.decode('utf-8', 'replace'),
                    path.stat().st_mtime)
        except (OSError, ValueError):
            return None

    try:
        fresh = _age(path) < max_age
    except OSError:
        fresh = False
    if fresh and not refresh:
        hit = read()
        if hit is not None:
            return hit[0], hit[1], False

    def stale(err: str):
        hit = read()
        if hit is not None:
            return hit[0], hit[1], True
        raise NetError(err) from None

    # A fetch that just failed isn't retried for a while (unless asked to):
    # offline, every caller would otherwise wait out the timeout again.
    failed = path.with_name(path.name + '.failed')
    try:
        if not refresh and _age(failed) < FAIL_TTL:
            return stale(f"{url}: failed recently ({failed.read_text(encoding='utf-8').strip()})")
    except OSError:
        pass
    try:
        raw = get(url, timeout=timeout)
        data = json.loads(raw) if as_json else raw.decode('utf-8', 'replace')
    except (NetError, ValueError) as e:
        try:
            write_atomic(failed, str(e)[:200])
        except OSError:
            pass
        return stale(str(e))
    # Two processes can fetch the same feed at once: each writes its own
    # temp file, and a cache that can't be written is no reason to fail.
    try:
        write_atomic(path, raw)
        failed.unlink(missing_ok=True)
    except OSError:
        pass
    return data, time.time(), False


def download(url: str, dest: Path, progress=None, timeout: float = 60,
             limit: int | None = None) -> int:
    """Stream `url` to `dest` (via a temp file). `progress(done, total)` is
    called as data arrives (total may be 0 if unknown). Returns bytes. With
    `limit`, a larger response is refused."""
    if not url.startswith('https://'):
        raise NetError(f"refusing non-https URL: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = _tmp_name(dest)
    done = total = 0
    try:
        with _open(_request(url), timeout) as r, open(part, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            if limit is not None and total > limit:
                raise NetError(f"{url}: {total} bytes is over the {limit}-byte limit")
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if limit is not None and done > limit:
                    raise NetError(f"{url}: response larger than {limit} bytes")
                if progress:
                    progress(done, total)
    except NetError:
        part.unlink(missing_ok=True)
        raise
    except (*errors(), ValueError) as e:
        part.unlink(missing_ok=True)
        raise NetError(f"{url}: {getattr(e, 'reason', e)}") from None
    if total and done != total:            # cut short without an error
        part.unlink(missing_ok=True)
        raise NetError(f"{url}: got {done} of {total} bytes")
    platform.replace(part, dest)
    return done
