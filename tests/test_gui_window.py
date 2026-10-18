"""MainWindow smoke tests: page registry, top-bar navigation, the shared
details panel, Home's rows. Offscreen with the CLI stubbed out, so nothing
runs, nothing is fetched and the user's prefs are never written."""

import json
import unittest
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx

if nx is not None:
    from PyQt6.QtWidgets import QApplication


def fake_games():
    return [
        {'slug': 'alpha', 'display_name': 'Alpha', 'source': 'nexus', 'launcher': '/a',
         'last_played': '2026-09-30T10:00:00', 'playtime_minutes': 120,
         'added_at': '2026-09-01T10:00:00'},
        {'slug': 'steam-1', 'display_name': 'Beta', 'source': 'steam', 'appid': 1,
         'launcher': 'steam://rungameid/1', 'last_played': '2026-09-29T10:00:00'},
        {'slug': 'gamma', 'display_name': 'Gamma', 'source': 'nexus', 'launcher': '/g',
         'update_pending': True},
    ]


@unittest.skipIf(nx is None, "PyQt6 not available")
class Pure(unittest.TestCase):
    def test_shelf_page_target(self):
        f = nx.shelf_page_target
        self.assertEqual(f(0, 0, 500, 162, 1), 0)              # nothing to scroll
        self.assertEqual(f(0, 1000, 500, 162, 1), 486)         # three whole tiles
        self.assertEqual(f(486, 1000, 500, 162, -1), 0)
        self.assertEqual(f(900, 1000, 500, 162, 1), 1000)      # clamped at the end
        self.assertEqual(f(0, 1000, 100, 162, 1), 162)         # at least one tile
        self.assertEqual(f(10, 1000, 300, 0, 1), 310)          # no pitch: a viewport

    def test_featured_games_order(self):
        games = fake_games()
        games[2]['playing'] = True
        out = [g['slug'] for g in nx.featured_games(games)]
        self.assertEqual(out[0], 'gamma')                      # playing first
        self.assertEqual(out[1:3], ['alpha', 'steam-1'])       # then most recent
        self.assertEqual(len(out), len(set(out)))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Collections(unittest.TestCase):
    def test_prefs_round_trip_and_rules(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(nx.LibraryPrefs, 'PATH', Path(d) / 'library.json'):
            p = nx.LibraryPrefs()
            self.assertEqual(p.add_collection('  Couch   co-op '), 'Couch co-op')
            self.assertEqual(p.add_collection('couch CO-OP'), 'Couch co-op')   # same one
            self.assertIsNone(p.add_collection('   '))
            self.assertTrue(p.toggle_member('Couch co-op', 'alpha'))
            p.add_collection('Short')
            self.assertIsNone(p.rename_collection('Short', 'couch co-op'))     # taken
            self.assertEqual(p.rename_collection('Short', 'Quick'), 'Quick')
            q = nx.LibraryPrefs()
            self.assertEqual(list(q.collections), ['Couch co-op', 'Quick'])     # order kept
            self.assertEqual(q.collections_of('alpha'), ['Couch co-op'])
            self.assertFalse(q.toggle_member('Couch co-op', 'alpha'))
            q.delete_collection('Quick')
            self.assertEqual(list(nx.LibraryPrefs().collections), ['Couch co-op'])

    def test_actions_offer_every_collection(self):
        saved = list(nx.COLLECTION_NAMES)
        nx.COLLECTION_NAMES[:] = ['Couch', 'Short']
        try:
            g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a', 'collections': ['Short']}
            acts = {a.key: a for a in nx.game_actions(g)}
            self.assertFalse(acts['col:Couch'].checked)
            self.assertTrue(acts['col:Short'].checked)
            self.assertIn('col-new', acts)
            owned = {'slug': 's', 'source': 'steam', 'appid': 1, 'installed': False}
            keys = [a.key for a in nx.game_actions(owned)]
            self.assertIn('col:Couch', keys)                  # not installed: still sortable
            self.assertIn('col:Couch', [a.key for a in nx.game_actions(g, theatre=True)])
        finally:
            nx.COLLECTION_NAMES[:] = saved

    def test_dashboard_gets_the_new_widgets_in_place(self):
        old = [{'id': k, 'size': d, 'hidden': h} for k, _s, d, h in nx.DASH_KINDS
               if k not in ('surprise', 'collections')]
        ids = [it['id'] for it in nx.dash_normalize(old)]
        self.assertEqual(ids[ids.index('jump') + 1], 'surprise')
        self.assertEqual(ids[ids.index('favourites') + 1], 'collections')
        self.assertEqual([i for i in ids if i not in ('surprise', 'collections')],
                         [it['id'] for it in old])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Surprise(unittest.TestCase):
    class Rng:
        def __init__(self):
            self.seen = None

        def choices(self, pop, weights):
            self.seen = dict(zip([g['slug'] for g in pop], weights))
            return [pop[weights.index(max(weights))]]

    def games(self):
        return [{'slug': 'fresh', 'launcher': '/f'},
                {'slug': 'old', 'launcher': '/o', 'last_played': '2026-01-01T10:00:00'},
                {'slug': 'recent', 'launcher': '/r', 'last_played': '2026-10-10T10:00:00',
                 'favorite': True},
                {'slug': 'hidden', 'launcher': '/h', 'hidden': True},
                {'slug': 'playing', 'launcher': '/p', 'playing': True},
                {'slug': 'owned', 'source': 'steam', 'installed': False}]

    def test_only_playable_games_weighted(self):
        from datetime import datetime
        rng = self.Rng()
        g = nx.surprise_pick(self.games(), rng, now=datetime(2026, 10, 14))
        self.assertEqual(rng.seen, {'fresh': 3.0, 'old': 2.0, 'recent': 1.5})
        self.assertEqual(g['slug'], 'fresh')

    def test_recent_picks_are_skipped_while_others_remain(self):
        import random
        rng = random.Random(1)
        for _ in range(30):
            g = nx.surprise_pick(self.games(), rng, recent=('fresh', 'old'))
            self.assertEqual(g['slug'], 'recent')
        g = nx.surprise_pick(self.games()[:1], rng, recent=('fresh',))
        self.assertEqual(g['slug'], 'fresh')             # nothing else: repeat it
        self.assertIsNone(nx.surprise_pick(self.games()[3:], rng))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Search(unittest.TestCase):
    def test_ranking(self):
        games = [{'slug': 'a', 'display_name': 'The Witcher 3: Wild Hunt'},
                 {'slug': 'b', 'display_name': 'Witchfire', 'playtime_minutes': 5},
                 {'slug': 'c', 'display_name': 'Mystery Witch', 'installed': False},
                 {'slug': 'd', 'display_name': 'Hades'}]
        self.assertEqual([g['slug'] for g in nx.search_games(games, 'witch')],
                         ['b', 'a', 'c'])          # starts with, then a word, owned last
        self.assertEqual([g['slug'] for g in nx.search_games(games, 'wild witcher')], ['a'])
        self.assertEqual([g['slug'] for g in nx.search_games(games, "witcher 3: ")], ['a'])
        self.assertEqual(nx.search_games(games, '   '), [])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Actions(unittest.TestCase):
    def test_theatre_drops_desktop_only_actions(self):
        g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a', 'steam': True}
        desk = [a.key for a in nx.game_actions(g)]
        couch = [a.key for a in nx.game_actions(g, theatre=True)]
        self.assertIn('open-folder', desk)
        self.assertIn('tool:winecfg', desk)
        self.assertIn('remove-steam', desk)                    # checked toggle flips
        self.assertFalse([k for k in couch if k.startswith(('open-folder', 'tool:'))])
        self.assertIn('uninstall', couch)

    def test_menu_renders_every_action(self):
        g = {'slug': 's', 'source': 'steam', 'appid': 1, 'launcher': 'steam://x',
             'favorite': True}
        m = nx.build_game_menu(None, g, lambda *a: None)

        def leaves(menu):          # submenus (e.g. Collections) hold their own items
            for a in menu.actions():
                if a.menu() is not None:
                    yield from leaves(a.menu())
                elif not a.isSeparator():
                    yield a
        items = list(leaves(m))
        self.assertEqual(len(items), len(nx.game_actions(g)))
        fav = next(a for a in items if a.text() == "Favourite")
        self.assertTrue(fav.isChecked())
        m.deleteLater()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Window(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        nx.cli_json = lambda *a, **k: None
        nx.LibraryPrefs.save = lambda self: None
        nx.CliQuery = lambda *a, **k: None

    @classmethod
    def tearDownClass(cls):
        nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery = cls._saved

    def setUp(self):
        self.win = nx.MainWindow()
        self.win.resize(1300, 850)
        self.win.show()
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        from PyQt6.QtCore import QEvent
        self.win.jobs.jobs.clear()
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        # processEvents() never runs deleteLater outside an event loop: every
        # test's window used to stay alive, and each restyle repolished them all.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def test_collections_chips_filter_rename_delete(self):
        w = self.win
        w.prefs.collections = {}
        w._create_collection('Couch', 'alpha')
        self.assertEqual(w.game('alpha')['collections'], ['Couch'])
        self.assertIn('col:Couch', w.chips)
        w.go(w.LIBRARY)
        w._set_chip('col:Couch')
        self.assertEqual([g['slug'] for g in w._shown_games], ['alpha'])
        w.on_game_action('col:Couch', w.game('gamma'))
        self.assertEqual(sorted(g['slug'] for g in w._shown_games), ['alpha', 'gamma'])
        w._rename_collection('Couch', 'Sofa')
        self.assertEqual(w._chip, 'col:Sofa')
        self.assertTrue(w.chips['col:Sofa'].isChecked())
        self.assertNotIn('col:Couch', w.chips)
        w._delete_collection('Sofa')
        self.assertEqual(w._chip, 'all')                       # its chip is gone
        self.assertEqual(w.game('alpha')['collections'], [])
        self.assertEqual(nx.COLLECTION_NAMES, [])

    def test_surprise_opens_a_game_page(self):
        w = self.win
        w.go(w.LIBRARY)
        w.surprise()
        self.assertEqual(len(w.selected), 1)
        self.assertTrue(w.details.isVisible())
        self.assertIn(w.selected[0], ('alpha', 'steam-1', 'gamma'))

    def test_quit_waits_for_running_jobs(self):
        import time
        from PyQt6.QtGui import QCloseEvent
        # Exits 0.3 s after SIGTERM, like a CLI stopping wine cleanly.
        job = nx.Job(['sh', '-c', 'trap "sleep 0.3; exit 0" TERM; '
                      'while :; do sleep 0.05; done'], "Fake install", 'alpha', 'install')
        self.win.jobs.submit(job)
        self.assertTrue(job.proc.waitForStarted(3000))
        ev = QCloseEvent()
        self.win.closeEvent(ev)
        self.assertFalse(ev.isAccepted())                      # asks first
        self.assertTrue(self.win.sheet.isVisible())
        self.win.sheet.close_sheet()
        self.win.quit_after_jobs()
        self.assertEqual(job.state, 'cancelling')
        self.assertTrue(self.win.isVisible())                  # still there meanwhile
        end = time.monotonic() + 5
        while self.win.isVisible() and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        self.assertEqual(job.state, 'cancelled')
        self.assertFalse(self.win.isVisible())

    def test_first_paint_from_the_cached_library(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / 'library.json'
            args = ['--library', '--json'] + (['--owned'] if self.win.prefs.show_owned else [])
            cache.write_text(json.dumps({'args': args,
                                         'data': {'games': fake_games(), 'disks': []}}))
            with mock.patch.object(nx.MainWindow, 'LIB_CACHE', cache):
                win = nx.MainWindow()
                try:
                    self.assertEqual(len(win._games), 3)              # before the CLI answers
                    alpha = win.game('alpha')
                    self.assertTrue(alpha['stale'])
                    self.assertFalse(nx.play_enabled(alpha))          # not until confirmed
                    fresh = fake_games()[:2]
                    win._on_library({'games': fresh, 'disks': []}, '')
                    self.assertFalse(win.game('alpha')['stale'])
                    self.assertTrue(nx.play_enabled(win.game('alpha')))
                    saved = json.loads(cache.read_text())
                    self.assertEqual(len(saved['data']['games']), 2)  # the new scan is kept
                finally:
                    win.close()
                    win.deleteLater()
                    QApplication.processEvents()

    def test_pad_opens_deal_links_in_the_sheet(self):
        card = nx.DealCard({'title': 'Deal Game', 'appid': 10, 'links': {}}, 'deals', None,
                           set())
        card.links = [("Open in Steam", 'steam://store/10'),
                      ("Open on Game-db", 'https://gamedb.example/g/10')]
        self.win.dealspage.layout().addWidget(card)
        self.win.go(self.win.DEALS)
        self.win.input.set('pad')
        card._menu(card.mapToGlobal(card.rect().center()))
        self.assertTrue(self.win.sheet.isVisible())
        rows = [b.text() for b in self.win.sheet.findChildren(nx.QPushButton)]
        self.assertEqual(rows, ["Open in Steam", "Open on Game-db", "Copy link"])
        self.win.sheet.close_sheet()

    WISH = {'configured': True, 'url': None, 'summary': {'wishlist': [
        {'appid': 10, 'title': 'Hollow Knight: Silksong', 'price': {'onSale': True,
                                                                     'currentStr': '9 €'}},
        {'appid': 11, 'title': 'Elden Ring', 'price': {'onSale': True, 'currentStr': '20 €'}},
        {'appid': 12, 'title': 'No Repack', 'price': {'onSale': True, 'currentStr': '1 €'}}]},
        'repacks': {'10': {'slug': 'hollow-knight-silksong', 'title': 'Hollow Knight: Silksong',
                           'url': 'https://fitgirl-repacks.site/hollow-knight-silksong/',
                           'confidence': 'exact'},
                    '11': {'slug': 'elden-ring', 'title': 'ELDEN RING: Deluxe Edition',
                           'url': 'https://fitgirl-repacks.site/elden-ring/',
                           'confidence': 'close'}}}

    def _deals(self):
        d = self.win.dealspage
        # Not the state the real app left in library.json.
        d._section, d._filter, d._sort, d._hide_owned, d._query = (
            'deals', 'sale', 'best', False, '')
        d.owned_chip.blockSignals(True)
        d.owned_chip.setChecked(False)
        d.owned_chip.blockSignals(False)
        d.res = json.loads(json.dumps(self.WISH))
        d._loaded = True
        d._render()
        return d

    def test_deals_show_fitgirl_matches_and_filter(self):
        d = self._deals()
        cards = {c.item['appid']: c for c in d._cards}
        self.assertIn("FitGirl", cards[10].art.badges())
        self.assertIn("FitGirl?", cards[11].art.badges())                # a likely match
        self.assertNotIn("FitGirl", cards[12].art.badges())
        self.assertIsNone(cards[12].grab)
        self.assertIsNotNone(cards[10].grab)
        d._pick('_filter', 'fitgirl')
        self.assertEqual(sorted(c.item['appid'] for c in d._cards), [10, 11])
        chips = [b.text() for b in d.sub_host.findChildren(nx.QPushButton)]
        self.assertIn("On FitGirl  2", chips)
        self.assertIn("2 games", d.summary.text())
        # A download for the game shows on its card and disables Grab.
        d.set_downloads([{'hash': 'a' * 40, 'appid': '10', 'percent': 42, 'tracked': True}])
        card = next(c for c in d._cards if c.item['appid'] == 10)
        self.assertEqual(card.dl.text(), "Downloading 42%")
        self.assertFalse(card.grab.isEnabled())
        d.forget_repack('11')
        self.assertEqual([c.item['appid'] for c in d._cards], [10])

    def test_deals_sort_search_owned_and_spotlight(self):
        d = self._deals()
        d.res['owned_appids'] = [11]
        wl = d.res['summary']['wishlist']
        wl[0]['price'].update(cutPct=10, current=9)
        wl[1]['price'].update(cutPct=50, current=20, atHistoricalLow=True)
        wl[2]['price'].update(cutPct=30, current=1)
        with mock.patch.object(d, '_save_state'):
            d.spot.items = []                    # it keeps its place on a refresh otherwise
            d._render()
            self.assertFalse(d.spot.isHidden())
            self.assertEqual(d.spot.current()['appid'], 11)          # ATL first
            self.assertEqual([c.item['appid'] for c in d._cards], [11, 12, 10])
            d._pick('_sort', 'price')
            self.assertEqual([c.item['appid'] for c in d._cards], [12, 10, 11])
            self.assertIn("Owned", next(c for c in d._cards if c.item['appid'] == 11)
                          .art.badges())
            d.owned_chip.setChecked(True)
            self.assertEqual([c.item['appid'] for c in d._cards], [12, 10])
            self.assertNotEqual(d.spot.current().get('appid'), 11)
            d.search.setText("silk")
            d._search_timer.timeout.emit()
            self.assertEqual([c.item['appid'] for c in d._cards], [10])
            self.assertTrue(d.spot.isHidden())                       # not while searching

    def test_deal_price_helpers(self):
        self.assertEqual(nx.deal_was({'current': 10.03, 'cutPct': 44, 'currentStr': '€10.03'}),
                         '€17.91')
        self.assertEqual(nx.deal_was({'current': 9.0, 'cutPct': 25, 'currentStr': '9,00€'}),
                         '12,00€')
        self.assertIsNone(nx.deal_was({'current': 9.0, 'cutPct': 0, 'currentStr': '€9'}))
        self.assertEqual(nx.DealsPage._month({'releaseDate': '2031-03-05T00:00:00.000Z'}),
                         "March 2031")
        self.assertEqual(nx.DealsPage._month({'releaseDate': None}), "Later")

    def test_grab_reads_the_page_then_offers_the_sheet(self):
        d = self._deals()
        calls = []
        page = {'title': 'Hollow Knight: Silksong – v1.0', 'repack_size': 'from 5 GB',
                'original_size': '8 GB', 'size_bytes': 5 << 30, 'magnets': 1,
                'client': 'transmission', 'dest': '/dl', 'free_bytes': 1 << 30}
        with mock.patch.object(nx, 'cli_json', lambda a, cb, *_x: (calls.append(a), cb(
                page if a[0] == '--repack-page' else {'hash': 'b' * 40}, None))):
            card = next(c for c in d._cards if c.item['appid'] == 10)
            card.grab.click()
            self.assertEqual(calls[0][:2], ['--repack-page', 'hollow-knight-silksong'])
            self.assertTrue(self.win.sheet.isVisible())
            self.assertIn("not enough space", self.win.sheet.sub.text())
            rows = [b for b in self.win.sheet.findChildren(nx.QPushButton)
                    if b.objectName() == 'SheetRow']
            self.assertEqual(rows[0].text(), "Download — install when it's done")
            rows[0].click()
            grab = next(a for a in calls if a[0] == '--repack-grab')
            self.assertEqual(grab, ['--repack-grab', 'hollow-knight-silksong', '--appid', '10',
                                    '--json', '--auto-install'])

    def test_downloads_page_rows_and_states(self):
        w = self.win
        dl = w.downloads
        w.go(w.DOWNLOADS)
        self.assertEqual(w.current(), w.DOWNLOADS)
        dl.set_data({'torrents': [], 'error': {'kind': 'torrent', 'message': 'nope'},
                     'configured': False})
        self.assertFalse(dl.empty_host.isHidden())
        self.assertEqual(dl.empty.title.text(), "Connect your torrent client")
        dl.set_detected({'client': 'transmission', 'url': 'http://127.0.0.1:9091'})
        self.assertEqual(dl.empty.button.text(), "Use Transmission")
        rows = [{'hash': 'a' * 40, 'name': 'Game A', 'percent': 50.0, 'done': False,
                 'state': 'downloading', 'rate': 2 << 20, 'eta': 600, 'tracked': True,
                 'auto_install': True, 'size': 5 << 30},
                {'hash': 'b' * 40, 'name': 'Game B', 'percent': 100.0, 'done': True,
                 'state': 'paused', 'ratio': 0.57, 'tracked': False}]
        dl.set_data({'torrents': rows, 'client': {'client': 'Transmission',
                                                  'version': '4.1.3', 'down': 0, 'up': 0}})
        self.assertTrue(dl.empty_host.isHidden())
        self.assertEqual(len(dl.rows), 2)
        a = dl.rows['a' * 40]
        self.assertIn("50%", a.state.text())
        self.assertIn("10 min left", a.state.text())
        self.assertEqual(a.tag.text(), "Nexus · installs when done")
        self.assertTrue(dl.rows['b' * 40].tag.isHidden())
        self.assertEqual(dl.active_count(), 1)
        sent = []
        dl.control.connect(lambda *x: sent.append(x))
        a.btn_toggle.click()
        dl.rows['b' * 40].btn_toggle.click()
        self.assertEqual(sent, [('pause', ['a' * 40], False), ('resume', ['b' * 40], False)])
        dl._pick_filter('done')
        self.assertEqual(list(dl.rows), ['b' * 40])
        same = dl.rows['b' * 40]
        dl.set_data({'torrents': rows, 'client': {}})              # rows are reused
        self.assertIs(dl.rows['b' * 40], same)

    def test_finished_download_installs_once_and_says_which_torrent(self):
        import tempfile
        from pathlib import Path
        w = self.win
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d, 'Game [FitGirl Repack]')
            folder.mkdir()
            row = {'hash': 'c' * 40, 'name': folder.name, 'path': str(folder), 'done': True,
                   'tracked': True, 'auto_install': True, 'installed': False, 'state': 'seeding'}
            started = []
            data = {'torrents': [row], 'client': {}}
            with mock.patch.object(nx, 'cli_json', lambda a, cb, *_x: cb(data, None)), \
                    mock.patch.object(w, '_start_install', lambda cmd, *a: started.append(cmd)):
                w._torrent_polling = False
                w._poll_torrents()
                w._torrent_polling = False
                w._poll_torrents()                                   # not twice
                self.assertEqual(len(started), 1)
                self.assertEqual(started[0][-2:], ['--from-torrent', 'c' * 40])
                self.assertNotIn('--torrent-done', sum(started, []))
                # One that already started (and failed) waits for "Install it now".
                w._torrents_started.clear()
                row['install_started'] = '2026-10-17T18:21:05'
                w._torrent_polling = False
                w._poll_torrents()
                self.assertEqual(len(started), 1)
                w._download_action('install', row)
                self.assertEqual(len(started), 2)

    def test_remove_with_files_asks_first(self):
        w = self.win
        with mock.patch.object(nx, 'cli_json', lambda *a, **k: None):
            w._torrent_control('remove', ['a' * 40], True)
        self.assertTrue(w.sheet.isVisible())
        rows = [b.text() for b in w.sheet.findChildren(nx.QPushButton)
                if b.objectName() == 'SheetRow']
        self.assertEqual(rows, ["Remove and delete files", "Cancel"])
        w.sheet.close_sheet()

    def test_starts_on_home(self):
        self.assertEqual(self.win.current(), self.win.HOME)
        self.assertEqual(self.win.tabs.current(), self.win.HOME)
        self.assertFalse(self.win.homeview.hero.isHidden())

    def test_every_page(self):
        for key in self.win.pages:
            self.win.go(key)
            self.assertEqual(self.win.current(), key)
            expect = key if key in self.win.TABS else ''
            self.assertEqual(self.win.tabs.current(), expect)
            for p, icon in self.win.icons.items():
                self.assertEqual(icon.isChecked(), p == key)

    def test_pad_paging_cycles_tabs(self):
        self.win.go(self.win.HOME)
        self.win._pad_action('page-next')
        self.assertEqual(self.win.current(), self.win.LIBRARY)
        self.win._pad_action('page-prev')
        self.win._pad_action('page-prev')
        self.assertIn(self.win.current(), self.win.TABS)

    def test_escape_returns_home(self):
        self.win.go(self.win.SETTINGS)
        self.win._escape()
        self.assertEqual(self.win.current(), self.win.HOME)

    def test_details_shared_and_hidden_elsewhere(self):
        game = self.win._games[0]
        self.win._tile_activated(game)
        QApplication.processEvents()
        self.assertTrue(self.win.details.isVisible())
        self.win.go(self.win.HEALTH)
        self.assertFalse(self.win.details.isVisible())
        self.win.go(self.win.LIBRARY)
        self.assertTrue(self.win.details.isVisible())

    def test_home_rows_and_tiles(self):
        tiles = self.win.homeview.tiles
        self.assertIn('alpha', tiles)
        rows = [r for r in self.win.homeview.findChildren(nx.ShelfRow)]
        self.assertTrue(rows)
        self.assertTrue(any(t.kind == 'wide' for ts in tiles.values() for t in ts))

    def test_search_jumps_to_library(self):
        self.win.go(self.win.HOME)
        self.win.search.setText('alp')
        self.assertEqual(self.win.current(), self.win.LIBRARY)
        self.win.apply_filter()
        self.assertEqual(list(self.win.library.tiles), ['alpha'])

    def test_compact_topbar(self):
        self.win.resize(860, 600)
        QApplication.processEvents()
        self.assertEqual(self.win.btn_install.text(), "")          # icon only
        self.win.resize(1300, 850)
        QApplication.processEvents()
        self.assertIn("Install", self.win.btn_install.text())

    def test_dashboard_edit_mode(self):
        d = self.win.dashboard
        self.win.prefs.dashboard = nx.dash_default()     # not the user's own layout
        d.relayout(force=True)
        self.assertFalse(d.editing)
        d.set_editing(True)
        self.assertTrue(d.widgets['system'].editbar.isVisibleTo(d))
        before = [i['id'] for i in self.win.prefs.dashboard]
        d._edit('news', 'left')
        after = [i['id'] for i in self.win.prefs.dashboard]
        self.assertLess(after.index('news'), before.index('news'))
        d._edit('news', 'hide')
        self.assertTrue(next(i for i in self.win.prefs.dashboard if i['id'] == 'news')['hidden'])
        self.assertFalse(d.widgets['news'].isVisibleTo(d))
        d._edit('news', 'show')
        self.assertTrue(d.widgets['news'].isVisibleTo(d))
        d.set_editing(False)
        self.assertFalse(d.widgets['system'].editbar.isVisibleTo(d))

    def test_dashboard_widgets_fill(self):
        d = self.win.dashboard
        self.assertTrue(d.widgets['jump'].isVisibleTo(d))            # has played games
        self.assertFalse(d.widgets['favourites'].isVisibleTo(d))     # none: stays out
        self.assertIn('alpha', self.win.homeview.tiles)

    def test_artwork_page_round_trip(self):
        game = self.win._games[1]
        self.win.go(self.win.LIBRARY)
        self.win.on_game_action('artwork', game)
        self.assertEqual(self.win.current(), self.win.ARTWORK)
        self.assertIn(game['display_name'], self.win.artpage.title.text())
        self.win._escape()
        self.assertEqual(self.win.current(), self.win.LIBRARY)
        self.assertEqual(self.win.selected, [game['slug']])

    def test_theatre_mode(self):
        w = self.win
        w.set_theatre(True)
        self.assertIs(w.shell.currentWidget(), w.theatre)
        self.assertTrue(w.hints.isVisible())
        self.assertIn('alpha', [t.game['slug'] for t in w.theatre.findChildren(nx.GameTile)])
        w._pad_action('page-next')
        self.assertEqual(w.theatre.tabs.current(), 'library')
        w.theatre.open_game(w._games[0])
        self.assertTrue(w.theatre.details.isVisible())
        w._escape()                                     # B: close the game page
        self.assertFalse(w.theatre.details.isVisible())
        w._escape()                                     # B: back to Home, still in Theatre
        self.assertEqual(w.theatre.tabs.current(), 'home')
        self.assertTrue(w._theatre)
        w._pad_action('tv')                             # Start leaves
        self.assertIs(w.shell.currentWidget(), w.desk)
        self.assertFalse(w.cursor_hider.hidden)

    def test_focus_ring_only_for_keyboard(self):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        ring, btn = self.win.focus_ring, self.win.homeview.hero.btn_details
        self.win.activateWindow()
        btn.setFocus()
        QApplication.processEvents()
        self.assertFalse(ring.isVisible())                  # mouse/programmatic: no ring
        QApplication.sendEvent(btn, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab,
                                              Qt.KeyboardModifier.NoModifier))
        btn.setFocus()
        QApplication.processEvents()
        ring._focus_changed(None, btn)
        self.assertTrue(ring.keyboard)
        self.assertTrue(ring.isVisible())
        ring._anim.stop()                                   # skip the glide to it
        ring._track()
        self.assertGreater(ring._rect.width(), btn.width())  # drawn outside the control
        self.win.search.setFocus()
        ring._focus_changed(None, self.win.search)
        self.assertFalse(ring.isVisible())                  # text fields keep their own

    def test_no_inner_focus_boxes(self):
        import re
        css = nx.build_stylesheet()
        self.assertEqual(re.findall(r':focus\s*\{[^}]*border:\s*2px', css), [])

    def test_live_appearance(self):
        try:
            self.win.settings.appearance.load({'theme': 'slate', 'accent': 'foam'}, emit=True)
            self.win._flush_appearance()                 # skip the debounce
            self.assertEqual(self.win.prefs.appearance['theme'], 'slate')
            self.assertEqual(nx.P['accent'], nx.ACCENTS['foam'])
            self.assertIn(nx.ACCENTS['foam'], QApplication.instance().styleSheet())
        finally:
            self.win.set_appearance(dict(nx.APPEARANCE_DEFAULT))
        self.assertEqual(nx.P['accent'], nx.ACCENTS['iris'])

    def test_launch_state_and_handoff(self):
        from unittest import mock
        w = self.win
        g = w._games[1]
        runs = []
        with mock.patch.object(nx.winfocus, 'available', lambda: False), \
                mock.patch.object(nx.winfocus, 'windows', lambda: []), \
                mock.patch.object(w, 'run', lambda *a, **k: runs.append(a)):
            w.on_game_action('launch', g)
            self.assertEqual(len(runs), 1)
            self.assertTrue(g['launching'])
            self.assertEqual(nx.play_label(g), "Starting…")
            self.assertFalse(nx.play_enabled(g))
            w.on_game_action('launch', g)                     # no second copy
            self.assertEqual(len(runs), 1)
            w._apply_running({g['slug']})
            self.assertTrue(g['playing'])
            self.assertFalse(g['launching'])
            self.assertEqual(nx.play_label(g), "Playing")
            w._apply_running(set())                           # the game closed
            self.assertFalse(g['playing'])
            self.assertEqual(nx.play_label(g), "Play")

    def test_handoff_focuses_the_game_window_and_back(self):
        from unittest import mock
        w = self.win
        g = w._games[1]
        focused = []
        before = [{'id': 1, 'app_id': 'nexus'}]
        after = before + [{'id': 9, 'app_id': f"steam_app_{g['appid']}"}]
        wins = [before]
        with mock.patch.object(nx.winfocus, 'available', lambda: True), \
                mock.patch.object(nx.winfocus, 'windows', lambda: wins[0]), \
                mock.patch.object(nx.winfocus, 'focus', lambda i: focused.append(i) or True), \
                mock.patch.object(w, 'run', lambda *a, **k: None):
            w.on_game_action('launch', g)
            wins[0] = after
            w._handoff_tick()
            from PyQt6.QtCore import QThreadPool
            for _ in range(2):                                # niri is asked off-thread
                QThreadPool.globalInstance().waitForDone(2000)
                QApplication.processEvents()
            self.assertEqual(focused, [9])                    # the game's window
            w._apply_running({g['slug']})
            w._apply_running(set())
            self.assertEqual(focused, [9, 1])                 # and back to Nexus

    def test_tile_focus_shows_no_overlay_buttons(self):
        t = next(iter(self.win.homeview.tiles.values()))[0]
        self.win.activateWindow()
        t.setFocus()
        QApplication.processEvents()
        self.assertFalse(t.btn_play.isVisible())
        self.assertFalse(t.btn_more.isVisible())

    def test_details_sections(self):
        d = self.win.details
        self.win._tile_activated(self.win._games[0])           # a repack
        QApplication.processEvents()
        self.assertFalse(d.sec_launch.isHidden())
        self.assertFalse(d.sec_launch.is_open)
        d.open_section('launch')
        self.assertTrue(d.sec_launch.is_open)
        d.sec_launch.set_open(False)
        self.assertEqual(d.st_played.value.text(), "2.0 h")
        self.win._tile_activated(self.win._games[1])           # a Steam game
        QApplication.processEvents()
        self.assertTrue(d.sec_launch.isHidden())               # Steam owns its settings

    def test_theatre_game_page_keeps_focus(self):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        w = self.win
        w.set_theatre(True)
        w.activateWindow()
        w.theatre.cycle_tab(1)                        # Library grid behind the overlay
        QApplication.processEvents()
        w.theatre.open_game(w._games[0])
        QApplication.processEvents()
        w.theatre.details.btn_fav.setFocus()
        for key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Left,
                    Qt.Key.Key_Right, Qt.Key.Key_Right):
            QApplication.sendEvent(QApplication.focusWidget(),
                                   QKeyEvent(QEvent.Type.KeyPress, key,
                                             Qt.KeyboardModifier.NoModifier))
            QApplication.processEvents()
            self.assertTrue(w.theatre.details.isAncestorOf(QApplication.focusWidget()),
                            QApplication.focusWidget())
        w.theatre.close_game()
        self.assertTrue(w.theatre.pages.isEnabled())
        w.set_theatre(False)

    def test_click_opens_card_no_play_overlay(self):
        t = next(iter(self.win.homeview.tiles.values()))[0]
        t._sync_buttons()
        self.assertFalse(t.btn_play.isVisible())
        doubled = []
        t.double.connect(doubled.append)
        from unittest import mock
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        ev = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(10, 10), QPointF(10, 10),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        w = self.win
        acts = []
        with mock.patch.object(w, 'on_game_action', lambda a, g: acts.append(a)):
            QApplication.sendEvent(t, ev)
            self.assertEqual(len(doubled), 1)
            self.assertEqual(acts, ['launch'])           # Settings → Controls: plays
            w.prefs.controls['double_click'] = 'details'
            QApplication.sendEvent(t, ev)
            self.assertEqual(acts, ['launch'])           # …or just opens the page
            self.assertEqual(w.selected, [t.game['slug']])
            w.prefs.controls['double_click'] = 'play'

    def test_no_stray_windows(self):
        """Nothing but the main window may become visible as its own window: a
        widget shown before it has a parent pops up as a separate (black,
        under niri) window — that happened with an orphan label on every
        tile click."""
        w = self.win
        # Other tests leave their own top-level test widgets around; only
        # windows that appear during this test count.
        # (keep refs for stable identity; a hidden one that becomes visible —
        # like the orphan label, created hidden with the window — counts too)
        before = [x for x in QApplication.topLevelWidgets() if x.isVisible()]

        def strays():
            return [(type(x).__name__, x.objectName(), [c.text() for c in
                                                       x.findChildren(nx.QLabel)][:3])
                    for x in QApplication.topLevelWidgets()
                    if x.isVisible() and x is not w and not any(x is b for b in before)
                    and not isinstance(x, (nx.QMenu,)) and x.windowType() not in (
                        nx.Qt.WindowType.ToolTip, nx.Qt.WindowType.Popup)]
        for g in w._games:
            g['proton'] = 'proton-test'          # the orphan label only showed with one
            g['launch_options'] = 'X=1 %command%'
            w._tile_activated(g)
            QApplication.processEvents()
            for key in ('launch', 'files', 'tune', 'log'):
                w.details.open_section(key)
            QApplication.processEvents()
        w.go(w.LIBRARY)
        for v in ('home', 'grid', 'list'):
            w.set_view(v, save=False)
            QApplication.processEvents()
        for page in w.pages:
            w.go(page)
            QApplication.processEvents()
        w.dashboard.set_editing(True)
        w.dashboard.set_editing(False)
        w.set_theatre(True)
        w.theatre.open_game(w._games[0])
        QApplication.processEvents()
        w.set_theatre(False)
        QApplication.processEvents()
        self.assertEqual(strays(), [])

    def test_no_transient_windows(self):
        """Stricter than test_no_stray_windows: also catches a widget that is
        shown parentless for an instant and reparented on the next line (each
        one is a real window on Wayland, which niri maps and unmaps)."""
        from PyQt6.QtCore import QEvent, QObject

        class Spy(QObject):
            def __init__(self):
                super().__init__()
                self.seen, self.main = [], None

            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Type.Show and isinstance(obj, nx.QWidget) \
                        and obj.isWindow() and obj is not self.main \
                        and not isinstance(obj, nx.QMenu) and obj.windowType() not in (
                            nx.Qt.WindowType.ToolTip, nx.Qt.WindowType.Popup):
                    self.seen.append(f"{type(obj).__name__}:{obj.objectName()}:"
                                     f"{getattr(obj, 'text', lambda: '')()!s:.30}")
                return False
        spy = Spy()
        app = QApplication.instance()
        app.installEventFilter(spy)
        try:
            w = nx.MainWindow()
            spy.main = w
            w.resize(1300, 850)
            w.show()
            w._on_library({'games': fake_games(), 'disks': []}, '')
            QApplication.processEvents()
            w.on_game_action('favorite', w._games[0])
            w._tile_activated(w._games[0])
            w.details._fill_tips({'recipes': [
                {'name': f'r{i}', 'launch_options': 'X=1 %command%'} for i in range(7)]})
            w.set_theatre(True)
            w.theatre.open_game(w._games[0])
            w.set_theatre(False)
            QApplication.processEvents()
            w.jobs.jobs.clear()
            w.close()
            w.deleteLater()
            QApplication.processEvents()
        finally:
            app.removeEventFilter(spy)
        self.assertEqual(spy.seen, [])

    def _key(self, w, key):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, key,
                                            nx.Qt.KeyboardModifier.NoModifier))
        QApplication.processEvents()

    def test_theatre_never_opens_a_desktop_menu(self):
        """X, right-click and the hero's ⋮ all lead to the controller sheet in
        Theatre — and right-click no longer opens the game page underneath."""
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QContextMenuEvent
        w = self.win
        w.set_theatre(True)
        QApplication.processEvents()
        self.assertTrue(w.theatre.hero.btn_more.isHidden())
        self.assertEqual(w.theatre.hero.btn_more.focusPolicy(), nx.Qt.FocusPolicy.NoFocus)
        tile = next(t for t in w.theatre.findChildren(nx.GameTile) if not t.isHidden())
        tile.setFocus()
        self._key(tile, nx.Qt.Key.Key_Menu)                    # X
        self.assertTrue(w.theatre_sheet.isVisible())
        self.assertIsNone(QApplication.activePopupWidget())
        self.assertIs(w.nav_scope(), w.theatre_sheet)
        self.assertTrue(w.theatre_sheet.isAncestorOf(QApplication.focusWidget()))
        keys = [b.text() for b in w.theatre_sheet.findChildren(nx.QPushButton)]
        self.assertFalse(any('folder' in k or 'winecfg' in k for k in keys), keys)
        w._escape()                                            # B closes just the sheet
        self.assertFalse(w.theatre_sheet.isVisible())
        self.assertTrue(w._theatre)
        QApplication.sendEvent(tile, QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                                       QPoint(5, 5), tile.mapToGlobal(QPoint(5, 5))))
        QApplication.processEvents()
        self.assertTrue(w.theatre_sheet.isVisible())
        self.assertFalse(w.theatre.details.isVisible())
        w._escape()
        w.theatre.open_game(w._games[0])
        QApplication.processEvents()
        w.theatre.details.btn_opts.click()
        self.assertTrue(w.theatre_sheet.isVisible())
        w._escape()
        self.assertTrue(w.theatre.details.isVisible())         # still on the game page
        w.set_theatre(False)

    def test_theatre_game_page_cards_and_reader(self):
        w = self.win
        w.set_theatre(True)
        g = w._games[1]                                       # Steam, appid 1
        w.theatre.open_game(g)
        d = w.theatre.details
        self.assertFalse(d.cards['stats'].isHidden())         # always something to say
        self.assertTrue(d.cards['compat'].isHidden())         # no data yet
        d.set_info(tips={'protondb': {'tier': 'gold', 'total': 9},
                         'gamedb': {'list': 'library', 'hltb': {'main': 10}}},
                   news=[{'appid': 1, 'title': 'Patch', 'summary': 'Fixes',
                          'date': '2026-10-01T10:00:00'},
                         {'appid': 2, 'title': 'Other game'}])
        self.assertFalse(d.cards['compat'].isHidden())
        self.assertFalse(d.cards['gamedb'].isHidden())
        self.assertFalse(d.cards['news'].isHidden())
        self.assertNotIn('Other game', d.cards['news'].more)
        d.cards['news'].setFocus()
        self._key(d.cards['news'], nx.Qt.Key.Key_Return)     # A opens the card
        self.assertEqual(d.lower.currentWidget(), d.reader)
        self.assertIn('Fixes', d.reader.toPlainText())
        w._escape()                                           # B: close the card…
        self.assertTrue(d.isVisible())
        self.assertEqual(d.lower.currentIndex(), 0)
        w._escape()                                           # …then the page
        self.assertFalse(d.isVisible())
        w.set_theatre(False)

    def test_theatre_favourite_label_follows_the_state(self):
        w = self.win
        w.set_theatre(True)
        g = w._games[0]
        w.theatre.open_game(g)
        d = w.theatre.details
        d.btn_fav.click()
        QApplication.processEvents()
        self.assertIn("★", d.btn_fav.text())
        d.btn_fav.click()
        QApplication.processEvents()
        self.assertIn("☆", d.btn_fav.text())
        w.set_theatre(False)

    def test_theatre_default_focus_skips_a_disabled_play(self):
        w = self.win
        w.set_theatre(True)
        g = dict(w._games[0], playing=True)
        w.theatre.open_game(g)
        self.assertFalse(w.theatre.details.btn_play.isEnabled())
        self.assertIs(w.theatre.default_focus(), w.theatre.details.btn_fav)
        w.set_theatre(False)

    def test_pad_on_desktop_gets_the_sheet_and_hints(self):
        w = self.win
        saved = dict(w.ctrl.pads.pads)
        w.ctrl.pads.pads[99] = (None, 'Test pad', 'ps')
        try:
            w.input.set('mouse')
            self.assertFalse(w.hints.isVisible())
            w.input.set('pad')
            self.assertTrue(w.hints.isVisible())
            w._show_menu(w._games[0], nx.QPoint())
            self.assertTrue(w.sheet.isVisible())
            self.assertIsNone(QApplication.activePopupWidget())
            w._escape()
            self.assertFalse(w.sheet.isVisible())
            w.input.set('mouse')
            self.assertFalse(w.hints.isVisible())
        finally:
            w.ctrl.pads.pads.clear()
            w.ctrl.pads.pads.update(saved)

    def test_input_mode_from_events(self):
        from PyQt6.QtCore import QEvent, QPointF
        from PyQt6.QtGui import QMouseEvent
        w = self.win
        w.input.set('pad')
        self._key(w.search, nx.Qt.Key.Key_Down)          # posted-style (not spontaneous)
        self.assertEqual(w.input.mode, 'pad')              # …keys we send keep 'pad'
        w.input.set('mouse')
        self._key(w.search, nx.Qt.Key.Key_Tab)
        self.assertEqual(w.input.mode, 'keyboard')
        QApplication.sendEvent(w, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(3, 3), QPointF(3, 3),
            nx.Qt.MouseButton.LeftButton, nx.Qt.MouseButton.LeftButton,
            nx.Qt.KeyboardModifier.NoModifier))
        self.assertEqual(w.input.mode, 'mouse')

    def test_owned_games_install_through_the_store(self):
        from unittest import mock
        w = self.win
        owned = {'slug': 'steam-30', 'display_name': 'Thirty', 'source': 'steam', 'appid': 30,
                 'installed': False, 'install_url': 'steam://install/30',
                 'launcher': 'steam://rungameid/30', 'playtime_minutes': 90}
        w._on_library({'games': fake_games() + [owned], 'disks': []}, '')
        QApplication.processEvents()
        g = next(x for x in w._games if x['slug'] == 'steam-30')
        self.assertNotIn('steam-30', w.homeview.tiles)          # Home: installed games only
        self.assertNotIn(g, nx.featured_games(nx._visible_games(w)))
        w.go(w.LIBRARY)
        w._set_owned_view('installed')
        self.assertNotIn('steam-30', w.views.currentWidget().tiles)
        w._set_owned_view('owned')
        self.assertIs(w.views.currentWidget(), w.library)        # a grid, not shelves
        self.assertEqual(set(w.library.tiles), {'steam-30'})
        self.assertEqual(nx.play_label(g), "Install")
        keys = [a.key for a in nx.game_actions(g)]
        self.assertNotIn('open-folder', keys)
        self.assertNotIn('steam-verify', keys)
        runs = []
        with mock.patch.object(w, 'run', lambda *a, **k: runs.append(a[0])), \
                mock.patch.object(nx, 'cli_json', lambda *a, **k: None):
            w.on_game_action('launch', g)
        self.assertEqual(runs, [['--open-url', 'steam://install/30']])
        self.assertEqual(nx.play_label(g), "Waiting for Steam")
        self.assertFalse(nx.play_enabled(g))                     # no second dialog
        self.assertTrue(w._install_timer.isActive())
        # Steam starts downloading, then finishes: progress, then a toast + Play.
        toasts = []
        done = dict(owned, installed=True)
        done.pop('installing', None)
        # The poll reads Steam's manifests (steamlib.downloads); a download
        # starting or finishing reloads the library once, progress doesn't.
        progress = {'done': 25, 'total': 100, 'paused': False}
        polls = ({'steam': {30: progress}, 'steam_installed': [], 'heroic': []},
                 {'steam': {30: dict(progress, done=60)}, 'steam_installed': [], 'heroic': []},
                 {'steam': {}, 'steam_installed': [30], 'heroic': []})
        libraries = (fake_games() + [dict(owned, installing=progress)], None,
                     fake_games() + [done])
        reloads = []
        for poll, games in zip(polls, libraries):
            data = {'games': games, 'disks': []}
            with mock.patch.object(nx, 'in_thread', lambda fn, cb, d=poll: cb(d, None)), \
                    mock.patch.object(nx, 'cli_json', lambda a, cb, *_x, d=data:
                                      (reloads.append(a), cb(d, ''))), \
                    mock.patch.object(w, 'toast', lambda *a, **k: toasts.append(a[0])):
                w._install_tick()
            cur = next(x for x in w._games if x['slug'] == 'steam-30')
            if games and not nx.is_installed(cur):
                self.assertEqual(nx.play_label(cur), "Installing 25%")
            elif not games:
                self.assertEqual(nx.play_label(cur), "Installing 60%")   # in place
        self.assertEqual(len([a for a in reloads if a[0] == '--library']), 2)
        self.assertTrue(nx.is_installed(cur))
        self.assertTrue(any('installed' in t for t in toasts), toasts)
        self.assertFalse(w._install_timer.isActive())
        w._set_owned_view('installed')

    def test_theatre_ready_to_install_row(self):
        w = self.win
        owned = {'slug': 'heroic-gog-x', 'display_name': 'Ex', 'source': 'heroic',
                 'installed': False, 'install_url': 'heroic://launch/gog/x'}
        w._on_library({'games': fake_games() + [owned], 'disks': []}, '')
        w.set_theatre(True)
        QApplication.processEvents()
        titles = [r.findChild(nx.QLabel).text() for r in w.theatre.findChildren(nx.ShelfRow)]
        self.assertTrue(any('Ready to install' in t for t in titles), titles)
        self.assertNotIn(owned['slug'], [g['slug'] for g in w.theatre.hero.games])
        w.set_theatre(False)

    def test_theatre_search_with_the_on_screen_keyboard(self):
        w = self.win
        owned = {'slug': 'steam-30', 'display_name': 'Gamma Quest™', 'source': 'steam',
                 'installed': False, 'install_url': 'steam://install/30'}
        w._on_library({'games': fake_games() + [owned], 'disks': []}, '')
        w.set_theatre(True)
        w._pad_action('search')                                 # Y
        srch = w.theatre.search
        self.assertTrue(srch.isVisible())
        self.assertIs(w.nav_scope(), srch)
        for ch in 'gam':
            srch.keys[ch].click()                               # A on keys
        found = [srch.flow.itemAt(i).widget().game['slug'] for i in range(srch.flow.count())]
        self.assertEqual(found, ['gamma', 'steam-30'])         # installed first, owned too
        srch.keys['q'].setFocus()
        self._key(srch.keys['q'], nx.Qt.Key.Key_Menu)          # X: delete
        self.assertEqual(srch.query, 'ga')
        w._pad_action('search')                                 # Y: a space
        self.assertEqual(srch.query, 'ga ')
        srch.picked.emit(w._games[2])                           # A on a result
        self.assertFalse(srch.isVisible())
        self.assertTrue(w.theatre.details.isVisible())
        w._escape()                                             # B: back to the search
        self.assertTrue(srch.isVisible())
        self.assertEqual(srch.query, 'ga ')
        w._escape()                                             # B: close it
        self.assertFalse(srch.isVisible())
        self.assertTrue(w.theatre.pages.isEnabled())
        self.assertTrue(w._theatre)
        w.set_theatre(False)

    def test_badges(self):
        self.win._deals_count(7)
        self.assertEqual(self.win.tabs._badges[self.win.DEALS].text(), '7')
        self.win._health_counts(2, 1)
        icon = self.win.icons[self.win.SYSTEM]
        self.assertEqual(icon.badge(), '2')
        self.win.jobs.active = lambda: [object()]          # a running job wins
        try:
            self.win._jobs_changed()
            self.assertEqual(icon.badge(), '1')
        finally:
            del self.win.jobs.active

    def test_system_aliases(self):
        w = self.win
        for alias, (page, section) in w.PAGE_ALIASES.items():
            w.go(alias)
            self.assertEqual(w.current(), page)
            self.assertEqual(w.pages[page].section(), section)
            self.assertTrue(w._showing(alias))
        w.go(w.STORAGE)
        self.assertTrue(w.icons[w.SYSTEM].isChecked())
        self.assertFalse(w._showing(w.HEALTH))
        self.assertNotIn('activity', w.pages)               # sections, not pages

    def test_system_disk_ring(self):
        disk = {'path': '/mnt/games', 'used': 90, 'total': 100, 'free': 10}
        self.win._on_library({'games': fake_games(), 'disk': disk, 'disks': [disk]}, '')
        self.assertAlmostEqual(self.win.icons[self.win.SYSTEM].usage, 0.9)

    def test_system_widget_tiles(self):
        w = self.win
        w.prefs.dashboard = nx.dash_default()
        w.dashboard.relayout(force=True)
        w.dashboard.refresh(['system'])
        sysw = w.dashboard.widgets['system']
        self.assertEqual([t.key for t in sysw.tiles], ['activity', 'storage', 'health', 'proton'])
        before = len(sysw.tiles[0].children())
        tile = sysw.tiles[0]
        w.dashboard.refresh(['system'])                     # unchanged: not rebuilt
        self.assertIs(sysw.tiles[0], tile)
        next(t for t in sysw.tiles if t.key == 'health').clicked.emit()
        self.assertEqual((w.current(), w.system.section()), (w.SYSTEM, 'health'))
        self.assertTrue(before)

    def test_upcoming_widget(self):
        w = self.win
        w.config['gamedb_enabled'] = True
        w.prefs.dashboard = nx.dash_default()
        price = {'currentStr': '€50', 'best': {'priceStr': '€40', 'category': 'keyshop'}}
        w.dealspage.res = {'configured': True, 'url': 'https://gdb.example', 'summary': {
            'upcoming': [{'appid': 1, 'title': 'Soon', 'releaseDate': '2099-01-02T00:00:00Z',
                          'releaseDateText': 'Jan 2, 2099', 'steamUrl': 'https://s/1',
                          'headerImage': 'none'}],
            'wishlist': [{'appid': 1, 'title': 'Soon', 'comingSoon': True, 'price': price},
                         {'appid': 2, 'title': 'Someday', 'comingSoon': True,
                          'releaseDateText': 'Q3 2099', 'price': None}]}}
        w.dealspage._loaded = True
        w.dashboard.relayout(force=True)
        w.dashboard.refresh(['upcoming'])
        up = w.dashboard.widgets['upcoming']
        self.assertTrue(up.isVisibleTo(w.dashboard))
        rows = [r for r in up.findChildren(nx.ClickRow)]
        self.assertEqual(len(rows), 2)
        texts = [l.text() for l in up.findChildren(nx.QLabel)]
        self.assertIn("€40 · keyshop", texts)
        self.assertIn("No price yet", texts)
        opened = []
        w.dealspage._open = opened.append
        rows[0].clicked.emit()
        self.assertEqual(opened, ['https://s/1'])
        w.go(w.UPCOMING)
        self.assertEqual((w.current(), w.dealspage.section()), (w.DEALS, 'upcoming'))
        w.config['gamedb_enabled'] = False
        w.dashboard.relayout(force=True)
        self.assertFalse(up.isVisibleTo(w.dashboard))       # no Game-db: no card

    def test_installed_marks_new(self):
        w = self.win
        games = fake_games()
        for g in games:
            g['added_at'] = '2026-09-01T10:00:00'
        w.prefs.dashboard = nx.dash_default()
        w._on_library({'games': games, 'disks': []}, '')
        w.dashboard.refresh(['installed'])
        tiles = {t.game['slug']: t.mark_new for t in w.dashboard.widgets['installed']._tiles}
        self.assertEqual((tiles['alpha'], tiles['gamma']), (False, True))



