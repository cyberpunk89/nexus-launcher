"""Theatre's Discover tab (w-62): news and deals from the couch.

Shelves of big cards — News for your games, On sale from your wishlist,
Coming soon, Latest news — a reader for a story, and a setup card when
Game-db isn't on. Theatre fetches nothing itself: the window refreshes the
News and Deals pages and hands their data to `TheatreDiscover.set_data`."""

import html

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QPushButton, QScrollArea,
    QTextBrowser, QVBoxLayout, QWidget)

from nexus import gamedb

from .common import alive
from .fmt import fmt_relative
from .theme import FS, G, R, SP

MAX_CARDS = 12                  # per shelf: a couch isn't a feed reader


class TheatreCard(QFrame):
    """One story or deal: art on top, a line of facts, a title. A opens it
    (the reader, the deal sheet), X its options."""
    activated = pyqtSignal(object)
    options = pyqtSignal(object)
    W = 380

    def __init__(self, kind: str, item: dict, images=None, owned: bool = False):
        super().__init__()
        self.kind, self.item = kind, item
        self.key = (item.get('link') or item.get('url') or item.get('title')
                    if kind == 'news' else f"{kind}:{item.get('appid') or item.get('title')}")
        self.setObjectName("TheatreCard")
        self.setFixedWidth(self.W)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, SP['4'])
        lay.setSpacing(SP['2'])
        if kind == 'news':
            from .newspage import NewsArt
            self.art = NewsArt(item, self.W - 2, int((self.W - 2) * 215 / 460), R['card'] - 1,
                               images, top_only=True)       # as tall as a deal's art
            self.art.load()
            facts = " · ".join(x for x in (item.get('source'), fmt_relative(item.get('date')))
                               if x)
        else:
            from .deals import DealArt
            self.art = DealArt(self.W - 2, int((self.W - 2) * 215 / 460), R['card'] - 1)
            price = item.get('price') or {}
            if price.get('cutPct') and kind == 'deal':
                self.art.top_left.append((f"-{int(price['cutPct'])}%", 'accent'))
            if owned:
                self.art.top_right.append(("Owned", 'glass'))
            self.art.set_url(item.get('headerImage'))
            if kind == 'upcoming':
                facts = gamedb.release_label(item)
            else:
                facts = price.get('currentStr') or ''
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        body = QVBoxLayout()
        body.setContentsMargins(SP['4'], 0, SP['4'], 0)
        body.setSpacing(SP['1'])
        self.facts = plain_label(facts, "TheatreSub")
        body.addWidget(self.facts)
        self.title = plain_label(item.get('title') or "", "TheatreMeta")
        self.title.setWordWrap(True)
        self.title.setFixedHeight(int(FS['lg'] * 1.5 * 1.4 * 2) + 4)    # two lines at Theatre's size
        self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        body.addWidget(self.title)
        lay.addLayout(body)

    def height_hint(self) -> int:
        return self.sizeHint().height()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self)
        elif ev.key() == Qt.Key.Key_Menu:
            self.options.emit(self)
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self)
        super().mouseReleaseEvent(ev)


class TheatreSetupCard(QFrame):
    """Without Game-db: why there are no deals, and the way to turn them on
    (which leaves Theatre: Settings is a desktop page). X hides it."""
    activated = pyqtSignal()
    hide_asked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("SetupCard")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['6'], SP['5'], SP['6'], SP['5'])
        lay.setSpacing(SP['1'])
        lay.addWidget(plain_label("Sales and coming soon", "TheatreMeta"))
        lay.addWidget(plain_label("Connect Game-db to see your wishlist's sales and upcoming "
                                  "releases here.", "TheatreSub"))
        lay.addWidget(plain_label("A  Open Settings · leaves Theatre      X  Hide this",
                                  "TheatreSub"))

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit()
        elif ev.key() == Qt.Key.Key_Menu:
            self.hide_asked.emit()
        else:
            super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mouseReleaseEvent(ev)


