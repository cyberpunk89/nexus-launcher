"""The game page's stat tiles (w-132): Players, Critics, How long to beat, On
Linux, PC features, Details and the site links, in a fixed order.

overview_model() is plain data (no Qt, no OS call: `linux` comes from the
caller); OverviewTiles shows it. Colours are palette role names, resolved
when painted or styled, so a theme change applies."""

from PyQt6.QtCore import QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (QBoxLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget)

from nexus import facts

from .gameinfo import PCGW_COLORS, PCGW_WORDS, TIER_COLORS
from .theme import _mix, FONTS, G, is_light, P, R, scaled, SP
from .widgets import clear_layout, FlowLayout, set_icon_label

ORDER = ('players', 'critics', 'length', 'linux', 'pc', 'details', 'links')
TITLES = {'players': "Players", 'critics': "Critics", 'length': "How long to beat",
          'linux': "On Linux", 'pc': "PC features", 'details': "Details", 'links': ""}
# Tiles that share a row when the panel is wide enough.
PAIRS = (('players', 'critics'), ('linux', 'pc'))
MODE_LABELS = {'single': "Single", 'coop': "Co-op", 'online': "Online", 'local': "Couch"}
PC_MAX = 5
PAIR_MIN_W = 460 - 2 * SP['5']       # panel width under which a pair stacks
SITES = (("Steam store", 'https://store.steampowered.com/'),
         ("ProtonDB", 'https://www.protondb.com/'),
         ("PCGamingWiki", 'https://www.pcgamingwiki.com/'),
         ("HowLongToBeat", 'https://howlongtobeat.com/'))


def score_tone(v) -> str:
    """The palette role for a 0-100 score: good from 66, middling from 33."""
    v = float(v or 0)
    return 'success' if v >= 66 else 'warning' if v >= 33 else 'danger'


def critic_word(v) -> str:
    """A word to go with the colour (never colour alone)."""
    return {'success': "Good", 'warning': "Mixed", 'danger': "Poor"}[score_tone(v)]


def count_text(n) -> str:
    """712345 -> '712k', 1834 -> '1.8k', 95 -> '95'."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace('.0M', 'M')
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace('.0k', 'k')
    return str(n)


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def desc_tone(desc: str):
    """The palette role for one of Steam's review words, None when unknown."""
    d = str(desc or '').lower()
    if 'positive' in d:
        return 'success'
    if 'negative' in d:
        return 'danger'
    return 'warning' if 'mixed' in d else None


def _players(rv: dict):
    pct = _number(rv.get('percent'))
    desc = str(rv.get('desc') or '')
    if pct is None and not desc:
        return None
    tone = desc_tone(desc) or (score_tone(pct) if pct is not None else 'muted')
    return {'value': None if pct is None else int(pct), 'word': desc,
            'of': int(rv.get('total') or 0), 'tone': tone}


def _critics(st: dict, rec: dict) -> list:
    score = _number((st.get('metacritic') or {}).get('score')) or _number(rec.get('metacritic'))
    if not score:
        return []
    v = int(score)
    return [{'name': "Metacritic", 'value': v, 'word': critic_word(v), 'tone': score_tone(v)}]


def _length(rec: dict, game: dict):
    ln = rec.get('length') or {}
    if not ln.get('main'):
        return None
    mins = (game or {}).get('playtime_minutes') or 0
    return {'main': ln['main'], 'plus': ln.get('plus'), 'complete': ln.get('complete'),
            'you': mins / 60 if mins > 0 else None, 'source': ln.get('source'),
            'count': ln.get('count')}


