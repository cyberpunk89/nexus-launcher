"""The Deals search over FitGirl's catalogue (w-146): the scope chip, the
suggestion list under the search, the results view with its rows and the
inline grab panel, and SearchMixin (DealsPage's search half).

The catalogue (`nexus-cli --repack-catalogue --json`) is read once per page
show; every keystroke filters it in memory (deal_model.catalogue_match).
All of it follows Settings > Repack features (DealsPage.set_repacks)."""

import html
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QEvent, QPoint, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QPushButton,
    QVBoxLayout, QWidget)

from nexus import hypervisor

from . import deal_model
from .deals import _pill, DealArt, deal_links
from .deals_home import initials_tile, Row, run_menu
from .theme import G, P, R, SP

SEARCH_H, SEARCH_W = 36, 420          # the title row's search (board: 36 high)
ROW_TILE_W, ROW_TILE_H = 64, 36       # a result row's tile (board: 64)
SUGGEST_ROWS = 5                      # per group in the suggestion list
FITGIRL = 'https://fitgirl-repacks.site/'
MARKS = {'wishlist': "In wishlist", 'nexus': "In Nexus", 'steam': "Owned on Steam"}
MARKS_W = 2 * ROW_TILE_W + SP['8']    # the marks column


def match_of(row: dict) -> dict:
    """The repack 'match' a catalogue row stands for (as _deal_repack takes it)."""
    return {'slug': row['slug'], 'title': row.get('title'), 'hv': row.get('hv'),
            'url': f"{FITGIRL}{row['slug']}/", 'confidence': 'exact'}


def item_of(row: dict) -> dict:
    """The game a catalogue row's Grab is for: no appid unless the row has one."""
    item = {'title': row.get('title') or row.get('slug')}
    if row.get('appid') is not None:
        item['appid'] = row['appid']
    return item


def post_day(iso: str) -> str:
    try:
        d = datetime.strptime((iso or '')[:10], '%Y-%m-%d').date()
    except ValueError:
        return ''
    return f"{d.day} {d:%b %Y}"


class ScopeChip(QPushButton):
    """"Wishlist + FitGirl ▾": what the search looks through."""
    changed = pyqtSignal(str)
    SCOPES = (('all', "Wishlist + FitGirl"), ('wishlist', "Wishlist only"))

    def __init__(self, scope: str = 'all'):
        super().__init__()
        self.setObjectName("Chip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("What the search looks through")
        set_glyph_icon(self, G['chevron_down'], 12)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)     # the ▾ after the text
        self._scope = scope if scope in dict(self.SCOPES) else 'all'
        self.setText(dict(self.SCOPES)[self._scope])
        self.clicked.connect(self._menu)

    def scope(self) -> str:
        return self._scope

    def set_scope(self, key: str):
        if key in dict(self.SCOPES) and key != self._scope:
            self._scope = key
            self.setText(dict(self.SCOPES)[key])
            self.changed.emit(key)

    def _menu(self):
        run_menu(self, "Search", [(text, G['check'] if k == self._scope else G['search'],
                                   lambda k=k: self.set_scope(k)) for k, text in self.SCOPES],
                 self.mapToGlobal(QPoint(0, self.height())))