class TheatreDiscover(QScrollArea):
    """The tab: shelves of cards under their titles."""
    news_opened = pyqtSignal(list, int, object)       # the shelf's stories, which, its card
    deal_asked = pyqtSignal(dict, str)                # a deal's item, its kind
    news_options = pyqtSignal(dict)
    open_settings = pyqtSignal()
    hide_gamedb = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.col = QVBoxLayout(host)
        self.col.setContentsMargins(0, SP['2'], 0, SP['8'])
        self.col.setSpacing(SP['6'])
        self.setWidget(host)
        self.shelves: list = []
        self.setup: TheatreSetupCard | None = None
        self.empty = None
        self.cards: list[TheatreCard] = []
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)     # somewhere to land while it loads

    def _clear(self):
        self.cards = []
        while self.col.count():
            it = self.col.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()
                it.widget().deleteLater()
        self.setup = self.empty = None
        self.shelves = []

    def set_data(self, news: list[dict], res: dict, ctx: dict):
        """`news`: the News page's stories; `res`: the Deals page's result;
        `ctx`: owned_appids (set), news_on, gamedb_on, repacks_on, hide_setup,
        images (a news_image callable), loading (bool), error (str)."""
        f = QApplication.focusWidget()
        focus_key = next((c.key for c in self.cards if c is f), None) \
            if f is not None else None
        had_setup = self.setup is not None and f is self.setup
        self._clear()
        owned = ctx.get('owned_appids') or set()
        images = ctx.get('images')
        shelves: list[tuple[str, str, list[dict]]] = []
        if ctx.get('news_on'):
            mine = [n for n in news if n.get('kind') == 'steam' and n.get('appid') in owned]
            rest = [n for n in news if n not in mine]
            shelves.append(("News for your games", 'news', mine))
        summary = (res or {}).get('summary') or {}
        if ctx.get('gamedb_on'):
            wish = [g for g in summary.get('wishlist') or []
                    if isinstance(g, dict) and (g.get('price') or {}).get('cutPct')
                    and not g.get('comingSoon')]
            wish.sort(key=lambda g: -(g['price'].get('cutPct') or 0))
            shelves.append(("On sale from your wishlist", 'deal', wish))
            shelves.append(("Coming soon", 'upcoming', gamedb.upcoming(summary)))
        if ctx.get('news_on'):
            shelves.append(("Latest news", 'news', rest))
        any_cards = False
        for title, kind, items in shelves:
            items = items[:MAX_CARDS]
            if not items:
                continue
            any_cards = True
            from .library import ShelfRow
            cards = []
            for i, it in enumerate(items):
                c = TheatreCard(kind, it, images, owned=it.get('appid') in owned)
                if kind == 'news':
                    c.activated.connect(lambda card, items=items, i=i:
                                        self.news_opened.emit(items, i, card))
                    c.options.connect(lambda card: self.news_options.emit(card.item))
                else:
                    c.activated.connect(lambda card, k=kind: self.deal_asked.emit(card.item, k))
                    c.options.connect(lambda card, k=kind: self.deal_asked.emit(card.item, k))
                cards.append(c)
            h = max(c.sizeHint().height() for c in cards) + SP['2']
            row = ShelfRow(title, TheatreCard.W, 'wide', tile_h=h)
            for c in cards:
                c.setFixedHeight(h)
                row.add(c)
                self.cards.append(c)
            row.finish()
            self.col.addWidget(row)
        if not ctx.get('gamedb_on') and not ctx.get('hide_setup'):
            self.setup = TheatreSetupCard()
            self.setup.activated.connect(self.open_settings)
            self.setup.hide_asked.connect(self.hide_gamedb)
            self.col.addWidget(self.setup)
        if not any_cards:
            from .widgets import EmptyState
            text = ("Loading…" if ctx.get('loading') else ctx.get('error')
                    or "Nothing to show yet")
            self.empty = EmptyState(G['news'], "Discover", text)
            self.col.insertWidget(0, self.empty)
        self.col.addStretch()
        if focus_key is not None:
            card = next((c for c in self.cards if c.key == focus_key), None)
            if card is not None:
                QTimer.singleShot(0, lambda c=card: alive(c) and focus_and_reveal(c))
        elif had_setup and self.setup is not None:
            QTimer.singleShot(0, lambda: self.setup is not None and alive(self.setup)
                              and focus_and_reveal(self.setup))

    def first_focus(self):
        return next(iter(self.cards), None) or self.setup or self


