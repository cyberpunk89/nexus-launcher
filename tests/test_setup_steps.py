"""First-run Setup's three steps and Connect services (w-104, w-105): the pure
helpers that turn `--setup-facts` and the library counts into what each step
shows, and the service cards' answers. Offscreen, no CLI, no network."""

import unittest
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx

from nexus import features  # noqa: E402

if nx is not None:
    from nexus.gui import setup
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

WINDOWS = features.WINDOWS

BASE = '/home/u/Games/Nexus'
ROOT = {'id': 'a', 'path': '/', 'fs': 'btrfs', 'free': 212e9, 'total': 931e9,
        'folder': BASE, 'default': True, 'steam_games': 55, 'warnings': []}
GAMES = {'id': 'b', 'path': '/mnt/games', 'fs': 'ext4', 'free': 1.4e12, 'total': 1.8e12,
         'folder': '/mnt/games/Games', 'default': False, 'steam_games': 88, 'warnings': []}
SMALL = {'id': 'c', 'path': '/mnt/small', 'fs': 'xfs', 'free': 20e9, 'total': 100e9,
         'folder': '/mnt/small/Games', 'default': False, 'steam_games': 0, 'warnings': []}
NAS = {'id': 'timeout:/mnt/nas', 'path': '/mnt/nas', 'fs': None, 'free': None, 'total': None,
       'folder': '/mnt/nas', 'default': False, 'steam_games': 0,
       'warnings': ["didn't answer within 3 s (a network drive?)"]}
LISTING = {'id': 'timeout:/home/u/.steam', 'path': '/home/u/.steam', 'fs': None, 'free': None,
           'total': None, 'folder': '/home/u/.steam', 'default': False, 'steam_games': 0,
           'warnings': ["couldn't list the Steam and Heroic folders (didn't finish)"]}
TOR = {'client': 'transmission', 'url': 'http://127.0.0.1:9091', 'configured': False,
       'download_dir': '/home/u/Downloads/torrents', 'error': None}


def facts(**kw):
    out = {'edition': 'linux', 'default_base': BASE, 'drives': [ROOT, GAMES], 'torrent': None,
           'needs': [], 'repacks_was': None}
    out.update(kw)
    return out


@unittest.skipIf(nx is None, "PyQt6 not available")
class Prepick(unittest.TestCase):
    """How you play: picked from what was said before, then a client that
    answers; nothing found picks nothing (user 2026-11-09)."""

    def test_repacks_was_wins(self):
        pick, why = setup.prepick(facts(repacks_was=False, torrent=TOR), {})
        self.assertIs(pick, False)
        self.assertIn("off", why)
        pick, why = setup.prepick(facts(repacks_was=True), {})
        self.assertIs(pick, True)
        self.assertIn("on", why)

    def test_a_client_that_answers_picks_repacks(self):
        pick, why = setup.prepick(facts(torrent=TOR), {})
        self.assertIs(pick, True)
        self.assertIn("Transmission", why)

    def test_a_client_with_an_error_does_not(self):
        pick, _why = setup.prepick(facts(torrent={**TOR, 'error': 'refused'}), {})
        self.assertIsNone(pick)

    def test_a_configured_client_counts_without_facts(self):
        pick, _why = setup.prepick(None, {'torrent_client': 'qbittorrent'})
        self.assertIs(pick, True)

    def test_nothing_found_picks_nothing(self):
        self.assertEqual(setup.prepick(facts(), {}), (None, ''))
        self.assertEqual(setup.prepick(None, {}, {}), (None, ''))

    def test_games_and_no_client_pick_play(self):
        pick, why = setup.prepick(facts(), {}, {'steam': 12})
        self.assertIs(pick, False)
        self.assertTrue(why)


