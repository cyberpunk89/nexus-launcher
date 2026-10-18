"""One Nexus per user: a second `nexus` launch hands its arguments to the
running window and exits (nirinit restores and Sunshine's Theatre launch used
to open a second window)."""

import os
import subprocess
import sys
import time
import unittest

try:
    from test_gui_layout import nx, REPO
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, REPO

if nx is not None:
    from PyQt6.QtWidgets import QApplication


@unittest.skipIf(nx is None, "PyQt6 not available")
class SingleInstance(unittest.TestCase):
    def setUp(self):
        self.name = f"nexus-test-{os.getpid()}-{time.monotonic_ns()}"
        self._saved = nx.INSTANCE
        nx.INSTANCE = self.name
        self.got = []
        self.server = nx.InstanceServer()
        self.server.request.connect(self.got.append)
        self.assertTrue(self.server.listen([]))

    def tearDown(self):
        self.server.server.close()
        self.server.deleteLater()
        nx.INSTANCE = self._saved

    def _wait(self, n=1):
        end = time.monotonic() + 3
        while len(self.got) < n and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    def _run(self, *args):
        env = dict(os.environ, NEXUS_INSTANCE=self.name, QT_QPA_PLATFORM='offscreen',
                   NEXUS_NO_GAMEPAD='1')
        return subprocess.run([sys.executable, str(REPO / 'bin' / 'nexus'), *args], env=env,
                              capture_output=True, text=True, timeout=20)

    def test_second_launch_forwards_and_exits(self):
        r = self._run('--theatre')
        self.assertEqual(r.returncode, 0, r.stderr)
        self._wait()
        self.assertEqual(self.got, [['--theatre']])

    def test_in_process_forward(self):
        self.assertTrue(nx.forward_to_running(['--leave-theatre']))
        self._wait()
        self.assertEqual(self.got, [['--leave-theatre']])

    def test_second_server_defers_to_the_first(self):
        other = nx.InstanceServer()
        try:
            self.assertFalse(other.listen(['x']))
            self._wait()
            self.assertEqual(self.got, [['x']])
        finally:
            other.deleteLater()

    def test_leave_theatre_never_starts_a_window(self):
        self.server.server.close()
        r = self._run('--leave-theatre')      # would hang in app.exec() if it did
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_handle_remote(self):
        calls = []

        class Fake:
            set_theatre = lambda s, on: calls.append(('theatre', on))   # noqa: E731
            bring_to_front = lambda s: calls.append(('front',))         # noqa: E731
        nx.MainWindow.handle_remote(Fake(), ['--theatre'])
        nx.MainWindow.handle_remote(Fake(), ['--leave-theatre'])
        nx.MainWindow.handle_remote(Fake(), [])
        self.assertEqual(calls, [('theatre', True), ('front',), ('theatre', False),
                                 ('front',)])


if __name__ == '__main__':
    unittest.main()
