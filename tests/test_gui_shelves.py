"""Shelves you build (w-51) in the window: New shelf on the divider, the
shelf modal (w-67: templates, rules, match count and covers, saved only on
Save), rows in the Shelves view, the Smart filter group, the ⋮ menu, hide /
remove / bring back, the Home widget, and prefs. Offscreen, CLI stubbed,
library.json in a temp dir."""

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
    from PyQt6.QtTest import QTest

if nx is not None:
    from PyQt6.QtCore import QEvent, QPoint, Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication, QPushButton
    from nexus import smart
    from nexus.features import has
    from nexus.gui.library import ShelfRow, SmartDivider
    from nexus.gui.shelfedit import ShelfForm


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

    def form(self) -> 'ShelfForm':
        """The open shelf modal's body (a closed one's waits for deleteLater)."""
        m = self.win.modal
        self.assertTrue(m.isVisible())
        return next(f for f in m.findChildren(ShelfForm) if not f.isHidden())

    def fill(self, f, name, *rules, match='all'):
        f.name.setText(name)
        for i, (field, op, value) in enumerate(rules):
            if i:
                f.btn_add.click()
            row = f.rows[i]
            row.field.setCurrentIndex(row.field.findData(field))
            row.op.setCurrentIndex(row.op.findData(op))
            if value is not None:
                row.value.setCurrentIndex(row.value.findData(value))
        if match != 'all':
            f.match.set_current(match, emit=True)

    def make(self, name, *rules, match='all'):
        """A shelf through the modal, as a user would: New shelf, the rules,
        Create shelf."""
        self.divider().btn_new.click()
        self.fill(self.form(), name, *rules, match=match)
        self.win.modal.primary.click()
        return self.win.prefs.user_shelves()[-1]

    def saved(self):
        return json.loads(self.path.read_text())

    def on_disk(self) -> bytes:
        self.win.prefs.save()
        return self.path.read_bytes()

    def key(self, key):
        w = QApplication.focusWidget() or self.win.modal
        QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, key,
                                            Qt.KeyboardModifier.NoModifier))
        QApplication.processEvents()

    def test_new_shelf_modal_count_strip_and_create(self):
        w, m = self.win, self.win.modal
        before = self.on_disk()
        self.divider().btn_new.click()
        f = self.form()
        self.assertEqual(m.title.text(), "New shelf")
        self.assertEqual(m.primary.text(), "Create shelf")
        self.assertTrue(m.btn_extra.isHidden())               # no Remove on a new one
        self.assertLessEqual(m.card.width(), 600)
        self.assertFalse(m.isWindow())
        self.assertIs(QApplication.focusWidget(), f.name)
        self.fill(f, "Steam games", ('store', 'is', 'steam'))
        # The count and the covers follow the rules; nothing is saved yet,
        # and the Library behind keeps its filter.
        self.assertEqual(f.count.text(), "3 games match")
        self.assertEqual(sorted(f._strip_slugs), ['play0', 'play1', 'play2'])
        f.btn_add.click()
        row = f.rows[1]
        self.assertIs(QApplication.focusWidget(), row.field)
        row.field.setCurrentIndex(row.field.findData('genre'))
        row.value.setCurrentIndex(row.value.findData('RPG'))
        self.assertEqual(f.count.text(), "2 games match")
        self.assertEqual(sorted(f._strip_slugs), ['play0', 'play1'])
        self.assertFalse(f.match_row.isHidden())
        self.assertEqual(w.filter.currentData(), 'all')
        self.assertEqual(self.on_disk(), before)
        self.assertEqual(w.prefs.smart_shelves, [])
        # Create shelf: one write, the row first after the divider.
        with mock.patch.object(w.prefs, 'save', wraps=w.prefs.save) as save:
            m.primary.click()
        self.assertEqual(save.call_count, 1)
        self.assertFalse(m.isVisible())
        data = self.saved()['smart_shelves']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], "Steam games")
        self.assertEqual(data[0]['rules'][1], {'field': 'genre', 'op': 'is', 'value': 'RPG'})
        self.assertEqual(self.rows()[0], "Steam games")
        self.assertIn(f"user:{data[0]['id']}", self.filter_keys())
        self.assertIs(w.views.currentWidget(), w.home)

    def test_cancel_new_shelf_creates_nothing(self):
        w, m = self.win, self.win.modal
        before = self.on_disk()
        for how in ('esc', 'b', 'cancel', 'close', 'scrim'):
            self.divider().btn_new.click()
            f = self.form()
            self.fill(f, "Nope", ('store', 'is', 'steam'))
            if how == 'esc':
                self.key(Qt.Key.Key_Escape)
            elif how == 'b':
                w._escape()                                   # a pad's B
            elif how == 'cancel':
                m.btn_cancel.click()
            elif how == 'close':
                m.btn_close.click()
            else:
                # A stray click on the scrim keeps rule edits; a second
                # modal without edits goes.
                QTest.mouseClick(m, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
                self.assertTrue(m.isVisible())
                m.back()
                self.divider().btn_new.click()
                QTest.mouseClick(m, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
            self.assertFalse(m.isVisible(), how)
            self.assertEqual(w.prefs.smart_shelves, [], how)
            self.assertNotIn("Nope", self.rows(), how)
        self.assertEqual(self.on_disk(), before)

    def test_edit_cancel_leaves_library_json_untouched(self):
        w, m = self.win, self.win.modal
        s = self.make("Mine", ('store', 'is', 'steam'))
        before, kept = self.on_disk(), [dict(x) for x in w.prefs.smart_shelves]
        w._edit_shelf(s['id'])
        f = self.form()
        self.assertEqual(m.title.text(), "Edit shelf")
        self.assertEqual(f.name.text(), "Mine")
        self.assertEqual(f.templates, {})                      # Start from is for new ones
        self.fill(f, "Changed", ('store', 'is', 'nexus'), ('genre', 'is', 'RPG'), match='any')
        w._escape()
        self.assertFalse(m.isVisible())
        self.assertEqual(w.prefs.smart_shelves, kept)
        self.assertEqual(self.on_disk(), before)
        self.assertIn("Mine", self.rows())
        # Save writes the edit, under the same key.
        w._edit_shelf(s['id'])
        self.fill(self.form(), "Changed", ('store', 'is', 'nexus'))
        m.primary.click()
        self.assertEqual(self.saved()['smart_shelves'][0]['name'], "Changed")
        self.assertEqual(self.saved()['smart_shelves'][0]['id'], s['id'])
        self.assertEqual(self.rows()[0], "Changed")

    def test_templates_fill_rules_and_hide_without_facts(self):
        self.divider().btn_new.click()
        f = self.form()
        keys = list(f.templates)
        # No main-story lengths in the index: no Quick games. Runs great on
        # Linux only with Proton (and some ProtonDB rating).
        self.assertEqual(keys, ['unfinished', 'never', 'favourites',
                                *(['linux'] if has('proton') else []), 'blank'])
        self.assertTrue(f.templates['blank'].isChecked())
        f.templates['unfinished'].click()
        self.assertEqual(f.name.text(), "Unfinished")
        self.assertEqual([r.rule for r in f.rows],
                         [{'field': 'playtime', 'op': 'atleast', 'value': 1},
                          {'field': 'last', 'op': 'over', 'value': 30}])
        self.assertFalse(f.match_row.isHidden())
        f.templates['never'].click()
        self.assertEqual(f.name.text(), "Never played")         # still the template's name
        self.assertEqual(f.count.text(), "3 games match")
        f.name.setText("My own")
        f.templates['favourites'].click()
        self.assertEqual(f.name.text(), "My own")               # typed: kept
        self.assertTrue(f.templates['favourites'].isChecked())
        row = f.rows[0]
        row.op.setCurrentIndex(row.op.findData('no'))          # edited by hand: no template
        self.assertFalse(any(b.isChecked() for b in f.templates.values()))
        f.templates['blank'].click()
        self.assertEqual(len(f.rows), 1)
        self.assertTrue(f.match_row.isHidden())
        self.win.modal.cancel()
        # With lengths, Quick games joins.
        self.win._facts_idx = facts_for('play0', 'new0', length={'main': 3})
        self.divider().btn_new.click()
        f = self.form()
        self.assertIn('quick', f.templates)
        f.templates['quick'].click()
        self.assertEqual(f.rows[0].rule, {'field': 'length', 'op': 'under', 'value': 5})
        self.assertEqual(sorted(f._strip_slugs), ['new0', 'play0'])
        self.win.modal.cancel()

    def test_protondb_rule_only_with_proton(self):
        self.divider().btn_new.click()
        f = self.form()
        fields = [f.rows[0].field.itemData(i) for i in range(f.rows[0].field.count())]
        self.assertEqual('protondb' in fields, has('proton'))
        if has('proton'):
            f.templates['linux'].click()
            self.assertEqual(f.rows[0].rule, {'field': 'protondb', 'op': 'is',
                                              'value': 'platinum'})
            self.assertEqual(f.count.text(), "3 games match")
        self.win.modal.cancel()

    def test_rule_cap_disables_add_at_8(self):
        self.divider().btn_new.click()
        f = self.form()
        h = self.win.modal.card.height()
        for _ in range(smart.MAX_RULES + 2):
            if f.btn_add.isEnabled():
                f.btn_add.click()
        self.assertEqual(len(f.rows), smart.MAX_RULES)
        self.assertFalse(f.btn_add.isEnabled())
        self.assertGreater(self.win.modal.card.height(), h)  # the card grew with the rows
        self.assertLessEqual(self.win.modal.card.height(), int(self.win.modal.height() * 0.85))
        f.rows[3].btn_remove.click()
        self.assertEqual(len(f.rows), smart.MAX_RULES - 1)
        self.assertTrue(f.btn_add.isEnabled())
        self.assertIs(QApplication.focusWidget(), f.rows[3].field)
        self.win.modal.cancel()

    def test_shelf_cap_toasts_and_opens_nothing(self):
        w = self.win
        w.prefs.smart_shelves = [smart.new_shelf(f'id{i}') for i in range(smart.MAX_USER_SHELVES)]
        for s in w.prefs.smart_shelves[::2]:
            s['removed'] = True                              # removed ones count too
        with mock.patch.object(w, 'toast') as toast:
            w._new_shelf()
        self.assertFalse(w.modal.isVisible())
        self.assertIn(f"{smart.MAX_USER_SHELVES} shelves", toast.call_args[0][0])
        self.assertEqual(len(w.prefs.smart_shelves), smart.MAX_USER_SHELVES)
        self.assertIsNone(w.prefs.add_shelf({'name': 'x', 'rules': []}))

    def test_builder_only_offers_facts_that_exist(self):
        self.divider().btn_new.click()
        row = self.form().rows[0]
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
        self.win.modal.cancel()

    def test_any_match_and_rename_keep_the_key(self):
        w = self.win
        s = self.make("Either", ('store', 'is', 'nexus'), ('genre', 'is', 'RPG'), match='any')
        self.assertEqual(len(smart.user_games(s, w._shelf_pool(), w._smart_facts())), 5)
        w._edit_shelf(s['id'])
        self.form().name.setText("Renamed")
        w.modal.primary.click()
        self.assertEqual(self.rows()[0], "Renamed")
        i = w.filter.findData(f"user:{s['id']}")
        self.assertEqual(w.filter.itemText(i).strip(), "Renamed")

    def test_empty_shelf_still_shows_saying_so(self):
        self.make("Nothing", ('store', 'is', 'steam'), ('store', 'is', 'nexus'))
        row = next(x for x in self.win.home._smart if isinstance(x, ShelfRow)
                   and x.title == "Nothing")
        self.assertTrue(row.body.isHidden())

    def test_menu_edit_hide_remove_and_bring_back(self):
        w = self.win
        s = self.make("Mine", ('store', 'is', 'steam'))
        key = f"user:{s['id']}"
        w.input.mode = 'pad'
        w._smart_menu(key, QPoint())
        rows = {b.text(): b for b in w.sheet.findChildren(QPushButton)
                if b.objectName() == 'SheetRow' and b.isVisibleTo(w.sheet)}
        self.assertEqual(list(rows), ["See all", "Edit shelf", "Save as collection",
                                      "Hide this shelf", "Remove shelf"])
        rows["Edit shelf"].click()                      # the ⋮ menu's Edit: the modal
        self.assertFalse(w.sheet.isVisible())
        self.assertEqual(w.modal.title.text(), "Edit shelf")
        self.assertEqual(self.form().name.text(), "Mine")
        w._escape()
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

    def test_remove_from_edit_footer(self):
        w, m = self.win, self.win.modal
        s = self.make("Gone", ('store', 'is', 'steam'))
        w._edit_shelf(s['id'])
        self.assertFalse(m.btn_extra.isHidden())
        self.assertEqual(m.btn_extra.text(), "Remove shelf")
        texts = [b.text() for b in m.findChildren(QPushButton) if not b.isHidden()]
        self.assertNotIn("Hide shelf", texts)                  # Hide stays in the ⋮ menu
        self.fill(self.form(), "Not saved")
        with mock.patch.object(w, 'toast') as toast:
            m.btn_extra.click()
        self.assertFalse(m.isVisible())
        self.assertEqual(w.prefs.user_shelves(), [])
        self.assertEqual(self.saved()['smart_shelves'][0]['name'], "Gone")   # edits dropped
        self.assertTrue(self.saved()['smart_shelves'][0]['removed'])
        self.assertEqual(toast.call_args[0][2][0], "Undo")

    def test_edit_button_next_to_the_filter(self):
        w = self.win
        s = self.make("Mine", ('store', 'is', 'steam'))
        w._show_filter(f"user:{s['id']}")
        self.assertFalse(w.btn_edit_shelf.isHidden())
        w.btn_edit_shelf.click()
        self.assertTrue(w.modal.isVisible())
        self.assertEqual(self.form().name.text(), "Mine")
        self.form().rows[0].value.setCurrentIndex(self.form().rows[0].value.findData('nexus'))
        w.modal.primary.click()
        # Still that filter: the grid now shows what the edited rules match.
        self.assertEqual(w.filter.currentData(), f"user:{s['id']}")
        self.assertEqual(sorted(g['slug'] for g in w._shown_games), ['new0', 'new1', 'new2'])

    def test_friends_rule_row_is_read_only(self):
        """A shelf saved from Play together (w-83): its "Friends own (all
        of)" row shows the friends' names in a disabled value combo, keeps
        the ids on Save, and the field isn't offered for new rules."""
        a, b = '76561190000000101', '76561190000000102'
        names = {a: "Ana", b: "Ben"}
        w = self.win
        shelf = w.prefs.add_shelf({'name': "Play with Ana and Ben", 'match': 'all',
                                   'rules': [{'field': 'friends', 'op': 'all_multi',
                                              'value': [a, b]}]})
        pool = w._shelf_pool()
        f = ShelfForm(shelf, pool, pool, {}, w._shelf_fields(), friends={a, b}, names=names)
        row = f.rows[0]
        self.assertEqual(row.field.currentData(), 'friends')
        self.assertEqual(row.op.currentData(), 'all_multi')
        self.assertEqual(row.value.count(), 1)
        self.assertEqual(row.value.currentText(), "Ana and Ben")
        self.assertFalse(row.value.isEnabled())
        self.assertFalse(row.value.isHidden())
        row.op.setCurrentIndex(row.op.findData('all'))             # the op can change
        self.assertEqual(f.draft()['rules'], [{'field': 'friends', 'op': 'all',
                                               'value': [a, b]}])
        f._add_rule()
        self.assertEqual(f.rows[1].field.findData('friends'), -1)   # never offered
        self.assertEqual(f.matches(), [])                           # nobody owns these
        f.deleteLater()
        # The window's own modal (no friends data: names unknown) never raises
        w._edit_shelf(shelf['id'])
        self.assertEqual(self.form().rows[0].value.currentText(), "2 friends")
        w.modal.cancel()

    def test_keyboard_and_pad_reach_every_shelf_control(self):
        w, m = self.win, self.win.modal
        w.activateWindow()
        QApplication.processEvents()
        self.divider().btn_new.click()
        f = self.form()
        f.btn_add.click()
        want = [f.name, *f.templates.values(), *(f.match.item(k) for k in f.match.keys()),
                *(c for r in f.rows for c in (r.field, r.op, r.value, r.btn_remove)
                  if not c.isHidden()),                  # "none yet" has no value
                f.btn_add, m.btn_cancel, m.primary, m.btn_close]
        f.name.setFocus()
        seen = []
        for _ in range(len(want) + 2):
            seen.append(QApplication.focusWidget())
            self.key(Qt.Key.Key_Tab)
        for wdg in want:
            self.assertIn(wdg, seen, wdg.accessibleName() or getattr(wdg, 'text', str)())
        self.assertTrue(all(m.isAncestorOf(x) for x in seen))
        # The D-pad stays in the card too.
        for key in (Qt.Key.Key_Down,) * 12 + (Qt.Key.Key_Right,) * 3 + (Qt.Key.Key_Up,) * 14:
            self.key(key)
            self.assertTrue(m.isAncestorOf(QApplication.focusWidget()), key)
        self.assertIs(w.nav_scope(), m)
        m.cancel()

    def test_theatre_new_shelf_at_scale(self):
        from nexus.gui.theatre import THEATRE_SCALE
        w = self.win
        w.set_theatre(True)
        QApplication.processEvents()
        w._new_shelf()
        m = w.theatre_modal
        self.assertTrue(m.isVisible())
        f = next(x for x in m.findChildren(ShelfForm) if not x.isHidden())
        self.assertEqual(f.scale, THEATRE_SCALE)
        self.assertIs(QApplication.focusWidget(), f.name)       # A here: the keyboard
        w._escape()
        self.assertFalse(m.isVisible())
        self.assertEqual(w.prefs.smart_shelves, [])
        w.set_theatre(False)

    def test_save_as_collection(self):
        w = self.win
        s = self.make("Steamy", ('store', 'is', 'steam'))
        w._save_smart(f"user:{s['id']}")
        self.assertEqual(sorted(w.prefs.collections["Steamy"]), ['play0', 'play1', 'play2'])

    def test_home_widget_can_show_your_shelf(self):
        w = self.win
        s = self.make("Steamy", ('store', 'is', 'steam'))
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
        made = p2.add_shelf({'id': 'k1', 'name': ' New  one ', 'removed': True,
                             'rules': [{'field': 'genre', 'op': 'is', 'value': 'RPG'},
                                       {'field': 'nope', 'op': 'is'}]})
        self.assertEqual(made['rules'], [{'field': 'genre', 'op': 'is', 'value': 'RPG'}])
        self.assertEqual(made['name'], "New one")
        self.assertFalse(made['removed'])
        self.assertNotEqual(made['id'], 'k1')                 # a fresh id, always
        self.assertEqual(nx.LibraryPrefs().find_shelf(made['id']), made)
        self.assertIsNone(p2.update_shelf({'id': 'unknown'}))


if __name__ == '__main__':
    unittest.main()