class Suggestions(QFrame):
    """The list under the search: your wishlist first, then "On FitGirl,
    not on your wishlist", then "See all N". A child of the page, so focus
    stays in the field while you type; Down enters it, Up from the top
    goes back. picked(kind, payload): 'wish' (item), 'fitgirl' (row),
    'all' (None)."""
    picked = pyqtSignal(str, object)

    def __init__(self, page: QWidget):
        super().__init__(page)
        self.setObjectName("Popover")
        self.rows: list[Row] = []
        self.kinds: list[tuple[str, str]] = []           # (kind, text) per row, for tests
        self.field: QWidget | None = None
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(SP['2'], SP['2'], SP['2'], SP['2'])
        self.lay.setSpacing(SP['0_5'])
        self.hide()

    def show_for(self, field: QWidget, wish: list, fitgirl: list, total: int, note: str):
        self.field = field
        clear_layout(self.lay)
        self.rows, self.kinds = [], []
        if wish:
            self.lay.addWidget(label("YOUR WISHLIST", "SectionKicker"))
            for g in wish[:SUGGEST_ROWS]:
                price = (g.get('price') or {}).get('currentStr') or ''
                self._add('wish', g, g.get('title') or '', price)
        if fitgirl:
            self.lay.addWidget(label("ON FITGIRL, NOT ON YOUR WISHLIST", "SectionKicker"))
            for r in fitgirl[:SUGGEST_ROWS]:
                marks = [MARKS[m] for m in r.get('marks') or () if m in MARKS]
                self._add('fitgirl', r, r.get('title') or '',
                          " · ".join([r.get('tail') or '', *marks]).strip(' ·'))
        if total:
            self._add('all', None, f"See all {total} FitGirl results", '')
        if note:
            self.lay.addWidget(label(note, "Muted", wrap=True))
        if not self.rows and not note:
            self.close()
            return
        page = self.parentWidget()
        tl = field.mapTo(page, QPoint(0, field.height() + SP['1']))
        w = max(field.width(), SEARCH_W)
        x = min(tl.x(), page.width() - w - SP['2'])
        self.setFixedWidth(w)
        self.adjustSize()
        self.move(max(0, x), tl.y())
        self.raise_()
        self.show()
        for r in self.rows:                       # added to a shown list: show them now
            r.show()

    def _add(self, kind: str, payload, title: str, sub: str):
        r = Row()
        lay = QHBoxLayout(r)
        lay.setContentsMargins(SP['2'], SP['1_5'], SP['2'], SP['1_5'])
        t = label(title, "NewsTitle" if kind != 'all' else None)
        if kind == 'all':
            t.setStyleSheet(f"color: {P['accent']};")
        lay.addWidget(t)
        lay.addStretch()
        if sub:
            lay.addWidget(label(sub, "Muted"))
        r.mute_children()
        r.clicked.connect(lambda k=kind, p=payload: self._pick(k, p))
        r.nav_jump = lambda key, i=len(self.rows): self._jump(i, key)
        self.rows.append(r)
        self.kinds.append((kind, title))
        self.lay.addWidget(r)

    def _pick(self, kind, payload):
        self.close()
        self.picked.emit(kind, payload)

    def _jump(self, i: int, key):
        if key == Qt.Key.Key_Up:
            return self.rows[i - 1] if i > 0 else self.field
        if key == Qt.Key.Key_Down:
            return self.rows[min(i + 1, len(self.rows) - 1)]
        return self.rows[i]                        # left/right stay in the list

    def enter(self) -> bool:
        if self.isVisible() and self.rows:
            self.rows[0].setFocus(Qt.FocusReason.TabFocusReason)
            return True
        return False

    def close(self):
        self.hide()


class SpaceBar(QWidget):
    """How much of the free space a grab needs (filled), on a track."""

    def __init__(self, frac: float):
        super().__init__()
        self.frac = max(0.0, min(1.0, frac))
        self.setFixedHeight(SP['1_5'])
        self.setMaximumWidth(SEARCH_W)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        rad = r.height() / 2
        pt.setBrush(QColor(P['surface_2']))
        pt.drawRoundedRect(r, rad, rad)
        if self.frac > 0:
            pt.setBrush(QColor(P['danger'] if self.frac >= 1 else P['accent']))
            pt.drawRoundedRect(QRectF(r.x(), r.y(), max(r.height(), r.width() * self.frac),
                                      r.height()), rad, rad)
        pt.end()