@unittest.skipIf(nx is None, "PyQt6 not available")
class Drives(unittest.TestCase):
    def test_text_with_sizes_fs_and_notes(self):
        title, free, note = setup.drive_text(ROOT)
        self.assertEqual(title, "/   btrfs")
        self.assertEqual(free, "212 GB free of 931 GB")
        self.assertEqual(note, "Default folder · Steam library · 55 games")
        title, free, note = setup.drive_text(GAMES)
        self.assertEqual(free, "1.4 TB free of 1.8 TB")
        self.assertEqual(note, "Steam library · 88 games")
        self.assertAlmostEqual(setup.drive_used(GAMES), 0.4 / 1.8, places=3)

    def test_unknown_free_space(self):
        d = {**ROOT, 'free': None, 'total': None}
        self.assertEqual(setup.drive_text(d)[1], "Free space unknown")
        self.assertIsNone(setup.drive_used(d))

    def test_windows_names_the_system_drive_and_drops_fs(self):
        d = {**ROOT, 'path': 'C:\\', 'fs': 'NTFS'}
        self.assertEqual(setup.drive_text(d, windows=True, system='C:')[0], "C:\\  System")
        self.assertEqual(setup.drive_text({**GAMES, 'path': 'D:\\', 'fs': 'NTFS'},
                                          windows=True, system='C:')[0], "D:\\")

    def test_order_and_timed_out_drives_grouped(self):
        cards, stuck = setup.split_drives([SMALL, NAS, GAMES, ROOT, LISTING])
        self.assertEqual([d['id'] for d in cards], ['a', 'b', 'c'])     # default, then free
        self.assertEqual(stuck, ['/mnt/nas', '/home/u/.steam'])
        text = setup.not_checked_text(stuck)
        self.assertTrue(text.startswith("Not checked: /mnt/nas, /home/u/.steam"))
        self.assertIn("Didn't answer within 3 s (a network drive?)", text)
        self.assertIn("Change…", text)

    def test_a_timed_out_default_stays_a_card(self):
        dflt = {**NAS, 'id': f'timeout:{BASE}', 'path': BASE, 'folder': BASE, 'default': True}
        cards, stuck = setup.split_drives([dflt, NAS])
        self.assertEqual([d['path'] for d in cards], [BASE])
        self.assertEqual(stuck, ['/mnt/nas'])
        self.assertEqual(setup.drive_text(cards[0])[1], "Free space unknown")


@unittest.skipIf(nx is None, "PyQt6 not available")
class Needs(unittest.TestCase):
    def test_none_is_one_ok_row(self):
        rows = setup.need_rows([], windows=False)
        self.assertEqual([(r['status'], r['title']) for r in rows],
                         [('ok', "Wine and Proton are ready")])
        rows = setup.need_rows([], windows=True)
        self.assertEqual(rows[0]['title'], "Long paths and free space are fine")

    def test_checks_that_did_not_finish(self):
        rows = setup.need_rows([{'status': 'warn', 'title': "Some checks didn't finish",
                                 'detail': "didn't finish in 5 s", 'fix': 'Open Health',
                                 'actions': [{'label': 'x', 'args': []}]}], windows=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'warn')
        self.assertEqual(rows[0]['actions'], [])
        self.assertIn("System › Health", rows[0]['detail'])

    def test_no_proton_keeps_its_fix(self):
        act = {'label': 'Install GE-Proton', 'args': ['--proton-install', 'latest'],
               'steam': False}
        rows = setup.need_rows([{'status': 'fail', 'title': 'Proton', 'detail': 'none installed',
                                 'fix': 'Install GE-Proton', 'actions': [act]}], windows=False)
        self.assertEqual(rows[0]['title'], "No GE-Proton yet")
        self.assertEqual(rows[0]['actions'], [act])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Found(unittest.TestCase):
    def test_counts_from_the_library(self):
        games = [{'source': 'steam'}] * 3 + [{'source': 'heroic', 'runner': 'legendary'},
                                             {'source': 'heroic', 'runner': 'gog'},
                                             {'source': 'nexus'}]
        self.assertEqual(setup.found_counts(games),
                         {'steam': 3, 'heroic': 2, 'heroic_stores': ['Epic', 'GOG']})

    def test_tiles(self):
        counts = {'steam': 143, 'heroic': 12, 'heroic_stores': ['Epic', 'GOG']}
        tiles = setup.found_tiles(counts, facts(torrent=TOR), heroic=True)
        self.assertEqual([t[2] for t in tiles], ["Steam", "Heroic", "Transmission"])
        self.assertEqual(tiles[0][3], "143 games · on 2 drives")
        self.assertEqual(tiles[1][3], "12 games · Epic and GOG")

    def test_windows_has_no_heroic_tile_and_errors_show_no_client(self):
        counts = {'steam': 1, 'heroic': 4, 'heroic_stores': ['Epic']}
        tiles = setup.found_tiles(counts, facts(torrent={**TOR, 'error': 'refused'},
                                                drives=[{**GAMES, 'steam_games': 1}]),
                                  heroic=False)
        self.assertEqual([(t[2], t[3]) for t in tiles], [("Steam", "1 game · on 1 drive")])
        self.assertEqual(setup.found_tiles({}, None, heroic=True), [])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Torrent(unittest.TestCase):
    def test_states(self):
        self.assertEqual(setup.torrent_state(None)[0], 'none')
        state, title, note = setup.torrent_state(TOR)
        self.assertEqual(state, 'ok')
        self.assertEqual(title, "Transmission, downloading to /home/u/Downloads/torrents")
        state, title, note = setup.torrent_state({**TOR, 'error': 'refused'})
        self.assertEqual(state, 'error')
        self.assertEqual(note, "Couldn't ask where it downloads (refused)")