def _linux(tips: dict, game: dict) -> list:
    items = []
    pdb = tips.get('protondb') or {}
    tier = str(pdb.get('tier') or (game or {}).get('protondb') or '')
    if tier:
        trend = pdb.get('trending') if pdb.get('trending') not in (None, pdb.get('tier')) else None
        items.append({'tier': tier, 'reports': int(pdb['total']) if pdb.get('total') else None,
                      'trending': trend})
    deck = (tips.get('deck') or {}).get('deck')
    if deck:
        tone = {'Verified': 'success', 'Playable': 'warning', 'Unsupported': 'danger'}.get(
            deck, 'muted')
        items.append({'chip': 'deck', 'text': f"Deck {deck}", 'tone': tone})
    ac = tips.get('anticheat') or {}
    if ac.get('status'):
        what = ', '.join(ac.get('anticheats') or []) or "Anti-cheat"
        word = {'Supported': "works", 'Running': "works", 'Broken': "broken",
                'Denied': "blocked", 'Planned': "planned"}.get(ac['status'], ac['status'].lower())
        tone = ('danger' if ac['status'] in ('Broken', 'Denied')
                else 'success' if word == "works" else 'muted')
        items.append({'chip': 'anticheat', 'text': f"{what} {word}", 'tone': tone})
    return items


def _pc(tips: dict) -> list:
    sup = (tips.get('pcgw') or {}).get('support') or {}
    out = []
    for name, v in list(sup.items())[:PC_MAX]:
        tone = PCGW_COLORS.get(v, 'muted')
        tone = 'muted' if tone == 'danger' else tone      # "no" is quiet, not an alarm
        out.append({'name': str(name), 'value': v, 'word': PCGW_WORDS.get(v, str(v)),
                    'tone': tone,
                    'glyph': {'success': 'check', 'warning': 'wrench'}.get(tone, 'close')})
    return out


def _details(rec: dict) -> list:
    items = []
    release = rec.get('releaseText') or rec.get('release') or rec.get('year')
    if release:
        items.append({'release': str(release)})
    if rec.get('modes') is not None:
        items.append({'modes': [(m, MODE_LABELS[m], m in rec['modes']) for m in facts.MODES]})
    return items


def _links(st: dict, tips: dict, linux: bool) -> list:
    out = []
    for (name, site), url in zip(SITES, (
            st.get('url'), (tips.get('protondb') or {}).get('url') if linux else None,
            (tips.get('pcgw') or {}).get('url'), (tips.get('hltb') or {}).get('url'))):
        if str(url or '').startswith(site):
            out.append({'name': name, 'url': str(url)})
    return out


def overview_model(rec, store, tips, game, linux: bool = True) -> list[dict]:
    """[{key, title, items}] in ORDER, each section only when its facts exist.
    `rec` is the facts index record, `store` --store-info's answer, `tips`
    --game-tips', `game` the library entry; `linux` says whether Linux-only
    parts (ProtonDB, Deck, anti-cheat) belong."""
    rec, store, tips, game = rec or {}, store or {}, tips or {}, game or {}
    st = store.get('store') or {}
    items = {
        'players': _players(store.get('reviews') or {}),
        'critics': _critics(st, rec),
        'length': _length(rec, game),
        'linux': _linux(tips, game) if linux else [],
        'pc': _pc(tips),
        'details': _details(rec),
        'links': _links(st, tips, linux),
    }
    return [{'key': k, 'title': TITLES[k], 'items': items[k]} for k in ORDER if items[k]]


# --- Painted parts -----------------------------------------------------------
# Each reads P when it paints, so a theme change applies; sizes go through
# scaled(). Only float overloads (QPointF / QRectF): an exception in a
# paintEvent is a qFatal.

def _tone(role: str, alpha: float = 1.0) -> QColor:
    c = QColor(P.get(role, P['muted']))
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def _bold(widget: QWidget, px: float) -> QFont:
    f = QFont(widget.font())
    f.setFamily(FONTS.ui)
    f.setPixelSize(scaled(px))
    f.setWeight(QFont.Weight.DemiBold)
    return f


class ScoreRing(QWidget):
    """A percentage as a ring with the number inside (Steam reviews)."""
    SIZE, PEN = 60, 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value, self.tone = 0, 'success'
        self.setFixedSize(scaled(self.SIZE), scaled(self.SIZE))

    def set_score(self, value: int, tone: str, name: str):
        self.value, self.tone = max(0, min(100, int(value))), tone
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = float(scaled(self.PEN))
        side = float(min(self.width(), self.height())) - pen
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        pt.setPen(QPen(_tone('surface_2'), pen))
        pt.drawEllipse(rect)
        arc = QPen(_tone(self.tone), pen)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        pt.setPen(arc)
        pt.drawArc(rect, 90 * 16, -int(self.value / 100 * 360 * 16))
        pt.setPen(_tone('text'))
        pt.setFont(_bold(self, 16))
        pt.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), str(self.value))
        pt.end()


