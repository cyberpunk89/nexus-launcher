"""Library metadata: Steam store info (trailers, reviews, similar games),
HowLongToBeat, play-session history, duplicates across stores, your own
names and notes. Network calls are mocked; files live in temp dirs."""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import history, hltb, net, storeinfo  # noqa: E402

APPDETAILS = {'10': {'success': True, 'data': {
    'name': 'Hades', 'short_description': '<b>Defy</b> the god &amp; more',
    'genres': [{'description': 'Action'}], 'developers': ['Supergiant'],
    'release_date': {'date': 'Sep 17, 2020'},
    'metacritic': {'score': 93, 'url': 'https://www.metacritic.com/game/pc/hades'},
    'movies': [{'name': 'Launch', 'thumbnail': 'https://shared.akamai.steamstatic.com/t.jpg',
                'hls_h264': 'https://video.akamai.steamstatic.com/x/hls.m3u8', 'highlight': False},
               {'name': 'Evil', 'hls_h264': 'https://evil.example/x.m3u8'},
               {'name': 'Teaser', 'hls_h264': 'https://video.akamai.steamstatic.com/t.m3u8',
                'highlight': True}],
    'screenshots': [{'path_full': 'https://shared.akamai.steamstatic.com/s.jpg'},
                    {'path_full': 'http://insecure/s.jpg'}]}}}

MORELIKE = """
<a class="similar_grid_capsule"  data-ds-appid="10" href="https://store.steampowered.com/app/10/Hades/">
<a class="similar_grid_capsule" data-ds-appid="250900" data-ds-itemkey="App_250900" href="https://store.steampowered.com/app/250900/The_Binding_of_Isaac_Rebirth/?snr=1">
<a class="similar_grid_capsule" data-ds-appid="250900" href="https://store.steampowered.com/app/250900/Again/">
<a class="similar_grid_capsule" data-ds-appid="1145350" href="https://store.steampowered.com/app/1145350/Hades_II/">
"""


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for mod in (storeinfo,):
            p = mock.patch.object(mod, 'CACHE_DIR', Path(self.tmp.name))
            p.start()
            self.addCleanup(p.stop)

    def test_details(self):
        with mock.patch.object(net, 'get', return_value=json.dumps(APPDETAILS).encode()):
            d = storeinfo.details(10)
        self.assertEqual(d['blurb'], 'Defy the god & more')
        self.assertEqual([t['name'] for t in d['trailers']], ['Teaser', 'Launch'])  # highlight 1st
        self.assertEqual(d['screenshots'], ['https://shared.akamai.steamstatic.com/s.jpg'])
        self.assertEqual(d['metacritic']['score'], 93)

    def test_reviews_and_missing_game(self):
        body = {'query_summary': {'review_score_desc': 'Very Positive', 'review_score': 8,
                                  'total_positive': 90, 'total_reviews': 100}}
        with mock.patch.object(net, 'get', return_value=json.dumps(body).encode()):
            self.assertEqual(storeinfo.reviews(10)['percent'], 90)
        with mock.patch.object(net, 'get',
                               return_value=json.dumps({'11': {'success': False}}).encode()):
            self.assertIsNone(storeinfo.details(11))

    def test_morelike(self):
        got = storeinfo.parse_morelike(MORELIKE, 10)
        self.assertEqual([(g['appid'], g['name']) for g in got],
                         [(250900, 'The Binding of Isaac Rebirth'), (1145350, 'Hades II')])