@unittest.skipIf(nx is None, "PyQt6 not available")
class Services(unittest.TestCase):
    """Connect services: empty writes nothing, a filled field writes its key
    only, a Steam key turns Friends on and Game-db its switch (user
    2026-11-11); a bad key or half a Game-db pair blocks Open my library."""

    KEY = 'A' * 32
    TOKEN = 'gdb_' + 'x' * 43

    def card(self, key):
        spec = next(s for s in setup.SERVICES if s['key'] == key)
        c = setup.ServiceCard(spec)
        self.addCleanup(c.deleteLater)
        return c

    def test_empty_writes_nothing(self):
        for s in setup.SERVICES:
            c = self.card(s['key'])
            self.assertEqual(c.value({}), {})
            self.assertIsNone(c.error())

    def test_sgdb_writes_only_its_key(self):
        c = self.card('sgdb')
        c.fields[0].setText(' abc ')
        self.assertEqual(c.value({}), {('steamgriddb', 'api_key'): 'abc'})
        self.assertEqual(c.value({'api_key': 'abc'}), {})            # unchanged

    def test_steam_key_turns_friends_on(self):
        c = self.card('steam')
        c.fields[0].setText(self.KEY)
        self.assertEqual(c.value({}), {('steam', 'web_api_key'): self.KEY,
                                       ('friends', 'enabled'): True})
        self.assertEqual(c.value({'friends_enabled': True}),
                         {('steam', 'web_api_key'): self.KEY})
        c.fields[0].setText('abc123')
        self.assertIn("32 letters and digits", c.error())
        self.assertFalse(c.err.isHidden())

    def test_gamedb_pair(self):
        c = self.card('gamedb')
        url, tok = c.fields
        url.setText('https://games.home.lan/')
        self.assertEqual(c.error(), "Add the token too, or clear the address.")
        tok.setText(self.TOKEN)
        self.assertIsNone(c.error())
        self.assertEqual(c.value({}), {('gamedb', 'url'): 'https://games.home.lan',
                                       ('gamedb', 'token'): self.TOKEN,
                                       ('gamedb', 'enabled'): True})
        url.setText('http://games.home.lan')
        self.assertIn("https", c.error())
        url.clear()
        self.assertEqual(c.error(), "Add the token too, or clear the address.")
        tok.setText('gdb_short')
        url.setText('https://games.home.lan')
        self.assertIn("gdb_", c.error())

    def test_gamedb_env_token_and_unchanged_url(self):
        from nexus import gamedb
        c = self.card('gamedb')
        with mock.patch.object(gamedb, 'settings', return_value={'token_source': 'env'}):
            c.load({'gamedb_url': 'https://g.lan/', 'gamedb_cfg_enabled': True})
        self.assertIsNone(c.error())                          # the env token counts
        self.assertEqual(c.value({'gamedb_url': 'https://g.lan/', 'gamedb_cfg_enabled': True}),
                         {})
        c.fields[0].setText('https://other.lan')
        self.assertEqual(c.value({'gamedb_url': 'https://g.lan/'}),
                         {('gamedb', 'url'): 'https://other.lan', ('gamedb', 'enabled'): True})
        d = self.card('gamedb')                               # no env token: the pair rule
        with mock.patch.object(gamedb, 'settings', return_value={'token_source': None}):
            d.load({'gamedb_url': 'https://g.lan/'})
        self.assertEqual(d.error(), "Add the token too, or clear the address.")
        d.fields[1].setText(self.TOKEN)
        self.assertEqual(d.value({'gamedb_url': 'https://g.lan/', 'gamedb_cfg_enabled': False}),
                         {('gamedb', 'token'): self.TOKEN, ('gamedb', 'enabled'): True})

    def test_open_buttons_open_the_right_pages(self):
        from nexus.gui import settings
        opened = []
        with mock.patch.object(setup.QDesktopServices, 'openUrl',
                               lambda u: opened.append(u.toString())):
            self.card('sgdb').btn_open.click()
            self.card('steam').btn_open.click()
            g = self.card('gamedb')
            g.btn_open.click()                                     # no address yet
            self.assertIn("address", g.error_text())
            g.fields[0].setText('https://games.home.lan')
            g.btn_open.click()
        self.assertEqual(opened, [settings.SGDB_KEY_URL, settings.STEAM_KEY_URL,
                                  'https://games.home.lan/account'])

    def test_account_url(self):
        from nexus import gamedb
        self.assertEqual(gamedb.account_url('https://g.lan/'), 'https://g.lan/account')
        with self.assertRaises(gamedb.GameDbError):
            gamedb.account_url('ftp://g.lan')