class ScoreBadge(QWidget):
    """A critic score in a soft square tinted by how good it is; the tint
    gets stronger with the score (and a little more on a light theme)."""
    SIZE = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value, self.tone = 0, 'success'
        self.setFixedSize(scaled(self.SIZE), scaled(self.SIZE))

    def set_score(self, value: int, tone: str, name: str):
        self.value, self.tone = int(value), tone
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        a = 0.1 + max(0, min(100, self.value)) / 100 * 0.3 + (0.08 if is_light() else 0.0)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        grad = QLinearGradient(QPointF(box.left(), box.top()), QPointF(box.right(), box.bottom()))
        grad.setColorAt(0.0, _tone(self.tone, a))
        grad.setColorAt(1.0, _tone(self.tone, 0.05))
        pt.setBrush(grad)
        pt.setPen(QPen(_tone(self.tone, a * 1.1), 1.0))
        r = float(R['control'])
        pt.drawRoundedRect(box, r, r)
        pt.setPen(_tone(self.tone))
        pt.setFont(_bold(self, 18))
        pt.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), str(self.value))
        pt.end()


class TierBadge(QWidget):
    """ProtonDB's tier as a small square in its own colour, with its letter."""
    SIZE = 28

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tier = ''
        self.setFixedSize(scaled(self.SIZE), scaled(self.SIZE))

    def set_tier(self, tier: str, name: str):
        self.tier = tier
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(TIER_COLORS.get(self.tier, P['muted']))
        low = QColor(c)
        c1, c2 = QColor(c), QColor(c)
        c1.setAlphaF(0.35)
        c2.setAlphaF(0.08)
        low.setAlphaF(0.4)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        grad = QLinearGradient(QPointF(box.left(), box.top()), QPointF(box.right(), box.bottom()))
        grad.setColorAt(0.0, c1)
        grad.setColorAt(1.0, c2)
        pt.setBrush(grad)
        pt.setPen(QPen(low, 1.0))
        r = float(R['sm'])
        pt.drawRoundedRect(box, r, r)
        pt.setPen(c)
        pt.setFont(_bold(self, 13))
        pt.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter),
                    (self.tier[:1] or '?').upper())
        pt.end()


class LengthBar(QWidget):
    """Main story, with extras and everything on one bar, in three shades
    of the accent, and a dot where you are."""
    H, DOT = 10, 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.main = self.plus = self.complete = self.you = None
        self.setFixedHeight(scaled(self.DOT) + 2)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_lengths(self, main, plus, complete, you, name: str):
        self.main, self.plus, self.complete, self.you = main, plus, complete, you
        self.setAccessibleName(name)
        self.update()

    def paintEvent(self, ev):
        top = self.complete or self.plus or self.main
        if not top:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        h, w = float(scaled(self.H)), float(self.width())
        y = (self.height() - h) / 2
        r = h / 2
        pt.setPen(Qt.PenStyle.NoPen)
        for value, shade in ((None, 0.0), (self.complete, 0.3), (self.plus, 0.65),
                             (self.main, 1.0)):
            frac = 1.0 if value is None else min(1.0, value / top) if value else 0.0
            if frac <= 0:
                continue
            col = P['surface_2'] if value is None else _mix(P['surface_2'], P['accent'], shade)
            pt.setBrush(QColor(col))
            pt.drawRoundedRect(QRectF(0.0, y, max(h, w * frac), h), r, r)
        if self.you:
            d = float(scaled(self.DOT))
            x = min(max(w * self.you / top, d / 2), w - d / 2)
            pt.setBrush(QColor(P['bg']))
            pen = QPen(QColor(P['text']), float(scaled(3)))
            pt.setPen(pen)
            pt.drawEllipse(QPointF(x, self.height() / 2), d / 2 - 1.5, d / 2 - 1.5)
        pt.end()


# --- Tiles -------------------------------------------------------------------

