"""MainWindow smoke tests: page registry, top-bar navigation, the shared
details panel, Home's rows. Offscreen with the CLI stubbed out, so nothing
runs, nothing is fetched and the user's prefs are never written."""

import json
import unittest
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import linux_features, patch_gui, set_gui  # noqa: E402
from tests.oskind import linux_only  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import Qt, QPoint
    from PyQt6.QtGui import QFont, QFontMetrics
    from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QPushButton, QWidget
    from nexus import features, winfocus


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
    @linux_features
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

    def test_move_and_snapshots_only_for_games_nexus_owns(self):
        g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a'}
        self.assertNotIn('move', [a.key for a in nx.game_actions(g)])
        g['owned_dir'] = True
        keys = [a.key for a in nx.game_actions(g)]
        self.assertIn('move', keys)
        self.assertIn('snapshots', keys)
        busy = {a.key: a.enabled for a in nx.game_actions({**g, 'playing': True})}
        self.assertFalse(busy['move'])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Palette(unittest.TestCase):
    def test_fuzzy_score_orders_sensibly(self):
        f = nx.fuzzy_score
        self.assertIsNone(f("xyz", "Hades"))
        self.assertGreater(f("had", "Hades"), f("had", "Hollow Adventure Drift"))
        self.assertIsNotNone(f("hk", "Hollow Knight"))         # word starts
        self.assertIsNone(f("hk", "Shark"))                    # not mid-word scatter
        self.assertIsNotNone(f("ark", "Shark"))                # but a substring is fine
        self.assertIsNotNone(f("hdes", "Hades"))
        self.assertGreater(f("app", "Appearance"), f("app", "Wrapped"))

    def test_rank_two_words_and_recent(self):
        E = nx.Entry
        es = [E('command', "Move to another drive…", key='a', keywords="move"),
              E('game', "Nocturne", key='game:noc'), E('game', "Hades", key='game:hades'),
              E('command', "Disabled thing", key='x', enabled=False)]
        self.assertEqual(nx.rank(es, "noc")[0].key, 'game:noc')
        self.assertEqual([e.key for e in nx.rank(es, "", recent=['game:hades'])][:1],
                         ['game:hades'])
        self.assertEqual(nx.rank(es, "zzz"), [])


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
        self.win.resize(1300, 850)
        self.win.show()
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        from PyQt6.QtCore import QEvent
        # A job or CLI query a failed test left running: kill it first, or it
        # calls back into this window after it's gone.
        stop_leftovers(self.win.jobs)
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

    def test_store_chips(self):
        w = self.win
        self.assertTrue(w.chips['epic'].isHidden())            # no Epic games yet
        epic = {'slug': 'epic-Kiwi', 'display_name': 'Kiwi', 'source': 'epic',
                'installed': True, 'launcher': 'com.epicgames.launcher://apps/x'}
        dup = {'slug': 'epic-Beta', 'display_name': 'Beta', 'source': 'epic', 'installed': True}
        w._on_library({'games': fake_games() + [epic, dup], 'disks': []}, '')
        self.assertFalse(w.chips['epic'].isHidden())
        w.go(w.LIBRARY)
        w._set_chip('epic')
        # Beta is on Steam too: one tile, the Steam one.
        self.assertEqual([g['slug'] for g in w._shown_games], ['epic-Kiwi'])
        self.assertEqual(w.game('epic-Beta')['dup_of'], 'steam-1')
        self.assertEqual(w.game('steam-1')['dup_also'], ['epic'])
        w._on_library({'games': fake_games(), 'disks': []}, '')
        self.assertFalse(w.chips['epic'].isHidden())           # still the chip in use
        w._set_chip('all')
        self.assertTrue(w.chips['epic'].isHidden())

    def test_no_heroic_chip_without_the_feature(self):
        from nexus import features
        with mock.patch.object(features, 'FEATURES', frozenset()):
            w = nx.MainWindow()
        self.addCleanup(w.deleteLater)
        self.assertNotIn('heroic', w.chips)
        self.assertEqual(w.filter.findData('no-heroic'), -1)
        self.assertLessEqual({'nexus', 'steam', 'epic'}, set(w.chips))
        with mock.patch.object(features, 'FEATURES', features.ALL):
            w = nx.MainWindow()
        self.addCleanup(w.deleteLater)
        self.assertIn('heroic', w.chips)                       # Linux keeps it
        self.assertGreaterEqual(w.filter.findData('no-heroic'), 0)

    def test_surprise_opens_a_game_page(self):
        w = self.win
        w.go(w.LIBRARY)
        w.surprise()
        self.assertEqual(len(w.selected), 1)
        self.assertTrue(w.details.isVisible())
        self.assertIn(w.selected[0], ('alpha', 'steam-1', 'gamma'))

    @linux_only                     # sh + SIGTERM; Windows cancelling is phase 4
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
        rows = [b.text() for b in self.win.sheet.findChildren(QPushButton)]
        self.assertEqual(rows, ["Open in Steam", "Open on Game-db", "Copy link"])
        self.win.sheet.close_sheet()

    def test_pad_opens_the_spotlight_options_in_the_sheet(self):
        spot = nx.DealSpotlight()
        self.win.dealspage.layout().addWidget(spot)
        spot.set_items([{'appid': 10, 'title': 'Deal Game',
                         'steamUrl': 'https://store.steampowered.com/app/10/',
                         'price': {'best': {'url': 'https://shop.example/10', 'shop': 'Shop'},
                                   'ggUrl': 'https://gg.deals/game/10/'}},
                        {'appid': 11, 'title': 'Other'}],
                       {'10': {'slug': 'deal-game', 'title': 'Deal Game'}}, None)
        self.win.go(self.win.DEALS)
        self.win.input.set('pad')
        got = []
        spot.activated.connect(got.append)
        spot.setFocus()
        # X on a pad arrives as the Menu key. Straight to the widget: a sent
        # key event would flip the input mode to 'keyboard' (and a QMenu).
        from PyQt6.QtGui import QKeyEvent
        spot.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Menu,
                                     Qt.KeyboardModifier.NoModifier))
        self.assertTrue(self.win.sheet.isVisible())
        rows = [b for b in self.win.sheet.findChildren(QPushButton)]
        self.assertEqual([b.text() for b in rows],
                         ["Open deal", "Grab from FitGirl", "Steam page", "Compare on GG.deals",
                          "Next deal", "Previous deal"])
        rows[3].click()                         # runs once the sheet has closed
        self.assertFalse(self.win.sheet.isVisible())
        self.assertEqual(got, ['https://gg.deals/game/10/'])

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

    def test_failed_deals_refresh_keeps_what_is_shown(self):
        d = self._deals()
        d._busy = False
        with patch_gui('cli_json', lambda args, cb, *a, **k: cb(None, 'timed out')):
            d.refresh(force=True)
        self.assertEqual(len(d._cards), 3)
        self.assertTrue(d.banner.isVisibleTo(d))
        self.assertIn('timed out', d.banner.text())
        self.assertEqual(d.error, '')                     # there is something to show

    def test_deals_search_reuses_cards(self):
        d = self._deals()
        before = {c.item['appid']: c for c in d._cards}
        for c in before.values():
            c._marker = True
        d._query = 'elden'
        d._fill()
        self.assertEqual([c.item['appid'] for c in d._cards], [11])
        self.assertIs(d._cards[0], before[11])
        d._query = ''
        d._fill()
        self.assertEqual(sorted(c.item['appid'] for c in d._cards), [10, 11, 12])
        self.assertTrue(all(getattr(c, '_marker', False) for c in d._cards))
        self.assertTrue(all(c.isVisibleTo(d) for c in d._cards))   # pooled ones re-shown
        d.res = json.loads(json.dumps(self.WISH))                  # new data: new cards
        d._render()
        self.assertFalse(any(getattr(c, '_marker', False) for c in d._cards))

    def test_deal_art_waits_for_the_resize_to_settle(self):
        from PyQt6.QtGui import QPixmap
        art = nx.DealArt(100, 50, 4)
        self.addCleanup(art.deleteLater)
        art._path, art._pm = '/x.png', QPixmap(100, 50)
        asked = []
        with mock.patch.object(nx.ART, 'peek', lambda *a: None), \
                mock.patch.object(nx.ART, 'request', lambda p, w, h, cb: asked.append((w, h))):
            for w in range(102, 140, 2):
                art.resize_to(w, 50)
            self.assertEqual(asked, [])                 # old pixmap, drawn scaled
            self.assertIsNotNone(art._pm)
            art.refresh_size()
            self.assertEqual(asked, [(138, 50)])

    def test_deals_filled_while_hidden_use_every_column_when_shown(self):
        # The warm-up fills Deals before it is ever shown, when the scroll
        # viewport still has its unlaid-out width; opening the page must
        # recount the columns (it used to stay at 1-2 until a tab switch).
        self.win.resize(1600, 900)
        self.win.go(self.win.HOME)
        QApplication.processEvents()
        d = self._deals()
        self.win.go(self.win.DEALS)
        QApplication.processEvents()
        self.assertGreaterEqual(d._columns(), 3)
        self.assertEqual(d._cols, d._columns())
        grid = d._grids[0]
        self.assertEqual({grid.getItemPosition(grid.indexOf(c))[1] for c in grid._cards},
                         set(range(min(3, d._cols))))

    def test_deals_card_cap_says_so(self):
        with mock.patch.object(nx.DealsPage, 'MAX_CARDS', 2):
            d = self._deals()
            self.assertEqual(len(d._cards), 2)
            notes = [lb.text() for lb in d.findChildren(QLabel) if not lb.isHidden()]
            self.assertTrue(any('first 2 of 3' in t for t in notes), notes)

    def test_spotlight_redecodes_after_a_resize_mid_decode(self):
        from PyQt6.QtGui import QPixmap
        sp = self.win.dealspage.spot
        asked = []
        with mock.patch.object(nx.ART, 'peek', lambda *a: None), \
                mock.patch.object(nx.ART, 'request', lambda p, w, h, cb: asked.append((w, h))):
            sp._path = '/art/x.jpg'
            want = sp._size()
            sp._decoded(('/art/x.jpg', want[0] + 128, want[1]), QPixmap(4, 4))   # old width
            self.assertEqual(asked, [want])
            asked.clear()
            sp._decoded(('/art/x.jpg', *want), QPixmap(4, 4))                    # right one
            self.assertEqual(asked, [])

    def test_spotlight_counter_reads_on_a_light_theme(self):
        from PyQt6.QtGui import QColor, QPixmap
        items = [{'title': f'Deal {i}', 'price': {'currentStr': '1€'}} for i in range(3)]
        try:
            self.win.set_appearance({**nx.APPEARANCE_DEFAULT, 'theme': 'dawn'})
            sp = nx.DealSpotlight()
            self.addCleanup(sp.deleteLater)
            sp.resize(900, sp.H)
            sp.set_items(items, {}, None)
            pm = QPixmap(900, sp.H)
            pm.fill(QColor('#f2ede4'))                 # light art under the counter
            sp._pm = pm
            sp.show()
            QApplication.processEvents()
            g = sp.counter.geometry()
            under = sp.grab().toImage().pixelColor(g.x() + 3, g.center().y())
            self.assertLess(under.lightnessF(), 0.35)  # white "1 / 3" on a dark backing
        finally:
            self.win.set_appearance(dict(nx.APPEARANCE_DEFAULT))

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
        chips = [b.text() for b in d.sub_host.findChildren(QPushButton)]
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
        with patch_gui('cli_json', lambda a, cb, *_x: (calls.append(a), cb(
                page if a[0] == '--repack-page' else {'hash': 'b' * 40}, None))):
            card = next(c for c in d._cards if c.item['appid'] == 10)
            card.grab.click()
            self.assertEqual(calls[0][:2], ['--repack-page', 'hollow-knight-silksong'])
            self.assertTrue(self.win.sheet.isVisible())
            self.assertIn("not enough space", self.win.sheet.sub.text())
            rows = [b for b in self.win.sheet.findChildren(QPushButton)
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
            from nexus import torrent
            torrent.save_tracked({'c' * 40: {'name': folder.name, 'auto_install': True,
                                             'installed': False},
                                  'd' * 40: {'name': 'Other', 'auto_install': True,
                                             'installed': False}})
            self.addCleanup(torrent.TRACKED.unlink, missing_ok=True)
            with patch_gui('cli_json', lambda a, cb, *_x: cb(data, None)), \
                    mock.patch.object(w, '_start_install', lambda cmd, *a: started.append(cmd)):
                w._torrent_polling = False
                w._poll_torrents()
                w._torrent_polling = False
                w._poll_torrents()                                   # not twice
                self.assertEqual(len(started), 1)
                self.assertTrue(torrent.load_tracked()['c' * 40].get('install_started'))
                # The background agent claimed this one first: the window leaves it.
                self.assertTrue(torrent.claim_install('d' * 40))
                data['torrents'] = [dict(row, hash='d' * 40), row]
                w._torrent_polling = False
                w._poll_torrents()
                self.assertEqual(len(started), 1)
                data['torrents'] = [row]
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

    def test_background_agent_messages_are_checked(self):
        import tempfile
        from pathlib import Path
        from nexus import torrent
        w = self.win
        shown, started = [], []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(w, '_repack_ready', shown.append), \
                mock.patch.object(w, '_start_install', lambda cmd, *a: started.append(cmd)), \
                mock.patch.object(w, 'bring_to_front') as front:
            w.handle_remote(['--agent-ready', json.dumps({'path': d, 'name': 'G'})])
            w.handle_remote(['--agent-ready', 'not json'])
            w.handle_remote(['--agent-ready', json.dumps({'path': d + '/nope', 'name': 'G'})])
            self.assertEqual([c['name'] for c in shown], ['G'])
            torrent.save_tracked({'e' * 40: {'name': 'G', 'installed': False}})
            self.addCleanup(torrent.TRACKED.unlink, missing_ok=True)
            w.handle_remote(['--agent-install', d, 'e' * 40])       # the agent never claimed it
            self.assertEqual(started, [])
            self.assertTrue(torrent.claim_install('e' * 40))
            w.handle_remote(['--agent-install', d, 'e' * 40])
            w.handle_remote(['--agent-install', d, 'e' * 40])       # once
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0][-2:], ['--from-torrent', 'e' * 40])
            front.assert_not_called()                               # never steals focus

    def test_alerts_section_saves_to_config(self):
        st = self.win.settings
        saved = []
        with patch_gui('save_config', saved.append), \
                patch_gui('load_config', return_value={'alerts_atl': True, 'alerts_fitgirl': True,
                                                       'alerts_repack_updates': True,
                                                       'alerts_proton': True,
                                                       'alerts_sale_min_cut': 30}):
            st.load()
            self.assertEqual(saved, [])                             # loading saves nothing
            self.assertEqual(st.al_sale.currentData(), 30)          # a hand-set value is kept
            st.al_proton.setChecked(False)                          # saved as it changes
            self.assertEqual({k: v for k, v in saved[-1].items() if k[0] == 'alerts'},
                             {('alerts', 'proton'): False})
            st.al_sale.setCurrentIndex(st.al_sale.findData(50))
            st.al_sale.activated.emit(st.al_sale.currentIndex())
        # (load_config is fixed here, so proton still differs from it)
        self.assertEqual({k: v for k, v in saved[-1].items() if k[0] == 'alerts'},
                         {('alerts', 'proton'): False, ('alerts', 'sale_min_cut'): 50})

    @linux_features
    def test_agent_switch_asks_before_turning_on(self):
        st = self.win.settings
        calls = []
        with patch_gui('cli_json', lambda args, cb, *_a: calls.append(args)):
            st.set_agent_status({'enabled': False, 'systemd': True})
            st.agent.setChecked(True)
            self.assertEqual(calls, [])                              # a question first
            self.assertFalse(st.agent.isChecked())
            self.assertTrue(self.win.sheet.isVisible())
            yes = next(b for b in self.win.sheet.findChildren(QPushButton)
                       if b.text() == "Turn it on")
            yes.click()
            self.assertEqual(calls, [['--agent-enable', '--json']])
            st.set_agent_status({'enabled': True, 'systemd': True, 'running': True})
            st.agent.setChecked(False)
            self.assertEqual(calls[-1], ['--agent-disable', '--json'])

    def test_close_goes_to_the_tray_when_asked(self):
        w = self.win
        with patch_gui('tray_available', lambda: True), \
                patch_gui('notify_desktop', lambda *a: None):
            w._apply_controls({'close_to_tray': True})
            self.assertIsNotNone(w.tray)
            w.close()
            self.assertFalse(w.isVisible())                 # hidden, still running
            self.assertFalse(w._quitting)
            w.tray._fill()
            texts = [a.text() for a in w.tray.menu.actions() if a.text()]
            self.assertEqual(texts[0], "Show Nexus")
            self.assertIn("Alpha", texts)                    # recently played, installed
            self.assertEqual(texts[-1], "Quit Nexus")
            w.tray.add_ready({'path': '/r/G', 'name': 'G'})
            w.tray._fill()
            self.assertIn("Install G", [a.text() for a in w.tray.menu.actions()])
            w.show_from_tray()
            self.assertTrue(w.isVisible())
            w._apply_controls({'close_to_tray': False})
            self.assertIsNone(w.tray)
        self.assertEqual(nx.recent_games([
            {'slug': 'a', 'last_played': '2026-01-01', 'launcher': '/a', 'source': 'nexus'},
            {'slug': 'b', 'last_played': '2026-02-01', 'launcher': '/b', 'source': 'nexus'},
            {'slug': 'c', 'source': 'nexus', 'launcher': '/c'}]), [
            {'slug': 'b', 'last_played': '2026-02-01', 'launcher': '/b', 'source': 'nexus'},
            {'slug': 'a', 'last_played': '2026-01-01', 'launcher': '/a', 'source': 'nexus'}])

    def test_quit_from_the_tray_really_quits(self):
        w = self.win
        with patch_gui('tray_available', lambda: True):
            w._apply_controls({'close_to_tray': True})
            w.quit_from_tray()
            self.assertFalse(w.isVisible())
            self.assertFalse(w._tray_quit)
            w._apply_controls({'close_to_tray': False})

    def test_tray_menu_entries_do_their_thing(self):
        w = self.win
        launched, started = [], []
        with patch_gui('tray_available', lambda: True), \
                mock.patch.object(w, 'on_game_action',
                                  side_effect=lambda what, g: launched.append(g['slug'])), \
                mock.patch.object(w, '_start_install',
                                  side_effect=lambda cmd, name, slug: started.append(name)):
            w._apply_controls({'close_to_tray': True})
            t = w.tray
            t.add_ready({'path': '/r/G', 'name': 'G'})
            t._fill()
            acts = {a.text(): a for a in t.menu.actions() if a.text()}
            acts["Alpha"].trigger()
            acts["Install G"].trigger()
            self.assertEqual(launched, ['alpha'])
            self.assertEqual(started, ['G'])
            self.assertEqual(t.ready, [])                   # offered once
            self.assertIs(nx.Tray.current, t)
            w._apply_controls({'close_to_tray': False})
        self.assertIsNone(nx.Tray.current)

    def test_notifications_without_notify_send(self):
        """Windows: the tray icon says it when the tray is on, else a toast
        (platform.notify_local); notify-send stays first where it exists."""
        from PyQt6.QtWidgets import QSystemTrayIcon
        from nexus import platform
        from nexus.gui import widgets
        w = self.win
        with mock.patch.object(widgets.shutil, 'which', return_value=None), \
                mock.patch.object(platform, 'notify_local') as toast:
            self.assertFalse(nx.tray.tray_message("T", "B"))      # no tray yet
            widgets.notify_desktop("T", "B")
            toast.assert_called_once_with("T", "B")
            toast.reset_mock()
            with patch_gui('tray_available', lambda: True), \
                    mock.patch.object(QSystemTrayIcon, 'supportsMessages', return_value=True):
                w._apply_controls({'close_to_tray': True})
                with mock.patch.object(w.tray.icon, 'showMessage') as show:
                    widgets.notify_desktop("T", "B")
                show.assert_called_once()
                self.assertEqual(show.call_args[0][:2], ("T", "B"))
                toast.assert_not_called()
                w._apply_controls({'close_to_tray': False})
        with mock.patch.object(widgets.shutil, 'which', return_value='/usr/bin/notify-send'), \
                mock.patch.object(widgets.QProcess, 'startDetached') as det, \
                mock.patch.object(platform, 'notify_local') as toast:
            widgets.notify_desktop("T", "B")
        self.assertEqual(det.call_args[0][0], 'notify-send')
        toast.assert_not_called()

    def test_a_second_hidden_launch_stays_in_the_tray(self):
        with mock.patch.object(self.win, 'bring_to_front') as front:
            self.win.handle_remote(['--hidden'])
            front.assert_not_called()
            self.win.handle_remote([])
            front.assert_called_once()

    def test_start_with_windows_needs_no_question_and_turns_the_tray_on(self):
        from nexus import features
        w = self.win
        calls = []

        def cli(args, cb, *_a):
            calls.append(args)
            cb({'startup': True, 'systemd': False, 'enabled': args[0] == '--agent-enable',
                'errors': []}, '')
        with mock.patch.object(features, 'FEATURES', frozenset()), \
                patch_gui('cli_json', cli), patch_gui('tray_available', lambda: True), \
                mock.patch.object(w, 'toast') as toast:
            w._apply_controls({'close_to_tray': False})
            w._background_changed('agent', True)
            self.assertEqual(calls, [['--agent-enable', '--json']])
            self.assertFalse(w.sheet.isVisible())                # no question first
            self.assertTrue(w.prefs.controls['close_to_tray'])   # where Nexus starts
            self.assertIsNotNone(w.tray)
            self.assertTrue(w.settings.tray.isChecked())
            self.assertIn("sign in", toast.call_args[0][0])
            w._background_changed('agent', False)
            self.assertEqual(calls[-1], ['--agent-disable', '--json'])
            self.assertTrue(w.prefs.controls['close_to_tray'])   # left as it is
            w._apply_controls({'close_to_tray': False})

    def test_first_library_load_fills_art_once(self):
        # `_art_filled` was also the name of the callback method, so the
        # getattr guard always saw a (truthy) bound method and never ran (w-10).
        from nexus.gui import win_library
        w = self.win
        w.__dict__.pop('_art_filled', None)
        w.config = {**w.config, 'api_key': 'k'}
        asked = []

        class Now:
            @staticmethod
            def singleShot(_ms, fn):
                fn()
        with mock.patch.object(win_library, 'QTimer', Now), \
                patch_gui('cli_json', lambda args, cb, *_a: asked.append(args[0])):
            w._on_library({'games': fake_games(), 'disks': []}, '')
            w._on_library({'games': fake_games(), 'disks': []}, '')
        self.assertEqual(asked.count('--fill-art'), 1)
        self.assertIs(w._art_filled, True)

    def test_the_window_stands_in_for_the_agent_on_windows(self):
        from nexus import features, instance
        from nexus.gui import win_downloads
        w = self.win
        asked = []
        alerts = [{'kind': 'atl', 'key': 'atl:1', 'title': 'One', 'text': 'One is at its low'},
                  {'kind': 'proton', 'key': 'p:1', 'title': 'GE', 'text': 'GE is out'}]
        w.config = {**w.config, 'api_key': 'k', 'gamedb_enabled': False}
        with mock.patch.object(instance, 'agent_running', return_value=False), \
                patch_gui('cli_json', lambda args, cb, *_a: asked.append(args[0])):
            with mock.patch.object(features, 'FEATURES', frozenset()):
                for _ in range(win_downloads.STANDIN_ART_EVERY):
                    w._check_repack_feed()
                self.assertEqual(w._feed_timer.interval(), win_downloads.STANDIN_MS)
                self.assertEqual(asked.count('--fill-art'), 1)       # every 6 h
                self.assertEqual(asked.count('--check-alerts'),
                                 win_downloads.STANDIN_ART_EVERY)
                self.assertEqual(asked.count('--game-facts'),
                                 win_downloads.STANDIN_ART_EVERY)        # every check
            with mock.patch.object(features, 'FEATURES', features.ALL):
                w._check_repack_feed()
                self.assertEqual(w._feed_timer.interval(), 6 * 3600 * 1000)   # Linux as before
        said = []
        with patch_gui('notify_desktop', lambda t, b: said.append(t)), \
                mock.patch.object(w, 'toast'):
            w._alerts_checked({'alerts': alerts}, '')
            self.assertEqual(said, [])                       # the window is showing
            w.hide()
            w._alerts_checked({'alerts': alerts}, '')
            w.show()
        self.assertEqual(said, ["Nexus", "Nexus"])

    def test_facts_fill_skips_with_an_agent_or_a_game(self):
        from nexus import instance
        w = self.win
        w.config = {**w.config, 'gamedb_enabled': False}
        for agent, playing, want in ((False, False, 1), (True, False, 0), (False, True, 0)):
            asked = []
            with mock.patch.object(instance, 'agent_running', return_value=agent), \
                    mock.patch.object(w, '_playing', playing), \
                    patch_gui('cli_json', lambda args, cb, *_a: asked.append(args)):
                w._check_repack_feed()
            facts = [a for a in asked if a[0] == '--game-facts']
            self.assertEqual(len(facts), want, (agent, playing))
            if facts:
                self.assertEqual(facts[0], ['--game-facts', '--fill', '30', '--json'])

    def test_shift_click_selects_a_range_and_batch_is_gated(self):
        w = self.win
        w.go(w.LIBRARY)
        w.apply_filter()
        order = [g['slug'] for g in w._shown_games]
        self.assertGreaterEqual(len(order), 3)
        S, N = Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.NoModifier
        w._tile_clicked(w.game(order[0]), N)
        w._tile_clicked(w.game(order[2]), S)
        self.assertEqual(w.selected, order[:3])
        # A Steam game in the selection: repack-only batch actions aren't offered.
        games = [w.game(s) for s in order[:3]]
        mixed = any(g.get('source') != 'nexus' for g in games)
        self.assertEqual(w.batch_applies('steam', games), not mixed)
        self.assertTrue(w.batch_applies('collection', games))
        repacks = [g for g in w._games if g.get('source', 'nexus') == 'nexus']
        w._playing = {repacks[0]['slug']}
        w._mark_states()
        ran = []
        with mock.patch.object(w, 'run', lambda args, *a, **k: ran.append(args)):
            w._batch_action('art', repacks)
        self.assertEqual(ran, [['--refetch-art', g['slug']] for g in repacks])   # not gated
        ran.clear()
        with mock.patch.object(w, 'run', lambda args, *a, **k: ran.append(args)):
            w._batch_action('uninstall', repacks)
            self.assertTrue(w.sheet.isVisible())                # asks first
            yes = next(b for b in w.sheet.findChildren(QPushButton)
                       if b.text().startswith("Uninstall"))
            yes.click()
        self.assertEqual(ran, [['--uninstall', g['slug'], '--yes', '--save-backup']
                               for g in repacks[1:]])           # the running one is skipped
        w._playing = set()
        w._mark_states()

    def test_crash_helper_after_a_quick_exit(self):
        w = self.win
        g = w.game('alpha')
        toasts = []
        with mock.patch.object(w, 'toast', lambda text, kind, act=None, t=0:
                               toasts.append((text, [a[0] for a in act or []]))), \
                patch_gui('last_exit_code', lambda slug: 3):
            w._after_session(g, 8)
            w._after_session(g, 300)                            # a real session: nothing
            w._trying['alpha'] = 'GE-Proton11-9'
            w._after_session(g, 120)
        self.assertEqual(toasts[0], ("Alpha closed after 8 s (exit code 3).",
                                     ["Show log", "Try another Proton…"]))
        self.assertEqual(len(toasts), 2)
        self.assertIn("ran with GE-Proton11-9", toasts[1][0])
        self.assertEqual(toasts[1][1], ["Use it"])
        steam = w.game('steam-1')
        with mock.patch.object(w, 'toast', lambda *a, **k: toasts.append(a)):
            w._after_session(steam, 3)                          # not a launcher we own
        self.assertEqual(len(toasts), 2)

    def test_command_palette_games_actions_and_commands(self):
        from PyQt6.QtTest import QTest
        w = self.win
        w.open_palette()
        p = w.palette
        self.assertTrue(p.isVisible())
        QTest.keyClicks(p.edit, "alph")
        self.assertEqual(p.current().title, "Alpha")
        QTest.keyClick(p.edit, Qt.Key.Key_Tab)                 # its actions
        self.assertIs(p._game['slug'], 'alpha')
        titles = [p.list.item(i).data(Qt.ItemDataRole.UserRole).title
                  for i in range(p.list.count())]
        self.assertIn("Favourite", titles)
        QTest.keyClick(p.edit, Qt.Key.Key_Escape)              # back to everything
        self.assertIsNone(p._game)
        self.assertTrue(p.isVisible())
        QTest.keyClicks(p.edit, "settings")
        self.assertTrue(any(p.list.item(i).data(Qt.ItemDataRole.UserRole).kind
                            in ('setting', 'page') for i in range(p.list.count())))
        p.edit.clear()
        QTest.keyClicks(p.edit, "surprise")
        self.assertEqual(p.current().key, 'cmd:surprise')
        with mock.patch.object(w, 'surprise') as surprise:
            p._all = nx.page_entries(w)                         # the entry holds the patched slot
            p._refill()
            QTest.keyClick(p.edit, Qt.Key.Key_Return)
        surprise.assert_called_once()
        self.assertFalse(p.isVisible())
        self.assertEqual(w.prefs.palette_recent[0], 'cmd:surprise')

    def test_settings_import_shows_the_plan_then_restarts(self):
        w = self.win
        calls = []
        plan = {'ok': True, 'replace': ['/c/config.toml'], 'keep_secrets': ['gamedb.token'],
                'games_added': ['a'], 'games_here': [], 'games_missing': [
                    {'slug': 'g', 'name': 'Gone', 'was': '/x'}], 'sessions_added': 0, 'art': 0}

        def fake(args, cb, *_a):
            calls.append(args)
            cb(plan if '--dry-run' in args else {'ok': True}, None)
        with patch_gui('cli_json', fake), mock.patch.object(w, 'restart') as restart:
            w._import_settings('/t/s.tar.gz')
            self.assertEqual(calls, [['--import-settings', '/t/s.tar.gz', '--dry-run', '--json']])
            self.assertTrue(w.sheet.isVisible())
            texts = " ".join(l.text() for l in w.sheet.findChildren(QLabel))
            self.assertIn("Gone", texts)
            yes = next(b for b in w.sheet.findChildren(QPushButton)
                       if b.text() == "Import and restart")
            yes.click()
            self.assertEqual(calls[-1], ['--import-settings', '/t/s.tar.gz', '--json'])
            restart.assert_called_once()

    def test_restart_starts_the_gui_without_a_shell(self):
        # It used to be `sh -c 'sleep 2; exec nexus'`, which can't run on
        # Windows (w-16). Now the new window waits for this one itself.
        from nexus import platform
        from nexus.gui import window as W
        started = []

        class Proc:
            @staticmethod
            def startDetached(program, args):
                started.append([program, *args])
                return True
        for argv in (['/src/bin/nexus'], [r'C:\Py\pythonw.exe', r'C:\src\nexus\gui\__main__.py']):
            started.clear()
            with mock.patch.object(W, 'QProcess', Proc), \
                    mock.patch.object(platform, 'gui_command', return_value=argv), \
                    mock.patch.object(self.win, 'quit_after_jobs') as quit_:
                self.win.restart()
            self.assertEqual(started, [[*argv, '--restarted']])
            quit_.assert_called_once()

    def test_a_restarted_window_waits_for_the_old_one(self):
        from nexus.gui import app
        answers = iter([True, True, False])
        with mock.patch.object(app.instance_mod, 'window_running', lambda: next(answers)), \
                mock.patch.object(app.time, 'sleep') as sleep:
            app.wait_for_exit()
        self.assertEqual(sleep.call_count, 2)

    def test_first_run_setup_walks_through_and_saves(self):
        import os
        from pathlib import Path
        w = self.win
        checks = {'checks': [
            {'group': 'Requirements', 'status': 'fail', 'title': 'Wine', 'detail': 'not found',
             'fix': 'Install it', 'actions': []},
            {'group': 'Requirements', 'status': 'ok', 'title': 'umu-run', 'detail': '/usr/bin',
             'actions': []},
            {'group': 'Integrations', 'status': 'ok', 'title': 'Heroic', 'detail': '~/.config',
             'actions': []}]}
        answers = {'--doctor': checks, '--list-protons': {'protons': [{'name': 'proton-x'}]},
                   '--torrent-detect': {'found': {'client': 'transmission',
                                                  'url': 'http://127.0.0.1:9091'}}}
        saved = []
        with patch_gui('cli_json', lambda args, cb, *_a: cb(answers.get(args[0]), None)), \
                patch_gui('save_config', saved.append), \
                mock.patch.object(w, 'set_appearance') as look:
            w.open_setup()
            self.assertEqual(w.current(), w.SETUP)
            st = w.setup
            self.assertIn("1 thing(s) to fix first: Wine", st.lead.text())
            self.assertFalse(st.get_ge.isHidden())                 # no GE-Proton: offer it
            for _ in range(len(st.steps) - 1):                 # no Proton step on Windows
                st._next()
            self.assertEqual(st.btn_next.text(), "Done")
            st.base.setText('/g/Games')
            st.theme.setCurrentIndex(st.theme.findData('dawn'))
            st._next()                                         # Done
        self.assertEqual(saved[0][('install', 'base')], '/g/Games')
        self.assertEqual(saved[0][('torrent', 'client')], 'transmission')
        self.assertTrue(w.prefs.setup_done)
        self.assertEqual(w.current(), w.HOME)
        self.assertEqual(look.call_args[0][0]['theme'], 'dawn')
        # Shown again only when nothing exists yet and setup wasn't done.
        w.prefs.setup_done = False
        with mock.patch.dict(os.environ, {'NEXUS_NO_SETUP': ''}), \
                mock.patch.object(type(w.prefs), 'PATH', Path('/nonexistent/library.json')), \
                patch_gui('CONFIG_PATH', Path('/nonexistent/config.toml')):
            self.assertTrue(w._first_run)
            w.prefs.setup_done = True
            self.assertFalse(w._first_run)

    def test_remove_with_files_asks_first(self):
        w = self.win
        with patch_gui('cli_json', lambda *a, **k: None):
            w._torrent_control('remove', ['a' * 40], True)
        self.assertTrue(w.sheet.isVisible())
        rows = [b.text() for b in w.sheet.findChildren(QPushButton)
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

    def test_settings_and_the_game_page_are_built_on_first_use(self):
        w = self.win
        self.assertFalse(w.page_built(w.SETTINGS))
        self.assertFalse(w.details._built)
        spot = w.pages[w.SETTINGS]
        w.go(w.SETTINGS)
        self.assertTrue(w.page_built(w.SETTINGS))
        self.assertIsInstance(w.pages[w.SETTINGS], nx.SettingsPage)
        self.assertIs(w.stack.currentWidget(), w.settings)
        self.assertEqual(w.current(), w.SETTINGS)
        self.assertEqual(w.stack.indexOf(spot), -1)          # the placeholder is gone
        self.assertEqual(w.settings.show_owned.isChecked(), w.prefs.show_owned)
        w.settings.show_owned.setChecked(not w.prefs.show_owned)   # wired to the window
        self.assertEqual(w.settings.show_owned.isChecked(), w.prefs.show_owned)
        w.go(w.LIBRARY)
        w.selected = ['alpha']
        w._sync_selection()
        self.assertTrue(w.details._built)
        self.assertEqual(w.details.name.text(), 'Alpha')

    def test_pointing_at_the_settings_icon_builds_it(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QEnterEvent
        w = self.win
        b = w.icons[w.SETTINGS]
        QApplication.sendEvent(b, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        self.assertTrue(w.page_built(w.SETTINGS))
        self.assertNotEqual(w.current(), w.SETTINGS)

    def test_title_bar_follows_the_theme(self):
        """Light themes get a light title bar, dark ones a dark one, again on
        every change (Windows; nexus.platform does the DWM part)."""
        from nexus import platform
        calls = []
        try:
            with mock.patch.object(platform, 'TITLE_BAR_COLORS', True), \
                    mock.patch.object(platform, 'style_title_bar',
                                      lambda *a: calls.append(a) or True):
                self.win.set_appearance({**nx.APPEARANCE_DEFAULT, 'theme': 'dawn'})
                self.win.set_appearance({**nx.APPEARANCE_DEFAULT, 'theme': 'midnight'})
        finally:
            self.win.set_appearance(dict(nx.APPEARANCE_DEFAULT))
        self.assertEqual([(c[1], c[4]) for c in calls],
                         [(nx.THEMES['dawn'][1]['base'], False),
                          (nx.THEMES['midnight'][1]['base'], True)])
        self.assertEqual(calls[0][0], int(self.win.winId()))

    def test_toasts_stack_without_squashing(self):
        w = self.win
        w.toast("Elden Ring is installed and ready to play.", 'success', None, 0)
        w.toast("Couldn't install Ratchet & Clank — setup exited with code 2", 'error',
                [("Verify & retry", lambda: None)])
        QApplication.processEvents()
        host = w.toasts
        toasts = [host.lay.itemAt(i).widget() for i in range(host.lay.count())]
        self.assertEqual(len(toasts), 2)
        for t in toasts:            # each as tall as it wants, not squeezed into one slot
            self.assertGreaterEqual(t.height(), t.minimumSizeHint().height())
        self.assertGreaterEqual(host.height(), sum(t.height() for t in toasts))
        buttons = [b.text() for b in toasts[1].findChildren(QPushButton)]
        self.assertIn("Verify && retry", buttons)       # "&&" draws one "&"

    def test_ampersands_show_as_ampersands(self):
        w = self.win
        nav = w.settings.nav_btns['backup']
        self.assertEqual(nav.text(), "Back up && move")
        w.settings._set_compact(True)
        w.settings._set_compact(False)              # the rail gives the names back
        self.assertEqual(nav.text(), "Back up && move")
        w._create_collection('Fast & Loud', 'alpha')
        self.assertEqual(w.chips['col:Fast & Loud'].text(), "Fast && Loud")
        w.prefs.collections = {}
        w._collections_changed()

    def test_owned_switch_drops_under_the_chips_when_narrow(self):
        """Installed / Not installed / Everything used to be squeezed until
        its labels were cut off (a 1366x768 laptop at 125-150 %, the game
        panel open); now it moves under the chips."""
        w = self.win
        w.go(w.LIBRARY)
        w.own_seg.setVisible(True)
        w._tile_activated(w._games[0])                  # the panel takes its share
        row = w.own_seg.parentWidget()
        for width, stacked in ((1900, False), (760, True), (1900, False)):
            w.resize(width, 850)
            QApplication.processEvents()
            self.assertEqual(row.stacked(), stacked, width)
            m = row.layout().contentsMargins()
            room = row.width() - m.left() - m.right()       # all of it, when stacked
            self.assertGreaterEqual(w.own_seg.width(), min(w.own_seg.sizeHint().width(), room),
                                    width)

    def test_launch_state_and_handoff(self):
        from unittest import mock
        w = self.win
        g = w._games[1]
        runs = []
        with mock.patch.object(winfocus, 'available', lambda: False), \
                mock.patch.object(winfocus, 'windows', lambda: []), \
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
        import threading
        from PyQt6.QtCore import QThreadPool
        gui_thread = []

        def windows():
            gui_thread.append(threading.current_thread() is threading.main_thread())
            return wins[0]

        def settle():
            for _ in range(2):                                # niri is asked off-thread
                QThreadPool.globalInstance().waitForDone(2000)
                QApplication.processEvents()
        with mock.patch.object(winfocus, 'available', lambda: True), \
                mock.patch.object(winfocus, 'windows', windows), \
                mock.patch.object(winfocus, 'focus', lambda i: focused.append(i) or True), \
                mock.patch.object(w, 'run', lambda *a, **k: None):
            w.on_game_action('launch', g)
            settle()                                          # the "before" snapshot
            wins[0] = after
            w._handoff_tick()
            settle()
            self.assertEqual(focused, [9])                    # the game's window
            w._apply_running({g['slug']})
            w._apply_running(set())
            settle()
            self.assertEqual(focused, [9, 1])                 # and back to Nexus
        self.assertTrue(gui_thread)
        self.assertNotIn(True, gui_thread)                    # never on the GUI thread

    def test_tile_focus_shows_no_overlay_buttons(self):
        t = next(iter(self.win.homeview.tiles.values()))[0]
        self.win.activateWindow()
        t.setFocus()
        QApplication.processEvents()
        self.assertFalse(t.btn_play.isVisible())
        self.assertFalse(t.btn_more.isVisible())

    @linux_features
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
        d = w.theatre.details
        d.btn_fav.setFocus()
        for key in (Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_Right,
                    Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Right):
            QApplication.sendEvent(QApplication.focusWidget(),
                                   QKeyEvent(QEvent.Type.KeyPress, key,
                                             Qt.KeyboardModifier.NoModifier))
            QApplication.processEvents()
            self.assertTrue(d.isAncestorOf(QApplication.focusWidget()),
                            QApplication.focusWidget())
        self.assertTrue(d.expanded())                     # Down went into the drawer
        QApplication.sendEvent(QApplication.focusWidget(),
                               QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                                         Qt.KeyboardModifier.NoModifier))
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), d.band_play)     # Play, one Up away
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
                                                       x.findChildren(QLabel)][:3])
                    for x in QApplication.topLevelWidgets()
                    if x.isVisible() and x is not w and not any(x is b for b in before)
                    and not isinstance(x, (QMenu,)) and x.windowType() not in (
                        Qt.WindowType.ToolTip, Qt.WindowType.Popup)]
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
                if ev.type() == QEvent.Type.Show and isinstance(obj, QWidget) \
                        and obj.isWindow() and obj is not self.main \
                        and not isinstance(obj, QMenu) and obj.windowType() not in (
                            Qt.WindowType.ToolTip, Qt.WindowType.Popup):
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
                                            Qt.KeyboardModifier.NoModifier))
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
        self.assertEqual(w.theatre.hero.btn_more.focusPolicy(), Qt.FocusPolicy.NoFocus)
        tile = next(t for t in w.theatre.findChildren(nx.GameTile) if not t.isHidden())
        tile.setFocus()
        self._key(tile, Qt.Key.Key_Menu)                    # X
        self.assertTrue(w.theatre_sheet.isVisible())
        self.assertIsNone(QApplication.activePopupWidget())
        self.assertIs(w.nav_scope(), w.theatre_sheet)
        self.assertTrue(w.theatre_sheet.isAncestorOf(QApplication.focusWidget()))
        keys = [b.text() for b in w.theatre_sheet.findChildren(QPushButton)]
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

    def test_theatre_drawer_sections_and_reader(self):
        w = self.win
        w.set_theatre(True)
        g = w._games[1]                                       # Steam, appid 1
        w.theatre.open_game(g)
        d = w.theatre.details
        dr = d.drawer
        # The stage: no pills, no Back button, no row of cards; tabs only.
        self.assertFalse(hasattr(d, 'cards'))
        self.assertFalse([b for b in d.findChildren(QPushButton) if b.text() == "Back"])
        self.assertFalse(d.expanded())
        self.assertFalse(dr.pages.isVisibleTo(d))
        self.assertEqual(dr.visible_keys(), ['overview'])      # nothing else known yet
        d.set_info(tips={'protondb': {'tier': 'gold', 'total': 9},
                         'gamedb': {'list': 'library', 'hltb': {'main': 10}}},
                   news=[{'appid': 1, 'title': 'Patch', 'summary': 'Fixes',
                          'date': '2026-10-01T10:00:00'},
                         {'appid': 2, 'title': 'Other game'}],
                   store={'store': {'blurb': 'A game about tests.', 'developers': ['Dev'],
                                    'trailers': [{'name': 'Launch trailer',
                                                  'url': 'https://example.invalid/t.m3u8'}]}})
        # Compatibility is a Proton fact: no section without Proton.
        self.assertEqual('compat' in dr.visible_keys(), features.has('proton'))
        self.assertIn('news', dr.visible_keys())
        self.assertIn('media', dr.visible_keys())
        self.assertNotIn('achievements', dr.visible_keys())   # this game has none
        self.assertEqual(dr.about.text(), 'A game about tests.')
        # D-pad Down from Play reaches the tabs and opens the drawer.
        w.activateWindow()
        d.btn_play.setFocus()
        self._key(d.btn_play, Qt.Key.Key_Down)
        self.assertTrue(d.expanded())
        self.assertTrue(dr.isAncestorOf(QApplication.focusWidget()))
        self.assertTrue(d.band_play.isVisible())              # Play, one Up away
        self.assertFalse(d.btn_play.isVisible())
        # News: one row for this game only; A opens it in the reader.
        dr.show_section('news')
        rows = dr.sections['news'].findChildren(nx.TheatreRow)
        self.assertEqual([r.title for r in rows], ['Patch'])
        rows[0].setFocus()
        self._key(rows[0], Qt.Key.Key_Return)
        self.assertTrue(dr.reader_open())
        self.assertIn('Fixes', dr.reader.toPlainText())
        w._escape()                                           # B: the reader...
        self.assertTrue(d.expanded())
        self.assertFalse(dr.reader_open())
        self.assertIs(QApplication.focusWidget(), rows[0])
        w._escape()                                           # ...the drawer...
        self.assertFalse(d.expanded())
        self.assertTrue(d.isVisible())
        self.assertIs(QApplication.focusWidget(), d.btn_play)
        w._escape()                                           # ...then the page
        self.assertFalse(d.isVisible())
        self.assertTrue(w._theatre)
        w.set_theatre(False)

    def test_theatre_achievements_section(self):
        w = self.win
        w.set_theatre(True)
        g = dict(w._games[1], achievements=[3, 4])
        asked = []
        with patch_gui('cli_json', lambda args, cb, *a, **k: asked.append((args, cb))):
            w.theatre.open_game(g)
        d = w.theatre.details
        self.assertIn('achievements', d.drawer.visible_keys())
        self.assertEqual(d.drawer.ach_line.text(), "3 of 4 achievements")    # Overview too
        cb = next(cb for args, cb in asked if args[0] == '--achievements')
        cb({'items': [{'title': 'First', 'unlocked': True}]}, '')
        row = d.drawer.sections['achievements'].findChild(nx.TheatreRow)
        self.assertIn('First', row.more)
        w.set_theatre(False)

    def test_theatre_media_plays_trailers_in_theatre(self):
        w = self.win
        w.set_theatre(True)
        w.theatre.open_game(w._games[1])
        d = w.theatre.details
        trailer = {'name': 'Launch trailer', 'url': 'https://example.invalid/t.m3u8'}
        d.set_info(store={'store': {'trailers': [trailer]}})
        d.cycle_section(1)                                    # RB opens the drawer...
        d.cycle_section(1)                                    # ...and moves along
        self.assertEqual(d.drawer.tabs.current(), 'media')
        shot = d.drawer.sections['media'].findChild(nx.TheatreShot)
        self.assertEqual(d.drawer.media_title.text(), 'Launch trailer')
        played = []
        with mock.patch.object(w.theatre.trailer, 'play', lambda t, name='': played.append(t)):
            shot.setFocus()
            self._key(shot, Qt.Key.Key_Return)
        self.assertEqual(played, [trailer])
        w.set_theatre(False)

    def test_theatre_b_order(self):
        """B: the trailer, the reader, the drawer, the page (back to the
        search it came from), the search, then Home; never out of Theatre."""
        w = self.win
        w.set_theatre(True)
        w.activateWindow()
        th = w.theatre
        w._pad_action('page-next')                            # Library
        th.open_game(w._games[1])
        d = th.details
        d.set_info(news=[{'appid': 1, 'title': 'Patch', 'summary': 'Fixes'}])
        d.cycle_section(1)
        d.drawer.show_section('news')
        d._open_row(d.drawer.sections['news'].findChild(nx.TheatreRow))
        th.trailer.show()                                     # as if a trailer played
        th.trailer.raise_()
        self.assertIs(w.nav_scope(), th.trailer)
        steps = []
        for _ in range(5):
            w._escape()
            steps.append((th.trailer.isVisible(), d.drawer.reader_open(), d.expanded(),
                          d.isVisible(), th.tabs.current()))
        self.assertEqual(steps, [(False, True, True, True, 'library'),
                                 (False, False, True, True, 'library'),
                                 (False, False, False, True, 'library'),
                                 (False, False, False, False, 'library'),
                                 (False, False, False, False, 'home')])
        w._pad_action('search')                               # from the search...
        th.search.picked.emit(w._games[0])
        d.cycle_section(1)
        w._escape()                                           # ...the drawer, then the search
        self.assertTrue(d.isVisible())
        w._escape()
        self.assertTrue(th.search.isVisible())
        w._escape()
        self.assertFalse(th.search.isVisible())
        w._escape()
        self.assertTrue(w._theatre)
        w.set_theatre(False)

    def test_theatre_lb_rb_switch_sections_on_the_game_page(self):
        w = self.win
        w.set_theatre(True)
        th = w.theatre
        w._pad_action('page-next')                            # elsewhere: the tabs
        self.assertEqual(th.tabs.current(), 'library')
        th.open_game(w._games[1])
        d = th.details
        d.set_info(news=[{'appid': 1, 'title': 'Patch'}],
                   store={'store': {'trailers': [{'url': 'https://example.invalid/t'}]}})
        w._pad_action('page-next')                            # opens the drawer
        self.assertTrue(d.expanded())
        self.assertEqual(d.drawer.tabs.current(), 'overview')
        w._pad_action('page-next')
        self.assertEqual(d.drawer.tabs.current(), 'media')
        w._pad_action('page-prev')
        w._pad_action('page-prev')                            # wraps, skipping hidden ones
        self.assertEqual(d.drawer.tabs.current(), d.drawer.visible_keys()[-1])
        self.assertEqual(th.tabs.current(), 'library')        # the tabs behind stay put
        th.close_game()
        w._pad_action('page-prev')                            # off the page: tabs again
        self.assertEqual(th.tabs.current(), 'home')
        w.set_theatre(False)

    def test_theatre_favourite_label_follows_the_state(self):
        w = self.win
        w.set_theatre(True)
        g = w._games[0]
        w.theatre.open_game(g)
        d = w.theatre.details
        d.btn_fav.click()
        QApplication.processEvents()
        self.assertEqual(d.btn_fav.text(), nx.G['star'])
        self.assertEqual(d.btn_fav.toolTip(), "Favourite")
        d.btn_fav.click()
        QApplication.processEvents()
        self.assertEqual(d.btn_fav.text(), nx.G['star_outline'])
        self.assertEqual(d.btn_fav.toolTip(), "Add to favourites")
        w.set_theatre(False)

    def test_theatre_default_focus_skips_a_disabled_play(self):
        w = self.win
        w.set_theatre(True)
        g = dict(w._games[0], playing=True)
        w.theatre.open_game(g)
        d = w.theatre.details
        self.assertFalse(d.btn_play.isEnabled())
        self.assertFalse(d.band_play.isEnabled())             # the band's Play too
        self.assertIs(w.theatre.default_focus(), d.btn_fav)
        w._game_changed(g['slug'])                            # the real game: both follow
        self.assertTrue(d.btn_play.isEnabled())
        self.assertTrue(d.band_play.isEnabled())
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
            w._show_menu(w._games[0], QPoint())
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
        self._key(w.search, Qt.Key.Key_Down)          # posted-style (not spontaneous)
        self.assertEqual(w.input.mode, 'pad')              # …keys we send keep 'pad'
        w.input.set('mouse')
        self._key(w.search, Qt.Key.Key_Tab)
        self.assertEqual(w.input.mode, 'keyboard')
        QApplication.sendEvent(w, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(3, 3), QPointF(3, 3),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
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
                patch_gui('cli_json', lambda *a, **k: None):
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
            with patch_gui('in_thread', lambda fn, cb, d=poll: cb(d, None)), \
                    patch_gui('cli_json', lambda a, cb, *_x, d=data:
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
        titles = [r.findChild(QLabel).text() for r in w.theatre.findChildren(nx.ShelfRow)]
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
        self._key(srch.keys['q'], Qt.Key.Key_Menu)          # X: delete
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

    @linux_features
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
        texts = [l.text() for l in up.findChildren(QLabel)]
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

    def test_manage_feeds_lists_every_source_and_batches_ticks(self):
        p = self.page
        feeds = [{'id': 'gol', 'name': 'GamingOnLinux', 'url': 'https://g/', 'user': False, 'on': True},
                 {'id': 'ign', 'name': 'IGN', 'url': 'https://i/', 'user': False, 'on': False},
                 {'id': 'user:https://m/', 'name': 'Mine', 'url': 'https://m/', 'user': True,
                  'on': True}]
        p.set_data({'items': [], 'sources': [], 'feeds': feeds,
                    'options': {'steam': True, 'proton': False}})
        boxes = [p.sources.itemAt(i).widget() for i in range(p.sources.count())]
        self.assertEqual([b.isChecked() for b in boxes], [True, False, True])
        self.assertEqual(boxes[2].text(), "Mine (yours)")
        self.assertEqual((p.opt_steam.isChecked(), p.opt_proton.isChecked()), (True, False))
        self.assertEqual(p.user_feeds.count(), 1)
        boxes[1].setChecked(True)
        boxes[0].setChecked(False)
        boxes[1].setChecked(False)               # the last tick on a source wins
        self.assertEqual(p._pending, {'ign': False, 'gol': False})
        sent = []
        patch = patch_gui('CliQuery', lambda args, cb: sent.append(args))
        patch.start()
        self.addCleanup(patch.stop)
        p._send_switches()
        self.assertEqual(sent, [['--news-source', 'ign', 'off', '--news-source', 'gol', 'off']])
        self.assertEqual(p._pending, {})

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
        fm = QFontMetrics(QFont())
        out = nx.elide_lines("word " * 200, fm, 120, 3)
        self.assertEqual(len(out.split('\n')), 3)
        self.assertTrue(out.endswith('…'))
        self.assertEqual(nx.elide_lines("short", fm, 120, 3), "short")


if __name__ == '__main__':
    unittest.main()
