"""The Windows UI, checked on Linux: with FEATURES set to what Windows has
(nothing Linux-only), no Proton, Heroic, Wine, perf or Sunshine control
shows; save backup (shared, through the Ludusavi manifest there) does.
Offscreen, every CLI call stubbed."""

import unittest
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
from tests.patching import set_gui  # noqa: E402

from nexus import features, platform  # noqa: E402

if nx is not None:
    from nexus.gui import (dashboard, details, install, library, setup, storage, system, topbar,
        win_actions)
    from nexus.gui.theme import G
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

WINDOWS = features.WINDOWS

LINUX_ACTIONS = ('add-heroic', 'remove-heroic', 'pg-default', 'protongen',
                 'benchmark', 'pause-toggle', 'add-sunshine', 'remove-sunshine',
                 'check-integrity', 'snapshots')


def windows():
    return mock.patch.object(features, 'FEATURES', WINDOWS)


def linux():
    return mock.patch.object(features, 'FEATURES', features.ALL)


class Calls(list):
    """A signal's calls, as argument tuples (a bound method, not a lambda slot)."""

    def add(self, *args):
        self.append(args)


GAME = {'slug': 'a', 'display_name': 'A', 'source': 'nexus', 'launcher': '/a',
        'playing': True, 'sunshine': False, 'heroic': False, 'owned_dir': True}
STEAM = {'slug': 's', 'display_name': 'S', 'source': 'steam', 'launcher': '/s', 'appid': 1}


@unittest.skipIf(nx is None, "PyQt6 not available")
class Actions(unittest.TestCase):
    def keys(self, g):
        return {a.key for a in nx.game_actions(g)}

    def test_windows_offers_nothing_linux_only(self):
        with windows():
            keys = self.keys(GAME) | self.keys(STEAM)
        self.assertFalse(keys & set(LINUX_ACTIONS), keys & set(LINUX_ACTIONS))
        self.assertFalse([k for k in keys if k.startswith('tool:')])
        self.assertIn('uninstall', keys)
        self.assertIn('add-steam', keys)
        self.assertIn('restore-saves', keys)

    def test_save_backup_can_still_be_turned_off(self):
        with mock.patch.object(features, 'FEATURES', WINDOWS - {'save_backup'}):
            self.assertNotIn('restore-saves', self.keys(GAME))

    def test_linux_keeps_them(self):
        with linux():
            keys = self.keys(GAME) | self.keys(STEAM)
        self.assertLessEqual({'add-heroic', 'protongen', 'restore-saves', 'pause-toggle',
                              'add-sunshine', 'tool:winecfg', 'check-integrity',
                              'snapshots'}, keys)