class Hltb(unittest.TestCase):
    ROWS = [{'game_id': 1, 'game_name': 'Hades II', 'comp_main': 100000, 'release_world': 2025},
            {'game_id': 2, 'game_name': 'Hades', 'game_alias': '', 'comp_main': 84960,
             'comp_plus': 175028, 'comp_100': 0, 'release_world': 2020}]

    def test_best_match(self):
        self.assertEqual(hltb.best_match('Hades™', self.ROWS)['game_id'], 2)
        self.assertIsNone(hltb.best_match('Completely Different', self.ROWS))

    def test_lookup_caches_hits_and_failures(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(hltb, 'CACHE_DIR', Path(d)):
            with mock.patch.object(hltb, 'search', return_value=self.ROWS) as s:
                hit = hltb.lookup('Hades')
                hltb.lookup('Hades')
            self.assertEqual(s.call_count, 1)                   # cached
            self.assertEqual((hit['main'], hit['plus'], hit['complete']), (23.6, 48.6, None))
            self.assertEqual(hit['url'], 'https://howlongtobeat.com/game/2')
            with mock.patch.object(hltb, 'search', side_effect=net.NetError('down')):
                self.assertIsNone(hltb.lookup('Other game'))
                self.assertEqual(hltb.lookup('Hades', refresh=True)['main'], 23.6)  # old answer


class History(unittest.TestCase):
    def test_record_load_summary(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'sessions.jsonl')
            today = date(2026, 10, 17)
            t = datetime(2026, 10, 15, 20, 0)
            self.assertFalse(history.record('x', 'nexus', t, t + timedelta(seconds=20),
                                            path=path))           # too short
            for day, mins, slug in ((15, 120, 'hades'), (16, 30, 'hades'), (17, 60, 'celeste'),
                                    (1, 45, 'hades')):
                s = datetime(2026, 10, day, 20, 0)
                history.record(slug, 'steam', s, s + timedelta(minutes=mins), slug.title(),
                               path=path)
            with open(path, 'a') as f:
                f.write('{torn line\n')
            sessions = history.load(path)
            self.assertEqual(len(sessions), 4)
            sm = history.summary(sessions, today)
        y = sm['year']
        self.assertEqual((y['sessions'], y['games'], y['best_streak']), (4, 2, 3))
        self.assertEqual(y['minutes'], 255)
        self.assertEqual(y['longest']['minutes'], 120)
        self.assertEqual(y['top'][0]['slug'], 'hades')
        self.assertEqual(sm['weeks'][-1]['minutes'], 210)       # this week: 15th–17th
        self.assertEqual(len(sm['weeks']), 12)
        self.assertEqual(sm['since'], '2026-10-01')


    def test_is_session_threshold(self):
        """One rule for play time and sessions.jsonl (w-18): 60 s."""
        self.assertFalse(history.is_session(59))
        self.assertTrue(history.is_session(60))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'sessions.jsonl')
            t = datetime(2026, 10, 15, 20, 0)
            for secs in (45, 59, 60, 90):
                self.assertEqual(history.record('x', 'nexus', t, t + timedelta(seconds=secs),
                                                path=path), history.is_session(secs))
            self.assertEqual(len(history.load(path)), 2)

    def test_current_streak(self):
        """w-63: days in a row up to today, or up to yesterday while today
        has no play yet; a gap ends it."""
        def sess(day):
            s = datetime(2026, 10, day, 20, 0)
            return {'slug': 'x', 'name': 'X', 'start': s.isoformat(), '_start': s,
                    'minutes': 30.0}
        days = [sess(d) for d in (20, 22, 23, 24, 24)]       # 21st missed; two on the 24th
        self.assertEqual(history.current_streak(days, date(2026, 10, 24)), 3)   # today
        self.assertEqual(history.current_streak(days, date(2026, 10, 25)), 3)   # from yesterday
        self.assertEqual(history.current_streak(days, date(2026, 10, 26)), 0)   # a gap
        self.assertEqual(history.current_streak(days, date(2026, 10, 20)), 1)
        self.assertEqual(history.current_streak([], date(2026, 10, 24)), 0)
        self.assertEqual(history.periods(days, date(2026, 10, 25))['current_streak'], 3)

    def test_periods(self):
        def sess(day, mins, slug, month=10):
            s = datetime(2026, month, day, 20, 0)
            return {'slug': slug, 'name': slug.title(), 'start': s.isoformat(),
                    '_start': s, 'minutes': float(mins)}
        sessions = [sess(26, 60, 'hades'), sess(28, 30, 'celeste'), sess(28, 15, 'hades'),
                    sess(1, 120, 'celeste'), sess(30, 999, 'hades', month=9)]
        p = history.periods(sessions, date(2026, 10, 28))          # a Wednesday
        week, month = p['week'], p['month']
        self.assertEqual((week['start'], len(week['days'])), ('2026-10-26', 7))
        self.assertEqual([d['minutes'] for d in week['days']], [60, 0, 45, 0, 0, 0, 0])
        self.assertEqual((week['minutes'], week['sessions']), (105, 3))
        self.assertEqual([g['slug'] for g in week['games']], ['hades', 'celeste'])
        self.assertEqual((month['start'], len(month['days'])), ('2026-10-01', 31))
        self.assertEqual(month['minutes'], 225)                    # September left out
        self.assertEqual(month['games'][0], {'slug': 'celeste', 'name': 'Celeste',
                                             'minutes': 150})
        self.assertEqual(history.periods([], date(2026, 2, 3))['month']['days'][-1]['date'],
                         '2026-02-28')
        self.assertIn('periods', history.summary(sessions, date(2026, 10, 28)))


try:
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ.setdefault('NEXUS_NO_GAMEPAD', '1')
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from nexus import gui as nx
except ImportError:
    nx = None