class TheatreReader(QFrame):
    """One story, full height on the right over a dim page. LB/RB step
    through the shelf it came from, X offers to open the article, A shows the
    game (when it is one you have), B closes."""
    show_game = pyqtSignal(object)         # the story's appid
    options = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("TheatreReaderScrim")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch()
        self.panel = QFrame()
        self.panel.setObjectName("TheatreReaderPanel")
        self.panel.setFixedWidth(760)
        outer.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(SP['8'], SP['8'], SP['8'], SP['6'])
        lay.setSpacing(SP['4'])
        self.title = plain_label("", "TheatreBig")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.meta = plain_label("", "TheatreSub")
        lay.addWidget(self.meta)
        self.body = QTextBrowser()
        self.body.setObjectName("TheatreReader")
        self.body.setOpenLinks(False)
        self.body.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        lay.addWidget(self.body, 1)
        self.btn_game = QPushButton()
        self.btn_game.setObjectName("PlayBig")
        self.btn_game.setFixedHeight(80)
        self.btn_game.clicked.connect(lambda: self.show_game.emit(self.item.get('appid')))
        lay.addWidget(self.btn_game)
        self.items: list[dict] = []
        self.index = 0
        self.item: dict = {}
        self._origin = None
        parent.installEventFilter(self)
        self.hide()

    def open(self, items: list[dict], index: int, origin, game_name=None):
        f = QApplication.focusWidget()
        self._origin = origin if origin is not None else f
        self.items, self.index = items, index
        self.game_name = game_name or (lambda item: None)
        self._fill()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._focus()

    def _fill(self):
        it = self.items[self.index]
        self.item = it
        self.title.setText(it.get('title') or "")
        self.meta.setText(" · ".join(x for x in (it.get('source'), fmt_relative(it.get('date')),
                                                 f"{self.index + 1} of {len(self.items)}") if x))
        text = it.get('summary') or "No summary for this story. X opens the full article."
        self.body.setHtml(f"<p>{html.escape(text)}</p>")
        name = self.game_name(it)
        self.btn_game.setVisible(bool(name))
        if name:
            self.btn_game.setText(f"Show {name}")

    def _focus(self):
        QTimer.singleShot(0, self._focus_now)

    def _focus_now(self):
        if self.isVisible():
            (self.btn_game if self.btn_game.isVisible() else self.body).setFocus()

    def step(self, d: int):
        if self.items:
            self.index = (self.index + d) % len(self.items)
            self._fill()
            self._focus()

    def close_reader(self):
        if not self.isVisible():
            return
        self.hide()
        o, self._origin = self._origin, None
        if o is not None and alive(o) and o.isVisible():
            focus_and_reveal(o)
        self.closed.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Back):
            self.close_reader()
        elif ev.key() == Qt.Key.Key_Menu:
            self.options.emit(self.item)
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        if not self.panel.geometry().contains(ev.position().toPoint()):
            self.close_reader()
        super().mousePressEvent(ev)

    def eventFilter(self, obj, ev):
        from PyQt6.QtCore import QEvent
        if ev.type() == QEvent.Type.Resize and obj is self.parentWidget() and self.isVisible():
            self.setGeometry(obj.rect())
        return False


from .input import focus_and_reveal  # noqa: E402
from .widgets import plain_label  # noqa: E402