@unittest.skipIf(nx is None, "PyQt6 not available")
class Page(unittest.TestCase):
    """The page: nothing found waits for a pick, late facts never undo the
    user's pick, the step list follows the fork."""

    def page(self, config=None):
        p = setup.SetupPage()
        self.addCleanup(p.deleteLater)
        with mock.patch.object(setup, 'cli_json'):
            p.open(config or {}, {})
        return p

    def test_nothing_found_waits_for_a_pick(self):
        p = self.page()
        p.set_found({})
        p.set_facts(facts(), '')
        self.assertFalse(p.nothing.isHidden())
        self.assertIsNone(p.fork.currentData())
        self.assertFalse(p.btn_next.isEnabled())
        self.assertFalse(p.next_hint.isHidden())
        p.fork.group.button(p.fork.findData(False)).click()
        self.assertTrue(p.btn_next.isEnabled())
        self.assertTrue(p.next_hint.isHidden())
        self.assertEqual([k for k, _n in p.steps], ['play', 'look', 'services'])
        self.assertEqual(p.step_note.text(), "1 of 2 · How you play")

    def test_late_facts_keep_the_users_pick(self):
        p = self.page()
        p.fork.group.button(p.fork.findData(False)).click()
        p.set_facts(facts(torrent=TOR), '')
        self.assertIs(p.fork.currentData(), False)
        self.assertTrue(all(w.isHidden() for w in p.fork_why.values()))

    def test_pick_with_its_reason_and_a_fourth_step(self):
        p = self.page()
        p.set_facts(facts(torrent=TOR), '')
        self.assertIs(p.fork.currentData(), True)
        self.assertFalse(p.fork_why[True].isHidden())
        self.assertIn("Transmission", p.fork_why[True].text())
        self.assertEqual(p.step_note.text(), "1 of 3 · How you play")
        self.assertEqual(p.track.optional_from, 3)

    def test_where_from_the_facts(self):
        p = self.page({'install_base': '/mnt/games/Mine', 'torrent_client': 'transmission',
                       'torrent_url': 'http://127.0.0.1:9091'})
        p.set_facts(facts(torrent=TOR, drives=[SMALL, NAS, GAMES, ROOT]), '')
        self.assertEqual([c.drive['id'] for c in p.drives.cards()], ['a', 'b', 'c'])
        self.assertEqual(p.drives.currentData(), 'b')         # the configured folder's drive
        self.assertEqual(p.folder_label.text(), '/mnt/games/Mine')
        rows = p._rows(p.drive_rows)
        self.assertEqual(len(rows), 1)
        self.assertIn("/mnt/nas", rows[0].need['title'])
        u, _ = p.answers_now()                                # nothing changed
        self.assertFalse({('install', 'base'), ('torrent', 'client'), ('torrent', 'url')} & set(u))
        self.assertEqual(u[('install', 'scan_dir')], TOR['download_dir'])
        p.drives.group.button(p.drives.findData('c')).click()
        self.assertEqual(p.answers_now()[0][('install', 'base')], '/mnt/small/Games')
        p.use_torrent.setChecked(False)
        self.assertNotIn(('install', 'scan_dir'), p.answers_now()[0])

    def test_torrent_error_keeps_the_switch_but_not_the_folder(self):
        p = self.page()
        p.set_facts(facts(torrent={**TOR, 'error': 'refused'}), '')
        self.assertFalse(p.tor_card.isHidden())
        self.assertIn("refused", p.tor_note.text())
        p.fork.group.button(p.fork.findData(True)).click()
        u, _ = p.answers_now()
        self.assertEqual(u[('torrent', 'client')], 'transmission')
        self.assertNotIn(('install', 'scan_dir'), u)

    def test_facts_failed_one_default_card(self):
        p = self.page()
        p.set_facts(None, 'timed out')
        self.assertEqual(p.drives.count(), 1)
        self.assertEqual(setup.drive_text(p.drives.cards()[0].drive)[1], "Free space unknown")
        self.assertEqual(p._rows(p.need_rows)[0].need['title'],
                         "Couldn't look at this computer (timed out). Nexus uses the defaults.")

    def test_late_facts_keep_the_users_folder_and_switch(self):
        p = self.page()
        with mock.patch.object(setup.QFileDialog, 'getExistingDirectory',
                               return_value='/mnt/games/Mine'):
            p._change_folder()
        p.use_torrent.click()                                 # off, before the facts
        p.set_facts(facts(torrent=TOR), '')
        self.assertEqual(p.folder_label.text(), '/mnt/games/Mine')
        self.assertEqual(p.drives.currentData(), 'b')         # its drive, marked
        self.assertFalse(p.use_torrent.isChecked())
        p.fork.group.button(p.fork.findData(True)).click()
        u, _ = p.answers_now()
        self.assertEqual(u[('install', 'base')], '/mnt/games/Mine')
        self.assertNotIn(('torrent', 'client'), u)
        q = self.page()                                       # a folder on no card
        with mock.patch.object(setup.QFileDialog, 'getExistingDirectory',
                               return_value='/elsewhere/Games'):
            q._change_folder()
        q.set_facts(facts(drives=[GAMES]), '')
        self.assertEqual(q.folder_label.text(), '/elsewhere/Games')
        self.assertIsNone(q.drives.currentData())

    def test_counts_before_facts_pick_nothing(self):
        p = self.page()
        p.set_found({'steam': 40})
        self.assertIsNone(p.fork.currentData())
        self.assertFalse(p.btn_next.isEnabled())
        self.assertEqual(p.next_hint.text(), "Looking at this computer…")
        p.set_facts(facts(torrent=TOR), '')                   # the running client counts
        self.assertIs(p.fork.currentData(), True)
        self.assertTrue(p.btn_next.isEnabled())
        q = self.page()
        q.set_found({'steam': 40})
        q.set_facts(None, 'timed out')                        # failed: counts decide
        self.assertIs(q.fork.currentData(), False)

    def test_agent_title_follows_the_fork(self):
        p = self.page()
        p.fork.group.button(p.fork.findData(False)).click()
        self.assertEqual(p.agent_card.title.text(), setup.AGENT_TITLE[False])
        self.assertNotIn("download", p.agent_card.title.text().lower())
        p.fork.group.button(p.fork.findData(True)).click()
        self.assertEqual(p.agent_card.title.text(), setup.AGENT_TITLE[True])

    def test_services_set_up_again_write_nothing_new(self):
        key = 'C' * 32
        p = self.page({'steam_web_api_key': key, 'friends_enabled': True, 'api_key': 'k'})
        self.assertEqual(p.services[1].fields[0].text(), key)
        u, _ = p.answers_now()
        self.assertFalse({('steam', 'web_api_key'), ('friends', 'enabled'),
                          ('steamgriddb', 'api_key')} & set(u))

    def test_a_bad_key_blocks_open_my_library(self):
        p = self.page()
        p.fork.group.button(p.fork.findData(False)).click()
        p.show_step(2)
        self.assertEqual(p.key(), 'services')
        self.assertTrue(p.btn_next.isEnabled())
        p.services[1].fields[0].setText('abc')
        self.assertFalse(p.btn_next.isEnabled())
        p.services[1].fields[0].clear()
        self.assertTrue(p.btn_next.isEnabled())