class CatalogueRow(Row):
    """One FitGirl game: tile, title (+HV), edition and older posts, price
    (wishlist games), post date, your marks, Grab. Activated, it opens the
    grab panel inline: sizes, a space bar, the fit line and the choices."""
    grab = pyqtSignal(str, dict, dict)            # 'grab'|'download_auto'|'download'|'page'
    expand = pyqtSignal(object)

    def __init__(self, row: dict, wish: dict | None, base):
        super().__init__()
        self.row, self.wish = row, wish
        self.title = row.get('title') or row['slug']
        self.panel: QWidget | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        outer.setSpacing(SP['2'])
        top = QHBoxLayout()
        top.setSpacing(SP['3'])
        if wish and wish.get('headerImage'):
            tile = DealArt(ROW_TILE_W, ROW_TILE_H, R['sm'])
            tile.round_all = True
            tile.set_url(wish.get('headerImage'))
        else:
            tile = initials_tile(self.title, ROW_TILE_W, ROW_TILE_H)
        top.addWidget(tile)
        col = QVBoxLayout()
        col.setSpacing(0)
        trow = QHBoxLayout()
        trow.setSpacing(SP['1_5'])
        name = label(self.title, "NewsTitle")
        name.setTextFormat(Qt.TextFormat.PlainText)
        trow.addWidget(name)
        if row.get('hv'):
            hv = _pill("HV", 'warning')
            hv.setToolTip(hypervisor.note('hv'))
            trow.addWidget(hv)
        trow.addStretch()
        col.addLayout(trow)
        older = row.get('older') or 0
        sub = " · ".join(x for x in (html.escape(row.get('tail') or ''),
                                     f"<b>+{older} older post{'s' if older > 1 else ''}</b>"
                                     if older else '') if x)
        s = label(sub, "Muted")
        s.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(s)
        top.addLayout(col, 1)
        price = (wish or {}).get('price') or {}
        if price.get('currentStr'):
            p = label(f"<b>{html.escape(price['currentStr'])}</b>"
                      + (f" <span style='color:{P['muted']}'>-{price['cutPct']}%</span>"
                         if price.get('cutPct') else ''), None)
            p.setTextFormat(Qt.TextFormat.RichText)
            top.addWidget(p)
        date = label(post_day(row.get('date')), "Muted")
        date.setMinimumWidth(ROW_TILE_W + SP['8'])
        top.addWidget(date)
        marks = QHBoxLayout()                     # a fixed column, so the dates line up
        marks.setSpacing(SP['1'])
        marks.addStretch()
        for m in row.get('marks') or ():
            if m in MARKS:
                marks.addWidget(_pill(MARKS[m], 'accent' if m == 'wishlist' else 'subtext'))
        box = QWidget()
        box.setLayout(marks)
        box.setFixedWidth(MARKS_W)
        top.addWidget(box)
        self.btn = QPushButton("Grab")
        self.btn.setObjectName("Chip")
        self.btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)          # the row's A / menu have it
        self.btn.clicked.connect(self._grab)
        top.addWidget(self.btn)
        outer.addLayout(top)
        self.mute_children()
        self.clicked.connect(self._toggle)
        self.menu.connect(self._menu)
        self.links = deal_links(wish, 'deal', base, set())[2] if wish else []

    def _grab(self):
        self.grab.emit('grab', item_of(self.row), match_of(self.row))

    def _toggle(self):
        if self.panel is not None:
            self.collapse()
        else:
            self.expand.emit(self)

    def _menu(self, pos):
        items = [("Grab", G['download'], self._grab),
                 ("Open the repack page", G['external'],
                  lambda: self.grab.emit('page', item_of(self.row), match_of(self.row)))]
        run_menu(self, self.title, items, pos)

    def collapse(self):
        if self.panel is not None:
            self.panel.deleteLater()
            self.panel = None
        self.setProperty('open', False)
        self.style().polish(self)

    def set_page(self, page: dict | None, err: str = ''):
        """The repack page arrived (or didn't): the grab panel."""
        self.collapse()
        self.setProperty('open', True)
        self.style().polish(self)
        self.panel = QWidget()
        lay = QHBoxLayout(self.panel)
        lay.setContentsMargins(ROW_TILE_W + SP['3'], 0, 0, 0)
        lay.setSpacing(SP['2'])
        info = QVBoxLayout()
        info.setSpacing(SP['1'])
        if page is None or page.get('error'):
            info.addWidget(label("Couldn't read the repack page — "
                                 f"{(page or {}).get('error') or err or 'no answer'}", "Muted",
                                 wrap=True))
        else:
            sizes = label(f"<b>{html.escape(page.get('repack_size') or '?')}</b> download · "
                          f"<b>{html.escape(page.get('original_size') or '?')}</b> installed",
                          None)
            sizes.setTextFormat(Qt.TextFormat.RichText)
            info.addWidget(sizes)
            need = page.get('original_bytes') or page.get('size_bytes') or 0
            free = page.get('install_free') or page.get('free_bytes')
            if need and free:
                info.addWidget(SpaceBar(need / free))
            fit = grab_fit_line(page)
            if fit:
                info.addWidget(label(fit, "Muted", wrap=True))
        lay.addLayout(info, 1)
        ok = page is not None and not page.get('error') and page.get('magnets') \
            and page.get('client')
        if ok:
            go = QPushButton("Grab — install when it's done")
            go.setObjectName("Primary")
            go.clicked.connect(lambda: self.grab.emit('download_auto', item_of(self.row),
                                                      match_of(self.row)))
            only = QPushButton("Download only")
            only.clicked.connect(lambda: self.grab.emit('download', item_of(self.row),
                                                        match_of(self.row)))
            lay.addWidget(go, 0, Qt.AlignmentFlag.AlignBottom)
            lay.addWidget(only, 0, Qt.AlignmentFlag.AlignBottom)
        elif page is not None and not page.get('client'):
            info.addWidget(label("Set up a torrent client in Downloads first.", "Muted"))
        opn = QPushButton("Open page")
        opn.setObjectName("Ghost")
        opn.clicked.connect(lambda: self.grab.emit('page', item_of(self.row), match_of(self.row)))
        lay.addWidget(opn, 0, Qt.AlignmentFlag.AlignBottom)
        self.layout().addWidget(self.panel)


