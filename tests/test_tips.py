"""Tests for nexus/tips.py recipe filtering and launch-string building."""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from nexus import tips  # noqa: E402

RECIPES = [
    {'name': 'Any GPU', 'kind': 'profile', 'gpu': 'any', 'env': [['A', '1']], 'wrappers': []},
    {'name': 'NVIDIA only', 'kind': 'profile', 'gpu': 'nvidia', 'env': [['N', '1']]},
    {'name': 'RDNA4', 'kind': 'profile', 'gpu': 'amd', 'needs': ['rdna4'],
     'env': [['PROTON_FSR4_UPGRADE', '1']], 'wrappers': [['gamemoderun', '']]},
    {'name': 'Crash fix', 'kind': 'fix', 'gpu': 'any', 'protondb_tiers': ['bronze'],
     'env': [['X', 'y z']]},
    {'name': 'HDR', 'kind': 'profile', 'gpu': 'any', 'needs': ['hdr'], 'env': [['H', '1']]},
    {'name': 'Scope', 'kind': 'profile', 'gpu': 'any',
     'wrappers': [['gamescope', '-W 2560 -H 1440']]},
]
HW = {'gpus': [], 'vendors': ['amd'], 'hybrid': False,
      'features': {'wayland': True, 'kde': False, 'ntsync': True, 'rdna3': False,
                   'rdna4': True, 'fsr4': True}}


class Recipes(unittest.TestCase):
    def names(self, tier=None):
        with mock.patch.object(tips, 'load_recipes', return_value=(RECIPES, 'test')):
            return [r['name'] for r in tips.recipes_for(HW, tier)]

    def test_filters_by_gpu_features_and_tier(self):
        self.assertEqual(self.names(), ['Any GPU', 'RDNA4', 'HDR', 'Scope'])
        self.assertIn('Crash fix', self.names('bronze'))
        self.assertNotIn('Crash fix', self.names('platinum'))

    def test_unknown_needs_are_flagged_not_dropped(self):
        with mock.patch.object(tips, 'load_recipes', return_value=(RECIPES, 'test')):
            hdr = next(r for r in tips.recipes_for(HW) if r['name'] == 'HDR')
        self.assertEqual(hdr['needs_unverified'], ['hdr'])

    def test_launch_string(self):
        self.assertEqual(tips.recipe_launch_string(RECIPES[2]),
                         'PROTON_FSR4_UPGRADE=1 gamemoderun %command%')
        self.assertEqual(tips.recipe_launch_string(RECIPES[3]), "X='y z' %command%")
        self.assertEqual(tips.recipe_launch_string(RECIPES[5]),
                         'gamescope -W 2560 -H 1440 -- %command%')

    def test_bundled_catalog_parses(self):
        import tomllib
        data = tomllib.loads(tips.BUNDLED_RECIPES.read_text())
        self.assertGreater(len(data['recipe']), 5)

    def test_system_checks_shape(self):
        for c in tips.system_checks(HW):
            self.assertEqual(c['group'], 'Performance')
            self.assertIn(c['status'], ('ok', 'warn', 'fail', 'info'))


if __name__ == '__main__':
    unittest.main()