@unittest.skipIf(nx is None, "PyQt6 not available")
class Fit(unittest.TestCase):
    """Make it yours and Connect services fit 1280x800 at 125 %, no scroll,
    the buttons in view; on Linux and with the Windows feature set."""

    def setUp(self):
        from nexus.gui import theme
        self.font, self.fonts = QApplication.font(), dict(vars(theme.FONTS))
        theme.resolve_fonts()                  # the bundled faces, as the app has them

    def tearDown(self):
        from nexus.gui import theme
        vars(theme.FONTS).update(self.fonts)
        nx.apply_appearance(nx.APPEARANCE_DEFAULT)
        QApplication.setFont(self.font)

    def test_steps_three_and_four_fit_at_125(self):
        for feats in (features.ALL, WINDOWS):
            for scale in (1.0, 1.25):
                with self.subTest(windows=feats is WINDOWS, scale=scale), \
                        mock.patch.object(features, 'FEATURES', feats):
                    nx.apply_appearance({'scale': scale})
                    f = QFont(nx.FONTS.ui)                     # as app.py sets it
                    f.setPixelSize(nx.scaled(nx.FS['sm']))
                    QApplication.setFont(f)
                    p = setup.SetupPage()
                    self.addCleanup(p.deleteLater)
                    p.setStyleSheet(nx.build_stylesheet())
                    with mock.patch.object(setup, 'cli_json'):
                        p.open({}, {'scale': scale})
                    p.set_found({'steam': 3})
                    p.set_facts(facts(torrent=TOR), '')
                    p.resize(1280, 800)
                    p.show()
                    for key in ('look', 'services'):
                        p.show_step([k for k, _n in p.steps].index(key))
                        for _ in range(5):
                            QApplication.processEvents()
                        self.assertEqual(p.scroll.verticalScrollBar().maximum(), 0, key)
                        bottom = p.btn_next.mapTo(p, p.btn_next.rect().bottomLeft()).y()
                        self.assertLessEqual(bottom, 800, key)
                    p.hide()


if __name__ == '__main__':
    unittest.main()