class ResultsView(QWidget):
    """FitGirl results: Hide what I have, Hide HV and the sort; then "On
    your wishlist" and "On FitGirl, not on your wishlist" groups of rows."""
    back = pyqtSignal()
    repack = pyqtSignal(str, dict, dict)
    expand = pyqtSignal(object)
    changed = pyqtSignal()
    SORTS = (('new', "Sort: Newest post"), ('name', "Sort: Name"))

    def __init__(self):
        super().__init__()
        self.rows: list[CatalogueRow] = []
        self.headings: list[str] = []
        self.total = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SP['2'])
        bar = QHBoxLayout()
        bar.setSpacing(SP['1_5'])
        self.hide_have = self._chip(bar, "Hide what I have")
        self.hide_hv = self._chip(bar, "Hide HV")
        bar.addStretch()
        self.sort = QComboBox()
        for key, text in self.SORTS:
            self.sort.addItem(text, key)
        self.sort.activated.connect(lambda _i: self.changed.emit())
        bar.addWidget(self.sort)
        outer.addLayout(bar)
        self.lay = QVBoxLayout()
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(SP['0_5'])
        outer.addLayout(self.lay)

    def _chip(self, bar, text) -> QPushButton:
        c = QPushButton(text)
        c.setObjectName("Chip")
        c.setCheckable(True)
        c.toggled.connect(lambda _on: self.changed.emit())
        bar.addWidget(c)
        return c

    def options(self) -> dict:
        return {'hide_have': self.hide_have.isChecked(), 'hide_hv': self.hide_hv.isChecked(),
                'sort': self.sort.currentData() or 'new'}

    def set_rows(self, match: dict, wish_by: dict, base, query: str, note: str = ''):
        clear_layout(self.lay)
        self.rows, self.headings = [], []
        self.total = match.get('total', 0)
        mine = [r for r in match['rows'] if 'wishlist' in (r.get('marks') or ())]
        rest = [r for r in match['rows'] if 'wishlist' not in (r.get('marks') or ())]
        if note:
            self.lay.addWidget(label(note, "Muted", wrap=True))
        for title, rows in (("ON YOUR WISHLIST", mine),
                            ("ON FITGIRL, NOT ON YOUR WISHLIST", rest)):
            if not rows:
                continue
            head = f"{title}  {len(rows)}"
            self.headings.append(head)
            h = label(head, "SectionKicker")
            h.setContentsMargins(SP['3'], SP['2'], 0, 0)
            self.lay.addWidget(h)
            for r in rows:
                row = CatalogueRow(r, wish_by.get(str(r.get('appid'))), base)
                row.grab.connect(self.repack)
                row.expand.connect(self._expand)
                self.rows.append(row)
                self.lay.addWidget(row)
        if not match['rows'] and not note:
            self.lay.addWidget(EmptyState(G['search'], f"No FitGirl game matches “{query}”"
                                          if query else "No FitGirl game to show"))
        elif self.total > len(match['rows']):
            self.lay.addWidget(label(f"Showing {len(match['rows'])} of {self.total}, refine "
                                     "the search.", "Muted"))

    def _expand(self, row):
        for r in self.rows:
            if r is not row:
                r.collapse()
        self.expand.emit(row)


