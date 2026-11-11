"""Slowing the torrent client while a game runs (nexus/slowplay.py): opt-in,
and the user's own alternative-speed setting is always put back. The client
is a fake; the state file lives in a temp dir."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import slowplay, torrent  # noqa: E402

TR = {'client': 'transmission', 'url': 'http://127.0.0.1:9091', 'username': '',
      'password': '', 'download_dir': None, 'seed_ratio': None}


class FakeClient:
    def __init__(self, alt=False, scheduled=False, down=False, state=None):
        self.alt, self.scheduled, self.down, self.calls = alt, scheduled, down, []
        self.state = state                       # the state file path, to look at it mid-call
        self.file_seen = None

    def _up(self):
        if self.down:
            raise torrent.TorrentError("client is not running")

    def alt_speed(self):
        self._up()
        return self.alt

    def alt_scheduled(self):
        self._up()
        return self.scheduled

    def set_alt_speed(self, on):
        self._up()
        self.calls.append(on)
        if self.state is not None:
            self.file_seen = self.state.exists()
        self.alt = on


class Sync(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.state = Path(d.name) / 'slow-while-playing.json'
        for p in (mock.patch.object(slowplay, 'STATE', self.state),
                  mock.patch.object(slowplay, 'CONFIG_PATH', Path(d.name) / 'config.toml'),
                  mock.patch.object(torrent, 'settings', return_value=dict(TR))):
            p.start()
            self.addCleanup(p.stop)

    def test_off_by_default_touches_nothing(self):
        c = FakeClient()
        self.assertEqual(slowplay.sync(True, c=c), 'idle')
        self.assertEqual(slowplay.sync(False, c=c), 'idle')
        self.assertEqual(c.calls, [])
        self.assertFalse(self.state.exists())

    def test_start_saves_state_before_slowing(self):
        c = FakeClient(state=self.state)
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'slowed')
        self.assertEqual(c.calls, [True])
        self.assertIs(c.file_seen, True)                  # written first
        data = json.loads(self.state.read_text())
        self.assertEqual((data['client'], data['url'], data['was']),
                         ('transmission', TR['url'], False))
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'kept')   # idempotent
        self.assertEqual(c.calls, [True])

    def test_already_slow_is_left_and_never_restored(self):
        c = FakeClient(alt=True)
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'left')
        self.assertFalse(self.state.exists())
        self.assertEqual(slowplay.sync(False, on=True, c=c), 'idle')
        self.assertEqual(c.calls, [])
        self.assertTrue(c.alt)

    def test_end_restores_and_forgets(self):
        c = FakeClient()
        slowplay.sync(True, on=True, c=c)
        self.assertEqual(slowplay.sync(False, on=True, c=c), 'restored')
        self.assertEqual(c.calls, [True, False])
        self.assertFalse(c.alt)
        self.assertFalse(self.state.exists())

    def test_user_turned_it_off_mid_game_is_left(self):
        c = FakeClient()
        slowplay.sync(True, on=True, c=c)
        c.alt = False                                     # the user switched it off
        self.assertEqual(slowplay.sync(False, on=True, c=c), 'left')
        self.assertEqual(c.calls, [True])                 # nothing set again
        self.assertFalse(self.state.exists())

    def test_restores_even_after_the_option_was_turned_off(self):
        c = FakeClient()
        slowplay.sync(True, on=True, c=c)
        self.assertEqual(slowplay.sync(False, on=False, c=c), 'restored')
        self.assertFalse(c.alt)
        self.assertFalse(self.state.exists())

    def test_client_down_keeps_state_for_next_time(self):
        c = FakeClient()
        slowplay.sync(True, on=True, c=c)
        c.down = True
        self.assertEqual(slowplay.sync(False, on=True, c=c), 'failed')
        self.assertTrue(self.state.exists())
        c.down = False
        self.assertEqual(slowplay.sync(False, on=True, c=c), 'restored')
        self.assertFalse(self.state.exists())
        # down at the start: nothing was saved, nothing slowed
        d = FakeClient(down=True)
        self.assertEqual(slowplay.sync(True, on=True, c=d), 'failed')
        self.assertFalse(self.state.exists())

    def test_other_client_drops_state(self):
        c = FakeClient()
        slowplay.sync(True, on=True, c=c)
        other = {**TR, 'client': 'qbittorrent', 'url': 'http://127.0.0.1:8080'}
        with mock.patch.object(torrent, 'settings', return_value=other):
            self.assertEqual(slowplay.sync(False, on=True, c=FakeClient(alt=True)), 'idle')
        self.assertFalse(self.state.exists())
        self.assertTrue(c.alt)                            # the old client is not our business

    def test_unreadable_record_is_dropped(self):
        for junk in ('{not json', ''):                    # a torn write; a zero-length file
            self.state.write_text(junk)
            self.assertEqual(slowplay.sync(False, on=True, c=FakeClient()), 'left')
            self.assertFalse(self.state.exists())

    def test_unreadable_record_turns_alt_speed_off_if_on(self):
        for junk in ('{not json', ''):
            self.state.write_text(junk)
            c = FakeClient(alt=True)
            self.assertEqual(slowplay.sync(False, on=False, c=c), 'restored')
            self.assertFalse(c.alt)
            self.assertFalse(self.state.exists())

    def test_record_is_fsynced(self):
        from nexus import net
        seen = []
        real = net.write_atomic
        with mock.patch.object(net, 'write_atomic',
                               lambda *a, **k: seen.append(k.get('fsync')) or real(*a, **k)):
            slowplay.sync(True, on=True, c=FakeClient())
        self.assertTrue(seen and all(seen))

    def test_failed_start_is_retried_while_playing(self):
        c = FakeClient()
        fail = {'on': True}
        real = c.set_alt_speed

        def flaky(on):
            if fail['on']:
                raise torrent.TorrentError("hiccup")
            real(on)
        c.set_alt_speed = flaky
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'failed')
        self.assertTrue(self.state.exists())
        fail['on'] = False
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'slowed')   # not stuck on 'kept'
        self.assertTrue(c.alt)
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'kept')
        c.alt = False                                      # the user switched it off: respected
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'kept')
        self.assertFalse(c.alt)
        self.assertEqual(slowplay.sync(False, on=True, c=c), 'left')

    def test_schedule_on_is_left_alone(self):
        c = FakeClient(scheduled=True)
        self.assertEqual(slowplay.sync(True, on=True, c=c), 'left')
        self.assertEqual(c.calls, [])
        self.assertFalse(self.state.exists())

    def test_option_read_from_config(self):
        self.assertFalse(slowplay.option())
        slowplay.CONFIG_PATH.write_text('[downloads]\nslow_while_playing = true\n')
        self.assertTrue(slowplay.option())
        slowplay.CONFIG_PATH.write_text('[downloads]\nslow_while_playing = "yes"\n')
        self.assertFalse(slowplay.option())               # only a real boolean counts

    def test_pending(self):
        self.assertFalse(slowplay.pending())
        slowplay.sync(True, on=True, c=FakeClient())
        self.assertTrue(slowplay.pending())


if __name__ == '__main__':
    unittest.main()