@unittest.skipIf(nx is None, "PyQt6 not available")
class Details(unittest.TestCase):
    def setUp(self):
        self._cli_json = nx.cli_json
        set_gui('cli_json', lambda *a, **k: None)

    def tearDown(self):
        set_gui('cli_json', self._cli_json)

    def panel(self):
        p = details.DetailsPanel()
        p.resize(380, 900)
        p.show()
        self.addCleanup(p.deleteLater)
        return p

    def test_windows_hides_linux_only_parts(self):
        with windows():
            p = self.panel()
            p.show_games([dict(GAME, playing=False)])
            for w in (p.sec_launch, p.snap_box, p.tune_pdb, p.st_proton, p.st_rating,
                      p.btn_heroic, p.ov_heroic, p.pg_notice):
                self.assertTrue(w.isHidden(), w)
            self.assertFalse(p.sec_files.isHidden())       # rename, exe, Steam: Windows too
            self.assertFalse(p.sec_trouble.isHidden())
            self.assertFalse(p.confirm_backup.isHidden())
            p.show_games([GAME, STEAM])
            for key in details.BATCH_NEEDS:
                self.assertFalse(p.batch_btns[key].isVisibleTo(p), key)
            sent = []
            p.request.connect(lambda args, *_: sent.append(args))
            p.show_games([dict(GAME, playing=False)])
            p.confirm_backup.setChecked(True)
            p._do_uninstall()
        self.assertIn('--save-backup', sent[0])

    def test_linux_shows_them(self):
        with linux():
            p = self.panel()
            p.show_games([dict(GAME, playing=False)])
            for w in (p.sec_launch, p.st_proton, p.btn_heroic, p.confirm_backup):
                self.assertFalse(w.isHidden(), w)
            sent = []
            p.request.connect(lambda args, *_: sent.append(args))
            p._do_uninstall()
            self.assertIn('--save-backup', sent[0])
            self.assertNotIn('--remove-snapshots', sent[0])      # kept unless ticked
            p.confirm_snaps.show()
            p.confirm_snaps.setChecked(True)
            p._do_uninstall()
        self.assertIn('--remove-snapshots', sent[1])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Pages(unittest.TestCase):
    def test_no_proton_icon_on_windows(self):
        bar = topbar.TopBar()
        self.addCleanup(bar.deleteLater)
        with windows():
            proton = bar.add_icon(G['proton'], "Proton")
            gear = bar.add_icon(G['settings'], "Settings")
        self.assertTrue(proton.isHidden())
        self.assertFalse(gear.isHidden())

    def test_settings_rows(self):
        with windows():
            page = nx.SettingsPage()
        self.addCleanup(page.deleteLater)
        for c in (page.heroic, page.runner, page.proton, page.al_proton):
            self.assertFalse(page.row_of[c].available, c)
        for c in (page.steam, page.base, page.scan_dir, page.scan_more, page.tor_client,
                  page.gdb_token, page.key, page.al_atl, page.ctl_double, page.desktop,
                  page.agent, page.tray):
            self.assertTrue(page.row_of[c].available, c)
        # The app-menu switch is the Start Menu one; the agent's is Start with Windows.
        self.assertEqual(page.row_of[page.desktop].title.text(), "Start Menu shortcuts")
        self.assertEqual(page.row_of[page.agent].title.text(), "Start with Windows")
        self.assertTrue(page.row_of[page.agent].matches("startup"))
        for key in ('appearance', 'controls', 'installing', 'artwork', 'gamedb', 'torrent',
                    'alerts', 'background'):
            self.assertIn(key, page.sections)

    def test_settings_rows_on_linux_are_unchanged(self):
        with linux():
            page = nx.SettingsPage()
        self.addCleanup(page.deleteLater)
        for c in (page.heroic, page.desktop, page.runner, page.proton, page.al_proton,
                  page.agent):
            self.assertTrue(page.row_of[c].available, c)
        self.assertEqual(page.row_of[page.desktop].title.text(),
                         "Add new games to the app menu")
        self.assertEqual(page.row_of[page.agent].title.text(),
                         "Keep working while Nexus is closed")
        with linux():
            page.set_agent_status({'systemd': False})
        self.assertFalse(page.agent.isEnabled())
        self.assertIn("systemd", page.agent_row.desc.text())

    def test_start_with_windows_switch(self):
        with windows():
            page = nx.SettingsPage()
            self.addCleanup(page.deleteLater)
            asked = Calls()
            page.background_changed.connect(asked.add)
            # What --agent-status says on Windows: no systemd, a Startup shortcut.
            page.set_agent_status({'startup': True, 'systemd': False, 'enabled': False})
            self.assertTrue(page.agent.isEnabled())             # never "needs systemd"
            self.assertFalse(page.agent.isChecked())
            self.assertIn("sign in", page.agent_row.desc.text())
            page.agent.setChecked(True)
            self.assertEqual(asked, [('agent', True)])
            page.set_agent_status({'startup': True, 'systemd': False, 'enabled': True})
            self.assertTrue(page.agent.isChecked())
            self.assertTrue(page.agent_row.desc.text().startswith("On"))
            page.agent.setChecked(False)
            self.assertEqual(asked[-1], ('agent', False))
            page.set_controls({'close_to_tray': False})
            self.assertNotIn("niri", page.tray_row.desc.text())

    def test_health_fix_buttons_only_open_windows_pages(self):
        from PyQt6.QtWidgets import QPushButton
        from nexus import platform
        from nexus.cli import doctor
        h = system.HealthPage()
        self.addCleanup(h.deleteLater)
        h.btn_ok_toggle.setChecked(True)
        h.checks = [
            {'group': 'Requirements', 'status': 'warn', 'title': 'Free space', 'detail': '9 GB',
             'fix': '', 'actions': [{'label': 'Storage settings', 'steam': False,
                                     'args': ['--open-url', 'ms-settings:storagesense']}]},
            {'group': 'Requirements', 'status': 'warn', 'title': 'Long paths', 'detail': 'off',
             'fix': '', 'actions': [{'label': 'How to turn it on', 'steam': False,
                                     'args': ['--open-url', doctor.LONG_PATHS_HELP]}]},
            {'group': 'Integrations', 'status': 'info', 'title': 'Windows Defender',
             'detail': 'on', 'fix': '',
             'actions': [{'label': 'Windows Security', 'steam': False,
                          'args': ['--open-url', 'windowsdefender://threatsettings']},
                         {'label': 'Elsewhere', 'steam': False,
                          'args': ['--open-url', 'https://example.com/']}]}]
        jobs, opened = Calls(), []
        h.request.connect(jobs.add)
        with windows():
            h._render()
        buttons = {b.text(): b for b in h.findChildren(QPushButton)}
        self.assertNotIn("Elsewhere", buttons)                  # not a page a fix may open
        with mock.patch.object(platform, 'open_url', side_effect=lambda u: opened.append(u)
                               or True):
            for name in ("Storage settings", "How to turn it on", "Windows Security"):
                buttons[name].click()
        self.assertEqual(opened, ['ms-settings:storagesense', doctor.LONG_PATHS_HELP,
                                  'windowsdefender://threatsettings'])
        self.assertEqual(jobs, [])                              # opened here, never a job
        toasts = Calls()
        h.toast.connect(toasts.add)
        with mock.patch.object(platform, 'open_url', return_value=False):
            buttons["Storage settings"].click()
        self.assertEqual(toasts[0][1], 'error')

    def test_health_linux_fixes_still_run_as_jobs(self):
        from PyQt6.QtWidgets import QPushButton
        h = system.HealthPage()
        self.addCleanup(h.deleteLater)
        h.checks = [{'group': 'Games', 'status': 'fail', 'title': 'G: missing', 'detail': '',
                     'fix': '', 'actions': [{'label': 'Prune missing games', 'steam': True,
                                             'args': ['--prune', '--yes']}]}]
        jobs = Calls()
        h.request.connect(jobs.add)
        with linux():
            h._render()
        next(b for b in h.findChildren(QPushButton)
             if b.text() == "Prune missing games").click()
        self.assertEqual(jobs[0][0], ['--prune', '--yes'])

    def test_health_rows(self):
        checks = [{'group': 'Performance', 'title': 'scx'},
                  {'group': 'Integrations', 'title': 'Heroic'},
                  {'group': 'Integrations', 'title': 'Steam account'}]
        with windows():
            left = [c['title'] for c in checks if system.check_supported(c)]
        self.assertEqual(left, ['Steam account'])
        with linux():
            self.assertTrue(all(system.check_supported(c) for c in checks))

    def test_storage_categories(self):
        page = storage.StoragePage()
        self.addCleanup(page.deleteLater)
        cands = [{'id': c, 'category': c, 'size': 1, 'path': '/x', 'title': c}
                 for c in ('mesa-cache', 'shadercache', 'orphan-prefix', 'old-logs')]
        with windows(), mock.patch.object(page, '_fill_quarantine', lambda *_: None):
            try:
                page._fill_cleanup({'candidates': cands}, None)
            except (KeyError, TypeError):
                pass                    # rendering details don't matter here
        self.assertEqual([c['category'] for c in page.cands], ['old-logs'])

    def test_dashboard_has_no_proton_tile(self):
        with windows():
            st = dashboard.system_status([], [], [], {'families': {'ge': {'update': True,
                                                                          'latest': 'GE-10'}}})
        self.assertNotIn('proton', [t['key'] for t in st['tiles']])
        self.assertIsNone(st['attention'])

    def test_proton_page_asks_nothing_on_windows(self):
        """The window refreshes it at start-up; on Windows that put "Couldn't
        read Proton builds: --protons isn't available" on screen."""
        from nexus.gui import protonpage
        page = protonpage.ProtonPage()
        self.addCleanup(page.deleteLater)
        with windows(), mock.patch.object(protonpage, 'cli_json') as cj:
            page.refresh()
        cj.assert_not_called()
        with linux(), mock.patch.object(protonpage, 'cli_json') as cj:
            page.refresh()
        self.assertTrue(cj.called)

    def test_list_view_hides_proton_column(self):
        with windows():
            lv = library.ListView()
        self.addCleanup(lv.deleteLater)
        self.assertTrue(lv.isColumnHidden(lv.COLS.index("Proton")))

