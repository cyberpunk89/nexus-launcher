"""nexus.net's disk cache: fresh copies, stale fallbacks, and failures that
are remembered for a while so offline use doesn't wait out every timeout."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from nexus import net  # noqa: E402


class Token(unittest.TestCase):
    def test_github_token_only_for_the_github_api_host(self):
        with mock.patch.dict('os.environ', {'GITHUB_TOKEN': 'secret'}):
            ok = net._request('https://api.github.com/repos/x/y/releases')
            self.assertEqual(ok.get_header('Authorization'), 'Bearer secret')
            for url in ('https://evil.example/api.github.com/feed',
                        'https://api.github.com.evil.example/x',
                        'https://evil.example/?u=https://api.github.com'):
                self.assertIsNone(net._request(url).get_header('Authorization'), url)

    def test_redirect_elsewhere_drops_authorization(self):
        import urllib.request
        # Build the opener for real (no request leaves) and drive its
        # redirect handler directly.
        net._opener = None
        with mock.patch.object(urllib.request.OpenerDirector, 'open'):
            net._open(urllib.request.Request('https://api.github.com/x'), 5)
        redirect = next(h for h in net._opener.handlers
                        if isinstance(h, urllib.request.HTTPRedirectHandler))
        req = urllib.request.Request('https://api.github.com/x',
                                     headers={'Authorization': 'Bearer secret'})
        same = redirect.redirect_request(req, None, 301, 'Moved', {},
                                         'https://api.github.com/y')
        self.assertEqual(same.get_header('Authorization'), 'Bearer secret')
        other = redirect.redirect_request(req, None, 302, 'Found', {},
                                          'https://objects.example/blob')
        self.assertIsNone(other.get_header('Authorization'))
        # https -> http is never followed (urllib raises the 3xx instead)
        self.assertIsNone(redirect.redirect_request(req, None, 302, 'Found', {},
                                                    'http://api.github.com/y'))
        net._opener = None


class Cached(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = mock.patch.object(net, 'CACHE_DIR', Path(self.tmp.name))
        self.dir.start()
        self.calls = []

    def tearDown(self):
        self.dir.stop()
        self.tmp.cleanup()

    def get_ok(self, url, timeout=20):
        self.calls.append(url)
        return b'{"v": 1}'

    def get_fail(self, url, timeout=20):
        self.calls.append(url)
        raise net.NetError("offline")

    def test_failure_is_remembered(self):
        with mock.patch.object(net, 'get', self.get_fail):
            for _ in range(3):
                with self.assertRaises(net.NetError):
                    net.cached('https://x/a', 'a.json', 60)
        self.assertEqual(len(self.calls), 1)              # not three timeouts
        with mock.patch.object(net, 'get', self.get_ok):
            data, _at, stale = net.cached('https://x/a', 'a.json', 60, refresh=True)
        self.assertEqual((data, stale), ({'v': 1}, False))

    def test_stale_copy_while_failing(self):
        with mock.patch.object(net, 'get', self.get_ok):
            net.cached('https://x/b', 'b.json', 60)
        with mock.patch.object(net, 'get', self.get_fail):
            for _ in range(2):
                data, _at, stale = net.cached('https://x/b', 'b.json', 0)
                self.assertEqual((data, stale), ({'v': 1}, True))
        self.assertEqual(len(self.calls), 2)              # one real try, one remembered

    def test_failure_expires(self):
        with mock.patch.object(net, 'get', self.get_fail), \
                mock.patch.object(net, 'FAIL_TTL', 0):
            for _ in range(2):
                with self.assertRaises(net.NetError):
                    net.cached('https://x/c', 'c.json', 60)
        self.assertEqual(len(self.calls), 2)

    def test_corrupt_cache_is_refetched(self):
        path = Path(self.tmp.name) / 'http' / 'd.json'
        path.parent.mkdir(parents=True)
        path.write_text('{not json')
        with mock.patch.object(net, 'get', self.get_ok):
            data, _at, stale = net.cached('https://x/d', 'd.json', 600)
        self.assertEqual((data, stale), ({'v': 1}, False))
        self.assertEqual(len(self.calls), 1)


class Download(unittest.TestCase):
    class Resp:
        def __init__(self, body, length):
            self.body, self.headers = body, {'Content-Length': str(length)}

        def read(self, n):
            out, self.body = self.body[:n], self.body[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_short_body_is_an_error(self):
        import http.client
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / 'f.bin'
            with mock.patch('urllib.request.urlopen',
                            lambda *a, **k: self.Resp(b'abc', 10)):
                with self.assertRaises(net.NetError):
                    net.download('https://x/f', dest)
            self.assertFalse(dest.exists())
            self.assertFalse((Path(d) / 'f.bin.part').exists())

            def boom(*a, **k):
                raise http.client.IncompleteRead(b'ab')
            with mock.patch('urllib.request.urlopen', boom):
                with self.assertRaises(net.NetError):        # not IncompleteRead
                    net.download('https://x/f', dest)


if __name__ == '__main__':
    unittest.main()
