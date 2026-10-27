"""Design-system checks for the Nexus GUI (the nexus/gui package): radius tokens, rounded images,
the segmented control. Offscreen, no network, no CLI."""

import re
import unittest

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
from tests.patching import linux_features, patch_gui  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPixmap
    from PyQt6.QtWidgets import QApplication, QLabel


@unittest.skipIf(nx is None, "PyQt6 not available")
class Stylesheet(unittest.TestCase):
    def _template(self):
        src = '\n'.join(p.read_text() for p in sorted((nx._REPO / 'nexus' / 'gui').glob('*.py')))
        a = src.index('def build_stylesheet')
        return src[a:src.index('return tpl.substitute', a)]

    def test_no_literal_or_square_radii(self):
        """Every radius comes from a token, and nothing is forced square."""
        tpl = self._template()
        self.assertEqual(re.findall(r'border-radius:\s*\d', tpl), [])
        self.assertNotIn('border-radius: 0', nx.build_stylesheet())

    def test_substitutes(self):
        css = nx.build_stylesheet()
        self.assertNotIn('$', css.replace('$$', ''))
        self.assertIn('qlineargradient', css)
        # A backslash in url("...") is an escape: Windows paths broke the sheet.
        self.assertEqual([u for u in re.findall(r'url\("([^"]*)"\)', css) if '\\' in u], [])

    def test_pill(self):
        self.assertEqual(nx.pill(28), 14)
        self.assertIn(f"border-radius: {nx.pill(nx.PILL_H)}px", nx.pill_qss('#000', '#fff'))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Popups(unittest.TestCase):
    def test_rounded_popups_drop_a_square_system_shadow(self):
        """Menus go translucent for their rounded corners; where the system
        draws a square shadow around popups (Windows), that is turned off."""
        from unittest import mock
        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QMenu
        from nexus import platform
        from nexus.gui.widgets import PopupRounder
        rounder = PopupRounder()
        for square in (False, True):
            m = QMenu()
            self.addCleanup(m.deleteLater)
            with mock.patch.object(platform, 'SQUARE_POPUP_SHADOW', square):
                rounder.eventFilter(m, QEvent(QEvent.Type.Polish))
            self.assertTrue(m.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            self.assertEqual(bool(m.windowFlags() & Qt.WindowType.NoDropShadowWindowHint),
                             square)

    def test_monospace_has_a_windows_face(self):
        """Windows has none of the Linux monospace faces, and 'monospace'
        resolves to Tahoma there."""
        self.assertIn('Cascadia Mono', nx.MONO_STACK)
        self.assertIn('Consolas', nx.MONO_STACK)


@unittest.skipIf(nx is None, "PyQt6 not available")
class RoundedImages(unittest.TestCase):
    def test_corners_transparent(self):
        src = QPixmap(40, 60)
        src.fill(QColor('#ff0000'))
        out = nx.rounded_pixmap(src, 8).toImage()
        self.assertEqual(out.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(out.pixelColor(39, 59).alpha(), 0)
        self.assertEqual(out.pixelColor(20, 30).alpha(), 255)

    def test_zero_radius_is_identity(self):
        src = QPixmap(4, 4)
        self.assertIs(nx.rounded_pixmap(src, 0), src)

    def test_dominant_color(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'a.png')
            pm = QPixmap(10, 10)
            pm.fill(QColor('#2040c0'))
            pm.save(path)
            c = nx.dominant_color(path)
            self.assertTrue(c.isValid())
            self.assertGreater(c.blue(), c.red())
            self.assertIsNone(nx.dominant_color(None))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Segmented(unittest.TestCase):
    def setUp(self):
        self.seg = nx.SegmentedControl('pill', 32)
        for k in ('home', 'grid', 'list'):
            self.seg.add(k, glyph='x', tip=k)
        self.seg.show()
        QApplication.processEvents()

    def tearDown(self):
        self.seg.deleteLater()

    def test_first_is_current(self):
        self.assertEqual(self.seg.current(), 'home')
        self.assertTrue(self.seg.item('home').isChecked())

    def test_click_emits_and_moves_thumb(self):
        got = []
        self.seg.changed.connect(got.append)
        self.seg.item('list').click()
        QApplication.processEvents()
        self.assertEqual(got, ['list'])
        self.assertEqual(self.seg.current(), 'list')
        self.assertFalse(self.seg.item('home').isChecked())
        self.seg._anim.stop()
        self.seg._set_thumb(self.seg._target('list'))
        self.assertAlmostEqual(self.seg._thumb.center().x(),
                               self.seg.item('list').geometry().center().x(), delta=1)

    def test_set_current_programmatic_does_not_emit(self):
        got = []
        self.seg.changed.connect(got.append)
        self.seg.set_current('grid')
        self.assertEqual(got, [])
        self.assertEqual(self.seg.current(), 'grid')

    def test_unknown_key_ignored(self):
        self.seg.set_current('nope')
        self.assertEqual(self.seg.current(), 'home')

    def test_thumb_follows_items_that_move_mid_slide(self):
        """A Size change restyles the app while the thumb slides: it lands on
        the item where it is now, not where it was (it stuck between items)."""
        from PyQt6.QtTest import QTest
        self.seg.set_current('list')
        for b in (self.seg.item(k) for k in self.seg.keys()):
            b.setFixedSize(b.width() + 20, b.height())
        QTest.qWait(400)
        self.assertAlmostEqual(self.seg._thumb.center().x(),
                               self.seg.item('list').geometry().center().x(), delta=1)


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(nx is None, "PyQt6 not available")
class DashboardModel(unittest.TestCase):
    def items(self, *spec):
        return [{'id': i, 'size': s, 'hidden': False} for i, s in spec]

    def test_pack_flows_rows(self):
        items = self.items(('jump', 'full'), ('activity', 'small'), ('storage', 'small'),
                           ('health', 'small'), ('deals', 'wide'), ('proton', 'small'))
        got = nx.dash_pack(items, 3)
        self.assertEqual(got, [('jump', 0, 0, 3), ('activity', 1, 0, 1), ('storage', 1, 1, 1),
                               ('health', 1, 2, 1), ('deals', 2, 0, 2), ('proton', 2, 2, 1)])

    def test_pack_wraps_and_clamps(self):
        items = self.items(('activity', 'small'), ('deals', 'wide'))
        self.assertEqual(nx.dash_pack(items, 2), [('activity', 0, 0, 1), ('deals', 1, 0, 2)])
        self.assertEqual(nx.dash_pack(items, 1), [('activity', 0, 0, 1), ('deals', 1, 0, 1)])

    def test_pack_skips_hidden_and_empty(self):
        items = self.items(('activity', 'small'), ('storage', 'small'))
        items[0]['hidden'] = True
        self.assertEqual([k for k, *_ in nx.dash_pack(items, 3)], ['storage'])
        self.assertEqual(nx.dash_pack(items, 3, skip={'storage'}), [])

    def test_move_left_right(self):
        items = self.items(('activity', 'small'), ('storage', 'small'), ('health', 'small'))
        out = nx.dash_move(items, 'storage', 'left', 3)
        self.assertEqual([i['id'] for i in out], ['storage', 'activity', 'health'])
        out = nx.dash_move(items, 'health', 'right', 3)          # already last: no-op
        self.assertEqual([i['id'] for i in out], ['activity', 'storage', 'health'])

    def test_move_up_down_by_column(self):
        items = self.items(('activity', 'small'), ('storage', 'small'), ('health', 'small'),
                           ('proton', 'small'), ('news', 'small'))
        # 3 cols: row0 = activity storage health, row1 = proton news
        out = nx.dash_move(items, 'news', 'up', 3)               # news under storage
        self.assertEqual([i['id'] for i in out][:2], ['activity', 'news'])
        out = nx.dash_move(items, 'activity', 'down', 3)
        self.assertEqual([i['id'] for i in out].index('activity'),
                         [i['id'] for i in out].index('proton') + 1)

    def test_normalize(self):
        saved = [{'id': 'news', 'size': 'huge'}, {'id': 'bogus'}, {'id': 'news'},
                 {'id': 'jump', 'size': 'full', 'hidden': True}]
        out = nx.dash_normalize(saved)
        ids = [i['id'] for i in out]
        self.assertEqual(ids[:2], ['news', 'jump'])
        self.assertEqual(out[0]['size'], nx.DASH_DEFAULT_SIZE['news'])   # bad size -> default
        self.assertTrue(out[1]['hidden'])
        self.assertEqual(sorted(ids), sorted(k for k, *_ in nx.DASH_KINDS))
        self.assertEqual(nx.dash_normalize(None), nx.dash_default())

    def test_normalize_merges_into_system(self):
        saved = self.items(('jump', 'full'), ('activity', 'small'), ('storage', 'wide'),
                           ('health', 'small'), ('installed', 'full'), ('proton', 'small'),
                           ('unplayed', 'full'))
        saved[2]['hidden'] = True
        out = nx.dash_normalize(saved)
        ids = [i['id'] for i in out]
        self.assertEqual(ids[:4], ['jump', 'surprise', 'system', 'installed'])
        # system is where activity was (surprise, added later, sits after jump)
        for gone in ('activity', 'storage', 'health', 'proton', 'unplayed'):
            self.assertNotIn(gone, ids)
        self.assertEqual(out[2], {'id': 'system', 'size': 'wide', 'hidden': False})
        self.assertEqual(nx.dash_normalize(out), out)                # idempotent
        all_hidden = [dict(i, hidden=True) for i in saved]
        sysw = next(i for i in nx.dash_normalize(all_hidden) if i['id'] == 'system')
        self.assertTrue(sysw['hidden'])

    def test_normalize_places_new_widgets_by_anchor(self):
        saved = self.items(('deals', 'wide'), ('jump', 'full'))
        ids = [i['id'] for i in nx.dash_normalize(saved)]
        self.assertEqual(ids[:3], ['deals', 'upcoming', 'jump'])

    def test_full_size_for_playtime_and_achievements(self):
        for k in ('playtime', 'achievements'):
            self.assertEqual(nx.DASH_SIZES[k], ('small', 'wide', 'full'))
            self.assertEqual(nx.DASH_DEFAULT_SIZE[k], 'small')
        items = self.items(('playtime', 'full'), ('achievements', 'small'))
        self.assertEqual(nx.dash_pack(items, 3), [('playtime', 0, 0, 3),
                                                  ('achievements', 1, 0, 1)])

    def test_old_layout_round_trips(self):
        """A layout saved before full size existed loads unchanged."""
        old = nx.dash_normalize([dict(i, size='wide') if i['id'] in ('playtime', 'achievements')
                                 else i for i in nx.dash_default()])
        self.assertEqual(nx.dash_normalize(old), old)
        self.assertEqual(nx.dash_normalize(nx.dash_normalize(old)), old)
        full = [dict(i, size='full') if i['id'] == 'playtime' else i for i in old]
        self.assertEqual(nx.dash_normalize(full), full)

    def test_system_badge(self):
        self.assertEqual(nx.system_badge(2, 3, 1), ('2', 'accent'))   # jobs first
        self.assertEqual(nx.system_badge(0, 3, 1), ('3', 'danger'))
        self.assertEqual(nx.system_badge(0, 2, 0), ('2', 'warning'))
        self.assertEqual(nx.system_badge(0, 0, 0), ('', 'accent'))

    @linux_features
    def test_system_status(self):
        st = nx.system_status([], [], [], {})
        self.assertEqual([t['key'] for t in st['tiles']],
                         ['activity', 'storage', 'health', 'proton'])
        self.assertEqual([t['value'] for t in st['tiles']],
                         ['Idle', 'Measuring…', 'Checking…', 'Checking…'])
        self.assertIsNone(st['attention'])
        full = [{'path': '/mnt/games', 'used': 95, 'total': 100, 'free': 5}]
        fail = [{'status': 'fail', 'title': 'Broken', 'fix': 'Fix it'},
                {'status': 'ok', 'title': 'Fine'}]
        ge = {'default': 'proton-x', 'families': {'ge': {'update': True, 'latest': 'GE-2'}}}
        job = [{'title': 'Installing X', 'state': 'running', 'step': 'Copying',
                'progress': 40, 'active': True}]
        st = nx.system_status(job, full, fail, ge)
        self.assertEqual(st['tiles'][1]['role'], 'danger')
        self.assertEqual(st['tiles'][2]['value'], '1 issue')
        self.assertEqual(st['tiles'][3]['value'], 'Update')
        self.assertEqual((st['attention']['key'], st['attention']['sub']),
                         ('activity', '40% · Copying'))
        # Then: an almost-full disk, a failing check, a Proton update.
        self.assertEqual(nx.system_status([], full, fail, ge)['attention']['key'], 'storage')
        self.assertEqual(nx.system_status([], [], fail, ge)['attention']['key'], 'health')
        att = nx.system_status([], [], [], ge)['attention']
        self.assertEqual((att['key'], att['action']), ('proton', 'install-ge'))

    def test_cols(self):
        self.assertEqual([nx.dash_cols(w) for w in (500, 800, 1200, 2000)], [1, 2, 3, 3])

    def test_prefs_round_trip_keeps_unknown_keys(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text(json.dumps({'favorites': ['a'], 'future_key': 1}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                pr = nx.LibraryPrefs()
                pr.dashboard[0]['hidden'] = True
                pr.save()
                again = nx.LibraryPrefs()
            data = json.loads(path.read_text())
            self.assertEqual(data['future_key'], 1)
            self.assertEqual(data['dashboard']['version'], 1)
            self.assertTrue(again.dashboard[0]['hidden'])
            self.assertEqual(again.favorites, {'a'})


@unittest.skipIf(nx is None, "PyQt6 not available")
class DashboardFill(unittest.TestCase):
    """Play time and Achievements at each size, from stub CLI answers that
    come back synchronously (as a cached cli_json answer does, inside fill)."""
    HIST = {'since': '2026-10-01', 'weeks': [{'start': '2026-10-26', 'minutes': 90}],
            'periods': {'today': '2026-10-28',
                        'week': {'minutes': 90, 'days': [{'date': f'2026-10-{d}', 'minutes': 45}
                                                         for d in range(26, 33) if d < 32],
                                 'games': [{'slug': 'a', 'name': 'Alpha', 'minutes': 90}]},
                        'month': {'minutes': 200,
                                  'days': [{'date': f'2026-10-{d:02}', 'minutes': 10}
                                           for d in range(1, 32)],
                                  'games': [{'slug': 'a', 'name': 'Alpha', 'minutes': 150},
                                            {'slug': 'b', 'name': 'Beta', 'minutes': 50}]}}}
    ANSWERS = {'--history': HIST,
               'recent': {'recent': [{'appid': 1, 'title': 'First steps', 'time': 1_700_000_000}]},
               'rarest': {'rarest': [{'appid': 1, 'title': 'Needle', 'time': 1_700_000_000,
                                      'percent': 0.7}]}}

    def setUp(self):
        from types import SimpleNamespace
        from unittest import mock
        from nexus.gui import dashboard as D
        self.D = D
        self.asked = []
        self.fail_rarest = False

        def cli_json(args, cb, timeout=0, ttl=0):
            key = args[1] if args[0] == '--achievements' else args[0]
            self.asked.append(key)
            if key == 'rarest' and self.fail_rarest:
                cb(None, 'offline')
            else:
                cb(self.ANSWERS[key], '')
        p = mock.patch.object(D, 'cli_json', cli_json)
        p.start()
        self.addCleanup(p.stop)
        self.hub = SimpleNamespace(_games=[
            {'slug': 'a', 'display_name': 'Alpha', 'playtime_minutes': 600, 'source': 'steam',
             'appid': 1, 'achievements': [9, 10]},
            {'slug': 'b', 'display_name': 'Beta', 'playtime_minutes': 60, 'source': 'steam',
             'appid': 2, 'achievements': [10, 10]}])

    def texts(self, w):
        return [lb.text() for lb in w.findChildren(QLabel) if lb.text()]

    def filled(self, cls, size):
        w = cls()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        w.fill(self.hub)
        return w

    def test_playtime_fill_sizes(self):
        D = self.D
        small = self.filled(D.PlaytimeWidget, 'small')
        self.assertEqual(len(small.findChildren(D.MinuteBars)), 1)
        self.assertIn('Alpha', self.texts(small))
        wide = self.filled(D.PlaytimeWidget, 'wide')
        self.assertIn('This month', self.texts(wide))
        bars = wide.findChildren(D.MinuteBars)[0]
        self.assertEqual(len(bars.bars), 31)
        self.assertEqual(bars.highlight, set(range(25, 31)))    # this week: 26th–31st
        full = self.filled(D.PlaytimeWidget, 'full')
        self.assertTrue({'This month', 'All time', 'Last 12 weeks'} <= set(self.texts(full)))
        self.assertEqual(len(full.findChildren(D.MinuteBars)), 2)
        # No change: the rows are kept (pad focus), not rebuilt.
        before = full.findChildren(QLabel)[0]
        full.fill(self.hub)
        self.assertIs(full.findChildren(QLabel)[0], before)

    def test_playtime_without_sessions(self):
        self.ANSWERS = dict(self.ANSWERS, **{'--history': {}})
        w = self.filled(self.D.PlaytimeWidget, 'wide')
        self.assertFalse(w.findChildren(self.D.MinuteBars))
        self.assertTrue(any('recorded from now on' in t for t in self.texts(w)))

    def test_achievements_fill_sizes(self):
        D = self.D
        small = self.filled(D.AchievementsWidget, 'small')
        self.assertNotIn('rarest', self.asked)                  # small never asks
        t = self.texts(small)
        self.assertEqual(t.count('First steps'), 1)             # drawn once despite the sync answer
        self.assertIn('Closest to 100%', t)
        wide = self.filled(D.AchievementsWidget, 'wide')
        self.assertIn('Needle', self.texts(wide))
        self.assertIn('0.7% · Alpha', self.texts(wide))
        full = self.filled(D.AchievementsWidget, 'full')
        self.assertTrue({'Recent', 'Closest to 100%', 'Rarest'} <= set(self.texts(full)))
        self.fail_rarest = True
        off = self.filled(D.AchievementsWidget, 'wide')
        self.assertNotIn('Rarest', self.texts(off))             # offline: section left out
        self.assertIn('First steps', self.texts(off))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Appearance(unittest.TestCase):
    def tearDown(self):
        nx.apply_appearance(nx.APPEARANCE_DEFAULT)

    def test_normalize_rejects_junk(self):
        ap = nx.normalize_appearance({'theme': 'nope', 'accent': 'red', 'partner': '#12345g',
                                      'glow': 'max', 'corners': 'round', 'art_tint': 'yes'})
        self.assertEqual(ap['theme'], 'rose-pine')
        self.assertEqual(ap['accent'], 'iris')
        self.assertEqual(ap['partner'], 'love')
        self.assertEqual(ap['glow'], 'subtle')
        self.assertEqual(ap['corners'], 'round')
        self.assertIs(ap['art_tint'], True)
        self.assertEqual(nx.normalize_appearance(None), nx.APPEARANCE_DEFAULT)

    def test_apply_rebuilds_palette(self):
        nx.apply_appearance({'theme': 'midnight', 'accent': '#ff7a59', 'partner': 'none',
                             'corners': 'sharp', 'glow': 'off'})
        self.assertEqual(nx.P['bg'], nx.THEMES['midnight'][1]['base'])
        self.assertEqual(nx.P['accent'], '#ff7a59')
        self.assertEqual(nx.GRAD['dusk'], ('#ff7a59', '#ff7a59'))
        self.assertLess(nx.R['card'], nx.R_BASE['card'])
        self.assertEqual(nx.LOOK['glow'], 0.0)
        self.assertIn('rgba(255,122,89,0.22)', nx.P['accent_22'])
        css = nx.build_stylesheet()
        self.assertIn('#ff7a59', css)

    def test_dark_accent_gets_light_text(self):
        nx.apply_appearance({'accent': 'pine'})
        self.assertEqual(nx.P['on_accent'], '#ffffff')
        nx.apply_appearance({'accent': 'gold'})
        self.assertEqual(nx.P['on_accent'], nx.P['bg'])

    def test_wordmark_is_the_svg_lettering_in_the_accent(self):
        w = nx.Wordmark()
        self.assertIsNotNone(w._svg)                    # assets/wordmark.svg, not text
        self.assertGreater(w.width(), 3 * w.height())   # the lettering's 59:12 shape

        def inked():
            pm = QPixmap(w.size())
            pm.fill(Qt.GlobalColor.transparent)
            w.render(pm)
            img = pm.toImage()
            return [img.pixelColor(x, y) for x in range(img.width())
                    for y in range(img.height()) if img.pixelColor(x, y).alpha() == 255]
        nx.apply_appearance({'accent': '#ff7a59', 'partner': 'none'})
        px = inked()
        self.assertTrue(px)
        self.assertTrue(all(c.name() == '#ff7a59' for c in px))
        nx.apply_appearance({'accent': '#9ccfd8', 'partner': 'none'})
        self.assertTrue(all(c.name() == '#9ccfd8' for c in inked()))   # repainted, not cached
        self.assertIsNone(nx.Wordmark("Other")._svg)    # other text keeps the font

    def test_every_theme_has_readable_text(self):
        def lum(h):
            c = QColor(h)
            ch = [x / 255 for x in (c.red(), c.green(), c.blue())]
            ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
            return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

        def contrast(a, b):
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)
        for key in nx.THEMES:
            for accent in ('iris', 'gold', 'foam'):
                nx.apply_appearance({'theme': key, 'accent': accent})
                for bg in ('bg', 'surface', 'surface_solid'):
                    self.assertGreaterEqual(contrast(nx.P['text'], nx.P[bg]), 4.5, (key, bg))
                self.assertGreaterEqual(contrast(nx.P['muted'], nx.P['surface']), 3.0, key)
                # The accent as a button fill with its label on it.
                self.assertGreaterEqual(contrast(nx.P['on_accent'], nx.P['accent']), 3.0,
                                        (key, accent))
        nx.apply_appearance({'theme': 'dawn'})
        self.assertTrue(nx.is_light())
        self.assertNotEqual(nx.P['danger'], nx.DARK_STATUS['love'])     # its own status hues
        nx.apply_appearance({'theme': 'rose-pine'})
        self.assertEqual(nx.P['danger'], nx.DARK_STATUS['love'])        # and back

    def test_size_scales_type_and_spacing(self):
        nx.apply_appearance({'scale': 1.25})
        css = nx.build_stylesheet()
        self.assertIn(f"font-size: {round(nx.FS['sm'] * 1.25)}px", css)
        self.assertEqual(nx.scaled(20), 25)
        nx.apply_appearance({'scale': 3})                     # not one of SCALES: default
        self.assertEqual(nx.ui_scale(), 1.0)

    def test_stylesheet_has_no_literal_font_sizes(self):
        import inspect
        src = inspect.getsource(nx.build_stylesheet)
        self.assertEqual(re.findall(r'font-size:\s*\d+px', src), [])

    def test_default_round_trip_matches_original(self):
        nx.apply_appearance(nx.APPEARANCE_DEFAULT)
        self.assertEqual(nx.P['bg'], '#191724')
        self.assertEqual(nx.P['accent'], '#c4a7e7')
        self.assertEqual(nx.R, {**nx.R_BASE})


@unittest.skipIf(nx is None, "PyQt6 not available")
class AppIcon(unittest.TestCase):
    def test_rendered_from_the_svg_with_round_corners(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        from PyQt6.QtGui import QImage
        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / 'nexus-icon.png'
            with patch_gui('ICON_PATH', png), \
                    patch_gui('DATA_DIR', Path(d)):
                self.assertEqual(nx.ensure_icon(), str(png))
                img = QImage(str(png))
                self.assertEqual((img.width(), img.height()), (256, 256))
                self.assertEqual(img.pixelColor(0, 0).alpha(), 0)          # rounded corner
                self.assertEqual(img.pixelColor(128, 128).name(), '#56949f')
                # A newer SVG is picked up; an up-to-date PNG is left alone.
                os.utime(png, (1, 1))
                nx.ensure_icon()
                self.assertGreater(png.stat().st_mtime, 1)
                before = png.stat().st_mtime_ns
                nx.ensure_icon()
                self.assertEqual(png.stat().st_mtime_ns, before)