@unittest.skipIf(nx is None, "PyQt6 not available")
class Gui(unittest.TestCase):
    def test_duplicates_keep_one_tile(self):
        games = [{'slug': 'hades', 'display_name': 'Hades', 'source': 'nexus',
                  'umu_gameid': 'umu-1145360'},
                 {'slug': 'steam-1145360', 'display_name': 'Hades', 'source': 'steam',
                  'appid': 1145360, 'installed': False},
                 {'slug': 'heroic-hades', 'display_name': 'Hades', 'source': 'heroic'},
                 {'slug': 'celeste', 'display_name': 'Celeste', 'source': 'steam', 'appid': 1}]
        nx.mark_duplicates(games)
        self.assertNotIn('dup_of', games[0])
        self.assertEqual(games[0]['dup_also'], ['heroic', 'steam'])
        self.assertEqual(games[1]['dup_of'], 'hades')
        self.assertEqual(games[2]['dup_of'], 'hades')               # folded in by name
        self.assertNotIn('dup_of', games[3])
        # An installed Steam copy beats a not-installed repack entry.
        games[0]['installed'], games[1]['installed'] = False, True
        nx.mark_duplicates(games)
        self.assertEqual(games[0]['dup_of'], 'steam-1145360')
        nx.mark_duplicates(games, enabled=False)
        self.assertFalse(any(g.get('dup_of') for g in games))

    def test_duplicates_across_epic_and_steam(self):
        games = [{'slug': 'epic-Fig', 'display_name': 'Hades™', 'source': 'epic',
                  'installed': True},
                 {'slug': 'steam-1145360', 'display_name': 'Hades', 'source': 'steam',
                  'appid': 1145360, 'installed': True, 'playtime_minutes': 5},
                 # A source that knows the Steam appid matches whatever the name.
                 {'slug': 'epic-Owl', 'display_name': 'Control Ultimate Edition',
                  'source': 'epic', 'store_appid': 870780, 'installed': True},
                 {'slug': 'steam-870780', 'display_name': 'Control', 'source': 'steam',
                  'appid': 870780, 'installed': False},
                 {'slug': 'epic-Solo', 'display_name': 'Solo', 'source': 'epic',
                  'installed': True}]
        nx.mark_duplicates(games)
        self.assertEqual(games[0]['dup_of'], 'steam-1145360')       # Steam before Epic
        self.assertEqual(games[1]['dup_also'], ['epic'])
        self.assertEqual(games[3]['dup_of'], 'epic-Owl')            # installed wins
        self.assertNotIn('dup_of', games[4])
        self.assertEqual(nx.SOURCE_LABEL['epic'], 'Epic')

    def test_about_html_escapes_and_links_only_known_sites(self):
        out = nx.about_html({'blurb': '<script>x</script>', 'genres': ['A'],
                             'url': 'https://store.steampowered.com/app/1/'},
                            {'desc': 'Mixed', 'percent': 60, 'total': 1000},
                            {'main': 6.9, 'plus': None, 'url': 'javascript:alert(1)'})
        self.assertNotIn('<script>', out)
        self.assertNotIn('javascript:', out)
        self.assertIn('main 6.9 h', out)
        self.assertIn('Mixed', out)
        self.assertEqual(nx.about_html(None, None, None), '')
        # What the game page's context block shows already is left out (w-40).
        trimmed = nx.about_html({'blurb': 'B', 'genres': ['RPG'], 'developers': ['D']}, None,
                                {'main': 6.9}, skip=('facts', 'hltb'))
        self.assertIn('B', trimmed)
        self.assertNotIn('RPG', trimmed)
        self.assertNotIn('6.9', trimmed)

    def test_play_history_page(self):
        page = nx.PlayHistoryPage()
        page.fill({})
        self.assertFalse(page.body.isVisibleTo(page))
        today = date.today()
        sessions = [{'slug': 'h', 'name': 'Hades', 'start': f"{today}T20:00:00",
                     '_start': datetime.combine(today, datetime.min.time()), 'minutes': 90.0}]
        page.fill(history.summary(sessions, today))
        self.assertTrue(page.body.isVisibleTo(page))
        self.assertEqual(page.t_sessions.value.text(), '1')
        page.deleteLater()

    def test_prefs_keep_notes_and_names(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'library.json')
            path.write_text(json.dumps({'notes': {'a': 'left off at boss 3', 'b': '  '},
                                        'overrides': {'c': {'name': 'My name'}, 'd': {}}}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                prefs = nx.LibraryPrefs()
                self.assertEqual(prefs.notes, {'a': 'left off at boss 3'})
                self.assertEqual(prefs.overrides, {'c': {'name': 'My name'}})
                self.assertTrue(prefs.controls['dedupe'])
                prefs.notes['e'] = 'x'
                prefs.save()
                self.assertEqual(json.loads(path.read_text())['notes']['e'], 'x')

    def test_menu_offers_local_rename_for_store_games(self):
        keys = [a.key for a in nx.game_actions({'slug': 's', 'source': 'steam', 'appid': 1,
                                                'launcher': 'steam://x', 'store_name': 'S'})]
        self.assertIn('rename-local', keys)
        self.assertIn('rename-reset', keys)
        self.assertNotIn('rename-local', [a.key for a in nx.game_actions(
            {'slug': 'r', 'source': 'nexus', 'launcher': '/x'})])


if __name__ == '__main__':
    unittest.main()