@unittest.skipIf(nx is None, "PyQt6 not available")
class InstallPages(unittest.TestCase):
    """The Install page and first-run setup: no Proton, runner or protongen
    on Windows, and the install folder's warnings (Program Files, OneDrive)."""
    CONFIG = {'steam': True, 'heroic': True, 'desktop': True, 'unattended': True}
    WARN = ["it's in Program Files, where older games can't write their saves"]

    def setUp(self):
        self._cli_json = nx.cli_json
        set_gui('cli_json', lambda *a, **k: None)

    def tearDown(self):
        set_gui('cli_json', self._cli_json)

    def install_page(self):
        page = nx.InstallPage()
        self.addCleanup(page.deleteLater)
        return page

    def test_install_page_on_windows(self):
        with windows(), mock.patch.object(install, 'protongen_has_default', return_value=True), \
                mock.patch.object(platform, 'install_base_warnings', return_value=self.WARN):
            page = self.install_page()
            page.open(dict(self.CONFIG, install_base=r'C:\Program Files\Games'))
            cmd = page._command(r'D:\Downloads\Game', '')
        for w in (page.pg_default, page.deps, page.heroic):
            self.assertTrue(w.isHidden(), w)
        self.assertEqual(page.desktop.text(), "Start Menu shortcut")
        self.assertNotIn('--use-protongen-default', cmd)
        self.assertNotIn('--deps', cmd)
        self.assertFalse(page.base_note.isHidden())
        self.assertIn(r"C:\Program Files\Games: it's in Program Files", page.base_note.text())

    def test_install_page_on_linux(self):
        with linux(), mock.patch.object(install, 'protongen_has_default', return_value=True), \
                mock.patch.object(platform, 'install_base_warnings', return_value=[]):
            page = self.install_page()
            page.open(self.CONFIG)
            cmd = page._command('/r/Game', '')
        for w in (page.pg_default, page.deps, page.heroic):
            self.assertFalse(w.isHidden(), w)
        self.assertIn('--use-protongen-default', cmd)
        self.assertTrue(page.base_note.isHidden())

    def test_install_base_default_comes_from_the_platform(self):
        self.assertEqual(nx.INSTALL_BASE_DEFAULT,
                         str(platform.default_install_base(nx.HOME)))
        with windows():
            page = setup.SetupPage()
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.base.placeholderText(), nx.INSTALL_BASE_DEFAULT)

    def test_setup_on_windows(self):
        with windows(), mock.patch.object(platform, 'install_base_warnings',
                                          side_effect=lambda b: self.WARN
                                          if 'Program Files' in str(b) else []):
            page = setup.SetupPage()
            self.addCleanup(page.deleteLater)
            page.open({}, {})
            self.assertEqual([k for k, _ in page.steps],
                             ['checks', 'launchers', 'folders', 'extras', 'look'])
            page.show_step(2)
            self.assertIs(page.stack.currentWidget(), page.base.parentWidget())
            self.assertIn("of 5", page.step_note.text())
            self.assertTrue(page.base_warn.isHidden())
            page.base.setText(r'C:\Program Files\Games')
            self.assertFalse(page.base_warn.isHidden())
            self.assertIn("Program Files", page.base_warn.text())
            page.show_step(3)
            self.assertEqual(page.title.text(), "Extras")
            page.proton.addItem("GE-Proton10-1", "GE-Proton10-1")
            page.proton.setCurrentIndex(1)
            updates, _ = page.answers_now()
        self.assertNotIn(('runner', 'proton'), updates)
        self.assertEqual(updates[('install', 'base')], r'C:\Program Files\Games')

    def test_setup_on_linux_keeps_proton(self):
        with linux():
            page = setup.SetupPage()
        self.addCleanup(page.deleteLater)
        self.assertEqual(len(page.steps), len(setup.STEPS))

    def test_setup_shows_a_few_themes_and_keeps_yours(self):
        """Setup shows a short list of themes (Settings has them all); setting
        up again with one only Settings lists keeps it, not the first card."""
        with linux():
            page = setup.SetupPage()
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.theme.count(), len(nx.SETUP_THEMES))
        self.assertTrue(set(nx.SETUP_THEMES) <= set(nx.THEMES))
        self.assertIn('contrast', nx.SETUP_THEMES)
        with mock.patch.object(setup, 'cli_json'):
            page.open({}, {'theme': 'ocean', 'scale': 1.0})
        self.assertEqual(page._look()['theme'], 'ocean')
        with mock.patch.object(setup, 'cli_json'):
            page.open({}, {'theme': 'ocean', 'scale': 1.0})     # no second card
        self.assertEqual(page.theme.count(), len(nx.SETUP_THEMES) + 1)

    def test_setup_cards_answer_like_the_menus(self):
        """Theme and size are cards now, the switches sit on cards: the same
        answers come out, and a click on a card is a click on its choice."""
        with windows():
            page = setup.SetupPage()
            self.addCleanup(page.deleteLater)
            page.open({}, {'theme': 'dawn', 'scale': 1.25})
            self.assertEqual(page._look(), {'theme': 'dawn', 'scale': 1.25})
            looks = []
            page.preview.connect(looks.append)
            page.theme.group.button(page.theme.findData('moon')).click()
            page.size.group.button(page.size.findData(1.5)).click()
            self.assertEqual(looks[-1], {'theme': 'moon', 'scale': 1.5})
            card = page.agent.parentWidget()
            self.assertIsInstance(card, setup.ToggleCard)
            QTest.mouseClick(card, Qt.MouseButton.LeftButton)
            self.assertTrue(page.agent.isChecked())
            self.assertEqual(card.property('on'), 'true')
            QTest.mouseClick(page.use_torrent.parentWidget(), Qt.MouseButton.LeftButton)
            self.assertFalse(page.use_torrent.isChecked())     # disabled: no client found
            _, opts = page.answers_now()
            self.assertEqual(opts['appearance'], {'theme': 'moon', 'scale': 1.5})
            self.assertTrue(opts['agent'])
            page.show_step(4)
            self.assertEqual((page.track.current, len(page.track.names)), (4, 5))
            # The stack is as tall as the step shown, not the tallest step.
            self.assertEqual(page.stack.minimumSizeHint(),
                             page.stack.currentWidget().minimumSizeHint())

    def test_antivirus_text(self):
        title, text = win_actions.antivirus_text({'detections': [
            {'file': r'C:\Games\ffxv\vcruntime140_1_app.dll', 'threat': 'Trojan:Win32/Wacatac.H!ml',
             'at': '2026-10-23T12:00:00'}]})
        self.assertEqual(title, "Windows Defender removed a game file")
        self.assertTrue(text.startswith("vcruntime140_1_app.dll — Trojan:Win32/Wacatac.H!ml\nin C:"))
        self.assertIn("isn't in your library", text)
        self.assertIn("doesn't change antivirus settings", text)
        title, text = win_actions.antivirus_text({'detections': [], 'log_lines': [
            'ISDone.dll: An error occurred when unpacking!']})
        self.assertEqual(title, "An antivirus stopped the install")
        self.assertIn("ISDone.dll: An error occurred", text)


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(nx is None, "PyQt6 not available")
class SteamChip(unittest.TestCase):
    def chip(self, feats, root, current='all', games=()):
        """The Steam chip after _store_chips on a bare stand-in for the window."""
        from PyQt6.QtWidgets import QPushButton
        from nexus.gui.win_library import LibraryMixin

        class Stub:
            _store_chips = LibraryMixin._store_chips

        stub = Stub()
        stub.chips = {'steam': QPushButton(), 'epic': QPushButton()}
        stub._chip, stub._games = current, list(games)
        self.addCleanup(stub.chips['steam'].deleteLater)
        self.addCleanup(stub.chips['epic'].deleteLater)
        with mock.patch.object(features, 'FEATURES', feats), \
                mock.patch('nexus.paths.STEAM_ROOT', root):
            stub._store_chips()
        return stub.chips['steam']

    def test_windows_hides_steam_chip_without_steam(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self.chip(WINDOWS, Path(d) / 'nope').isHidden())
            # In use, or the library has a Steam game: stays reachable.
            self.assertFalse(self.chip(WINDOWS, Path(d) / 'nope', current='steam').isHidden())
            self.assertFalse(self.chip(WINDOWS, Path(d) / 'nope',
                                       games=[{'source': 'steam'}]).isHidden())
            self.assertFalse(self.chip(WINDOWS, Path(d)).isHidden())     # Steam is there

    def test_linux_always_shows_steam_chip(self):
        from pathlib import Path
        self.assertFalse(self.chip(features.ALL, Path('/nonexistent-steam')).isHidden())


