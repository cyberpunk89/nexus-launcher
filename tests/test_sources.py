"""The extra-store sources (nexus/sources): Epic's manifests read from
synthetic fixture files, and a broken or missing store never breaks the
library."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexus import sources
from nexus.sources import epic

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'sources' / 'epic'


class EpicSource(unittest.TestCase):
    def test_normal_manifests_list_the_installed_games(self):
        src = epic.Epic(FIXTURES / 'normal' / 'Manifests')
        self.assertTrue(src.available())
        games = src.installed()
        self.assertEqual([g['slug'] for g in games], ['epic-Fable', 'epic-Comet'])  # no DLC, no half install
        fable = games[0]
        self.assertEqual(fable['source'], 'epic')
        self.assertEqual(fable['display_name'], 'Sample Fable')
        self.assertTrue(fable['installed'])
        self.assertEqual(fable['disk_bytes'], 1234567)
        self.assertEqual(fable['launcher'], 'com.epicgames.launcher://apps/nsfable%3Aitemfable%3AFable'
                                            '?action=launch&silent=true')
        self.assertTrue(fable['exe'].endswith('Fable.exe'))
        self.assertEqual(src.health(), {'name': 'epic', 'label': 'Epic Games', 'ok': True,
                                        'games': 2, 'message': None})

    def test_an_empty_install_lists_nothing(self):
        src = epic.Epic(FIXTURES / 'empty' / 'Manifests')
        self.assertTrue(src.available())
        self.assertEqual(src.installed(), [])
        self.assertTrue(src.health()['ok'])

    def test_corrupt_or_changed_manifests_are_skipped_and_reported(self):
        src = epic.Epic(FIXTURES / 'broken' / 'Manifests')
        with self.assertLogs('nexus.sources.epic', 'WARNING'):
            games = src.installed()
        self.assertEqual([g['slug'] for g in games], ['epic-Fable'])
        h = src.health()
        self.assertFalse(h['ok'])
        self.assertEqual(h['message'], "Couldn't read Epic's install list "
                                       "(the launcher may have changed its format)")

    def test_the_same_folder_twice_shows_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('AAA1.item', 'BBB2.item'):
                shutil.copy(FIXTURES / 'normal' / 'Manifests' / 'AAA1.item', Path(tmp, name))
            text = Path(tmp, 'BBB2.item').read_text(encoding='utf-8')
            Path(tmp, 'BBB2.item').write_text(text.replace('D:\\\\Epic Games', 'd:/epic games'),
                                              encoding='utf-8')
            self.assertEqual(len(epic.Epic(Path(tmp)).installed()), 1)

    def test_not_available_without_the_launcher(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(epic.default_manifests())
            self.assertFalse(epic.Epic().available())
        self.assertFalse(epic.Epic(FIXTURES / 'missing').available())

    def test_the_windows_manifest_folder(self):
        with mock.patch.dict(os.environ, {'ProgramData': 'C:\\ProgramData'}, clear=True):
            self.assertEqual(epic.default_manifests(),
                             Path('C:\\ProgramData', 'Epic', 'EpicGamesLauncher', 'Data', 'Manifests'))


class Hook(unittest.TestCase):
    def test_only_available_sources_count(self):
        here, gone = epic.Epic(FIXTURES / 'normal' / 'Manifests'), epic.Epic(FIXTURES / 'missing')
        self.assertEqual(sources.sources([gone, here]), [here])
        self.assertEqual(len(sources.installed_games([gone, here])), 2)

    def test_a_source_that_throws_is_skipped(self):
        class Bad:
            name = 'bad'

            def available(self):
                return True

            def installed(self):
                raise RuntimeError('boom')

            def health(self):
                return {}

        good = epic.Epic(FIXTURES / 'normal' / 'Manifests')
        with self.assertLogs('nexus.sources', 'ERROR'):
            games = sources.installed_games([Bad(), good])
        self.assertEqual(len(games), 2)

    def test_linux_has_no_epic(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sources.sources(), [])


if __name__ == '__main__':
    unittest.main()