def _label(text: str, name: str, parent: QWidget, wrap: bool = False) -> QLabel:
    """A label that never widens its panel (horizontal policy Ignored)."""
    lab = QLabel(text, parent)
    lab.setObjectName(name)
    lab.setWordWrap(wrap)
    lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    lab.setMinimumWidth(10)
    return lab


def _role(lab: QLabel, prop: str, value: str):
    """Set a style property and repolish, only when it changed."""
    if lab.property(prop) != value:
        lab.setProperty(prop, value)
        lab.style().unpolish(lab)
        lab.style().polish(lab)


def _pill(text: str, state: str, parent: QWidget) -> QLabel:
    lab = QLabel(text, parent)
    lab.setObjectName("OvPill")
    lab.setProperty('state', state)
    return lab


def _hours(v) -> str:
    return f"{v:g} h"


class _Elide(QLabel):
    """One line that ends in ... instead of widening its tile."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("OvSub")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setFont(self.font())
        pt.setPen(QColor(P['muted']))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                             self.width())
        pt.drawText(self.rect(), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    text)
        pt.end()


class Tile(QFrame):
    """A rounded surface with a small caption; `fill(items)` redraws it and
    is only called when its own items changed."""
    key = ''
    ICON = None

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("OvTile")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        self.lay.setSpacing(SP['2'])
        head = QHBoxLayout()
        head.setSpacing(SP['1_5'])
        if self.ICON:
            ic = QLabel(self)
            set_icon_label(ic, G[self.ICON], 12, 'muted')
            ic.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            head.addWidget(ic)
        cap = QLabel(TITLES[key].upper(), self)
        cap.setObjectName("SectionKicker")
        f = cap.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        cap.setFont(f)
        head.addWidget(cap)
        head.addStretch()
        self.lay.addLayout(head)
        self.setAccessibleName(TITLES[key])

    def fill(self, items):
        raise NotImplementedError


class PlayersTile(Tile):
    def __init__(self, parent=None):
        super().__init__('players', parent)
        row = QHBoxLayout()
        row.setSpacing(SP['3'])
        self.ring = ScoreRing(self)
        row.addWidget(self.ring)
        col = QVBoxLayout()
        col.setSpacing(SP['0_5'])
        col.addStretch()
        self.word = _label('', "OvWord", self, wrap=True)
        self.sub = _label('', "OvSub", self)
        col.addWidget(self.word)
        col.addWidget(self.sub)
        col.addStretch()
        row.addLayout(col, 1)
        self.lay.addLayout(row)
        self.lay.addStretch(1)

    def fill(self, it):
        has_score = it['value'] is not None
        word = it['word'] or critic_word(it['value'])
        name = f"{it['value']} percent positive" if has_score else "Steam reviews"
        self.ring.setVisible(has_score)
        if has_score:
            self.ring.set_score(it['value'], it['tone'], f"{word}, {name}")
        self.word.setText(word)
        _role(self.word, 'role', it['tone'])
        self.sub.setText(f"{count_text(it['of'])} Steam reviews" if it['of'] else "Steam reviews")
        self.setToolTip((f"{word}: {name}" if has_score else word) + (f" of {it['of']:,} Steam reviews" if it['of'] else ''))


class CriticsTile(Tile):
    def __init__(self, parent=None):
        super().__init__('critics', parent)
        self.row = QHBoxLayout()
        self.row.setSpacing(SP['1_5'])
        self.lay.addLayout(self.row)
        self.lay.addStretch(1)

    def fill(self, items):
        clear_layout(self.row)
        for c in items:                       # appended by later sources (w-133)
            col = QVBoxLayout()
            col.setSpacing(SP['1'])
            badge = ScoreBadge(self)
            badge.set_score(c['value'], c['tone'], f"{c['name']} {c['value']} of 100, {c['word']}")
            col.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)
            name = _label(c['name'], "OvName", self)
            name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(name)
            self.row.addLayout(col, 1)
        if len(items) < 3:
            self.row.addStretch(3 - len(items))
        self.setToolTip(", ".join(f"{c['name']} {c['value']}: {c['word']}" for c in items))


class LengthTile(Tile):
    ICON = 'clock'

    def __init__(self, parent=None):
        super().__init__('length', parent)
        self.bar = LengthBar(self)
        self.lay.addWidget(self.bar)
        grid = QGridLayout()
        grid.setHorizontalSpacing(SP['2'])
        grid.setVerticalSpacing(0)
        self.cells = []
        for i, name in enumerate(("Main story", "+ Extras", "Everything")):
            big, cap = _label('', "OvBig", self), _label(name, "OvName", self)
            grid.addWidget(big, 0, i)
            grid.addWidget(cap, 1, i)
            grid.setColumnStretch(i, 1)
            self.cells.append((big, cap))
        self.lay.addLayout(grid)
        self.you = _label('', "OvSub", self, wrap=True)
        self.lay.addWidget(self.you)
        self.lay.addStretch(1)

    def fill(self, it):
        vals = (it['main'], it['plus'], it['complete'])
        for (big, cap), v in zip(self.cells, vals):
            big.setText(_hours(v) if v else '')
            big.setVisible(bool(v))
            cap.setVisible(bool(v))
        you = it['you']
        self.you.setVisible(bool(you))
        if you:
            note = ("past the main story" if you > it['main']
                    else f"{round(100 * you / it['main'])} % of the main story")
            self.you.setText(f"You: {you:.1f} h · {note}")
        self.bar.set_lengths(*vals, you, "How long to beat: " + ", ".join(
            f"{n} {_hours(v)}" for n, v in zip(("main story", "with extras", "everything"), vals)
            if v))
        if it['source'] == 'igdb':
            n = it.get('count')
            tip = "From IGDB" + (f", {int(n)} submission{'s' if n != 1 else ''}" if n else '')
        else:
            tip = "From HowLongToBeat"
        self.setToolTip(tip)


class LinuxTile(Tile):
    ICON = 'proton'

    def __init__(self, parent=None):
        super().__init__('linux', parent)
        self.tier_row = QWidget(self)
        tr = QHBoxLayout(self.tier_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(SP['2'])
        self.badge = TierBadge(self.tier_row)
        tr.addWidget(self.badge)
        self.tier_text = _Elide(self.tier_row)
        tr.addWidget(self.tier_text, 1)
        self.lay.addWidget(self.tier_row)
        self.chip_host = QWidget(self)
        self.chips = FlowLayout(self.chip_host, margin=0, spacing=SP['1_5'])
        self.lay.addWidget(self.chip_host)
        self.lay.addStretch(1)

    def fill(self, items):
        tier = next((i for i in items if 'tier' in i), None)
        self.tier_row.setVisible(tier is not None)
        if tier:
            name = f"ProtonDB {tier['tier'].capitalize()}"
            text, tip = name, name
            if tier['reports']:
                text += f" · {tier['reports']}"
                tip += f", {tier['reports']} reports"
            if tier['trending']:
                text += f" · \u2197 {tier['trending'].capitalize()}"
                tip += f", trending {tier['trending'].capitalize()}"
            self.badge.set_tier(tier['tier'], name)
            self.tier_text.setText(text)
            self.tier_row.setToolTip(tip)
        clear_layout(self.chips)
        chips = [i for i in items if 'chip' in i]
        for c in chips:
            self.chips.addWidget(_pill(c['text'], c['tone'], self.chip_host))
        self.chip_host.setVisible(bool(chips))


class PcTile(Tile):
    ICON = 'system'

    def __init__(self, parent=None):
        super().__init__('pc', parent)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(SP['1'])
        self.lay.addLayout(self.rows)
        self.lay.addStretch(1)
        self.setToolTip("From PCGamingWiki")

    def fill(self, items):
        clear_layout(self.rows)
        for it in items:
            row = QHBoxLayout()
            row.setSpacing(SP['2'])
            ic = QLabel(self)
            set_icon_label(ic, G[it['glyph']], 13, it['tone'])
            ic.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row.addWidget(ic)
            row.addWidget(_label(it['name'], "OvRowName", self), 1)
            word = _label(it['word'], "OvRowWord", self)
            word.setProperty('role', it['tone'])
            word.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            row.addWidget(word)
            self.rows.addLayout(row)


class DetailsTile(Tile):
    ICON = 'info'

    def __init__(self, parent=None):
        super().__init__('details', parent)
        self.kv = QGridLayout()
        self.kv.setHorizontalSpacing(SP['3'])
        self.kv.setColumnStretch(1, 1)
        self.lay.addLayout(self.kv)
        self.pill_host = QWidget(self)
        self.pills = FlowLayout(self.pill_host, margin=0, spacing=SP['1_5'])
        self.lay.addWidget(self.pill_host)
        self.lay.addStretch(1)

    def fill(self, items):
        clear_layout(self.kv)
        clear_layout(self.pills)
        modes = next((i['modes'] for i in items if 'modes' in i), [])
        for r, rel in enumerate(i['release'] for i in items if 'release' in i):
            key = QLabel("Released", self)
            key.setObjectName("OvKey")
            key.setFixedWidth(scaled(84))
            self.kv.addWidget(key, r, 0)
            self.kv.addWidget(_label(rel, "OvValue", self), r, 1)
        for _key, text, on in modes:
            self.pills.addWidget(_pill(text, 'on' if on else 'off', self.pill_host))
            self.pills.itemAt(self.pills.count() - 1).widget().setToolTip(
                f"{text}: {'yes' if on else 'no'}")
        self.pill_host.setVisible(bool(modes))


class LinksRow(QWidget):
    """The sites the game is on, as buttons that open the browser."""
    key = 'links'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self, margin=0, spacing=SP['1_5'])

    def fill(self, items):
        clear_layout(self.flow)
        for it in items:
            b = QPushButton(f"{it['name']} \u2197", self)
            b.setObjectName("OvLink")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(it['url'])
            b.setAccessibleName(f"{it['name']}, opens in browser")
            b.clicked.connect(lambda _=False, u=it['url']: QDesktopServices.openUrl(QUrl(u)))
            self.flow.addWidget(b)


class TilePair(QWidget):
    """Two tiles side by side, one above the other under PAIR_MIN_W; one
    alone is full width."""

    def __init__(self, a: Tile, b: Tile, parent=None):
        super().__init__(parent)
        self.lay = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(SP['2_5'])
        self.tiles = (a, b)
        for t in self.tiles:
            t.setParent(self)
            self.lay.addWidget(t, 1)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        want = (QBoxLayout.Direction.TopToBottom if self.width() < scaled(PAIR_MIN_W)
                else QBoxLayout.Direction.LeftToRight)
        if self.lay.direction() != want:
            self.lay.setDirection(want)

    def stacked(self) -> bool:
        return self.lay.direction() == QBoxLayout.Direction.TopToBottom


class OverviewTiles(QWidget):
    """The tiles in ORDER. All are built once; a section that is missing
    hides its tile, one that changed redraws only that tile, so a late
    store or tips answer neither reorders anything nor drops focus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SP['2_5'])
        self.tile = {'players': PlayersTile(self), 'critics': CriticsTile(self),
                     'length': LengthTile(self), 'linux': LinuxTile(self), 'pc': PcTile(self),
                     'details': DetailsTile(self), 'links': LinksRow(self)}
        self.pairs = [TilePair(self.tile[a], self.tile[b], self) for a, b in PAIRS]
        rows = {'players': self.pairs[0], 'length': self.tile['length'],
                'linux': self.pairs[1], 'details': self.tile['details'],
                'links': self.tile['links']}
        for key in ORDER:
            if key in rows:
                v.addWidget(rows[key])
        self._items: dict[str, object] = {}
        self._shown: list[str] = []
        for w in (*self.tile.values(), *self.pairs):
            w.hide()

    def set_sections(self, sections: list[dict]) -> bool:
        """Show `sections` (overview_model's), and the whole block only when
        any tile shows; True then."""
        got = {s['key']: s['items'] for s in sections}
        for key in ORDER:
            tile = self.tile[key]
            items = got.get(key)
            if items:
                if self._items.get(key) != items:
                    tile.fill(items)
                    self._items[key] = items
                tile.setVisible(True)
            else:
                self._items.pop(key, None)
                tile.setVisible(False)
        for pair in self.pairs:
            pair.setVisible(any(not t.isHidden() for t in pair.tiles))
        self._shown = [k for k in ORDER if k in got and got[k]]
        self.setVisible(bool(self._shown))
        return bool(self._shown)

    def keys(self) -> list[str]:
        return list(self._shown)