@unittest.skipIf(nx is None, "PyQt6 not available")
class StartupAndSettings(unittest.TestCase):
    def test_desktop_entry_only_where_the_feature_is(self):
        from nexus.gui import app
        with windows(), mock.patch.object(app, 'install_desktop_entry') as wrote:
            self.assertFalse(app.refresh_desktop_entry('/icon.png', ['--install-desktop']))
        wrote.assert_not_called()
        with linux(), mock.patch.object(app, 'install_desktop_entry') as wrote, \
                mock.patch.object(app, 'DESKTOP_PATH') as path, \
                mock.patch.object(app, 'LEGACY_DESKTOP') as legacy:
            path.exists.return_value = False
            legacy.exists.return_value = False
            self.assertTrue(app.refresh_desktop_entry('/icon.png', []))
        wrote.assert_called_once_with('/icon.png')

    def test_dedupe_text_follows_the_dup_order(self):
        with windows():
            page = nx.SettingsPage()
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.ctl_dedupe.itemText(0),
                         "Show it once (repack, then Steam, then Epic)")
        with linux():
            page = nx.SettingsPage()
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.ctl_dedupe.itemText(0),
                         "Show it once (repack, then Steam, then Heroic, then Epic)")

    def test_install_folder_warning_follows_the_field(self):
        warn = mock.patch.object(platform, 'install_base_warnings',
                                 side_effect=lambda b: ["it's in Program Files"]
                                 if 'Program Files' in str(b) else [])
        with windows(), warn:
            page = nx.SettingsPage()
            self.addCleanup(page.deleteLater)
            row = page.row_of[page.base]
            self.assertTrue(row.note.isHidden())
            page.base.setText(r'C:\Program Files\Games')
            self.assertFalse(row.note.isHidden())
            self.assertIn("it's in Program Files", row.note.text())
            page.base.setText(r'D:\Games')
            self.assertTrue(row.note.isHidden())
