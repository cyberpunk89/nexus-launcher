"""The game's rail in Theatre's Ribbon layout (w-145), the pure part: which
glass cards the rail shows with their counts, which actions fill the small
grid (only what the game's own options sheet offers, on Linux and with the
Windows feature set), and what the preview pane says for a section. No
window."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)

try:
    from nexus import features
    from nexus.gui.actions import game_actions
    from nexus.gui.game_rail import GameInfo, preview, rail
except ImportError:            # PyQt6 not installed
    game_actions = None

ALL_SECTIONS = ['overview', 'friends', 'media', 'news', 'achievements', 'compat']


def info(**kw):
    base = dict(rec={}, store=None, tips=None, news=[], owners=[], ach=None)
    return GameInfo(**{**base, **kw})


REPACK = {'slug': 'alpha', 'display_name': "Alpha", 'source': 'nexus', 'launcher': '/a',
          'installed': True, 'playtime_minutes': 25 * 60, 'last_played': '2026-11-14T10:00:00'}
STEAM = {'slug': 'steam-1', 'display_name': "Beta", 'source': 'steam', 'appid': 1,
         'launcher': 'steam://rungameid/1', 'installed': True}
FULL = info(rec={'length': {'main': 55}, 'description': "Eight travellers set out.",
                 'genres': ["RPG"], 'modes': ['single']},
            store={'store': {'trailers': [{'url': 'https://x/1.mp4'}, {'url': 'https://x/2.mp4'},
                                          {'url': 'https://x/3.mp4'}, {'url': 'http://no'}]}},
            tips={'protondb': {'tier': 'platinum'}},
            owners=[{'steamid': '1', 'name': "Ana"}, {'steamid': '2', 'name': "Ben"}],
            ach=(18, 47))


@unittest.skipIf(game_actions is None, "PyQt6 not available")
class Rail(unittest.TestCase):
    def test_sections_follow_visible_keys_with_counts(self):
        cards, _grid = rail(['overview', 'achievements', 'media', 'friends', 'news', 'compat'],
                            FULL)
        self.assertEqual([c.key for c in cards],
                         ['overview', 'achievements', 'media', 'friends', 'news', 'compat'])
        counts = {c.key: c.count for c in cards}
        self.assertEqual(counts, {'overview': '', 'achievements': "18 of 47",
                                  'media': "3 trailers", 'friends': "2 own it", 'news': '',
                                  'compat': "Platinum"})
        self.assertEqual([c.title for c in cards][:3], ["Overview", "Achievements", "Media"])
        cards, _ = rail(['overview', 'compat'], FULL)              # what the drawer hides goes
        self.assertEqual([c.key for c in cards], ['overview', 'compat'])
        one = info(store={'store': {'trailers': [{'url': 'https://x/1.mp4'}]}},
                   owners=[{'steamid': '1'}])
        counts = {c.key: c.count for c in rail(['media', 'friends'], one)[0]}
        self.assertEqual(counts, {'media': "1 trailer", 'friends': "1 owns it"})

    def check_grid(self, g):
        acts = game_actions(g, theatre=True)
        _cards, grid = rail(ALL_SECTIONS, info(), acts)
        keys = {a.key for a in acts}
        subs = {a.sub for a in acts}
        want = [k for k, ok in (('favorite', 'favorite' in keys),
                                ('collections', "Collections" in subs),
                                ('maintenance', "Maintenance" in subs),
                                ('uninstall', bool(keys & {'uninstall', 'steam-uninstall'})))
                if ok]
        self.assertEqual([c.key for c in grid], want, g['slug'])
        un = next((c for c in grid if c.key == 'uninstall'), None)
        if un is not None:
            self.assertTrue(un.danger)
            self.assertEqual(un.action, next(a.key for a in acts
                                             if a.key in ('uninstall', 'steam-uninstall')))
        return grid

    def test_actions_from_game_actions(self):
        with mock.patch.object(features, 'FEATURES', features.ALL):
            grid = self.check_grid(REPACK)
            self.assertEqual([c.key for c in grid],
                             ['favorite', 'collections', 'maintenance', 'uninstall'])
            self.check_grid(STEAM)
            fav = self.check_grid(dict(REPACK, favorite=True))[0]
            self.assertTrue(fav.checked)
            # Not installed: nothing to uninstall or maintain
            gone = self.check_grid(dict(STEAM, installed=False))
            self.assertNotIn('uninstall', [c.key for c in gone])
        windows = features.ALL - features.LINUX_ONLY
        with mock.patch.object(features, 'FEATURES', windows):     # the Windows feature set
            self.check_grid(REPACK)
            self.check_grid(STEAM)
        self.assertEqual(rail(ALL_SECTIONS, info())[1], [])        # no actions given: no grid


@unittest.skipIf(game_actions is None, "PyQt6 not available")
class Preview(unittest.TestCase):
    def test_overview_progress_and_about(self):
        pv = preview('overview', REPACK, FULL)
        self.assertEqual(pv.kicker, "YOUR PROGRESS")
        self.assertEqual(pv.big, "25.0 h played")
        self.assertEqual(pv.progress, (25 / 55, "25 h of about 55 h to finish"))
        self.assertIn("About 55 h for the main story", pv.line)
        self.assertIn("Last played", pv.line)
        self.assertEqual(pv.about, "Eight travellers set out.")
        self.assertEqual(pv.hint, "Press A for the full Overview")
        ach = preview('achievements', REPACK, FULL)
        self.assertEqual((ach.big, ach.progress[0]), ("18 of 47 unlocked", 18 / 47))
        self.assertEqual(preview('media', REPACK, FULL).big, "3 trailers")
        self.assertEqual(preview('friends', REPACK, FULL).line, "Ana and Ben")
        self.assertEqual(preview('compat', REPACK, FULL).big, "ProtonDB Platinum")
        about = info(store={'store': {'blurb': "From the store."}})    # no description: the blurb
        self.assertEqual(preview('overview', REPACK, about).about, "From the store.")

    def test_no_data_no_count(self):
        bare = dict(REPACK, playtime_minutes=0, last_played=None)
        cards, _ = rail(ALL_SECTIONS, info())
        self.assertTrue(all(c.count == '' for c in cards))
        pv = preview('overview', bare, info())
        self.assertEqual(pv.big, "Not played yet")
        self.assertIsNone(pv.progress)                            # no length, no play
        self.assertEqual(pv.about, '')                            # no About block
        self.assertIsNone(preview('overview', REPACK, info()).progress)   # played, no length
        self.assertIsNone(preview('achievements', bare, info()).progress)


if __name__ == '__main__':
    unittest.main()
