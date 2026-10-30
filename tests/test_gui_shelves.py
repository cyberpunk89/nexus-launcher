"""Shelves you build (w-51) in the window: New shelf on the divider, the
inline builder with its live preview and autosave, rows in the Shelves view,
the Smart filter group, the ⋮ menu, hide / remove / bring back, the Home
widget, and prefs. Offscreen, CLI stubbed, library.json in a temp dir."""

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
from tests.test_gui_smart import facts_for, games  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import QEvent, QPoint
    from PyQt6.QtWidgets import QApplication, QPushButton
    from nexus import smart
    from nexus.gui.library import ShelfRow, SmartDivider


@unittest.skipIf(nx is None, "PyQt6 not available")
class UserShelves(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        set_gui('CliQuery', lambda *a, **k: None)

    @classmethod
    def tearDownClass(cls):
        set_gui('cli_json', cls._saved[0])
        set_gui('CliQuery', cls._saved[1])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'library.json'
        self._patch = mock.patch.object(nx.LibraryPrefs, 'PATH', self.path)
        self._patch.start()
        self.win = w = nx.MainWindow()
        w.prefs.dashboard = nx.dash_default()
        w.resize(1300, 850)
        w.show()
        idx = facts_for('play0', 'play1', 'new0')
        idx['games']['new0']['genres'] = ['Racing']
        w._facts_idx = idx
        w._smart_sig = None
        w._home_sig = None
        w._on_library({'games': games(), 'disks': []}, '')
        w.go(w.LIBRARY)
        w.set_view('home', save=False)
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self._patch.stop()
        self._tmp.cleanup()

    def divider(self):
        return next(x for x in self.win.home._smart if isinstance(x, SmartDivider))

    def rows(self):
        return [x.title for x in self.win.home._smart if isinstance(x, ShelfRow)]

    def filter_keys(self):
        f = self.win.filter
        return [f.itemData(i) for i in range(f.count())
                if str(f.itemData(i) or '').startswith('user:')]

    def make(self, name, *rules, match='all'):
        """A shelf through the builder, as a user would: New shelf, then edits."""
        w, ed = self.win, self.win.shelf_editor
        self.divider().btn_new.click()
        self.assertFalse(ed.isHidden())
        ed.name.setText(name)
        ed.name.textEdited.emit(name)
        for i, (field, op, value) in enumerate(rules):
            if i:
                ed.btn_add.click()
            row = ed.rows[i]
            row.field.setCurrentIndex(row.field.findData(field))
            row.op.setCurrentIndex(row.op.findData(op))
            if value is not None:
                row.value.setCurrentIndex(row.value.findData(value))
        if match != 'all':
            ed.match.set_current(match, emit=True)
        return w.prefs.user_shelves()[-1]

    def saved(self):
        self.win._shelf_save.timeout.emit()
        return json.loads(self.path.read_text())

    def test_make_a_shelf_preview_autosave_and_done(self):
        w, ed = self.win, self.win.shelf_editor
        s = self.make("Steam games", ('store', 'is', 'steam'))
        key = f"user:{s['id']}"
        # The grid is the live preview, and the count says the same.
        self.assertEqual(w.filter.currentData(), key)
        self.assertIs(w.views.currentWidget(), w.library)
        self.assertEqual(sorted(g['slug'] for g in w._shown_games), ['play0', 'play1', 'play2'])
        self.assertEqual(ed.count.text(), "3 games match")
        # A second rule narrows it as you go.
        ed.btn_add.click()
        row = ed.rows[1]
        row.field.setCurrentIndex(row.field.findData('genre'))
        row.value.setCurrentIndex(row.value.findData('RPG'))
        self.assertEqual(sorted(g['slug'] for g in w._shown_games), ['play0', 'play1'])
        self.assertEqual(ed.count.text(), "2 games match")
        self.assertFalse(ed.match_row.isHidden())
        # Saved without a Save button.
        data = self.saved()
        self.assertEqual(data['smart_shelves'][0]['name'], "Steam games")
        self.assertEqual(data['smart_shelves'][0]['rules'][1],
                         {'field': 'genre', 'op': 'is', 'value': 'RPG'})
        # Done: back to the shelves, with the new row there.
        ed.btn_done.click()
        self.assertTrue(ed.isHidden())
        self.assertEqual(w.filter.currentData(), 'all')
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertEqual(self.rows()[0], "Steam games")
        self.assertIn(key, self.filter_keys())

    def test_builder_only_offers_facts_that_exist(self):
        self.make("Mine", ('playtime', 'none', None))
        row = self.win.shelf_editor.rows[0]
        fields = [row.field.itemData(i) for i in range(row.field.count())]
        self.assertIn('genre', fields)                 # the index has genres
        self.assertNotIn('length', fields)             # but no lengths
        self.assertNotIn('size', fields)               # and no game has a size
        row.field.setCurrentIndex(row.field.findData('genre'))
        values = [row.value.itemData(i) for i in range(row.value.count())]
        self.assertEqual(values, ['RPG', 'Racing'])
        row.field.setCurrentIndex(row.field.findData('playtime'))
        self.assertTrue(row.value.isHidden())          # "none yet" needs no value
        self.assertTrue(row.btn_remove.isHidden())     # the last rule can't go

    def test_any_match_and_rename_keep_the_key(self):
        w, ed = self.win, self.win.shelf_editor
        s = self.make("Either", ('store', 'is', 'nexus'), ('genre', 'is', 'RPG'), match='any')
        self.assertEqual(len(w._shown_games), 5)
        ed.name.setText("Renamed")
        ed.name.textEdited.emit("Renamed")
        ed.btn_done.click()
        self.assertEqual(self.rows()[0], "Renamed")
        i = w.filter.findData(f"user:{s['id']}")
        self.assertEqual(w.filter.itemText(i).strip(), "Renamed")

    def test_empty_shelf_still_shows_saying_so(self):
        self.make("Nothing", ('store', 'is', 'steam'), ('store', 'is', 'nexus'))
        self.win.shelf_editor.btn_done.click()
        row = next(x for x in self.win.home._smart if isinstance(x, ShelfRow)
                   and x.title == "Nothing")
        self.assertTrue(row.body.isHidden())

    def test_menu_edit_hide_remove_and_bring_back(self):
        w = self.win
        s = self.make("Mine", ('store', 'is', 'steam'))
        key = f"user:{s['id']}"
        w.shelf_editor.btn_done.click()
        w.input.mode = 'pad'
        w._smart_menu(key, QPoint())
        texts = [b.text() for b in w.sheet.findChildren(QPushButton)
                 if b.objectName() == 'SheetRow' and b.isVisibleTo(w.sheet)]
        self.assertEqual(texts, ["See all", "Edit shelf", "Save as collection",
                                 "Hide this shelf", "Remove shelf"])
        w.sheet.close_sheet()
        w.input.mode = 'mouse'
        # Hide: off the Shelves view, still a filter.
        w._hide_smart(key)
        self.assertNotIn("Mine", self.rows())
        self.assertIn(key, self.filter_keys())
        self.assertEqual(self.divider().btn_hidden.text(), "1 hidden")
        w._unhide_smart(key)
        self.assertIn("Mine", self.rows())
        # Remove: gone everywhere, kept in the file, Undo brings it back.
        w._remove_shelf(s['id'])
        self.assertNotIn("Mine", self.rows())
        self.assertNotIn(key, self.filter_keys())
        self.assertTrue(self.saved()['smart_shelves'][0]['removed'])
        self.assertEqual(self.divider().btn_hidden.text(), "1 hidden")
        w._restore_shelf(s['id'])
        self.assertIn("Mine", self.rows())
        self.assertFalse(self.saved()['smart_shelves'][0]['removed'])

    def test_remove_from_the_builder_closes_it(self):
        w, ed = self.win, self.win.shelf_editor
        self.make("Gone", ('store', 'is', 'steam'))
        ed.btn_remove.click()
        self.assertTrue(ed.isHidden())
        self.assertEqual(w.filter.currentData(), 'all')
        self.assertEqual(w.prefs.user_shelves(), [])

    def test_edit_button_next_to_the_filter(self):
        w, ed = self.win, self.win.shelf_editor
        s = self.make("Mine", ('store', 'is', 'steam'))
        ed.btn_done.click()
        w._show_filter(f"user:{s['id']}")
        self.assertFalse(w.btn_edit_shelf.isHidden())
        w.btn_edit_shelf.click()
        self.assertFalse(ed.isHidden())
        self.assertTrue(w.btn_edit_shelf.isHidden())
        w._escape()                                    # B on a pad: done
        self.assertTrue(ed.isHidden())

    def test_save_as_collection(self):
        w = self.win
        s = self.make("Steamy", ('store', 'is', 'steam'))
        w.shelf_editor.btn_done.click()
        w._save_smart(f"user:{s['id']}")
        self.assertEqual(sorted(w.prefs.collections["Steamy"]), ['play0', 'play1', 'play2'])

    def test_home_widget_can_show_your_shelf(self):
        w = self.win
        s = self.make("Steamy", ('store', 'is', 'steam'))
        w.shelf_editor.btn_done.click()
        dash = w.dashboard
        dash.set_editing(True)
        for _ in range(len(smart.WIDGET_RULES) + 1):
            it = next(i for i in w.prefs.dashboard if i['id'] == 'smart')
            if it['shelf'] == f"user:{s['id']}":
                break
            dash._edit('smart', 'shelf')
        it = next(i for i in w.prefs.dashboard if i['id'] == 'smart')
        self.assertEqual(it['shelf'], f"user:{s['id']}")
        self.assertEqual(dash.widgets['smart'].btn_shelf.text(), "Shelf: Steamy")
        self.assertEqual(sorted(g['slug'] for g in dash.widgets['smart'].games(w)),
                         ['play0', 'play1', 'play2'])
        dash.set_editing(False)
        # Removed: Home falls back to the default rule, and says so.
        w._remove_shelf(s['id'])
        self.assertEqual(dash.widgets['smart'].key(w), 'smart:unfinished')
        items = nx.dash_normalize(w.prefs.dashboard)            # the choice is kept
        self.assertEqual(next(i for i in items if i['id'] == 'smart')['shelf'],
                         f"user:{s['id']}")

    def test_prefs_round_trip_keeps_shelves_and_unknown_keys(self):
        self.path.write_text(json.dumps({
            'smart_shelves': [{'id': 'k1', 'name': 'Kept', 'rules': [
                {'field': 'store', 'op': 'is', 'value': 'steam'}]}, {'bad': 1}],
            'smart_hidden': ['user:k1', 'user:', 'nope'], 'future_key': {'x': 1}}))
        p = nx.LibraryPrefs()
        self.assertEqual([s['id'] for s in p.smart_shelves], ['k1'])
        self.assertEqual(p.smart_hidden, ['user:k1'])
        p.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(data['future_key'], {'x': 1})
        self.assertEqual(data['smart_shelves'][0]['name'], 'Kept')
        p2 = nx.LibraryPrefs()
        made = p2.new_shelf(['genre'])
        self.assertEqual(made['rules'][0]['field'], 'genre')
        self.assertNotEqual(made['id'], 'k1')
        self.assertIsNone(p2.update_shelf({'id': 'unknown'}))


if __name__ == '__main__':
    unittest.main()
