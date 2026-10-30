"""Collection polish (w-52): each collection's icon, colour, place and top-bar
pin. Prefs round trip and migration from older library.json files, then the
chips, the collection sheet, menus and the top bar, offscreen."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import set_gui  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication, QLabel
    from nexus.icons import COLLECTION_ICONS


def fake_games():
    return [{'slug': s, 'display_name': s.title(), 'source': 'nexus', 'launcher': f'/{s}'}
            for s in ('alpha', 'beta', 'gamma')]


@unittest.skipIf(nx is None, "PyQt6 not available")
class Prefs(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / 'library.json'
        p = mock.patch.object(nx.LibraryPrefs, 'PATH', self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_an_older_library_json_loads_and_saves_unchanged(self):
        old = {'collections': {'Couch': ['a', 'b'], 'Short': []}, 'favorites': ['a'],
               'future_key': {'x': 1}}
        self.path.write_text(json.dumps(old))
        p = nx.LibraryPrefs()
        self.assertEqual(p.collections, {'Couch': ['a', 'b'], 'Short': []})
        self.assertEqual(p.collection_look, {})
        self.assertIsNone(nx.collection_look('Couch'))           # looks as it always did
        p.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(data['collections'], old['collections'])
        self.assertEqual(list(data['collections']), ['Couch', 'Short'])
        self.assertEqual(data['future_key'], {'x': 1})
        self.assertEqual(data['collection_look'], {})

    def test_theatre_library_defaults_bad_values_and_round_trip(self):
        self.assertEqual(nx.LibraryPrefs().theatre_library, {'sort': 'name', 'show': 'installed'})
        self.assertFalse(nx.LibraryPrefs().theatre_hide_gamedb)
        self.path.write_text(json.dumps({'theatre_library': {'sort': 'nope', 'show': 'all'},
                                         'library_state': {'sort': 'most'}}))
        p = nx.LibraryPrefs()
        self.assertEqual(p.theatre_library, {'sort': 'name', 'show': 'all'})
        p.theatre_library = {'sort': 'collection', 'show': 'missing'}
        p.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(data['theatre_library'], {'sort': 'collection', 'show': 'missing'})
        self.assertEqual(data['library_state'], {'sort': 'most'})   # the desktop's, untouched
        self.assertEqual(nx.LibraryPrefs().theatre_library,
                         {'sort': 'collection', 'show': 'missing'})

    def test_theatre_controls_defaults_bad_values_and_unknown_kept(self):
        """w-63: Theatre's controls keys default, fall back on junk, and a
        newer version's controls keys survive a save."""
        c = nx.LibraryPrefs().controls
        self.assertEqual({k: c[k] for k in ('theatre_start', 'theatre_on_pad', 'sounds',
                                            'volume', 'week_home')},
                         {'theatre_start': 'never', 'theatre_on_pad': False, 'sounds': False,
                          'volume': 50, 'week_home': True})
        for bad in ('loud', None, True, [], float('nan')):
            self.path.write_text(json.dumps({'controls': {'volume': bad}}))
            self.assertEqual(nx.LibraryPrefs().controls['volume'], 50, bad)
        self.path.write_text(json.dumps({'controls': {
            'theatre_start': 'tv-ish', 'theatre_on_pad': 'yes', 'sounds': 1, 'volume': 147,
            'week_home': 0, 'future': {'x': 1}}}))
        p = nx.LibraryPrefs()
        self.assertEqual(p.controls['theatre_start'], 'never')
        self.assertFalse(p.controls['theatre_on_pad'])          # only a real true
        self.assertFalse(p.controls['sounds'])
        self.assertEqual(p.controls['volume'], 100)              # clamped
        self.assertTrue(p.controls['week_home'])                 # only a real false hides it
        self.path.write_text(json.dumps({'controls': {'volume': 34, 'theatre_start': 'tv',
                                                      'future': {'x': 1}}}))
        p = nx.LibraryPrefs()
        self.assertEqual(p.controls['volume'], 30)               # steps of 10
        self.assertEqual(p.controls['theatre_start'], 'tv')
        p.controls = {**p.controls, 'sounds': True}
        p.save()
        data = json.loads(self.path.read_text())['controls']
        self.assertEqual(data['future'], {'x': 1})
        self.assertTrue(data['sounds'])
        self.assertEqual(data['volume'], 30)

    def test_look_order_and_pin_round_trip(self):
        p = nx.LibraryPrefs()
        for n in ('Couch', 'Short', 'Spooky'):
            p.add_collection(n)
        self.assertTrue(p.set_collection_look('Couch', icon='sofa', color='foam', pinned=True))
        self.assertTrue(p.set_collection_look('Spooky', icon='ghost'))
        self.assertTrue(p.move_collection('Spooky', -1))
        self.assertFalse(p.move_collection('Couch', -1))           # already first
        self.assertFalse(p.set_collection_look('Nope', icon='sofa'))
        q = nx.LibraryPrefs()
        self.assertEqual(list(q.collections), ['Couch', 'Spooky', 'Short'])
        self.assertEqual(q.look_of('Couch'), {'icon': 'sofa', 'color': 'foam', 'pinned': True})
        self.assertEqual(q.look_of('Spooky'), {'icon': 'ghost'})
        self.assertEqual(q.pinned_collections(), ['Couch'])
        # None puts a choice back to the default; nothing chosen, no entry.
        q.set_collection_look('Spooky', icon=None)
        self.assertNotIn('Spooky', nx.LibraryPrefs().collection_look)

    def test_rename_carries_the_look_and_delete_drops_it(self):
        p = nx.LibraryPrefs()
        p.add_collection('Couch')
        p.set_collection_look('Couch', color='love', pinned=True)
        self.assertEqual(p.rename_collection('Couch', 'Sofa'), 'Sofa')
        q = nx.LibraryPrefs()
        self.assertEqual(q.look_of('Sofa'), {'color': 'love', 'pinned': True})
        self.assertNotIn('Couch', json.loads(self.path.read_text())['collection_look'])
        q.delete_collection('Sofa')
        self.assertEqual(json.loads(self.path.read_text())['collection_look'], {})

    def test_bad_hand_edits_are_dropped_and_new_keys_kept(self):
        self.path.write_text(json.dumps({
            'collections': {'Couch': [], 'Short': [], 'A': [], 'B': []},
            'collection_look': {
                'couch': {'icon': 'sofa', 'color': '#123456', 'pinned': 'yes', 'sort': 'name'},
                'Short': {'icon': 'not-an-icon', 'color': 'gold', 'pinned': True},
                'A': {'pinned': True}, 'B': {'pinned': True},
                'Gone': {'icon': 'sofa'}, 'Bad': 'x'}}))
        p = nx.LibraryPrefs()
        # Matched ignoring case; a newer Nexus's 'sort' survives.
        self.assertEqual(p.look_of('Couch'), {'icon': 'sofa', 'sort': 'name'})
        self.assertEqual(p.look_of('Short'), {'color': 'gold', 'pinned': True})
        self.assertNotIn('Gone', p.collection_look)
        self.assertEqual(p.pinned_collections(), ['Short', 'A', 'B'])

    def test_no_more_pins_than_fit(self):
        p = nx.LibraryPrefs()
        for n in 'ABCD':
            p.add_collection(n)
        for n in 'ABC':
            self.assertTrue(p.set_collection_look(n, pinned=True))
        self.assertFalse(p.set_collection_look('D', pinned=True))
        self.assertTrue(p.set_collection_look('A', pinned=True))     # already pinned: fine
        self.assertEqual(p.pinned_collections(), ['A', 'B', 'C'])
        # A file with too many pins (hand-edited) keeps the first ones.
        data = json.loads(self.path.read_text())
        data['collection_look']['D'] = {'pinned': True}
        self.path.write_text(json.dumps(data))
        self.assertEqual(nx.LibraryPrefs().pinned_collections(), ['A', 'B', 'C'])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Window(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        nx.LibraryPrefs.save = lambda self: None
        set_gui('CliQuery', lambda *a, **k: None)

    @classmethod
    def tearDownClass(cls):
        set_gui('cli_json', cls._saved[0])
        nx.LibraryPrefs.save = cls._saved[1]
        set_gui('CliQuery', cls._saved[2])

    def setUp(self):
        self.win = nx.MainWindow()
        self.win.prefs.collections = {}
        self.win.prefs.collection_look = {}
        self.win._collections_changed()
        self.win.resize(1400, 850)
        self.win.show()
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        nx.COLLECTION_NAMES[:] = []
        nx.COLLECTION_LOOK.clear()

    def chip_order(self):
        lay = self.win._col_chips
        return [lay.itemAt(i).widget().name for i in range(lay.count())]

    def test_chip_wears_its_icon_and_colour(self):
        w = self.win
        w._create_collection('Couch', 'alpha')
        chip = w.chips['col:Couch']
        self.assertTrue(chip.icon().isNull())                    # nothing chosen: as before
        w._set_collection_look('Couch', 'icon', 'sofa')
        self.assertIs(w.chips['col:Couch'], chip)                 # kept, not rebuilt
        self.assertFalse(chip.icon().isNull())
        self.assertEqual(chip._glyph_spec[:3], (COLLECTION_ICONS['sofa'], 14, 'subtext'))
        w._set_collection_look('Couch', 'color', 'love')
        self.assertEqual(chip._glyph_spec[2], 'col_love')
        self.assertEqual(nx.COLLECTION_LOOK['Couch'], {'icon': 'sofa', 'color': 'love'})
        w._set_collection_look('Couch', 'icon', None)             # colour alone: bookmark
        self.assertEqual(chip._glyph_spec[0], COLLECTION_ICONS['bookmark'])

    def test_menus_and_home_use_the_look(self):
        w = self.win
        w._create_collection('Couch', 'alpha')
        w._create_collection('Short')
        w._set_collection_look('Short', 'color', 'gold')
        g = w.game('alpha')
        m = nx.build_game_menu(w, g, lambda *a: None)
        sub = next(a.menu() for a in m.actions() if a.menu() and a.text() == "Collections")
        acts = {a.text(): a for a in sub.actions()}
        self.assertTrue(acts['Couch'].icon().isNull())
        self.assertFalse(acts['Short'].icon().isNull())
        m.deleteLater()
        w.go(w.HOME)
        w._set_collection_look('Couch', 'icon', 'sofa')
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)   # old rows
        rows = [r for r in w.dashboard.findChildren(nx.ShelfRow) if r.title == 'Couch']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        glyphs = [lab.text() for lab in row.findChildren(QLabel)
                  if getattr(lab, '_icon_spec', None)]
        self.assertIn(COLLECTION_ICONS['sofa'], glyphs)

    def test_move_up_and_down(self):
        w = self.win
        for n in ('A', 'B', 'C'):
            w._create_collection(n)
        chip_b = w.chips['col:B']
        w._move_collection('C', -1)
        self.assertEqual(self.chip_order(), ['A', 'C', 'B'])
        self.assertEqual(nx.COLLECTION_NAMES, ['A', 'C', 'B'])
        self.assertIs(w.chips['col:B'], chip_b)
        # Ctrl+→ on a focused chip moves it; from the sheet it opens again on
        # the same row, so A keeps moving it.
        QApplication.sendEvent(w.chips['col:A'], QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier))
        self.assertEqual(self.chip_order(), ['C', 'A', 'B'])
        w._move_collection('A', 1, sheet=True)
        self.assertEqual(self.chip_order(), ['C', 'B', 'A'])
        self.assertTrue(w.sheet.isVisible())
        self.assertEqual(QApplication.focusWidget().text(), "Move up")   # at the end now
        w.sheet.close_sheet()

    def test_sheet_picks_colour_and_icon_inline(self):
        w = self.win
        w._create_collection('Couch')
        w._manage_collection('Couch')
        self.assertTrue(w.sheet.isVisible())
        picker = w.sheet.findChild(nx.CollectionLookPicker)
        self.assertIsNotNone(picker)
        picker.swatches['foam'].click()
        picker.icons['sofa'].click()
        self.assertTrue(w.sheet.isVisible())                     # stays open
        self.assertEqual(w.prefs.look_of('Couch'), {'color': 'foam', 'icon': 'sofa'})
        self.assertTrue(picker.icons['sofa'].isChecked())
        self.assertFalse(picker.icons['bookmark'].isChecked())
        self.assertEqual(w.chips['col:Couch']._glyph_spec[2], 'col_foam')
        picker.swatches[None].click()
        self.assertEqual(w.prefs.look_of('Couch'), {'icon': 'sofa'})
        w.sheet.close_sheet()

    def test_pin_to_the_top_bar(self):
        w = self.win
        bar = w.topbar
        self.assertEqual(bar.pins, [])
        self.assertTrue(bar.pin_sep.isHidden())
        w._create_collection('Couch co-op night', 'alpha')
        w._pin_collection('Couch co-op night', True)
        self.assertEqual([b.name for b in bar.pins], ['Couch co-op night'])
        self.assertFalse(bar.pin_sep.isHidden())
        pin = bar.pins[0]
        self.assertTrue(pin.text().endswith("…"))                # a long name is cut
        w.go(w.HOME)
        w.search.setText("zzz")
        pin.click()
        self.assertEqual(w.current(), w.LIBRARY)
        self.assertEqual(w._chip, 'col:Couch co-op night')
        self.assertEqual(w.search.text(), "")
        self.assertEqual([g['slug'] for g in w._shown_games], ['alpha'])
        self.assertTrue(pin.isChecked())
        w.go(w.HOME)
        self.assertFalse(pin.isChecked())
        # Rename keeps the pin (the same button), delete takes it away.
        w._rename_collection('Couch co-op night', 'Couch')
        self.assertEqual([b.name for b in bar.pins], ['Couch'])
        w._delete_collection('Couch')
        self.assertEqual(bar.pins, [])

    def test_pins_fold_to_icons_on_a_narrow_window(self):
        w = self.win
        for n in ('A', 'B', 'C', 'D'):
            w._create_collection(n)
            w._pin_collection(n, True)
        self.assertEqual([b.name for b in w.topbar.pins], ['A', 'B', 'C'])    # D didn't fit
        self.assertTrue(all(b.text() for b in w.topbar.pins))
        for width in (1100, 900):
            w.resize(width, 700)
            QApplication.processEvents()
            self.assertTrue(all(not b.text() and b.width() == b.H for b in w.topbar.pins))
            # Everything in the bar still fits: nothing pushed past its right edge.
            self.assertLessEqual(w.btn_install.geometry().right(), w.topbar.width())
            self.assertGreaterEqual(w.search.width(), w.search.minimumWidth())
        w.resize(1400, 850)
        QApplication.processEvents()
        self.assertTrue(all(b.text() for b in w.topbar.pins))


if __name__ == '__main__':
    unittest.main()