@unittest.skipIf(nx is None, "PyQt6 not available")
class News(unittest.TestCase):
    def setUp(self):
        self._fetch = nx.ART.fetch
        self.fetched = []
        nx.ART.fetch = lambda url, cb: self.fetched.append(url)     # no network
        self.page = nx.NewsPage()
        self.page.resize(1200, 700)
        items = []
        for n in range(60):
            kind = ('feed', 'steam', 'proton')[n % 3]
            items.append({'kind': kind, 'source': {'feed': 'GOL', 'steam': 'Game',
                                                   'proton': 'GE-Proton'}[kind],
                          'title': f"Story {n}", 'link': f"https://e.com/{n}",
                          'date': f"2026-10-{8 - n // 20:02d}T10:00:00",
                          'summary': 'Words', 'appid': 7 if kind == 'steam' else None,
                          'image': f"https://img.e/{n}.webp" if kind == 'feed' and n else None})
        self.page.set_data({'items': items, 'sources': [], 'feeds': []})

    def tearDown(self):
        nx.ART.fetch = self._fetch
        self.page.deleteLater()
        QApplication.processEvents()

    def test_feature_grid_and_filters_reuse_cards(self):
        p = self.page
        # Story 0 has no picture; the lead is the newest one that does.
        self.assertEqual(p.feature.index, 3)
        self.assertEqual(len(p.tiles), p.PAGE)
        first = dict(p.tiles)
        p._pick('Proton')
        shown = [n for n, t in p.tiles.items() if t.isVisibleTo(p)]
        self.assertTrue(shown and all(p.items[n]['kind'] == 'proton' for n in shown))
        p._pick('all')
        for n, t in first.items():                       # the same cards, not rebuilt
            self.assertIs(p.tiles[n], t)
        self.assertTrue(p.btn_more.isVisibleTo(p))
        p._more()
        # 60 stories, one of them featured above the grid.
        self.assertEqual(sum(t.isVisibleTo(p) for t in p.tiles.values()), 59)
        self.assertFalse(p.btn_more.isVisibleTo(p))

    def test_pictures_load_lazily(self):
        p = self.page
        p.show()
        for _ in range(3):           # 1st pass fits the cards to the width, then loads
            QApplication.processEvents()
            p._load_visible()
        widths = {t.W for t in p.tiles.values()}
        self.assertEqual(len(widths), 1)
        self.assertGreaterEqual(widths.pop(), p.MIN_W)
        asked = [n for n, t in p.tiles.items() if t.art.requested]
        self.assertTrue(asked)
        self.assertLess(len(asked), len(p.tiles))        # far-down cards wait
        self.assertTrue(all(u.startswith('https://img.e/') for u in self.fetched))

    def test_reader(self):
        p = self.page
        p.show()
        p.open_index(p._view[1])
        self.assertTrue(p.reader_open())
        self.assertEqual(p.reader.item['title'], p.items[p._view[1]]['title'])
        p.reader.step.emit(1)
        self.assertEqual(p._reading, p._view[2])
        p.close_reader()
        self.assertFalse(p.reader_open())
        p.open_item(p.items[10])
        self.assertEqual(p._reading, 10)

    def test_elide_lines(self):
        fm = nx.QFontMetrics(nx.QFont())
        out = nx.elide_lines("word " * 200, fm, 120, 3)
        self.assertEqual(len(out.split('\n')), 3)
        self.assertTrue(out.endswith('…'))
        self.assertEqual(nx.elide_lines("short", fm, 120, 3), "short")


if __name__ == '__main__':
    unittest.main()
