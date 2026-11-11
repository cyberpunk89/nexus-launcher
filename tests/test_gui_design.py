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
class Radii(unittest.TestCase):
    """Qt draws a box square once two radii on a side add up to more than
    the side, so no rounded widget may be narrower or shorter than 2r."""

    def tearDown(self):
        nx.apply_appearance(nx.APPEARANCE_DEFAULT)

    def test_hint_glyphs_are_circles_or_capsules_in_theatre(self):
        from nexus.gui.input import HintBar
        from nexus.gui.theatre import THEATRE_SCALE
        for scale in (1.0, 1.5):
            nx.apply_appearance({'scale': scale})
            bar = HintBar()
            self.addCleanup(bar.deleteLater)
            bar.setStyleSheet(nx.build_stylesheet(THEATRE_SCALE))
            bar.resize(2400, 80)
            bar.show()
            bar.show_hints([('a', 'Play'), ('y', 'Search'), ('lb rb', 'Tabs'), ('Menu', 'Quick')])
            QApplication.processEvents()
            glyphs = bar.findChildren(QLabel, "HintGlyph")
            self.assertEqual(len(glyphs), 4)
            for g in glyphs:
                with self.subTest(scale=scale, glyph=g.text()):
                    self.assertGreaterEqual(g.width(), g.height())        # square-cornered otherwise
                    self.assertGreaterEqual(g.width(), g.sizeHint().width())   # text not clipped
            one = glyphs[0]
            self.assertEqual(one.width(), one.height())                    # one letter: a circle

    def test_for_you_pill_is_concentric(self):
        """The action and ✕ are capsules inset evenly from the pill's edge, and
        the round badge sits as far from the left as from the top."""
        from nexus.gui.foryou import ForYouPill
        m = {'title': 'Frostpunk 2', 'why': '', 'action': {'label': 'Launch'},
             'badge': ('check', 'accent'), 'snooze_key': 'idle:frostpunk-2'}
        for scale in (1.0, 1.25):
            nx.apply_appearance({'scale': scale, 'corners': 'round'})
            QApplication.instance().setStyleSheet(nx.build_stylesheet())
            for moments in ([m, m], [m]):
                p = ForYouPill()
                self.addCleanup(p.deleteLater)
                p.set_height(int(44 * scale))
                p.set_moments(moments)
                p.show()
                QApplication.processEvents()
                with self.subTest(scale=scale, many=len(moments) > 1):
                    last = p.act if len(moments) > 1 else p.x
                    top = last.y()
                    self.assertEqual(p.width() - last.geometry().right() - 1, top)
                    self.assertEqual(p.height() - last.geometry().bottom() - 1, top)
                    b = p.badge.mapTo(p, p.badge.rect().topLeft())
                    self.assertEqual(b.x(), b.y())
                    self.assertTrue(p.badge.round)
                    self.assertIn(f"QPushButton#MomentAct {{ border-radius: {nx.pill(p.act.height())}px",
                                  p.styleSheet())

    def test_inline_radii_fit_their_widgets(self):
        """Every visible widget that sets its own border-radius, on Home,
        Library and Settings, under each Corners."""
        from unittest import mock
        from tests.patching import set_gui
        saved = (nx.cli_json, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        set_gui('CliQuery', lambda *a, **k: None)
        self.addCleanup(set_gui, 'cli_json', saved[0])
        self.addCleanup(set_gui, 'CliQuery', saved[1])
        with mock.patch.object(nx.LibraryPrefs, 'save', lambda self: None):
            win = nx.MainWindow()
            self.addCleanup(win.deleteLater)
            win.resize(1400, 850)
            win.show()
            win._on_library({'games': [{'slug': s, 'display_name': s.title(), 'source': 'nexus',
                                        'launcher': f'/{s}'} for s in ('alpha', 'beta')],
                             'disks': []}, '')
            base = dict(win.prefs.appearance)
            for corners in nx.CORNERS:
                win.set_appearance({**base, 'corners': corners}, save=False)
                for page in ('home', 'library', 'settings'):
                    win.go(page)
                    QApplication.processEvents()
                    app_wide = (win.restyler.sheet('desk'), win.restyler.sheet('theatre'))
                    for w in QApplication.allWidgets():
                        if w.styleSheet() in app_wide:          # the look's own, on a root (w-111)
                            continue
                        m = re.search(r'border-radius:\s*(\d+)px', w.styleSheet() or '')
                        if not m or not w.isVisible() or min(w.width(), w.height()) <= 2:
                            continue
                        with self.subTest(corners=corners, page=page, widget=w.objectName()
                                          or type(w).__name__):
                            self.assertLessEqual(2 * int(m.group(1)), min(w.width(), w.height()))


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
        items = self.items(('stage', 'cinematic'), ('activity', 'small'), ('storage', 'small'),
                           ('health', 'small'), ('deals', 'wide'), ('proton', 'small'))
        got = nx.dash_pack(items, 3)
        self.assertEqual(got, [('stage', 0, 0, 3), ('activity', 1, 0, 1), ('storage', 1, 1, 1),
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
                 {'id': 'deals', 'size': 'wide', 'hidden': True}]
        out = nx.dash_normalize(saved)
        ids = [i['id'] for i in out]
        self.assertEqual(ids[:2], ['news', 'deals'])
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
        self.assertEqual(ids[:7], ['stage', 'save', 'foryou', 'friends', 'pick', 'system',
                                   'installed'])
        # system is where activity was (Your save, For you now, friends and Pick for
        # me, added later, sit after the stage, which took jump's place)
        for gone in ('activity', 'storage', 'health', 'proton', 'unplayed'):
            self.assertNotIn(gone, ids)
        self.assertEqual(out[5], {'id': 'system', 'size': 'wide', 'hidden': False})
        self.assertEqual(nx.dash_normalize(out), out)                # idempotent
        all_hidden = [dict(i, hidden=True) for i in saved]
        sysw = next(i for i in nx.dash_normalize(all_hidden) if i['id'] == 'system')
        self.assertTrue(sysw['hidden'])

    def test_normalize_places_new_widgets_by_anchor(self):
        saved = self.items(('deals', 'wide'), ('jump', 'full'))
        ids = [i['id'] for i in nx.dash_normalize(saved)]
        self.assertEqual(ids[:8], ['deals', 'upcoming', 'week', 'stage', 'save', 'foryou',
                                   'friends', 'pick'])
        # A layout from before w-82 gains Friends playing now right after the Stage
        old = [i for i in nx.dash_kind_defaults() if i['id'] != 'friends']
        ids = [i['id'] for i in nx.dash_normalize(old)]
        self.assertEqual(ids[:5], ['stage', 'friends', 'save', 'foryou', 'pick'])
        self.assertEqual(nx.DASH_SIZES['friends'], ('small', 'wide', 'full'))   # w-91
        self.assertEqual(nx.DASH_DEFAULT_SIZE['friends'], 'full')

    def test_normalize_turns_jump_into_the_stage(self):
        """w-85: Jump back in becomes the Stage in place, at Cinematic and
        shown even when Jump back in was hidden (the hero always was);
        Friends and Surprise, added later, go after it."""
        saved = self.items(('system', 'wide'), ('jump', 'full'), ('installed', 'full'))
        saved[1]['hidden'] = True
        out = nx.dash_normalize(saved)
        ids = [i['id'] for i in out]
        self.assertEqual(ids[:7], ['system', 'stage', 'save', 'foryou', 'friends', 'pick',
                                   'installed'])
        self.assertNotIn('jump', ids)
        stage = out[1]
        self.assertEqual(stage, {'id': 'stage', 'size': 'cinematic', 'hidden': False,
                                 'style': 'row', 'bg': 'art'})
        self.assertEqual(nx.dash_normalize(out), out)                # idempotent
        self.assertEqual(nx.DASH_KINDS[0][:3], ('stage', ('cinematic', 'wide', 'banner'),
                                                'cinematic'))
        self.assertEqual(nx.dash_default()[0]['id'], 'stage')
        both = self.items(('jump', 'full'), ('stage', 'banner'))  # one stage, the first
        self.assertEqual([i['id'] for i in nx.dash_normalize(both)].count('stage'), 1)
        self.assertNotIn('jump', nx.DASH_WIDGETS)

    def test_normalize_checks_stage_options(self):
        out = nx.dash_normalize([{'id': 'stage', 'size': 'wide', 'style': 'fancy',
                                  'bg': 42, 'hidden': True}])
        stage = next(i for i in out if i['id'] == 'stage')
        self.assertEqual((stage['size'], stage['style'], stage['bg'], stage['hidden']),
                         ('wide', 'row', 'art', True))               # bad options fall back
        stage = next(i for i in nx.dash_normalize([{'id': 'stage', 'size': 'huge'}])
                     if i['id'] == 'stage')
        self.assertEqual((stage['size'], stage['style'], stage['bg']), ('cinematic', 'row', 'art'))
        self.assertEqual(nx.DASH_OPTIONS['stage'], {'style': ('row', 'clean'),
                                                    'bg': ('art', 'pinned', 'gradient', 'surface')})
        for style in ('row', 'clean'):
            for bg in ('art', 'gradient', 'surface'):
                st = nx.dash_normalize([{'id': 'stage', 'size': 'cinematic', 'style': style,
                                         'bg': bg}])[0]
                self.assertEqual((st['style'], st['bg']), (style, bg))
                self.assertNotIn('pin', st)
        news = next(i for i in out if i['id'] == 'news')
        self.assertNotIn('style', news)                              # only the stage has them

    def test_pack_spans_stage_sizes(self):
        for size, span in (('cinematic', 3), ('banner', 3), ('wide', 2)):
            items = self.items(('stage', size), ('system', 'small'))
            self.assertEqual(nx.dash_pack(items, 3)[0], ('stage', 0, 0, span))
        self.assertEqual(nx.dash_pack(self.items(('stage', 'wide'), ('system', 'small')), 3),
                         [('stage', 0, 0, 2), ('system', 0, 2, 1)])   # Wide leaves a column
        self.assertEqual(nx.dash_pack(self.items(('stage', 'cinematic')), 1),
                         [('stage', 0, 0, 1)])

    def test_stage_height(self):
        h = nx.stage_height
        self.assertEqual(h('cinematic', 850), 560)                   # vh - 290
        self.assertEqual(h('cinematic', 1400), 610)                  # capped
        self.assertEqual(h('cinematic', 600), 480)                   # at least
        self.assertEqual(h('wide', 850), 420)
        self.assertEqual(h('wide', 700), 340)
        self.assertEqual(h('wide', 500), 320)
        self.assertEqual(h('banner', 850), 250)
        self.assertEqual(h('banner', 400), 250)

    def test_stage_height_styles(self):
        """w-90: Clean is shorter (the Continue shelf under it shows whole at
        850 high); off the art (gradient, Plain) shorter still; the Row on
        art style is as tall on every background, so cycling never jumps."""
        h = nx.stage_height
        for bg in ('art', 'pinned', 'gradient', 'surface'):
            self.assertEqual(h('cinematic', 850, 'row', bg), 560)
            self.assertEqual(h('wide', 850, 'clean', bg), 420)        # only Cinematic changes
            self.assertEqual(h('banner', 850, 'clean', bg), 250)
        for bg in ('art', 'pinned'):
            self.assertEqual(h('cinematic', 850, 'clean', bg), 530)   # vh - 320
            self.assertEqual(h('cinematic', 1400, 'clean', bg), 540)
            self.assertEqual(h('cinematic', 600, 'clean', bg), 400)
        for bg in ('gradient', 'surface'):
            self.assertEqual(h('cinematic', 850, 'clean', bg), 320)
            self.assertEqual(h('cinematic', 1400, 'clean', bg), 320)

    def test_normalize_keeps_a_valid_pin_only_on_pinned_art(self):
        """w-90: Pinned art keeps its game's slug; any other background drops
        it; a bad pin makes Pinned art plain Game art."""
        def stage(**kw):
            return nx.dash_normalize([{'id': 'stage', 'size': 'cinematic', **kw}])[0]
        st = stage(bg='pinned', pin='steam-323190')
        self.assertEqual((st['bg'], st['pin']), ('pinned', 'steam-323190'))
        self.assertEqual(nx.dash_normalize([st])[0], st)               # idempotent
        for bg in ('art', 'gradient', 'surface'):
            self.assertNotIn('pin', stage(bg=bg, pin='steam-323190'))
        for bad in (None, 42, '', ' x', 'x\n', 'a' * 81, ['x'], {'slug': 'x'}):
            st = stage(bg='pinned', pin=bad)
            self.assertEqual(st['bg'], 'art', bad)
            self.assertNotIn('pin', st)
        self.assertEqual(stage(bg='pinned', pin='a' * 80)['pin'], 'a' * 80)
        news = next(i for i in nx.dash_normalize([{'id': 'news', 'pin': 'x'}]) if i['id'] == 'news')
        self.assertNotIn('pin', news)
        self.assertNotIn('pin', nx.DASH_OPTIONS['stage'])            # a key, not an option

    def test_stage_and_week_labels(self):
        self.assertEqual(nx.DASH_STYLE_LABELS['stage'], {'row': "Row on art", 'clean': "Clean"})
        self.assertEqual(nx.DASH_STYLE_LABELS['week'], {'bars': "Bars", 'number': "Number only"})
        self.assertEqual(nx.DASH_BG_LABELS['stage'], {'art': "Game art", 'pinned': "Pinned art",
                                                      'gradient': "Accent gradient",
                                                      'surface': "Plain"})
        for kind, labels in list(nx.DASH_STYLE_LABELS.items()) + list(nx.DASH_BG_LABELS.items()):
            key = 'style' if labels in nx.DASH_STYLE_LABELS.values() else 'bg'
            self.assertEqual(tuple(labels), nx.DASH_OPTIONS[kind][key], kind)

    def test_density_values(self):
        """Comfortable is Home as it always was; Compact tightens it (w-90)."""
        sp = nx.SP
        self.assertEqual(set(nx.DASH_DENSITY), {'comfortable', 'compact'})
        c, k = nx.DASH_DENSITY['comfortable'], nx.DASH_DENSITY['compact']
        self.assertEqual(set(c), set(k))
        self.assertEqual((c['grid_h'], c['grid_v'], c['card_pad']), (sp['4'], sp['5'], sp['4']))
        self.assertEqual((k['grid_h'], k['grid_v'], k['card_pad']), (sp['3'], sp['3'], sp['3']))
        self.assertEqual((c['art_side'], c['art_tb'], c['row_gap']), (48, sp['5'], sp['3']))
        self.assertEqual((k['art_side'], k['art_tb'], k['row_gap']), (sp['8'], sp['4'], sp['2']))
        self.assertEqual((c['home_side'], c['home_top']), (sp['5'], sp['4']))
        self.assertEqual((k['home_side'], k['home_top']), (sp['4'], sp['3']))
        self.assertTrue(all(k[x] <= c[x] for x in c))

    def test_density_prefs_round_trip(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text(json.dumps({'dashboard': {'version': 2, 'density': 'tiny',
                                                      'layouts': ['bad'], 'future': 'kept'}}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                pr = nx.LibraryPrefs()
                self.assertEqual(pr.density, 'comfortable')          # a bad one falls back
                pr.density = 'compact'
                pr.save()
                again = nx.LibraryPrefs()
                self.assertEqual(again.density, 'compact')
            data = json.loads(path.read_text())
            self.assertEqual(data['dashboard']['density'], 'compact')
            self.assertEqual(data['dashboard']['future'], 'kept')   # the merge keeps the rest
            self.assertEqual(data['dashboard']['layouts'], [])      # a bad layout is dropped
            for raw in ({}, {'dashboard': {'density': ['compact']}},
                        {'dashboard': {'density': {'a': 1}}}):
                path.write_text(json.dumps(raw))
                with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                    self.assertEqual(nx.LibraryPrefs().density, 'comfortable')

    def test_pinned_stage_round_trips_through_prefs(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                pr = nx.LibraryPrefs()
                pr.dashboard[0].update(style='clean', bg='pinned', pin='alpha')
                pr.save()
                st = nx.LibraryPrefs().dashboard[0]
            self.assertEqual((st['id'], st['style'], st['bg'], st['pin']),
                             ('stage', 'clean', 'pinned', 'alpha'))

    def test_glass_raised_reads_in_every_theme(self):
        """w-90: the pill's and list's buttons sit on glass_raised over the
        list's tile_scrim; white text on it reads (4.5:1) in every theme,
        even over white art. The Dawn render finding of w-89."""
        import re
        from nexus.gui import theme as th

        def over(top, under):
            m = re.fullmatch(r'rgba\((\d+),(\d+),(\d+),([\d.]+)\)', top.replace(' ', ''))
            rgb, a = [int(m[i]) for i in (1, 2, 3)], float(m[4])
            return [round(rgb[i] * a + under[i] * (1 - a)) for i in range(3)]

        saved = dict(th.LOOK)
        try:
            for key in th.THEMES:
                th.apply_appearance({'theme': key})
                for art in ((255, 255, 255), (0, 0, 0)):
                    c = over(th.P['glass_raised'], over(th.P['tile_scrim'], art))
                    ratio = th.contrast(th.P['on_art'], '#%02x%02x%02x' % tuple(c))
                    self.assertGreaterEqual(ratio, 4.5, (key, art, ratio))
                self.assertIn(th.P['glass_raised'], th.build_stylesheet())
        finally:
            th.apply_appearance({})
            th.LOOK.update(saved)

    def test_week_sizes(self):
        """w-87: Your week is small or wide; Play time and Achievements left Home."""
        self.assertEqual(nx.DASH_SIZES['week'], ('small', 'wide'))
        self.assertEqual(nx.DASH_DEFAULT_SIZE['week'], 'small')
        for gone in ('playtime', 'achievements'):
            self.assertNotIn(gone, nx.DASH_SIZES)
            self.assertNotIn(gone, nx.DASH_WIDGETS)
        items = self.items(('deals', 'wide'), ('week', 'small'), ('news', 'small'))
        self.assertEqual(nx.dash_pack(items, 3), [('deals', 0, 0, 2), ('week', 0, 2, 1),
                                                  ('news', 1, 0, 1)])
        self.assertEqual(nx.DASH_OPTIONS['week'], {'style': ('bars', 'number')})

    def test_week_shown_by_default(self):
        d = nx.dash_kind_defaults()
        ids = [i['id'] for i in d]
        week = d[ids.index('week')]
        self.assertEqual((week['size'], week['hidden'], week['style']), ('small', False, 'bars'))
        self.assertEqual(ids.index('week'), ids.index('upcoming') + 1)
        # A layout saved without it (or Play time) gets it after Upcoming.
        saved = self.items(('stage', 'cinematic'), ('deals', 'wide'), ('upcoming', 'small'),
                           ('news', 'small'))
        ids = [i['id'] for i in nx.dash_normalize(saved)
               if i['id'] not in ('foryou', 'friends', 'pick', 'save')]
        self.assertEqual(ids[:5], ['stage', 'deals', 'upcoming', 'week', 'news'])
        self.assertEqual(nx.DASH_PLACE_AFTER['week'], 'upcoming')

    def test_playtime_becomes_week_in_place(self):
        saved = self.items(('stage', 'cinematic'), ('system', 'wide'), ('playtime', 'full'),
                           ('deals', 'wide'), ('achievements', 'wide'), ('news', 'small'))
        out = nx.dash_normalize(saved)
        ids = [i['id'] for i in out]
        self.assertEqual([i for i in ids if i not in ('foryou', 'friends', 'pick', 'save')][:4],
                         ['stage', 'system', 'week', 'deals'])
        self.assertNotIn('achievements', ids)
        self.assertNotIn('playtime', ids)
        week = out[ids.index('week')]
        self.assertEqual((week['size'], week['hidden'], week['style']), ('small', False, 'bars'))
        self.assertEqual(nx.dash_normalize(out), out)                # idempotent
        saved[2]['hidden'] = True                                    # hidden stays hidden
        out = nx.dash_normalize(saved)
        self.assertTrue(next(i for i in out if i['id'] == 'week')['hidden'])
        self.assertNotIn('week', nx.DASH_MERGED_SHOWN)
        self.assertEqual(nx.DASH_MERGED['playtime'], 'week')

    def test_old_layout_round_trips(self):
        """A saved layout with Your week wide and as a number loads unchanged."""
        old = nx.dash_normalize([dict(i, size='wide', style='number') if i['id'] == 'week'
                                 else i for i in nx.dash_default()])
        week = next(i for i in old if i['id'] == 'week')
        self.assertEqual((week['size'], week['style']), ('wide', 'number'))
        self.assertEqual(nx.dash_normalize(old), old)
        self.assertEqual(nx.dash_normalize(nx.dash_normalize(old)), old)
        bad = [dict(i, size='full', style='pie') if i['id'] == 'week' else i for i in old]
        week = next(i for i in nx.dash_normalize(bad) if i['id'] == 'week')
        self.assertEqual((week['size'], week['style']), ('small', 'bars'))

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

    @linux_features
    def test_native_crash_attention(self):
        """A native crash from last time beats everything else on the Home
        System widget; any other kind of crash file makes no row (w-77)."""
        job = [{'title': 'Installing X', 'state': 'running', 'step': 'Copying',
                'progress': 40, 'active': True}]
        native = {'id': '20261105T120000Z-abcd', 'kind': 'native-crash', 'count': 2}
        att = nx.system_status(job, [], [], {}, crash=native)['attention']
        self.assertEqual((att['key'], att['role'], att['crash']),
                         ('report', 'danger', native['id']))
        self.assertIn("(2 times)", att['text'])
        other = dict(native, kind='gui-exception')
        self.assertEqual(nx.system_status(job, [], [], {}, crash=other)['attention']['key'],
                         'activity')
        self.assertIsNone(nx.system_status([], [], [], {}, crash=None)['attention'])

    @linux_features
    def test_a_freeze_says_it_froze(self):
        """w-101: a freeze record (native-crash with freeze: true) reads as
        one on Home; an old record without the key is still a crash."""
        froze = {'id': '20261105T120000Z-abcd', 'kind': 'native-crash', 'freeze': True}
        att = nx.system_status([], [], [], {}, crash=froze)['attention']
        self.assertTrue(att['text'].startswith("Nexus stopped answering"), att)
        self.assertEqual(att['crash'], froze['id'])
        old = {'id': '20261105T120000Z-abcd', 'kind': 'native-crash'}
        self.assertTrue(nx.system_status([], [], [], {}, crash=old)['attention']['text']
                        .startswith("Nexus closed unexpectedly"))
        from nexus.gui.report_page import crash_title
        self.assertEqual(crash_title(dict(froze, type='Nexus stopped answering')),
                         "Nexus stopped answering")
        self.assertEqual(crash_title(dict(old, type='Segmentation fault')),
                         "Nexus crashed: Segmentation fault")

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
            self.assertEqual(data['dashboard']['version'], nx.DASH_VERSION)
            self.assertNotIn('dashboard_v1', data)         # nothing saved before: nothing to keep
            self.assertTrue(again.dashboard[0]['hidden'])
            self.assertEqual(again.favorites, {'a'})

    V1 = [('jump', 'full'), ('friends', 'full'), ('surprise', 'wide'), ('playtime', 'full'),
          ('system', 'wide'), ('achievements', 'wide'), ('installed', 'full'),
          ('smart', 'full'), ('upcoming', 'full'), ('deals', 'full'), ('news', 'full'),
          ('favourites', 'full'), ('collections', 'full'), ('updates', 'full'),
          ('owned', 'full')]

    def test_v1_layout_opens_with_the_stage_first(self):
        """w-86: a saved version 1 layout shaped like a real one keeps every
        widget's order, size and hidden flag; only the Stage moves to the top."""
        saved = self.items(*self.V1)
        saved[-1]['hidden'] = True
        saved[7]['shelf'] = 'unfinished'
        out = nx.dash_normalize(saved, version=1)
        ids = [i['id'] for i in out]
        self.assertEqual(ids[0], 'stage')
        rest = [i for i in ids if i != 'stage']
        want = [i for i in nx.dash_normalize(saved) if i['id'] != 'stage']
        self.assertEqual(rest, [i['id'] for i in want])
        self.assertEqual(rest.index('system') > rest.index('friends'), True)
        owned = next(i for i in out if i['id'] == 'owned')
        self.assertTrue(owned['hidden'])
        smart = next(i for i in out if i['id'] == 'smart')
        self.assertEqual(smart['shelf'], 'unfinished')
        # w-87: Play time (full) is Your week (small) in its place; Achievements left.
        self.assertEqual(rest[rest.index('pick') + 1], 'week')       # w-91: in place
        self.assertEqual(next(i for i in out if i['id'] == 'week')['size'], 'small')
        self.assertNotIn('achievements', ids)
        self.assertEqual(nx.dash_normalize(out), out)                # idempotent

    def test_v1_layout_with_the_stage_mid_page_or_hidden(self):
        saved = self.items(('system', 'wide'), ('jump', 'full'), ('installed', 'full'))
        out = nx.dash_normalize(saved, version=1)
        self.assertEqual([i['id'] for i in out][:2], ['stage', 'system'])
        saved[1]['hidden'] = True
        out = nx.dash_normalize(saved, version=1)
        self.assertEqual(out[0]['id'], 'stage')
        self.assertFalse(out[0]['hidden'])                           # shown, as w-85

    def test_a_current_layout_is_not_reordered(self):
        saved = self.items(('system', 'wide'), ('stage', 'wide'), ('installed', 'full'))
        out = nx.dash_normalize(saved)               # the default version is current
        ids = [i['id'] for i in out]
        self.assertEqual(ids[:2], ['system', 'stage'])     # not moved to the top
        self.assertLess(ids.index('stage'), ids.index('installed'))
        self.assertEqual(nx.DASH_VERSION, 2)

    def test_merged_widgets_never_shadow_a_live_kind(self):
        """A rename registered before its new kind exists (or while the old one
        still does) would silently drop or merge a widget."""
        for old, new in nx.DASH_MERGED.items():
            self.assertNotIn(old, nx.DASH_SIZES, old)
            self.assertIn(new, nx.DASH_SIZES, new)

    # ---- Pick for me, Friends small, Your save (w-91) ----
    def test_surprise_becomes_pick_in_place(self):
        saved = self.items(('stage', 'cinematic'), ('system', 'wide'), ('surprise', 'full'),
                           ('news', 'small'))
        saved[2]['hidden'] = True
        out = nx.dash_normalize(saved)
        ids = [i['id'] for i in out]
        rest = [i for i in ids if i not in ('save', 'foryou', 'friends', 'week')]
        self.assertEqual(rest[:4], ['stage', 'system', 'pick', 'news'])
        self.assertNotIn('surprise', ids)
        pick = out[ids.index('pick')]
        self.assertEqual(pick, {'id': 'pick', 'size': 'small', 'hidden': True, 'vibe': 'any'})
        self.assertEqual(nx.dash_normalize(out), out)                # idempotent
        saved[2]['hidden'] = False
        self.assertFalse(next(i for i in nx.dash_normalize(saved) if i['id'] == 'pick')['hidden'])
        self.assertEqual(nx.DASH_MERGED['surprise'], 'pick')
        self.assertNotIn('surprise', nx.DASH_WIDGETS)
        self.assertNotIn('surprise', nx.DASH_OPTIONS)
        self.assertNotIn('pick', nx.DASH_MERGED_SHOWN)
        self.assertEqual(nx.dash_normalize(None), nx.dash_default())

    def test_pick_vibe_is_checked(self):
        self.assertEqual(nx.DASH_OPTIONS['pick'],
                         {'vibe': ('any', 'short', 'coop', 'unfinished', 'almost', 'never')})
        for bad in ('chill', ['coop'], {'a': 1}, None, 3):
            out = nx.dash_normalize([{'id': 'pick', 'size': 'wide', 'vibe': bad}])
            pick = next(i for i in out if i['id'] == 'pick')
            self.assertEqual((pick['vibe'], pick['size']), ('any', 'small'), bad)
        out = nx.dash_normalize([{'id': 'pick', 'vibe': 'coop'}])
        self.assertEqual(next(i for i in out if i['id'] == 'pick')['vibe'], 'coop')

    def test_new_kinds_and_default_order(self):
        rows = {r[0]: r[1:] for r in nx.DASH_KINDS}
        self.assertEqual(rows['pick'], (('small',), 'small', False))
        self.assertEqual(rows['save'], (('small',), 'small', True))         # hidden by default
        self.assertEqual(rows['friends'], (('small', 'wide', 'full'), 'full', False))
        ids = [i['id'] for i in nx.dash_kind_defaults()]
        self.assertEqual(ids[:5], ['stage', 'save', 'foryou', 'friends', 'pick'])
        self.assertEqual((nx.DASH_PLACE_AFTER['pick'], nx.DASH_PLACE_AFTER['save']),
                         ('stage', 'stage'))
        # A saved layout from before w-91 gains Your save (hidden) right after the Stage.
        old = [i for i in nx.dash_kind_defaults() if i['id'] != 'save']
        out = nx.dash_normalize(old)
        self.assertEqual([i['id'] for i in out][:2], ['stage', 'save'])
        self.assertTrue(out[1]['hidden'])
        # Default packing at 3 columns: Pick for me and System share a row.
        packed = nx.dash_pack(nx.dash_kind_defaults(), 3, skip={'foryou', 'friends'})
        where = {k: (r, c) for k, r, c, _s in packed}
        self.assertEqual(where['pick'][0], where['system'][0])
        # Your save beside a Wide Stage.
        items = [dict(i, size='wide') if i['id'] == 'stage' else
                 dict(i, hidden=False) if i['id'] == 'save' else i
                 for i in nx.dash_kind_defaults()]
        packed = nx.dash_pack(items, 3, skip={'foryou'})
        self.assertEqual([(k, r, c) for k, r, c, _s in packed[:2]],
                         [('stage', 0, 0), ('save', 0, 2)])

    # ---- For you now (w-89) ----
    def test_foryou_kind_and_options(self):
        row = next(r for r in nx.DASH_KINDS if r[0] == 'foryou')
        self.assertEqual(row, ('foryou', ('full',), 'full', False))
        ids = [r[0] for r in nx.DASH_KINDS]
        self.assertEqual(ids.index('foryou'), ids.index('stage') + 2)    # Your save between
        self.assertEqual(nx.DASH_PLACE_AFTER['foryou'], 'stage')
        self.assertEqual(nx.DASH_OPTIONS['foryou'], {'style': ('stage', 'strip')})
        self.assertNotIn('foryou', nx.DASH_MERGED)
        self.assertNotIn('foryou', nx.DASH_MERGED.values())
        self.assertIn('foryou', nx.DASH_WIDGETS)
        d = nx.dash_default()
        self.assertEqual(d, nx.dash_normalize(None))
        self.assertEqual(d[1], {'id': 'foryou', 'size': 'full', 'hidden': False,
                                'style': 'stage'})

    def test_foryou_normalize_round_trip(self):
        # A layout saved before w-89 gets For you now right after the Stage.
        saved = [i for i in nx.dash_kind_defaults() if i['id'] != 'foryou']
        out = nx.dash_normalize(saved)
        ids = [i['id'] for i in out]
        self.assertEqual(ids[:4], ['stage', 'foryou', 'save', 'friends'])
        self.assertEqual(nx.dash_normalize(out), out)                # idempotent
        moved = [dict(i) for i in out]
        fy = next(i for i in moved if i['id'] == 'foryou')
        fy.update(style='strip', hidden=True)
        moved.append(moved.pop(moved.index(fy)))                     # moved to the end
        again = nx.dash_normalize(moved)
        self.assertEqual(again[-1], {'id': 'foryou', 'size': 'full', 'hidden': True,
                                     'style': 'strip'})
        bad = nx.dash_normalize([{'id': 'foryou', 'style': 'posters', 'size': 'small'}])
        fy = next(i for i in bad if i['id'] == 'foryou')
        self.assertEqual((fy['style'], fy['size']), ('stage', 'full'))

    def test_foryou_mode(self):
        from nexus.gui.foryou import foryou_mode
        d = nx.dash_default()

        def with_(**kw):
            out = [dict(i) for i in d]
            for kind, changes in kw.items():
                next(i for i in out if i['id'] == kind).update(changes)
            return out
        self.assertEqual(foryou_mode(d, False, True), 'pill')
        self.assertEqual(foryou_mode(d, True, True), 'placeholder')   # Customise
        self.assertEqual(foryou_mode(d, False, False), 'strip')       # no Stage to sit on
        self.assertEqual(foryou_mode(with_(stage={'size': 'wide'}), False, True), 'pill')
        self.assertEqual(foryou_mode(with_(stage={'size': 'banner'}), False, True), 'strip')
        self.assertEqual(foryou_mode(with_(stage={'hidden': True}), False, True), 'strip')
        self.assertEqual(foryou_mode(with_(foryou={'style': 'strip'}), False, True), 'strip')
        self.assertEqual(foryou_mode(with_(foryou={'style': 'strip'}), True, True), 'strip')
        self.assertEqual(foryou_mode(with_(foryou={'hidden': True}), False, True), 'off')
        self.assertEqual(foryou_mode([i for i in d if i['id'] != 'foryou'], False, True),
                         'off')

    def test_foryou_signature_and_sizes(self):
        from unittest import mock
        from nexus.gui import foryou as F
        from nexus.gui.theme import LOOK
        a = {'id': 'running:x', 'kind': 'running', 'title': 'X', 'why': '10%',
             'action': {'id': 'open_job', 'label': 'View', 'arg': 'x'}, 'badge': ('download',
                                                                                  'accent')}
        b = dict(a, why='11%')
        c = dict(a, id='friends:y')
        self.assertEqual(F.moments_sig([a]), F.moments_sig([b]))      # only the text moved
        self.assertNotEqual(F.moments_text([a]), F.moments_text([b]))
        self.assertNotEqual(F.moments_sig([a]), F.moments_sig([a, c]))
        self.assertNotEqual(F.moments_sig([a, c]), F.moments_sig([c, a]))
        self.assertEqual(F.split_snooze('friends:steam-1'), ('friends', 'steam-1'))
        self.assertEqual(F.split_snooze('proton:GE-Proton10-25'), ('proton', 'GE-Proton10-25'))
        self.assertEqual(F.split_snooze('repack_updates:'), ('repack_updates', ''))
        with mock.patch.dict(LOOK, scale=1.0):
            self.assertEqual(F.strip_count(1366 - 64), 3)
            self.assertEqual(F.strip_count(960), 2)
            self.assertEqual(F.strip_count(500), 1)
            self.assertEqual(F.strip_count(100), 1)
            self.assertEqual(F.strip_count(5000), 3)
            self.assertEqual(F.list_width(3, 1200), 440)
            self.assertEqual(F.list_width(2, 900), 400)
            self.assertEqual(F.list_width(1, 600), 600)
        with mock.patch.dict(LOOK, scale=1.5):
            self.assertEqual(F.list_width(3, 1200), 660)
        self.assertEqual([k for k, _t in F.KIND_CHIPS],
                         ['running', 'friends', 'store', 'update', 'achievements', 'idle',
                          'disk'])

    def test_foryou_kinds_off_are_checked(self):
        from nexus.gui import moments as M
        self.assertEqual(M.clean_kinds_off(['store', 'broken', 'bogus', 3, 'store', 'proton',
                                            'idle']), ['store', 'proton', 'idle'])
        self.assertEqual(M.clean_kinds_off('store'), [])
        self.assertEqual(M.clean_kinds_off(None), [])
        self._prefs({'dashboard': {'version': 2, 'items': self.items(('stage', 'cinematic')),
                                   'kinds_off': ['broken', 'disk', 'nope']}})
        self.assertEqual(nx.LibraryPrefs().kinds_off, ['disk'])

    def _prefs(self, saved):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / 'library.json'
        if saved is not None:
            path.write_text(json.dumps(saved))
        p = mock.patch.object(nx.LibraryPrefs, 'PATH', path)
        p.start()
        self.addCleanup(p.stop)
        return path

    def test_prefs_keep_the_v1_list_once_and_write_version_2(self):
        import json
        v1 = self.items(('system', 'wide'), ('jump', 'full'), ('installed', 'full'))
        path = self._prefs({'dashboard': {'version': 1, 'items': v1}, 'future_key': 1})
        pr = nx.LibraryPrefs()
        self.assertEqual(pr.dashboard[0]['id'], 'stage')
        pr.save()
        data = json.loads(path.read_text())
        self.assertEqual(data['dashboard']['version'], 2)
        self.assertEqual(data['dashboard_v1'], v1)
        self.assertEqual(data['dashboard']['items'][0]['id'], 'stage')
        self.assertEqual(data['future_key'], 1)
        pr2 = nx.LibraryPrefs()                      # a second load and save change nothing
        pr2.dashboard[0]['hidden'] = True
        pr2.save()
        again = json.loads(path.read_text())
        self.assertEqual(again['dashboard_v1'], v1)
        self.assertEqual(again['dashboard']['version'], 2)
        self.assertTrue(again['dashboard']['items'][0]['hidden'])

    def test_prefs_without_a_saved_layout_or_from_a_newer_nexus(self):
        import json
        path = self._prefs(None)
        nx.LibraryPrefs().save()
        self.assertNotIn('dashboard_v1', json.loads(path.read_text()))
        newer = self.items(('system', 'wide'), ('stage', 'wide'), ('installed', 'full'))
        path = self._prefs({'dashboard': {'version': 5, 'items': newer}})
        pr = nx.LibraryPrefs()
        self.assertEqual([i['id'] for i in pr.dashboard][:2], ['system', 'stage'])
        pr.save()
        data = json.loads(path.read_text())
        self.assertEqual(data['dashboard']['version'], 5)           # never lowered
        self.assertNotIn('dashboard_v1', data)

    def test_reset_gives_the_cinematic_layout_and_keeps_the_backup(self):
        import json
        v1 = self.items(('jump', 'full'))
        path = self._prefs({'dashboard': {'version': 1, 'items': v1}})
        pr = nx.LibraryPrefs()
        pr.save()
        pr.dashboard = nx.dash_default()
        pr.save()
        data = json.loads(path.read_text())
        self.assertEqual(data['dashboard']['items'], nx.dash_default())
        self.assertEqual(data['dashboard']['items'][0]['size'], 'cinematic')
        self.assertEqual(data['dashboard_v1'], v1)

    # ---- For you now's dismiss state (w-88) ----
    def test_moments_state_round_trips_with_other_dashboard_keys(self):
        """Snoozes, Quiet and Kinds survive save/load in a v2 file, and keys
        a newer Nexus put in `dashboard` (layouts, active) are kept."""
        import json
        from datetime import datetime, timedelta
        from nexus.gui import moments as M
        v2 = self.items(('stage', 'cinematic'), ('system', 'wide'))
        path = self._prefs({'dashboard': {'version': 2, 'items': v2, 'layouts': [
                                              {'name': 'Evening', 'items': v2, 'n': 1}],
                                          'active': 'Evening', 'density': 'compact'},
                            'dashboard_v1': [{'id': 'jump'}]})
        pr = nx.LibraryPrefs()
        self.assertEqual((pr.snoozes, pr.quiet_until, pr.kinds_off), ({}, None, []))
        now = datetime.now()
        pr.snooze('friends', 'steam-1', now)
        pr.quiet_tomorrow(now)
        pr.kinds_off = ['store', 'proton']
        pr.save()
        data = json.loads(path.read_text())
        dash = data['dashboard']
        lt = dash['layouts'][0]
        self.assertEqual((lt['name'], lt['n'], lt['density'], dash['active'], dash['density']),
                         ('Evening', 1, 'compact', 'Evening', 'compact'))  # unknown key kept
        self.assertEqual(lt['items'], pr.dashboard)                  # written through
        self.assertEqual(dash['version'], 2)
        self.assertEqual(data['dashboard_v1'], [{'id': 'jump'}])
        until = (now.date() + timedelta(days=M.SNOOZE_DAYS)).isoformat()
        self.assertEqual(dash['snoozes'], {'friends:steam-1': until})
        self.assertEqual(dash['quiet_until'], M.quiet_until_next(now).isoformat())
        self.assertEqual(dash['kinds_off'], ['store', 'proton'])
        again = nx.LibraryPrefs()
        self.assertEqual(again.snoozes, {'friends:steam-1': until})
        self.assertEqual(again.quiet_until, M.quiet_until_next(now))
        self.assertEqual(again.kinds_off, ['store', 'proton'])
        self.assertTrue(M.is_snoozed(again.snoozes, 'friends:steam-1', now))
        again.clear_quiet()
        self.assertIsNone(nx.LibraryPrefs().quiet_until)
        self.assertEqual(json.loads(path.read_text())['dashboard']['layouts'][0]['n'], 1)
        again.clear_snoozes()                                       # w-107: Show again
        self.assertEqual(nx.LibraryPrefs().snoozes, {})

    def test_moments_state_from_a_v1_file_keeps_the_backup(self):
        import json
        from datetime import datetime
        v1 = self.items(('jump', 'full'), ('system', 'wide'))
        path = self._prefs({'dashboard': {'version': 1, 'items': v1}})
        pr = nx.LibraryPrefs()
        pr.snooze('atl', 'steam-5', datetime.now())
        data = json.loads(path.read_text())
        self.assertEqual(data['dashboard_v1'], v1)
        self.assertEqual(data['dashboard']['version'], 2)
        self.assertIn('atl:steam-5', data['dashboard']['snoozes'])

    def test_bad_or_expired_moments_state_is_ignored(self):
        import json
        from datetime import datetime, timedelta
        today = datetime.now().date()
        good = (today + timedelta(days=3)).isoformat()
        cases = [
            {'snoozes': ['x'], 'quiet_until': 5, 'kinds_off': 'store'},
            {'snoozes': {'a:b': 'soon', 'c:d': 7, 'e:f': (today - timedelta(days=1)).isoformat(),
                         'g:h': today.isoformat(), 5: good, 'ok:1': good},
             'quiet_until': '2000-01-01T04:00:00', 'kinds_off': ['store', 3, None, 'store']},
            {'snoozes': None, 'quiet_until': 'tomorrow', 'kinds_off': {'store': True}},
        ]
        for extra in cases:
            items = self.items(('stage', 'cinematic'))
            self._prefs({'dashboard': {'version': 2, 'items': items, **extra}})
            pr = nx.LibraryPrefs()                     # never raises
            self.assertIsNone(pr.quiet_until, extra)
            self.assertIsInstance(pr.snoozes, dict)
            self.assertTrue(all(isinstance(k, str) and isinstance(v, str)
                                for k, v in pr.snoozes.items()))
            self.assertIsInstance(pr.kinds_off, list)
            pr.save()
        self.assertEqual(pr.snoozes, {})
        pr = None
        self._prefs({'dashboard': cases[1] | {'version': 2, 'items': self.items(
            ('stage', 'cinematic'))}})
        pr = nx.LibraryPrefs()
        self.assertEqual(pr.snoozes, {'ok:1': good})          # expired, today, bad: dropped
        self.assertEqual(pr.kinds_off, ['store'])
        self._prefs({'dashboard': 'not a dict'})
        self.assertEqual(nx.LibraryPrefs().snoozes, {})
        future = (datetime.now() + timedelta(hours=3)).replace(microsecond=0)
        self._prefs({'dashboard': {'version': 2, 'quiet_until': future.isoformat()}})
        self.assertEqual(nx.LibraryPrefs().quiet_until, future)

    def test_snoozes_are_capped(self):
        import json
        from datetime import datetime
        from nexus.gui import moments as M
        path = self._prefs(None)
        pr = nx.LibraryPrefs()
        now = datetime.now()
        for i in range(M.SNOOZE_MAX + 20):
            pr.snooze('friends', f'g{i}', now)
        self.assertEqual(len(pr.snoozes), M.SNOOZE_MAX)
        self.assertIn(f'friends:g{M.SNOOZE_MAX + 19}', pr.snoozes)       # the newest kept
        self.assertEqual(len(json.loads(path.read_text())['dashboard']['snoozes']),
                         M.SNOOZE_MAX)


@unittest.skipIf(nx is None, "PyQt6 not available")
class PickAndSave(unittest.TestCase):
    """w-91's pure parts: vibe_pools, pick_kicker, pick_note, save_rows."""
    FACTS = {'short': {'length': {'main': 3.0}},
             'coop': {'modes': ['single', 'coop'], 'length': {'main': 20.0}},
             'half': {'length': {'main': 10.0}}}

    def games(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(days=60)).isoformat(timespec='seconds')
        return [{'slug': 'short', 'launcher': '/s', 'playtime_minutes': 30, 'last_played': old},
                {'slug': 'coop', 'launcher': '/c', 'playtime_minutes': 600, 'last_played': old},
                {'slug': 'half', 'launcher': '/h', 'playtime_minutes': 300, 'last_played': old},
                {'slug': 'new', 'launcher': '/n'},
                {'slug': 'running', 'launcher': '/r', 'playing': True},
                {'slug': 'hidden', 'launcher': '/x', 'hidden': True},
                {'slug': 'owned', 'source': 'steam', 'installed': False}]

    def test_vibe_pools(self):
        pools = nx.vibe_pools(self.games(), self.FACTS)
        self.assertEqual(list(pools), ['any', 'short', 'coop', 'unfinished', 'never'])
        self.assertEqual([g['slug'] for g in pools['any']], ['short', 'coop', 'half', 'new'])
        self.assertEqual([g['slug'] for g in pools['short']], ['short'])
        self.assertEqual([g['slug'] for g in pools['never']], ['new'])
        self.assertNotIn('almost', pools)                          # nothing almost done: no chip
        self.assertEqual({g['slug'] for g in pools['unfinished']}, {'coop', 'half'})
        # The running game is never in a pool, not even Never played.
        running = [dict(g, playing=True) if g['slug'] == 'new' else g for g in self.games()]
        self.assertNotIn('never', nx.vibe_pools(running, self.FACTS))
        # From memory alone (no facts): no short, co-op or almost done.
        self.assertEqual(list(nx.vibe_pools(self.games(), {})), ['any', 'never'])
        self.assertEqual(nx.vibe_pools([], self.FACTS), {})
        self.assertEqual(nx.vibe_pools(self.games()[4:], self.FACTS), {})
        self.assertEqual([k for k, _l in nx.VIBES],
                         ['any', 'short', 'coop', 'unfinished', 'almost', 'never'])

    def test_pick_kicker_and_note(self):
        from nexus.gui.dashboard import pick_note
        self.assertEqual(nx.pick_kicker(), "PICKED FOR YOU")
        self.assertEqual(nx.pick_kicker('any'), "PICKED FOR YOU")
        self.assertEqual(nx.pick_kicker('coop'), "PICKED FOR YOU  ·  CO-OP")
        self.assertEqual(nx.pick_kicker('short'), "PICKED FOR YOU  ·  QUICK SESSION")
        self.assertEqual(nx.pick_kicker('bogus'), "PICKED FOR YOU")
        self.assertEqual(nx.pick_kicker('coop', "Hades"), "PICKED FOR YOU  ·  HADES STAYS PINNED")
        long = nx.pick_kicker('any', "The Elder Scrolls V: Skyrim Special Edition")
        self.assertEqual(long, "PICKED FOR YOU  ·  THE ELDER SCROLLS V: SK… STAYS PINNED")
        self.assertEqual(len("THE ELDER SCROLLS V: SK…"), 24)
        self.assertEqual(pick_note('any', 21), "From 21 games")
        self.assertEqual(pick_note('coop', 7), "From 7 co-op games")
        self.assertEqual(pick_note('any', 1), "From 1 game")
        self.assertEqual(pick_note('almost', 2), "From 2 games almost done")

    def test_save_rows_leave_out_what_is_missing(self):
        from nexus.gui.dashboard import last_unlock, save_rows, SAVE_EMPTY
        g = {'slug': 'coop', 'display_name': 'Co Op', 'source': 'steam', 'appid': 5,
             'playtime_minutes': 600, 'achievements': [6, 13]}
        owners = [{'steamid': '1', 'name': 'Ana'}, {'steamid': '2', 'name': 'Ben'},
                  {'steamid': '3', 'name': 'Cy'}]
        rows = save_rows(g, self.FACTS, 24, owners, {'title': 'Cash Flow', 'time': 1})
        kinds = [r[0] for r in rows]
        self.assertEqual(kinds, ['big', 'sub', 'bar', 'bar', 'owners', 'last'])
        self.assertEqual(rows[0][1], '10.0 h')
        self.assertEqual(rows[1][1], "Co Op · 24 sessions")
        self.assertEqual(rows[2][1:4], ("Achievements", "6 of 13", 6 / 13))
        self.assertEqual(rows[3][1:4], ("Main story", "20 h", 0.5))
        self.assertEqual(rows[4][2], "Ana, Ben +1 own it too")
        self.assertTrue(rows[5][1].startswith("Last unlock: Cash Flow · "))
        # Past the main story: the bar stays full.
        self.assertEqual(save_rows(dict(g, playtime_minutes=6000), self.FACTS)[3][3], 1.0)
        # A repack: no Steam achievements; one owner; one session; no HLTB.
        thin = save_rows({'slug': 'x', 'display_name': 'X', 'source': 'nexus',
                          'achievements': [1, 2], 'playtime_minutes': 90}, {}, 1,
                         owners[:1], None)
        self.assertEqual([r[0] for r in thin], ['big', 'sub', 'owners'])
        self.assertEqual(thin[1][1], "X · 1 session")
        self.assertEqual(thin[2][2], "Ana owns it too")
        self.assertEqual(save_rows({'slug': 'x', 'playtime_minutes': 5}, {}, None, [
            {'name': 'Ana'}, {'name': 'Ben'}])[-1][2], "Ana and Ben own it too")
        # Nothing at all: the name and a line saying so.
        self.assertEqual(save_rows({'slug': 'z', 'display_name': 'Zed'}, {}, 0, [], None),
                         [('sub', 'Zed'), ('empty', SAVE_EMPTY)])
        # The newest unlocked achievement of an --achievements answer.
        data = {'items': [{'title': 'A', 'unlocked': True, 'time': 5},
                          {'title': 'B', 'unlocked': True, 'time': 9},
                          {'title': 'C', 'unlocked': False, 'time': None}]}
        self.assertEqual(last_unlock(data)['title'], 'B')
        self.assertIsNone(last_unlock({'items': []}))
        self.assertIsNone(last_unlock(None))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Dwell(unittest.TestCase):
    """w-85: the focus-to-stage helper shared by the desk Stage and Theatre."""

    def setUp(self):
        from unittest import mock
        from nexus.gui import art
        p = mock.patch.object(art, 'MOTION', True)
        p.start()
        self.addCleanup(p.stop)
        self.got = []
        self.d = nx.Dwell(140, self.got.append)

    def tearDown(self):
        self.d.cancel()
        self.d.deleteLater()

    def test_settles_once_after_the_wait(self):
        self.d.push('a')
        self.assertEqual(self.got, [])
        self.assertTrue(self.d.timer.isActive())
        self.assertEqual(self.d.timer.interval(), 140)
        self.d.timer.stop()
        self.d._fire()
        self.assertEqual(self.got, ['a'])
        self.d.push('b', 350)                                    # its own wait
        self.assertEqual(self.d.timer.interval(), 350)

    def test_new_push_restarts(self):
        from PyQt6.QtTest import QTest
        self.d.ms = 30
        self.d.push('a')
        self.d.push('b')
        QTest.qWait(120)
        self.assertEqual(self.got, ['b'])                         # only where it stopped

    def test_instant_without_motion(self):
        from unittest import mock
        from nexus.gui import art
        with mock.patch.object(art, 'MOTION', False):
            self.d.push('a')
            self.assertEqual(self.got, ['a'])                     # read at push time
            self.assertFalse(self.d.timer.isActive())

    def test_cancel(self):
        self.d.push('a')
        self.d.cancel()
        self.assertFalse(self.d.timer.isActive())
        self.assertIsNone(self.d.value)
        self.assertEqual(self.got, [])


@unittest.skipIf(nx is None, "PyQt6 not available")
class DashboardFill(unittest.TestCase):
    """Home's Your week at each size and style, from stub CLI answers that
    come back synchronously (as a cached cli_json answer does, inside fill)."""
    HIST = {'since': '2026-10-01', 'weeks': [{'start': '2026-10-26', 'minutes': 90}],
            'periods': {'today': '2026-10-28',
                        'week': {'minutes': 90,
                                 'days': [{'date': f'2026-10-{d}', 'minutes': 45 * (d < 28)}
                                          for d in range(26, 32)]
                                 + [{'date': '2026-11-01', 'minutes': 0}],
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

    GAMES = [{'slug': 'a', 'display_name': 'Alpha', 'playtime_minutes': 600, 'source': 'steam',
              'appid': 1, 'achievements': [9, 10]},
             {'slug': 'b', 'display_name': 'Beta', 'playtime_minutes': 60, 'source': 'steam',
              'appid': 2, 'achievements': [10, 10]}]

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
        self.hub = SimpleNamespace(_games=[dict(g) for g in self.GAMES])

    def texts(self, w):
        return [lb.text() for lb in w.findChildren(QLabel) if lb.text()]

    def filled(self, cls, size, **options):
        w = cls()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        w.options = options
        w.fill(self.hub)
        return w

    def test_week_fill_styles(self):
        D = self.D
        small = self.filled(D.WeekWidget, 'small')
        charts = small.findChildren(nx.MinuteBars)
        self.assertEqual(len(charts), 1)
        self.assertEqual(len(charts[0].bars), 7)
        self.assertEqual(charts[0].highlight, {2})                   # today, the 28th
        t = self.texts(small)
        self.assertIn('1.5 h', t)
        self.assertIn('1 game this week', t)
        self.assertNotIn('Alpha', t)                                 # small: no top games
        wide = self.filled(D.WeekWidget, 'wide')
        self.assertEqual(len(wide.findChildren(nx.MinuteBars)), 1)
        self.assertIn('Alpha', self.texts(wide))
        self.assertEqual(D.WeekWidget.LINK, 'history')
        num = self.filled(D.WeekWidget, 'wide', style='number')
        self.assertFalse(num.findChildren(nx.MinuteBars))
        self.assertIn('1.5 h', self.texts(num))
        # No change: the rows are kept (pad focus), not rebuilt.
        before = set(wide.findChildren(QLabel))
        wide.fill(self.hub)
        self.assertEqual(set(wide.findChildren(QLabel)), before)
        self.assertEqual(self.asked, ['--history'] * 3)              # one ask per widget

    def test_week_without_sessions(self):
        self.ANSWERS = dict(self.ANSWERS, **{'--history': {}})
        w = self.filled(self.D.WeekWidget, 'small')
        self.assertFalse(w.findChildren(nx.MinuteBars))
        self.assertIn('0 h', self.texts(w))


@unittest.skipIf(nx is None, "PyQt6 not available")
class PlayHistoryFill(unittest.TestCase):
    """The big Play time and Achievements charts on Play history (w-87), from
    stub CLI answers that come back synchronously, as a cached one does."""

    def setUp(self):
        from unittest import mock
        from nexus.gui import system as S
        self.S = S
        self.asked = []
        self.fail_rarest = False
        self.answers = dict(DashboardFill.ANSWERS)

        def cli_json(args, cb, timeout=0, ttl=0):
            key = args[1] if args[0] == '--achievements' else args[0]
            self.asked.append(key)
            if key == 'rarest' and self.fail_rarest:
                cb(None, 'offline')
            else:
                cb(self.answers[key], '')
        p = mock.patch.object(S, 'cli_json', cli_json)
        p.start()
        self.addCleanup(p.stop)
        self.games = [dict(g) for g in DashboardFill.GAMES]

    def page(self):
        p = self.S.PlayHistoryPage()
        self.addCleanup(p.deleteLater)
        p.refresh(games=self.games)
        return p

    def texts(self, p):
        return [lb.text() for lb in p.findChildren(QLabel) if lb.text() and lb.isVisibleTo(p)]

    def charts(self, p, n):
        return [b for b in p.findChildren(self.S.MinuteBars)
                if len(b.bars) == n and b.isVisibleTo(p)]

    def test_play_time_section(self):
        p = self.page()
        t = self.texts(p)
        self.assertTrue({'This month by day', 'This month', 'All time'} <= set(t), t)
        month = self.charts(p, 31)
        self.assertEqual(len(month), 1)
        self.assertEqual(month[0].highlight, set(range(25, 31)))    # this week: 26th-31st
        self.assertIn('Beta', t)                                     # all time, by game
        # Nothing changed: the same widgets stay (pad focus), nothing is redrawn.
        before = set(p.findChildren(QLabel))
        p.refresh(games=[dict(g) for g in self.games])
        p.refresh(force=True, games=self.games)
        self.assertEqual(set(p.findChildren(QLabel)), before)
        self.assertEqual(self.asked.count('--history'), 2)          # the 30 s throttle

    def test_no_sessions_still_shows_all_time(self):
        self.answers['--history'] = {}
        p = self.page()
        self.assertTrue(p.empty.isVisibleTo(p))
        t = self.texts(p)
        self.assertIn('No sessions yet', t)
        self.assertNotIn('This month by day', t)
        self.assertFalse(self.charts(p, 31))
        self.assertIn('All time', t)
        self.assertIn('Alpha', t)
        self.assertIn('Closest to 100%', t)

    def test_achievements_section(self):
        p = self.page()
        t = self.texts(p)
        self.assertEqual(t.count('First steps'), 1)        # drawn once despite the sync answer
        self.assertTrue({'Recent', 'Closest to 100%', 'Rarest', 'Needle', '0.7% · Alpha'}
                        <= set(t), t)
        self.assertIn('19', t)                                       # 9 + 10 unlocked
        self.fail_rarest = True
        off = self.page()
        self.assertNotIn('Rarest', self.texts(off))                  # offline: left out
        self.assertIn('First steps', self.texts(off))
        self.asked.clear()
        for g in self.games:
            g.pop('achievements')
        bare = self.page()
        self.assertEqual(self.asked, ['--history'])                  # no Steam stats: not asked
        self.assertNotIn('Closest to 100%', self.texts(bare))

    def test_achievements_error_clears_on_an_empty_success(self):
        p = self.page()
        p._got_recent(None, 'offline')
        self.assertTrue(any('unavailable' in t for t in self.texts(p)))
        p._got_recent({'recent': []}, '')
        self.assertFalse(any('unavailable' in t for t in self.texts(p)))
        self.assertNotIn('First steps', self.texts(p))


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
            for accent in (*nx.ACCENTS, '#ffff00', '#202020'):      # every preset, and extremes
                nx.apply_appearance({'theme': key, 'accent': accent})
                for bg in ('bg', 'surface', 'surface_solid'):
                    self.assertGreaterEqual(contrast(nx.P['text'], nx.P[bg]), 4.5, (key, bg))
                self.assertGreaterEqual(contrast(nx.P['muted'], nx.P['surface']), 3.0, key)
                # The accent as a button fill with its label on it.
                self.assertGreaterEqual(contrast(nx.P['on_accent'], nx.P['accent']), 3.0,
                                        (key, accent))
                if nx.is_light():
                    # Deep enough to read as text (links, the active tab) on the page.
                    self.assertGreaterEqual(contrast(nx.P['accent'], nx.P['bg']), 3.0,
                                            (key, accent))
        self.assertAlmostEqual(nx.contrast('#ffffff', '#000000'), 21.0)   # the shared helper
        self.assertAlmostEqual(nx.contrast(nx.P['text'], nx.P['bg']),
                               contrast(nx.P['text'], nx.P['bg']))
        nx.apply_appearance({'theme': 'dawn'})
        self.assertTrue(nx.is_light())
        self.assertNotEqual(nx.P['danger'], nx.DARK_STATUS['love'])     # its own status hues
        nx.apply_appearance({'theme': 'rose-pine'})
        self.assertEqual(nx.P['danger'], nx.DARK_STATUS['love'])        # and back

    def test_themes_dark_light_and_high_contrast(self):
        kinds = [t.get('kind', 'dark') for _n, t in nx.THEMES.values()]
        self.assertGreaterEqual(kinds.count('dark'), 8)
        self.assertGreaterEqual(kinds.count('light'), 4)
        for key, (name, t) in nx.THEMES.items():
            self.assertTrue(name, key)
            for k in ('base', 'surface', 'overlay', 'overlay_hi', 'text', 'subtle', 'muted'):
                self.assertTrue(nx._hex_ok(t[k]), (key, k))
        nx.apply_appearance({'theme': 'contrast'})
        P = nx.P
        self.assertGreaterEqual(nx.contrast(P['text'], P['bg']), 15)
        self.assertGreaterEqual(nx.contrast(P['muted'], P['surface']), 7)      # AAA
        self.assertGreaterEqual(nx.contrast(P['border'], P['bg']), 3)           # edges you see
        self.assertGreaterEqual(nx.contrast(P['info'], P['surface']), 4.5)
        self.assertNotIn('tokens', nx.PAL)
        nx.apply_appearance({'theme': 'rose-pine'})
        self.assertEqual(nx.P['border'], nx.PAL['overlay'])       # overrides don't stick

    def test_light_theme_deepens_pale_accents(self):
        nx.apply_appearance({'theme': 'paper', 'accent': '#ffff00', 'partner': 'none'})
        self.assertEqual(nx.P['on_accent'], '#ffffff')
        self.assertGreaterEqual(nx.contrast('#ffffff', nx.P['accent']), 3.5)
        self.assertEqual(nx.GRAD['dusk'][0], nx.GRAD['dusk'][1])
        a, b = nx.accent_pair('mist', 'teal', 'silver')
        self.assertNotEqual(a, nx.ACCENTS['teal'])
        self.assertNotEqual(b, nx.ACCENTS['silver'])
        self.assertEqual(nx.accent_pair('midnight', 'teal', 'none'),
                         (nx.ACCENTS['teal'],) * 2)              # dark: as picked

    def test_presets_are_valid_looks(self):
        self.assertTrue(3 <= len(nx.PRESETS) <= 6)
        for key, (name, look) in nx.PRESETS.items():
            self.assertEqual(set(look), set(nx.PRESET_KEYS), key)
            ap = nx.normalize_appearance({**nx.APPEARANCE_DEFAULT, **look})
            self.assertEqual({k: ap[k] for k in nx.PRESET_KEYS}, look, key)   # nothing dropped
        self.assertTrue(any(nx.THEMES[look['theme']][1].get('kind') == 'light'
                            for _n, look in nx.PRESETS.values()))
        self.assertIn('contrast', {look['theme'] for _n, look in nx.PRESETS.values()})

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
class AppearanceSettings(unittest.TestCase):
    """Settings > Appearance offscreen: every theme, accent and look has a
    swatch; a look is one change; picking one part of it unmarks it."""

    def setUp(self):
        self.card = nx.AppearanceCard()
        self.addCleanup(self.card.deleteLater)
        self.card.resize(760, 900)
        self.card.show()
        self.got = []
        self.card.changed.connect(self.got.append)
        self.card.load(dict(nx.APPEARANCE_DEFAULT))

    def tearDown(self):
        self.card.close()
        nx.apply_appearance(nx.APPEARANCE_DEFAULT)

    def swatch(self, group, value):
        return next(sw for sw in self.card.groups[group] if sw.value == value)

    def test_every_theme_accent_and_look_has_a_swatch(self):
        g = self.card.groups
        self.assertEqual([sw.value for sw in g['theme']], list(nx.THEMES))
        self.assertEqual([sw.value for sw in g['accent']], list(nx.ACCENTS.values()))
        self.assertEqual([sw.value for sw in g['preset']], list(nx.PRESETS))
        self.assertFalse(any(sw.isChecked() for sw in g['preset']))    # default isn't a look

    def test_look_applies_in_one_change_and_is_marked(self):
        self.swatch('preset', 'neon').click()
        self.assertEqual(len(self.got), 1)                             # one re-style, not five
        look = nx.PRESETS['neon'][1]
        self.assertEqual({k: self.got[0][k] for k in nx.PRESET_KEYS}, look)
        self.assertEqual(self.got[0]['scale'], nx.APPEARANCE_DEFAULT['scale'])   # Size kept
        self.assertTrue(self.swatch('preset', 'neon').isChecked())
        self.assertTrue(self.swatch('theme', look['theme']).isChecked())
        self.assertEqual(self.card.theme_row.desc.text(), nx.THEMES[look['theme']][0])
        self.swatch('preset', 'neon').click()                          # again: nothing new
        self.assertEqual(len(self.got), 1)
        self.swatch('theme', 'ocean').click()                          # a part changed
        self.assertFalse(any(sw.isChecked() for sw in self.card.groups['preset']))
        self.assertEqual(self.got[-1]['theme'], 'ocean')
        self.card.load(self.got[-1] | look)                            # loaded back: marked
        self.assertTrue(self.swatch('preset', 'neon').isChecked())

    def test_swatches_paint_in_every_theme(self):
        for key in ('contrast', 'latte'):
            nx.apply_appearance({'theme': key})
            for grp in ('preset', 'theme'):
                for sw in self.card.groups[grp]:
                    self.assertFalse(sw.grab().isNull())


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


@unittest.skipIf(nx is None, "PyQt6 not available")
class HomeLayouts(unittest.TestCase):
    """Presets, saved layouts and Switch by time (w-92): the pure parts."""

    def setUp(self):
        from nexus.gui import layouts as L
        self.L = L

    def shown(self, items):
        return [(i['id'], i['size']) for i in items if not i['hidden']]

    def _prefs(self, saved):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / 'library.json'
        if saved is not None:
            path.write_text(json.dumps(saved))
        p = mock.patch.object(nx.LibraryPrefs, 'PATH', path)
        p.start()
        self.addCleanup(p.stop)
        return path

    # ---- presets ----
    def test_presets_are_idempotent_and_complete(self):
        every = sorted(k for k, *_ in nx.DASH_KINDS)
        for key, _label, _tip in self.L.PRESETS:
            items = self.L.dash_preset(key)
            self.assertEqual(nx.dash_normalize(items), items, key)
            self.assertEqual(sorted(i['id'] for i in items), every, key)
            stage = next(i for i in items if i['id'] == 'stage')
            self.assertEqual((stage['style'], stage['bg']), ('row', 'art'), key)
            self.assertNotIn('pin', stage)
            smart = next(i for i in items if i['id'] == 'smart')
            self.assertIn(smart['shelf'], ('unfinished', 'continue'))     # never `linux`
            self.assertIn(self.L.PRESET_DENSITY[key], nx.DASH_DENSITY)

    def test_cinematic_is_the_default_and_calm(self):
        d = nx.dash_default()
        self.assertEqual(d, self.L.dash_preset('cinematic'))
        self.assertEqual(d, nx.dash_normalize(None))
        self.assertEqual(d, nx.dash_normalize([]))
        self.assertEqual(self.shown(d), [('stage', 'cinematic'), ('foryou', 'full'),
                                         ('week', 'small'), ('pick', 'small'),
                                         ('friends', 'small'), ('smart', 'full')])
        by = {i['id']: i for i in d}
        self.assertEqual(by['foryou']['style'], 'stage')
        self.assertEqual(by['smart']['shelf'], 'unfinished')
        self.assertEqual(self.L.PRESET_DENSITY['cinematic'], 'comfortable')

    def test_other_presets(self):
        d = self.L.dash_preset('dashboard')
        self.assertEqual(self.shown(d), [
            ('stage', 'wide'), ('save', 'small'), ('foryou', 'full'), ('system', 'wide'),
            ('week', 'small'), ('friends', 'small'), ('pick', 'small'), ('news', 'small'),
            ('deals', 'wide'), ('smart', 'full')])
        self.assertEqual(next(i for i in d if i['id'] == 'foryou')['style'], 'strip')
        self.assertEqual(self.L.PRESET_DENSITY['dashboard'], 'compact')
        m = self.L.dash_preset('minimal')
        self.assertEqual(self.shown(m), [('stage', 'banner'), ('foryou', 'full'),
                                         ('smart', 'full')])
        self.assertEqual(next(i for i in m if i['id'] == 'smart')['shelf'], 'continue')
        self.assertEqual(self.L.PRESET_DENSITY['minimal'], 'comfortable')
        s = self.L.dash_preset('shelves')
        self.assertEqual(self.shown(s), [('foryou', 'full'), ('smart', 'full'),
                                         ('installed', 'full'), ('favourites', 'full'),
                                         ('collections', 'full'), ('updates', 'full')])
        stage = next(i for i in s if i['id'] == 'stage')
        self.assertEqual((stage['hidden'], stage['size']), (True, 'cinematic'))
        self.assertEqual(self.L.PRESET_DENSITY['shelves'], 'compact')

    # ---- layouts and rules ----
    def test_clean_layouts(self):
        L = self.L
        items = nx.dash_default()
        raw = [
            {'name': '  Couch  ', 'items': items, 'density': 'compact',
             'rule': {'from': '20:00', 'to': '06:00', 'x': 1}, 'auto': True, 'future': [1]},
            {'name': 'couch', 'items': items},                       # duplicate, dropped
            {'name': 'Desk', 'items': [{'id': 'news'}], 'density': 'tiny',
             'rule': {'from': '8:00', 'to': '09:00'}, 'auto': 'yes'},
            {'name': '', 'items': items}, {'name': 'x' * 41, 'items': items},
            {'name': 'Empty', 'items': []}, {'name': 'NotList', 'items': {'a': 1}},
            {'name': 3, 'items': items}, 'junk', None,
            {'name': 'Same', 'items': items, 'rule': {'from': '10:00', 'to': '10:00'}},
            {'name': 'Late', 'items': items, 'rule': {'from': '24:00', 'to': '01:00'}},
        ]
        out = L.clean_layouts(raw)
        self.assertEqual([lt['name'] for lt in out], ['Couch', 'Desk', 'Same', 'Late'])
        couch = out[0]
        self.assertEqual((couch['density'], couch['auto'], couch['future']),
                         ('compact', True, [1]))
        self.assertEqual(couch['rule'], {'from': '20:00', 'to': '06:00', 'x': 1})
        desk = out[1]
        self.assertNotIn('density', desk)
        self.assertNotIn('rule', desk)                               # 8:00 isn't zero-padded
        self.assertNotIn('auto', desk)
        self.assertEqual(desk['items'], nx.dash_normalize([{'id': 'news'}]))
        self.assertNotIn('rule', out[2])                             # from == to
        self.assertNotIn('rule', out[3])
        many = [{'name': f"L{i}", 'items': items} for i in range(20)]
        self.assertEqual(len(L.clean_layouts(many)), L.MAX_LAYOUTS)
        for bad in (None, {}, 'x', 3):
            self.assertEqual(L.clean_layouts(bad), [])
        self.assertEqual(L.clean_layouts(out), out)                  # idempotent

    def test_layout_for(self):
        from datetime import datetime
        L = self.L

        def at(h, m, layouts):
            return L.layout_for(layouts, datetime(2026, 3, 29, h, m))

        night = {'name': 'Couch', 'rule': {'from': '20:00', 'to': '06:00'}}
        day = {'name': 'Desk', 'rule': {'from': '09:00', 'to': '17:00'}}
        friends = {'name': 'Friends', 'rule': {'from': '19:00', 'to': '23:00'}}
        none = {'name': 'Plain'}
        ls = [night, day, none]
        table = [((23, 59), 'Couch'), ((0, 0), 'Couch'), ((5, 59), 'Couch'), ((6, 0), None),
                 ((19, 59), None), ((20, 0), 'Couch'), ((9, 0), 'Desk'), ((16, 59), 'Desk'),
                 ((17, 0), None), ((8, 59), None)]
        for (h, m), want in table:
            self.assertEqual(at(h, m, ls), want, (h, m))
        # Several: the one that started most recently
        both = [friends, night]
        self.assertEqual(at(19, 30, both), 'Friends')
        self.assertEqual(at(21, 0, both), 'Couch')                    # 20:00 is later
        self.assertEqual(at(23, 30, both), 'Couch')
        cross = [{'name': 'A', 'rule': {'from': '22:00', 'to': '02:00'}},
                 {'name': 'B', 'rule': {'from': '01:00', 'to': '03:00'}}]
        self.assertEqual(at(0, 30, cross), 'A')
        self.assertEqual(at(1, 30, cross), 'B')                       # A started yesterday
        tie = [{'name': 'First', 'rule': {'from': '10:00', 'to': '12:00'}},
               {'name': 'Second', 'rule': {'from': '10:00', 'to': '11:00'}}]
        self.assertEqual(at(10, 30, tie), 'First')                    # tie: the earlier one
        self.assertIsNone(L.layout_for([], datetime(2026, 1, 1, 12)))
        self.assertIsNone(L.layout_for([none, {'name': 'Bad', 'rule': 'x'}],
                                       datetime(2026, 1, 1, 12)))
        # DST: wall-clock only. Fall back repeats 02:30; spring forward skips it.
        fall = [{'name': 'Early', 'rule': {'from': '02:00', 'to': '03:00'}}]
        self.assertEqual(L.layout_for(fall, datetime(2026, 10, 25, 2, 30)), 'Early')
        self.assertEqual(L.layout_for(fall, datetime(2026, 10, 25, 2, 30, fold=1)), 'Early')
        gap = [{'name': 'Gap', 'rule': {'from': '02:00', 'to': '02:45'}}]
        self.assertIsNone(L.layout_for(gap, datetime(2026, 3, 29, 1, 59)))
        self.assertIsNone(L.layout_for(gap, datetime(2026, 3, 29, 3, 0)))

    def test_rule_text_and_overlaps(self):
        from nexus.gui import layouts_bar as B
        L = self.L
        couch = {'from': '20:00', 'to': '06:00'}
        self.assertEqual(L.rule_text(couch), "from 20:00 to 06:00")
        self.assertEqual(L.rule_text(None), "")
        others = [('Friends night', {'from': '19:00', 'to': '23:00'}),
                  ('Morning', {'from': '07:00', 'to': '09:30'}),
                  ('Dawn', {'from': '05:45', 'to': '06:15'})]
        self.assertEqual([n for n, _r in L.overlaps(couch, others)], ['Friends night', 'Dawn'])
        self.assertEqual(L.overlaps({'from': '06:15', 'to': '07:00'}, others), [])  # edges touch
        self.assertEqual(L.overlaps({'from': '10:00', 'to': '10:00'}, others), [])
        self.assertIn("the next morning", B.rule_note(couch))
        self.assertNotIn("next morning", B.rule_note({'from': '07:00', 'to': '09:00'}))
        self.assertEqual(B.rule_note({'from': '07:00', 'to': '07:00'}),
                         "Pick two different times.")
        self.assertEqual(B.overlap_note(couch, others[:1]),
                         "Overlaps “Friends night” (19:00 to 23:00): the one that starts "
                         "later wins.")
        self.assertIn("and 1 more", B.overlap_note(couch, others))
        self.assertEqual(B.rule_line({'name': 'a', 'rule': couch}), "switches from 20:00 to 06:00")
        self.assertEqual(B.rule_line({'name': 'a', 'auto': True}), "saved for you before a preset")
        self.assertEqual(B.rule_line({'name': 'a'}), "no time rule")

    def test_names(self):
        L = self.L
        ls = [{'name': 'Couch'}, {'name': 'Couch 2'}]
        self.assertEqual(L.unique_name(ls, 'couch'), 'couch 3')
        self.assertEqual(L.unique_name(ls, 'Desk'), 'Desk')
        self.assertEqual(L.clean_name('  a   b '), 'a b')
        self.assertIsNone(L.clean_name('   '))
        self.assertIsNone(L.clean_name(['x']))

    # ---- prefs ----
    def test_prefs_round_trip(self):
        import json
        v1 = [{'id': 'jump', 'size': 'full'}, {'id': 'news', 'size': 'wide'}]
        items = nx.dash_default()
        path = self._prefs({
            'dashboard': {'version': 2, 'items': items, 'future': {'k': 1},
                          'layouts': [{'name': 'Couch', 'items': items, 'zz': 2,
                                       'rule': {'from': '20:00', 'to': '06:00'}},
                                      {'name': 'Desk', 'items': items, 'density': 'compact'},
                                      'bad'],
                          'active': 'couch', 'base': 'Gone', 'v1_taken': 'yes'},
            'dashboard_v1': v1})
        pr = nx.LibraryPrefs()
        self.assertEqual([lt['name'] for lt in pr.layouts], ['Couch', 'Desk'])
        self.assertEqual((pr.active, pr.base, pr.v1_taken), ('Couch', None, False))
        self.assertEqual(pr.old_home(), v1)
        # Write-through: Home's edits go into the active layout
        pr.dashboard = nx.dash_normalize([dict(i, hidden=True) if i['id'] == 'week' else i
                                          for i in items])
        pr.density = 'compact'
        pr.save()
        data = json.loads(path.read_text())
        dash = data['dashboard']
        self.assertEqual(dash['future'], {'k': 1})
        self.assertEqual(data['dashboard_v1'], v1)
        couch = dash['layouts'][0]
        self.assertEqual((couch['zz'], couch['density']), (2, 'compact'))
        self.assertTrue(next(i for i in couch['items'] if i['id'] == 'week')['hidden'])
        self.assertEqual(dash['layouts'][1]['items'], items)          # not the active one
        self.assertEqual((dash['active'], dash['base'], dash['v1_taken']),
                         ('Couch', None, False))
        again = nx.LibraryPrefs()
        self.assertEqual(again.layouts, pr.layouts)

    def test_bad_layout_values_never_stop_start_up(self):
        for dash in ({'layouts': 'x', 'active': 3, 'base': [], 'v1_taken': 1},
                     {'layouts': [{'name': {'a': 1}, 'items': [1]}], 'active': None},
                     {'layouts': [None, 4, [], {'items': None}]}, []):
            self._prefs({'dashboard': dash})
            pr = nx.LibraryPrefs()
            self.assertEqual((pr.layouts, pr.active, pr.base, pr.v1_taken),
                             ([], None, None, False), dash)

    def test_keep_current_and_the_cap(self):
        L = self.L
        self._prefs(None)
        pr = nx.LibraryPrefs()
        self.assertEqual(pr.keep_current("Before Minimal"), 'saved')
        self.assertEqual(pr.layouts[0]['name'], "Before Minimal")
        self.assertTrue(pr.layouts[0]['auto'])
        self.assertEqual(pr.keep_current("Before Dashboard"), 'kept')  # same items, density
        self.assertEqual(len(pr.layouts), 1)
        for i in range(L.MAX_LAYOUTS - 1):
            pr.dashboard = nx.dash_normalize([dict(it, hidden=it['id'] == 'week' and i % 2 == 0,
                                                   size='wide' if it['id'] == 'news' else
                                                   it['size']) for it in nx.dash_default()])
            self.assertIsNotNone(pr.add_layout(f"Mine {i}", pr.dashboard))
        self.assertEqual(len(pr.layouts), L.MAX_LAYOUTS)
        pr.dashboard = L.dash_preset('minimal')
        self.assertEqual(pr.keep_current("Before Shelf-first"), 'saved')   # oldest auto went
        self.assertNotIn("Before Minimal", [lt['name'] for lt in pr.layouts])
        self.assertEqual(pr.layouts[-1]['name'], "Before Shelf-first")
        pr.delete_layout("Before Shelf-first")
        pr.add_layout("Mine last", nx.dash_default())
        pr.dashboard = L.dash_preset('shelves')
        self.assertEqual(pr.keep_current("Before Cinematic"), 'full')     # none saved for you
        self.assertIsNone(pr.save_as("One more"))
        # Active layout: already saved
        pr.active = "Mine 0"
        self.assertEqual(pr.keep_current("Before Minimal"), 'kept')

    def test_save_as_rename_delete_and_rules(self):
        self._prefs(None)
        pr = nx.LibraryPrefs()
        lt = pr.save_as("Couch")
        self.assertEqual((pr.active, pr.base), ("Couch", "Couch"))
        self.assertEqual(lt['density'], pr.density)
        self.assertEqual(pr.save_as("couch")['name'], "couch 2")
        self.assertEqual(pr.rename_layout("couch 2", "Desk"), "Desk")
        self.assertEqual((pr.active, pr.base), ("Desk", "Desk"))
        self.assertEqual(pr.rename_layout("Desk", "COUCH"), "COUCH 2")
        pr.set_rule("Couch", {'from': '20:00', 'to': '06:00'})
        self.assertEqual(pr.layout("Couch")['rule'], {'from': '20:00', 'to': '06:00'})
        pr.set_rule("Couch", {'from': '20:00', 'to': '20:00'})          # bad: removed
        self.assertNotIn('rule', pr.layout("Couch"))
        pr.delete_layout("COUCH 2")
        self.assertEqual((pr.active, pr.base), (None, None))
        self.assertEqual([x['name'] for x in pr.layouts], ["Couch"])

    def test_old_home_offer(self):
        self._prefs({'dashboard': {'version': 1, 'items': [{'id': 'jump'}, {'id': 'news'}]}})
        pr = nx.LibraryPrefs()
        self.assertEqual(pr.old_home(), [{'id': 'jump'}, {'id': 'news'}])  # this run's backup
        pr.v1_taken = True
        self.assertIsNone(pr.old_home())
        self._prefs({'dashboard': {'version': 2, 'items': nx.dash_default(), 'layouts': [
            {'name': 'My old Home', 'items': nx.dash_default()}]}, 'dashboard_v1': [{'id': 'x'}]})
        self.assertIsNone(nx.LibraryPrefs().old_home())                 # a layout has the name
        self._prefs({'dashboard': {'version': 2, 'items': nx.dash_default()}})
        self.assertIsNone(nx.LibraryPrefs().old_home())                 # nothing to offer


@unittest.skipIf(nx is None, "PyQt6 not available")
class Restyle(unittest.TestCase):
    """w-111: a look change styles what shows (the Restyler), never the whole app."""

    def setUp(self):
        from unittest import mock
        from tests.patching import set_gui
        saved = (nx.cli_json, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        set_gui('CliQuery', lambda *a, **k: None)
        self.addCleanup(set_gui, 'cli_json', saved[0])
        self.addCleanup(set_gui, 'CliQuery', saved[1])
        p = mock.patch.object(nx.LibraryPrefs, 'save', lambda self: None)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._back_to_default)
        self.win = nx.MainWindow()
        self.addCleanup(self.win.deleteLater)
        self.win.resize(1400, 850)
        self.win.show()
        self.win._on_library({'games': [{'slug': s, 'display_name': s.title(), 'source': 'nexus',
                                         'launcher': f'/{s}'} for s in ('alpha', 'beta')],
                              'disks': []}, '')
        QApplication.processEvents()
        self.base = dict(self.win.prefs.appearance)

    def _back_to_default(self):
        nx.apply_appearance(nx.APPEARANCE_DEFAULT)
        QApplication.instance().setStyleSheet(nx.build_stylesheet())

    def _change(self, **kw):
        self.win.set_appearance({**self.base, **kw}, save=False)
        QApplication.processEvents()

    def test_hidden_pages_wait_for_their_show(self):
        w = self.win
        w.go('home')
        w.page_widget('library')
        QApplication.processEvents()
        before = w.restyler.sheet('desk')
        app_sheet = QApplication.instance().styleSheet()
        self._change(theme='forest')
        after = w.restyler.sheet('desk')
        self.assertNotEqual(before, after)
        self.assertEqual(QApplication.instance().styleSheet(), app_sheet)   # never set again
        self.assertEqual(w.pages['home'].styleSheet(), after)               # visible: at once
        self.assertNotEqual(w.pages['library'].styleSheet(), after)         # hidden: waits
        self.assertNotEqual(w.theatre.styleSheet(), w.restyler.sheet('theatre'))
        w.go('library')
        QApplication.processEvents()
        self.assertEqual(w.pages['library'].styleSheet(), after)
        w.set_theatre(True)
        QApplication.processEvents()
        self.assertEqual(w.theatre.styleSheet(), w.restyler.sheet('theatre'))
        self.assertEqual(w.hints.styleSheet(), w.restyler.sheet('theatre'))
        w.set_theatre(False)
        QApplication.processEvents()
        self.assertEqual(w.hints.styleSheet(), w.restyler.sheet('desk'))

    def _snapshot(self):
        from PyQt6.QtWidgets import QWidget
        out = {}
        containers = self.win.restyler._containers()      # they paint no text
        for x in self.win.findChildren(QWidget):
            if not x.isVisible() or x in containers:
                continue
            pal = QWidget.palette(x)
            f = QWidget.font(x)
            out[x] = (type(x).__name__, x.objectName(), pal.color(x.backgroundRole()).name(),
                      pal.color(x.foregroundRole()).name(), f.pixelSize(), f.family(),
                      x.width(), x.height())
        return out

    def _full_restyle(self):
        """The reference: the look set app-wide, as set_appearance used to."""
        w = self.win
        for r in w.restyler.roots():
            r.setStyleSheet('')
        w.theatre.setStyleSheet(nx.build_stylesheet(nx.THEATRE_SCALE))
        w.hints.setStyleSheet(nx.build_stylesheet(nx.THEATRE_SCALE) if w._theatre else '')
        QApplication.instance().setStyleSheet(nx.build_stylesheet())
        QApplication.processEvents()

    def test_every_page_matches_a_full_restyle(self):
        w = self.win
        w.go('library')
        QApplication.processEvents()
        self._change(theme='dawn', accent='foam', scale=1.2)    # Settings not built yet; Size too
        seen = {}
        for page in ('home', 'library', 'settings', 'system', 'downloads'):
            w.go(page)
            QApplication.processEvents()
            seen[page] = self._snapshot()
        w.set_theatre(True)
        QApplication.processEvents()
        seen['theatre'] = self._snapshot()
        w.set_theatre(False)
        QApplication.processEvents()
        seen['desk again'] = self._snapshot()
        self._full_restyle()
        for page in ('home', 'library', 'settings', 'system', 'downloads'):
            w.go(page)
            QApplication.processEvents()
            ref = self._snapshot()
            diff = [(k, seen[page][k], ref[k]) for k in ref if k in seen[page] and seen[page][k] != ref[k]]
            with self.subTest(page=page):
                self.assertEqual(diff[:3], [])
        w.set_theatre(True)
        QApplication.processEvents()
        ref = self._snapshot()
        diff = [(k, seen['theatre'][k], ref[k]) for k in ref if k in seen['theatre'] and seen['theatre'][k] != ref[k]]
        self.assertEqual(diff[:3], [])
        w.set_theatre(False)
        QApplication.processEvents()
        ref = self._snapshot()
        diff = [(k, seen['desk again'][k], ref[k]) for k in ref
                if k in seen['desk again'] and seen['desk again'][k] != ref[k]]
        self.assertEqual(diff[:3], [])

    def test_splitter_handle_follows_the_theme(self):
        w = self.win
        w.go('library')
        w.details.setVisible(True)
        QApplication.processEvents()
        for theme in ('forest', 'dawn'):
            self._change(theme=theme)
            h = w.split.handle(1)
            img = h.grab().toImage()
            c = img.pixelColor(img.width() // 2, img.height() // 2)
            self.assertEqual(c.name(), QColor(nx.P['border']).name(), theme)

    def test_change_is_quick(self):
        import statistics
        import time
        from tests.test_gui_category import lib_games
        w = self.win
        games = lib_games(3000)
        for i, g in enumerate(games):
            g['appid'] = 100000 + i
        w._on_library({'games': games, 'disks': []}, '')
        for _ in range(5):
            QApplication.processEvents()
        took = []
        for theme in ('campfire', 'forest', 'dawn'):
            t = time.perf_counter()
            self.win.set_appearance({**self.base, 'theme': theme}, save=False)
            took.append(time.perf_counter() - t)
            QApplication.processEvents()
        self.assertLess(statistics.median(took), 1.0, took)