class SearchMixin:
    """DealsPage's search: the scope, suggestions, the results view and
    the catalogue they read (loaded once per page show)."""
    catalogue: dict | None = None
    _cat_busy = False
    _filling = False
    _before = 'home'

    def load_catalogue(self):
        """Ask for the catalogue (repack features on): once per page show."""
        if not self._repacks_on or self._cat_busy:
            return
        self._cat_busy = True                     # first: cli_json may answer right away
        cli_json(['--repack-catalogue', '--json'], self._catalogue_done, 60000)

    def _catalogue_done(self, data, _err):
        self._cat_busy = False
        if data is not None and isinstance(data.get('rows'), list):
            self.catalogue = data
        try:
            if self._filling:                     # answered inside a fill: after it
                QTimer.singleShot(0, self._catalogue_shown)
            else:
                self._catalogue_shown()
        except RuntimeError:                      # the page is gone
            pass

    def _catalogue_shown(self):
        self._fill_tabs()
        if self.view() == 'results':
            self._fill_results()
        elif self.view() == 'home' and self._data():
            self._feed_rail()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.load_catalogue()

    def _catalogue_scope(self) -> bool:
        return self._repacks_on and self.catalogue is not None and self.scope.scope() == 'all'

    def _scope_changed(self, _key):
        self._save_state()
        self._fill_tabs()
        if self.search.text():
            self._search_now()

    def _search_now(self):
        """The 150 ms after a keystroke: filter what is on show, in memory."""
        view = self.view()
        if view == 'results':
            self._fill_results()
            self._sync_tags()
        elif view == 'home':
            self._suggest()
        else:
            self._fill()

    def _wish_matches(self, q: str) -> list[dict]:
        q = q.casefold()
        return [g for g in self._data().get('wishlist') or []
                if q in (g.get('title') or '').casefold()]

    def _suggest(self):
        q = self.search.text().strip()
        if not q:
            self.suggest.close()
            return
        wish, rest, total, note = self._wish_matches(q), [], 0, ''
        if self._catalogue_scope():
            cat = self.catalogue
            if cat.get('building'):
                note = self._building_note()
            else:
                m = deal_model.catalogue_match(cat.get('rows') or [], q)
                rest = [r for r in m['rows'] if 'wishlist' not in (r.get('marks') or ())]
                total = m['total']
        self.suggest.show_for(self.search, wish, rest, total, note)

    def _building_note(self) -> str:
        cat = self.catalogue or {}
        n, of = cat.get('page'), cat.get('pages')
        return ("The FitGirl list is still being built"
                + (f", {n} of {of} pages." if n and of else "."))

    def _suggested(self, kind, payload):
        if kind in ('fitgirl', 'all'):
            self.open_results(self.search.text().strip() if kind == 'all'
                              else payload.get('title') or '')
        else:                                     # a wishlist game: the grid, searched
            self._view, self._filter = 'grid', 'all'
            self._query = (payload.get('title') or '').casefold()
            self._fill_tabs()
            self._fill()

    def open_results(self, query: str = '') -> bool:
        """The FitGirl results for `query` (False with repack features off
        or no catalogue)."""
        if not self._repacks_on or self.catalogue is None:
            return False
        if self._view != 'results':
            self._before = self._view if self._section == 'deals' else 'home'
        self._section, self._view = 'deals', 'results'
        self.search.blockSignals(True)
        self.search.setText(query)
        self.search.blockSignals(False)
        self._query = query.casefold()
        self.suggest.close()
        self._fill_tabs()
        self._fill()
        self.scroll.verticalScrollBar().setValue(0)
        return True

    def _fill_results(self):
        cat = self.catalogue or {}
        q = self.search.text().strip()
        note = self._building_note() if cat.get('building') else ''
        m = deal_model.catalogue_match(cat.get('rows') or [], q, **self.results.options())
        wish_by = {str(g.get('appid')): g for g in self._data().get('wishlist') or []}
        self.results.set_rows(m, wish_by, self._base(), q, note)
        self._sync_tags()

    def _expand_row(self, row):
        """A result row opens: its sizes and free space from the repack page."""
        slug = row.row['slug']
        cli_json(['--repack-page', slug, '--json'],
                 lambda data, err, s=slug: self._page_read(s, data, err), 40000)

    def _page_read(self, slug, data, err):
        try:
            row = next((r for r in self.results.rows if r.row['slug'] == slug), None)
            if row is not None:
                row.set_page(data, err or '')
        except RuntimeError:                      # rebuilt meanwhile
            pass

    def _search_event(self, ev) -> bool:
        """The search field's keys: Down into the suggestions, Return to
        the results (or the grid), Escape closes the list first."""
        t = ev.type()
        if t == QEvent.Type.KeyPress:
            if ev.key() == Qt.Key.Key_Down and self.suggest.enter():
                return True
            if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.search.text().strip():
                if self.view() == 'home':
                    if not self.open_results(self.search.text().strip()):
                        self._suggested('wish', {'title': self.search.text().strip()})
                    return True
        elif t == QEvent.Type.FocusOut:
            QTimer.singleShot(0, self._fold_suggest)
        return False

    def _fold_suggest(self):
        try:
            f = QApplication.focusWidget()
            if f is not self.search and not self.suggest.isAncestorOf(f):
                self.suggest.close()
        except RuntimeError:
            pass

    def search_jump(self, key):
        """SpatialNav's neighbour for the field: Down into the suggestions."""
        if key == Qt.Key.Key_Down and self.suggest.isVisible() and self.suggest.rows:
            return self.suggest.rows[0]
        return None

    def escape(self) -> bool:
        """Escape / B on Deals: the suggestions, then back to the home."""
        if self.suggest.isVisible():
            self.suggest.close()
            self.search.setFocus()
            return True
        return self.back()



def late_imports():
    """Sibling imports, run by deals.py once DealsPage exists: they import
    the window, which imports DealsPage back."""
    global grab_fit_line, cli_json, clear_layout, EmptyState, label, set_glyph_icon
    from .downloads import grab_fit_line
    from .jobs import cli_json
    from .widgets import clear_layout, EmptyState, label, set_glyph_icon
